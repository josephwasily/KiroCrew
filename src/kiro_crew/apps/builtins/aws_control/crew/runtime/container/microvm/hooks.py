#!/usr/bin/env python3
"""PID 1 on the Lambda MicroVM lane: answer the platform's hooks, start the crew.

``python -m container.microvm.hooks``. On Fargate ECS starts
``container.supervisor`` and this module does not run at all; the image is the
same and the entrypoint is what differs, which is the whole shape of the lane.

WHAT THE PLATFORM GIVES AND TAKES
---------------------------------
A MicroVM has exactly one channel in before it has registered itself anywhere: an
HTTP call from the platform to one port in the guest, at each lifecycle event.
Six paths under ``/aws/lambda-microvms/runtime/v1``: ``ready`` and ``validate``
during the image build, then ``run`` / ``resume`` / ``suspend`` / ``terminate``
while a VM is alive. ``run`` carries the launch payload, which is the only way
the guest learns its SSM activation, its secret references and its archive
coordinates.

**The run hook's budget is 60 seconds.** Documented as 600 and refused above 60 by
the service -- measured live, ``ValidationException: Value '300' at
'hooks.microvmHooks.runTimeoutInSeconds' failed to satisfy constraint: Member must
have value less than or equal to 60``. So this module does the minimum the launch
is waiting on INSIDE the hook (give the VM its own machine identity, register the
SSM node) and everything else on a worker thread. A crew's first boot -- secrets,
restore, backend, model -- does not fit in a minute and nothing is waiting for it.

WHAT IT MUST NOT DISCLOSE
-------------------------
This listener's port is reachable from the internet by anyone holding
``lambda:CreateMicrovmAuthToken`` in the owner's own account: the VM's HTTPS
endpoint is always up, the endpoint credential names a PORT rather than a path,
and the ``NO_INGRESS`` connector does not govern it. Measured live, from
a machine outside the account's network. Three consequences are code here, not
advice:

* **Every reply is a constant.** Any path that is not one of the six answers one
  fixed body, and no reply says whether a crew is running, which crew it is, or
  whether a credential arrived. The stranger asking is indistinguishable from the
  platform, because the hook call carries no credential this process can check.
* **``run`` is single-shot.** A second ``run`` would reset ``/etc/machine-id`` and
  re-register the SSM agent under a new identity, severing the owner's only route
  to their own crew. A probe really did reach ``POST .../run`` with a token and
  get a 200; it was inert only because of the marker this module writes.
* **No ``Server`` header.** The default names the interpreter version.

The crew itself is NOT on this port. The front process binds loopback on this
lane (``SMC_FRONT_BIND``), so the only thing the endpoint can reach is the fixed
replies above, and the owner reaches the front through an SSM port-forward that
dials loopback inside the guest's own network namespace.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .payload_shape import decode_payload, payload_encoding

HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1"
HOOK_PORT = int(os.environ.get("SMC_HOOK_PORT", "8080"))

#: Guest-only state, deliberately OUTSIDE the data home so none of it can be
#: swept into a session archive and read back as crew state.
# Absolute paths INSIDE THE GUEST, which is a Linux MicroVM and never a
# developer's machine. Composed from segments rather than written as literals so
# that nothing here reads as a path this repository expects to exist on the host
# it is built on: this module only ever runs as PID 1 in the image.
_ROOT = "/"


def _guest_path(*segments: str) -> str:
    """One absolute guest path, as POSIX."""
    return _ROOT + "/".join(segments)


ETC = "etc"
VAR = "var"
USR = "usr"

#: Where the hook listener keeps its own state. Outside the data home so it
#: cannot be swept into an archive.
STATE_DIR = _guest_path(VAR, "lib", "microvm-guest")

#: The node identity the SSM agent fingerprints. Rewritten per VM, because every
#: MicroVM resumes from one snapshot and a shared id collides on a single node.
MACHINE_ID = _guest_path(ETC, "machine-id")

#: dbus's copy of the same id, kept in step when the file exists.
DBUS_MACHINE_ID = _guest_path(VAR, "lib", "dbus", "machine-id")

#: The agent's registration record. Its ABSENCE in the image is what proves the
#: build did not register a node identity into the snapshot.
SSM_REGISTRATION = _guest_path(VAR, "lib", "amazon", "ssm", "registration")

#: Where the crew's data home sits when the environment names none.
DEFAULT_DATA_HOME = _guest_path(VAR, "lib", "kirocrew")
RUN_MARKER = f"{STATE_DIR}/run.done"
PAYLOAD_SEEN = f"{STATE_DIR}/payload-seen.json"
BOOT_STATE = f"{STATE_DIR}/boot.json"
RESTORE_WORK = _guest_path(VAR, "tmp", "microvm-restore")

AGENT_BIN = _guest_path(USR, "bin", "amazon-ssm-agent")
#: Where the SSM agent publishes the managed node's role credentials. It writes
#: them under the HOME of the process that runs it, and this image sets
#: ``HOME=/var/lib/crew`` -- so the obvious ``/root/.aws/credentials`` is the
#: WRONG path here and would have stalled the boot at the credential wait once
#: registration began working. Both are checked, because the agent's own HOME is
#: the image's to change.
SHARED_CREDS_CANDIDATES = (
    os.path.join(os.environ.get("HOME", "/root"), ".aws", "credentials"),
    "/root/.aws/credentials",
    _guest_path(VAR, "lib", "crew", ".aws", "credentials"),
)
#: The login user the image's crew processes run as (``Dockerfile``: ``USER crew``).
CREW_USER = "crew"

_lock = threading.Lock()
_children: "dict[str, subprocess.Popen]" = {}
_boot: "dict[str, Any]" = {"stage": "waiting for run"}
_region = os.environ.get("AWS_REGION", "us-east-1")
_managed_instance_id = ""


def log(event: str, **fields: Any) -> None:
    """One JSON object per line on stdout, which the platform collects.

    Value-free by construction: every call site passes identifiers, states,
    counts and lengths. There is no call that takes an activation code, a control
    secret or an identity document, so one cannot be added by a call site that
    merely looks harmless.
    """
    record = {"ts": round(time.time(), 3), "event": event}
    record.update(fields)
    try:
        sys.stdout.write(json.dumps(record, default=str) + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 - a log must never be why a boot fails
        pass


def write_state(path: str, data: "dict[str, Any]") -> None:
    # The directory of the PATH given, not of ``STATE_DIR``. Every caller in the
    # guest writes under that directory, so the two are the same there -- but a
    # function that takes a path and then creates a different directory can only
    # be called with one argument, and this one is also called from the tick's
    # ops module and from tests that redirect it.
    os.makedirs(os.path.dirname(path) or STATE_DIR, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, sort_keys=True)
    os.replace(tmp, path)


# ── this VM's own identity ───────────────────────────────────────────────────


def reset_machine_id() -> str:
    """Give this VM a machine id of its own before the SSM agent fingerprints it.

    Every MicroVM from one image resumes from the same snapshot, so
    ``/etc/machine-id`` is byte-identical across all of them, and the agent builds
    its hardware fingerprint from it. Without this, every VM in a fleet claims one
    managed node and each registration invalidates the last.

    **This is best effort, and the distinction matters.** The platform may
    bind-mount the file; a bind mount cannot be truncated in place, and dropping
    it needs a capability the container may not hold. So the write can fail for
    reasons that are the platform's and not this guest's.

    What a failure costs is FLEET correctness, not this VM: one VM with the
    snapshot's baked id registers perfectly well. So the failure is recorded and
    the boot continues, rather than taking a crew down over a collision that
    needs a second VM to happen. The earlier shape of this function raised, which
    meant a platform that bind-mounts the file made every launch fail at the step
    BEFORE the SSM registration -- the crew never came online and the reason was
    invisible, because nothing in the guest had a log destination yet.

    Returns the id in effect afterwards, which is the old one when the write
    failed. The caller logs it either way, so "which id did this VM register
    under" is answerable.
    """
    new_id = uuid.uuid4().hex
    umount = subprocess.run(
        ["umount", MACHINE_ID], capture_output=True, text=True, encoding="utf-8", check=False
    )
    try:
        with open(MACHINE_ID, "w", encoding="ascii") as fh:
            fh.write(new_id + "\n")
    except OSError as exc:
        current = ""
        try:
            with open(MACHINE_ID, encoding="ascii") as fh:
                current = fh.read().strip()
        except OSError:
            pass
        log(
            "machine_id.not_reset",
            error=repr(exc),
            umount_rc=umount.returncode,
            umount_stderr=(umount.stderr or "").strip()[:200],
            kept=current,
        )
        return current
    if os.path.exists(DBUS_MACHINE_ID):
        try:
            shutil.copyfile(MACHINE_ID, DBUS_MACHINE_ID)
        except OSError as exc:
            log("machine_id.dbus_copy_failed", error=repr(exc))
    log("machine_id.reset", machine_id=new_id, umount_rc=umount.returncode)
    return new_id


def register_agent(activation_id: str, activation_code: str, region: str) -> None:
    """Enroll this VM as the hybrid managed node the launch payload names.

    The activation is minted per launch with a registration limit of one, so a
    code that leaked cannot enroll a second machine -- which is why it travels in
    the payload rather than being baked into the image.

    The code does reach this process's argv: ``amazon-ssm-agent -register`` has no
    other input for it. Bounded rather than clean -- the only reader of this VM's
    ``/proc`` is this VM, the code is single-use, it expires within the hour, and
    the registration consumes it immediately. Recorded so it is a known cost.
    """
    started = time.time()
    try:
        proc = subprocess.run(
            [
                AGENT_BIN,
                "-register",
                "-code",
                activation_code,
                "-id",
                activation_id,
                "-region",
                region,
                "-y",
            ],
            # Inside the hook's 60-second budget with room to report. A register
            # that has not answered in fifty seconds will not answer in sixty, and
            # a TimeoutExpired that escapes this function is indistinguishable
            # from a crash -- so it is caught and named.
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=50,
        )
    except subprocess.TimeoutExpired:
        log("ssm.register", rc="timeout", seconds=round(time.time() - started, 2))
        raise RuntimeError("amazon-ssm-agent -register did not answer within 50s") from None
    # stderr is logged whatever happens; stdout only on success. The agent's own
    # usage text, which it prints when it rejects an argument, can echo the
    # registration code back -- so the failure path logs the half that cannot
    # carry it.
    log(
        "ssm.register",
        rc=proc.returncode,
        seconds=round(time.time() - started, 2),
        stderr=(proc.stderr or "").strip()[:400],
        stdout=((proc.stdout or "").strip()[:200] if proc.returncode == 0 else "(withheld)"),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"amazon-ssm-agent -register exited {proc.returncode}")


def read_managed_instance_id() -> str:
    try:
        with open(SSM_REGISTRATION, encoding="utf-8") as fh:
            return str(json.load(fh).get("ManagedInstanceID") or "")
    except Exception:  # noqa: BLE001
        return ""


# ── children ─────────────────────────────────────────────────────────────────


def spawn(
    name: str,
    argv: "list[str]",
    env: "Optional[dict[str, str]]" = None,
    *,
    as_user: str = "",
) -> None:
    """Start a long-lived child and remember it, so shutdown can be orderly.

    Not a restart-on-exit supervisor. The crew is one process tree whose failure
    the owner must be able to see, and a loop that silently restarts a crew that
    cannot start is how a VM looks alive for its whole billable lifetime while
    answering nothing.

    ``as_user`` exists because this process is root and the crew must not be. The
    image's own posture is ``USER crew``; the MicroVM layer has to take root back
    for PID 1 (``/etc/machine-id``, the SSM registration), so the drop that
    ``USER crew`` would have done has to happen HERE for the one child that is
    the crew. Dropping both uid and gid, and not relying on sudo: ``sudo -i``
    would also reset the environment this function was given.
    """
    log("child.start", name=name, argv0=argv[0], as_user=as_user or "root")
    kwargs: "dict[str, Any]" = {}
    if as_user:
        kwargs["user"] = as_user
        kwargs["group"] = as_user
    proc = subprocess.Popen(argv, env=env, stdout=sys.stdout, stderr=sys.stderr, **kwargs)
    with _lock:
        _children[name] = proc


# ── secrets ──────────────────────────────────────────────────────────────────


def wait_for_shared_credentials(timeout: float = 120.0) -> bool:
    """Wait for the SSM agent to publish the managed node's role credentials.

    ``Profile.ShareCreds`` in the agent's config is what makes it write them, and
    they are the guest's ONLY AWS identity: a MicroVM has no instance profile and
    this image carries no baked credential. So no secret can be read before this
    file exists.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for path in SHARED_CREDS_CANDIDATES:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                log("ssm.credentials_ready", path=path)
                # boto3 resolves the shared file from AWS_SHARED_CREDENTIALS_FILE
                # or from its own HOME, which is not necessarily the agent's. Point
                # it at the file that actually exists rather than hoping the two
                # agree.
                os.environ["AWS_SHARED_CREDENTIALS_FILE"] = path
                return True
        time.sleep(2)
    log("ssm.credentials_absent", checked=list(SHARED_CREDS_CANDIDATES))
    return False


def read_secret(secret_id: str, region: str) -> str:
    """Fetch one secret's value by the reference the payload carried.

    A reference and never a value is the payload's rule: ``run-hook-payload`` is
    an argument to ``RunMicrovm``, and AWS does not document it as sensitive, so a
    value there could sit in that account's CloudTrail request history. One extra
    call in the guest costs the owner nothing.
    """
    import boto3  # imported late: only this path needs an AWS client
    from botocore.config import Config

    client = boto3.client(
        "secretsmanager",
        region_name=region,
        config=Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 3}),
    )
    return str(client.get_secret_value(SecretId=secret_id)["SecretString"])


# ── session persistence ──────────────────────────────────────────────────────


def restore_data_home(
    bucket: str, key: str, etag: str, region: str, home: str, *, expected: bool = True
) -> "dict[str, Any]":
    """Unpack this crew's session archive into the data home, if there is one.

    The crew's CONTENT -- persona, skills, MCP config -- is not here: it is a
    layer of the image, installed by the supervisor from ``/app/crew-bundle``. What
    the archive carries is the part that cannot be baked because it is written
    while the crew runs: its conversations.

    The ETag is a PRECONDITION, not a hint. An archive whose ETag is not the one
    the launch recorded is not this crew's archive at the generation this launch
    believes in, and restoring it would present another generation's
    conversations as this one's. ``If-Match`` makes S3 refuse rather than this
    guest compare.

    A missing archive is the NORMAL first launch, not a failure: the crew simply
    has no conversations yet. But WHICH launch that is, this function does not
    work out for itself -- *expected* carries it, from the launcher, which reads
    it off the ledger. The reason is a grant: this guest holds ``s3:GetObject``
    on its own prefix and not ``s3:ListBucket``, and S3 answers a GET for a key
    that does not exist with ``AccessDenied`` rather than ``NoSuchKey`` unless
    the caller can list. So the error code cannot tell a fresh crew from a broken
    grant, and every fresh crew's first launch ended at ``failed:restore``.

    With *expected* false there is nothing to fetch and the call is skipped. With
    it true an archive exists as far as the ledger knows, so EVERY failure to
    fetch one is fatal -- including ``NoSuchKey``, which then means the ledger and
    the bucket disagree about this crew's history. Booting empty over that would
    present a crew with conversations as a crew without them, and the first turn
    would write a new archive over the one that was not read.
    """
    if not expected:
        return {"restored": False, "reason": "the launch carried no restore point"}
    if not bucket or not key:
        return {"restored": False, "reason": "no archive coordinates in the payload"}
    import boto3  # imported late
    from botocore.config import Config
    from botocore.exceptions import ClientError

    os.makedirs(RESTORE_WORK, exist_ok=True)
    body_path = f"{RESTORE_WORK}/home.tar.gz"
    client = boto3.client(
        "s3",
        region_name=region,
        config=Config(connect_timeout=5, read_timeout=60, retries={"max_attempts": 3}),
    )
    kwargs: "dict[str, Any]" = {"Bucket": bucket, "Key": key}
    if etag:
        kwargs["IfMatch"] = etag
    try:
        response = client.get_object(**kwargs)
    except ClientError as exc:
        # Nothing is swallowed here. The launcher already said an archive exists,
        # so every way of failing to read it -- gone, forbidden, or an ETag that
        # does not match the one the launch recorded -- is a disagreement with the
        # ledger and not a fresh crew. The stage name in the boot state is the
        # diagnosis.
        code = exc.response.get("Error", {}).get("Code", "")
        raise RuntimeError(
            f"the launch said this crew has an archive at s3://{bucket}/{key}, but it "
            f"could not be read ({code or type(exc).__name__}). Restoring nothing here "
            "would serve a crew's history as empty and then overwrite it"
        ) from exc
    served = (response.get("ETag") or "").strip('"')
    with open(body_path, "wb") as fh:
        shutil.copyfileobj(response["Body"], fh)
    size = os.path.getsize(body_path)
    os.makedirs(home, exist_ok=True)
    # --no-same-owner then an explicit chown: the archive was written by whichever
    # uid packed it, and a data home the crew user cannot read is a crew that
    # boots to an empty conversation list rather than one that fails.
    subprocess.run(
        ["tar", "-xzf", body_path, "-C", home, "--no-same-owner"], check=True, timeout=300
    )
    subprocess.run(["chown", "-R", f"{CREW_USER}:{CREW_USER}", home], check=False)
    os.remove(body_path)
    log("archive.restored", bytes=size, etag=served)
    return {"restored": True, "bytes": size, "etag": served}


# ── the wall ─────────────────────────────────────────────────────────────────

WATCHDOG_STATE = f"{STATE_DIR}/wall.json"

#: How long to wait for the crew's processes to exit after SIGTERM, before
#: killing them. Generous against a backend that flushes transcripts and
#: checkpoints SQLite on the way down, and bounded because the archive is the
#: thing being protected and the VM's wall does not move.
SEAL_TIMEOUT_SECONDS = 60

#: Seconds between liveness checks while waiting for that exit.
SEAL_POLL_SECONDS = 1

#: Where the backend answers which chat slots are running. Loopback, and the
#: BACKEND rather than the front: the front requires the per-crew control secret
#: on this lane and this process does not hold it after boot hands it to the
#: supervisor. The backend is bound to loopback inside this VM, so the only
#: caller that can reach it is a process already inside the guest.
SLOTS_URL = f"http://127.0.0.1:{os.environ.get('SMC_BACKEND_PORT', '8765')}/api/chat/slots"


#: The header the backend authenticates a loopback caller with, and the file it
#: reads the value from. Spelled here rather than imported from ``common``,
#: because this module runs as PID 1 before the crew's own package tree is in
#: play and deliberately imports nothing from it.
BACKEND_SECRET_HEADER = "X-Internal-Secret"

#: The run-directory file the backend writes that value to, as a format string on
#: the port. Both this and the header name are pinned against ``common``'s own
#: definitions by ``test_microvm_slots_probe``, so a rename there fails a test
#: rather than silently returning this probe to being unauthenticated.
BACKEND_SECRET_FILE = "gateway-{port}.secret"


def _no_redirect_opener() -> Any:
    """An opener that REFUSES redirects, for any request carrying the secret.

    A default ``urlopen`` follows a 3xx and re-sends the request's headers to
    wherever it points -- so a loopback call that is answered with a redirect
    carries the backend's secret off the machine, and the failure presents as a
    hang long before it presents as a leak. The gateway has
    ``kiro_crew.loopback_http.loopback_urlopen`` for exactly this; this module
    cannot use it, because it runs as PID 1 and imports nothing from the
    installed package, so it builds the same refusal here.

    Refusing is right rather than merely safe: the one legitimate answer to this
    probe is the backend's own JSON, and a redirect from loopback is not
    something to follow in either direction.
    """
    import urllib.error
    import urllib.request

    class _Refuse(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
            raise urllib.error.HTTPError(
                req.full_url, code, f"refused a redirect to {newurl}", headers, fp
            )

    return urllib.request.build_opener(_Refuse)


def _backend_auth_header() -> "dict[str, str]":
    """The one header the backend needs, read fresh from the run directory.

    Returns an EMPTY mapping when the secret cannot be read, rather than raising.
    The caller's own contract is that an unreadable probe means BUSY, and it gets
    there either way -- an unauthenticated request is refused and raises there.
    Failing here instead would turn one diagnosable 403 into a different error at
    a different layer.
    """
    run_dir = os.environ.get("SMC_BACKEND_RUN_DIR") or f"{DEFAULT_DATA_HOME}/run"
    port = os.environ.get("SMC_BACKEND_PORT", "8765")
    try:
        with open(f"{run_dir}/{BACKEND_SECRET_FILE.format(port=port)}", encoding="utf-8") as fh:
            value = fh.read().strip()
    except OSError:
        return {}
    return {BACKEND_SECRET_HEADER: value} if value else {}


def running_slots() -> int:
    """How many chat slots are mid-turn, by the crew's own accounting.

    Raises rather than returning 0 when it cannot tell. A probe that reported
    "idle" on failure would let the soft edge pack in the middle of a turn, so
    the caller treats an unreadable probe as BUSY and falls through to the hard
    edge.
    """
    import urllib.request

    # AUTHENTICATED, even though the call is to loopback. The backend requires a
    # token on every route and does not exempt 127.0.0.1 -- deliberately, because
    # a proxy in front of it can make remote traffic look local. An unauthenticated
    # probe is refused, this function raises, and the watchdog reads a raise as
    # BUSY -- so the soft edge would never fire and every pack would land at the
    # hard edge, which is the one that does not wait for a turn to finish.
    #
    # Read per call and never cached: the backend rotates it, and holding it in a
    # module global re-introduces the failure the secret module is written to
    # avoid.
    request = urllib.request.Request(SLOTS_URL, headers=_backend_auth_header())
    with _no_redirect_opener().open(request, timeout=5) as response:
        data = json.loads(response.read().decode("utf-8"))
    slots = data.get("slots") if isinstance(data, dict) else data
    if not isinstance(slots, list):
        raise RuntimeError(f"the slots route answered {type(slots).__name__}, not a list")
    return sum(1 for slot in slots if isinstance(slot, dict) and slot.get("running"))


def pack_data_home(
    *,
    bucket: str,
    key: str,
    etag: str,
    kms_key_id: str,
    region: str,
    home: str,
) -> "dict[str, Any]":
    """Archive the data home to S3 under the condition the launch recorded.

    The host's pack, performed from inside the VM. Same tar shape -- the whole
    home minus :data:`watchdog.ARCHIVE_DENYLIST`, which is the one form that
    cannot silently drop a file nobody thought to list. Same compare-and-set:
    ``If-None-Match: *`` when this crew has never been packed, ``If-Match`` on
    the recorded ETag afterwards.

    A refused precondition raises :class:`watchdog.PackConflict` and is NOT
    retried. Re-reading the ETag and writing again is exactly the last-write-wins
    the condition exists to prevent.
    """
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
    from container.microvm.watchdog import ARCHIVE_DENYLIST, PackConflict

    if not bucket or not key:
        raise RuntimeError("no archive coordinates in the payload, so there is nowhere to pack")
    os.makedirs(RESTORE_WORK, exist_ok=True)
    body_path = f"{RESTORE_WORK}/pack.tar.gz"
    argv = ["tar", "-czf", body_path, "-C", home]
    for name in ARCHIVE_DENYLIST:
        argv += ["--exclude", f"./{name}"]
    argv.append(".")
    # tar's exit code is READ, and only one non-zero value is tolerated.
    #
    # 1 is "some files differ" / "file changed as we read it", which a live crew
    # produces routinely: the archive is complete apart from files rewritten
    # mid-read. Anything higher is a FATAL tar error -- a full disk, an
    # unreadable tree, a write that failed -- and tar still leaves a partial file
    # behind. Accepting the archive because the file merely EXISTS would publish
    # that truncation as this crew's restore point, which is worse than failing
    # the pack: a failed pack leaves the previous archive intact and the
    # watchdog's next pass tries again, while a truncated one replaces a good
    # archive with a broken one under a condition S3 cannot refuse.
    completed = subprocess.run(argv, check=False, timeout=600, encoding="utf-8")
    # ``in (0, 1)``, not ``> 1``. A tar killed by a SIGNAL reports a NEGATIVE
    # returncode -- -15 for SIGTERM, -9 for SIGKILL -- and a negative number is
    # not greater than 1, so a comparison would accept a tar the kernel stopped
    # mid-write and publish whatever it had got to as this crew's restore point.
    # 1 is the one tolerated non-zero: "file changed as we read it", which a live
    # crew produces routinely.
    if completed.returncode not in (0, 1):
        raise RuntimeError(
            f"tar ended with status {completed.returncode}, so the archive is not complete; "
            "refusing to publish it as this crew's restore point"
        )
    if not os.path.exists(body_path):
        raise RuntimeError("tar produced no archive, so there is nothing to upload")
    if os.path.getsize(body_path) == 0:
        raise RuntimeError("tar produced an empty archive, so there is nothing to restore from")
    size = os.path.getsize(body_path)
    client = boto3.client(
        "s3",
        region_name=region,
        config=Config(connect_timeout=5, read_timeout=120, retries={"max_attempts": 3}),
    )
    extra: "dict[str, Any]" = {}
    if etag:
        extra["IfMatch"] = etag
    else:
        extra["IfNoneMatch"] = "*"
    if kms_key_id:
        extra["ServerSideEncryption"] = "aws:kms"
        extra["SSEKMSKeyId"] = kms_key_id
    else:
        # The header is always sent: a bucket policy that denies unencrypted puts
        # tests the request HEADER, and a bucket default does not set one.
        extra["ServerSideEncryption"] = "AES256"
    try:
        with open(body_path, "rb") as handle:
            response = client.put_object(Bucket=bucket, Key=key, Body=handle, **extra)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in ("PreconditionFailed", "ConditionalRequestConflict") or status == 412:
            raise PackConflict(
                "the archive write was refused because its precondition failed: another "
                "writer holds this crew's archive. Not retrying."
            ) from exc
        raise
    finally:
        if os.path.exists(body_path):
            os.remove(body_path)
    served = (response.get("ETag") or "").strip('"')
    log("archive.packed", bytes=size, etag=served)
    return {"bytes": size, "etag": served}


def arm_wall_watchdog(payload: "dict[str, Any]", started_at: float) -> None:
    """Start the wall watchdog on a daemon thread, if the payload armed one.

    On a THREAD of this process rather than a timer the gateway holds: a wall
    timer is only as awake as the process holding it, and the gateway runs on the
    owner's machine, which sleeps. This process is PID 1 in the VM, so it is the
    one thing guaranteed to be running when the wall arrives.
    """
    from container.microvm import watchdog as wd

    region = str(payload.get("region") or _region)
    archive = payload.get("archive") or {}
    home = os.environ.get("SMC_DATA_HOME", DEFAULT_DATA_HOME)

    def _pack(edge: str) -> "dict[str, Any]":
        log("wall.packing", edge=edge)
        return pack_data_home(
            bucket=str(archive.get("bucket") or ""),
            key=str(archive.get("key") or ""),
            etag=str(archive.get("etag") or ""),
            kms_key_id=str(archive.get("kms") or ""),
            region=region,
            home=home,
        )

    def _seal() -> None:
        """Stop the crew's gateway once its home is archived.

        The supervisor is this process's child, so a terminate reaches it. The
        archive is a snapshot of a moment, and a crew that keeps serving past it
        answers turns that are in no archive and that the VM's remaining minutes
        will take with the disk.
        """
        log("wall.sealing")
        # SIGTERM, then WAIT for the processes to go. A signal is a request, not
        # an exit: the supervisor and the backend flush transcripts and checkpoint
        # SQLite on the way down, and archiving while that is in flight captures a
        # torn database -- which restores as a crew whose history ends
        # mid-sentence or will not open at all. The whole point of sealing before
        # the pack is that the writers have stopped, and only a confirmed exit
        # says they have.
        #
        # A direct run, not ``spawn``: these are one-shot, not long-lived children
        # to remember for shutdown.
        subprocess.run(
            ["pkill", "-TERM", "-f", "container.supervisor"],
            check=False,
            timeout=30,
            encoding="utf-8",
        )
        deadline = time.time() + SEAL_TIMEOUT_SECONDS
        while time.time() < deadline:
            # ``pkill -0`` signals nothing and only reports whether a match
            # exists, so this is a liveness question rather than a second
            # terminate. Exit 1 means no process matched: they are gone.
            alive = subprocess.run(
                ["pkill", "-0", "-f", "container.supervisor"],
                check=False,
                timeout=10,
                encoding="utf-8",
            )
            if alive.returncode != 0:
                log("wall.sealed", waited=round(time.time() - (deadline - SEAL_TIMEOUT_SECONDS), 1))
                return
            time.sleep(SEAL_POLL_SECONDS)
        # Still alive at the deadline. KILL, because the alternative is archiving
        # a home with a live writer in it, and the VM is minutes from its wall
        # either way. Logged, because a supervisor that would not stop on TERM is
        # a thing worth seeing in the record.
        log("wall.seal_timeout", seconds=SEAL_TIMEOUT_SECONDS)
        subprocess.run(
            ["pkill", "-KILL", "-f", "container.supervisor"],
            check=False,
            timeout=10,
            encoding="utf-8",
        )
        time.sleep(SEAL_POLL_SECONDS)

    dog = wd.from_payload(
        payload,
        started_at=started_at,
        pack=_pack,
        running_slots=running_slots,
        state_path=WATCHDOG_STATE,
        seal=_seal,
    )
    if dog is None:
        log("wall.not_armed", reason="the payload carried no usable wall block")
        return
    log(
        "wall.armed",
        soft_in=int(dog.soft_deadline - started_at),
        hard_in=int(dog.hard_deadline - started_at),
    )
    threading.Thread(target=dog.run_forever, name="wall-watchdog", daemon=True).start()


# ── the crew ─────────────────────────────────────────────────────────────────


#: The read-only crew payload the image carries, as ``Dockerfile.crew`` lays it
#: out. The manifest in it is what says which crew this image IS.
BUNDLE_DIR = "/app/crew-bundle"  # noqa: S108 - an image path, not a temp dir


def bundle_crew_name() -> str:
    """The crew name the image's own manifest declares, or ``""``.

    Read rather than taken from the launch payload, because the supervisor
    compares the name it is given against this manifest and refuses a mismatch.
    The two answer different questions: the manifest says what was BUILT, and
    the payload's tag says what this launch calls it.

    Empty on any failure, so a bundle whose manifest cannot be read falls back to
    the tag rather than refusing to boot -- a crew that starts with the wrong name
    is recoverable and one that never starts is not visible at all.
    """
    try:
        with open(f"{BUNDLE_DIR}/manifest.json", encoding="utf-8") as handle:
            return str(json.load(handle).get("crew_name") or "")
    except (OSError, ValueError):
        return ""


def supervisor_env(
    payload: "dict[str, Any]", control_secret: str, identity: str
) -> "dict[str, str]":
    """The environment the supervisor would have been given by a task definition.

    This is the lane's whole adaptation: on Fargate these values are a task
    definition's environment and secrets; here they are assembled from the run
    payload and two Secrets Manager reads. The NAMES are the Fargate contract
    unchanged, so one supervisor serves both lanes and neither has a branch.

    ``SMC_FRONT_BIND`` is the one value whose answer differs by lane, and it is
    set rather than defaulted: see ``front/__main__``.
    """
    archive = payload.get("archive") or {}
    env = dict(os.environ)
    env.update(
        # The BUNDLE's crew name, not the launch tag. The supervisor checks the
        # name it is given against the bundle's manifest and refuses when they
        # differ, so taking the tag here crashes the crew whenever an operator's
        # launch tag is not also the crew's name -- which is ordinary, since the
        # tag is this launch's id and the name belongs to the bundle. The tag is
        # still what the control plane calls this crew; it is just not what the
        # IMAGE is.
        SMC_CREW_NAME=bundle_crew_name() or str(payload.get("tag") or ""),
        # Strict auth, asserted here as well as baked into the image's own ``ENV``.
        #
        # Not redundant: this environment starts from ``os.environ``, so the
        # image's value already arrives -- but a launch that passed its own value
        # would be in there too, and this line is what makes the guest's answer
        # the final one. A lane whose compute carries an internet-reachable
        # endpoint and no security group cannot let a caller choose the posture.
        SMC_REQUIRE_AUTH_ALL_ROUTES="1",
        SMC_CONTROL_SECRET=control_secret,
        KIRO_IDENTITY=identity,
        # The VM is the isolation boundary and the crews are the operator's own.
        # Both flags name a trust boundary the deployment is asserting, which is
        # why neither has a default that assumes it.
        SMC_SINGLE_PRINCIPAL="1",
        SMC_INTERNAL_ONLY="1",
        SMC_FRONT_BIND="127.0.0.1",
        SMC_BACKUP_BUCKET=str(archive.get("bucket") or ""),
        SMC_BACKUP_PREFIX=str(archive.get("key") or "").rsplit("/", 1)[0],
    )
    return env


def boot_crew(payload: "dict[str, Any]") -> None:
    """Secrets, then conversations, then the crew -- on a worker thread.

    Off the hook's own thread because the hook has 60 seconds and this does not
    fit in them, and because nothing is waiting for it: the launch waits for the
    SSM node, which the inline half of ``run`` has already arranged.

    The order is forced. Credentials before secrets, because the guest's only AWS
    identity is the one the SSM agent publishes. Restore before the crew, because
    the supervisor installs the bundle and the backend reads the data home at
    start. The identity last of the two secrets, so a failure to read it is
    reported against a data home that is already in its final shape.
    """
    stage = "start"
    try:
        region = str(payload.get("region") or _region)
        control_ref = str(payload.get("secretRef") or "")
        archive = payload.get("archive") or {}
        home = os.environ.get("SMC_DATA_HOME", DEFAULT_DATA_HOME)

        stage = "shared-credentials"
        _boot.update(stage=stage)
        if not wait_for_shared_credentials():
            raise RuntimeError(
                "the SSM agent never published node credentials, so no secret can be read"
            )

        stage = "restore"
        _boot.update(stage=stage)
        _boot["restore"] = restore_data_home(
            str(archive.get("bucket") or ""),
            str(archive.get("key") or ""),
            str(archive.get("etag") or ""),
            region,
            home,
            # The launcher's statement, read strictly: only an explicit True is a
            # restore point. A payload from a launcher too old to carry the field
            # reads as False, which skips the fetch -- the safe direction, because
            # the alternative on this grant is a boot that fails at the restore
            # stage for every crew rather than one that starts with the
            # conversations it has.
            expected=archive.get("restore") is True,
        )

        stage = "secrets"
        _boot.update(stage=stage)
        if not control_ref:
            raise RuntimeError("the payload carried no secret reference")
        control_secret = read_secret(control_ref, region)
        # The payload's OWN reference, not one derived from the control secret's
        # name. Both were once built from the launch tag, and that tag is minted
        # by the launcher rather than chosen by the operator -- so a derived name
        # names a secret nobody could have created ahead of the launch, and this
        # read ended every boot at its secrets stage while the VM billed to its
        # wall. A launcher too old to send the field is refused here rather than
        # falling back to the derived name, because that fallback IS the failure.
        identity_ref = str(payload.get("identityRef") or "")
        if not identity_ref:
            raise RuntimeError(
                "the payload carried no model-credential reference. Set "
                "microvm.identity_secret_ref in cloud.json to the secret the "
                "operator created; it cannot be derived from the launch tag, "
                "which the launcher mints"
            )
        identity = read_secret(identity_ref, region)
        # Lengths, never values. Two numbers are enough to tell "the secret was
        # read" from "the secret was empty", which is the only question a log can
        # usefully answer about a credential.
        log("secrets.read", control_chars=len(control_secret), identity_chars=len(identity))

        stage = "supervisor"
        _boot.update(stage=stage)
        spawn(
            "supervisor",
            [sys.executable, "-m", "container.supervisor"],
            env=supervisor_env(payload, control_secret, identity),
            as_user=CREW_USER,
        )
        del control_secret, identity
        started_at = time.time()
        _boot.update(stage="started", started_at=started_at)
        # AFTER the crew is up, and measured from the VM's own start rather than
        # from here: the edges in the payload are offsets from the moment the
        # platform began charging for this VM, not from the moment boot finished.
        arm_wall_watchdog(payload, float(_boot.get("vm_started_at") or started_at))
    except Exception as exc:  # noqa: BLE001 - the stage IS the diagnosis
        log("boot.failed", stage=stage, error=repr(exc))
        _boot.update(stage=f"failed:{stage}", error=repr(exc))
    finally:
        write_state(BOOT_STATE, dict(_boot))


def handle_run(body: bytes) -> "dict[str, Any]":
    """The real bootstrap, and the one hook that may happen only once.

    A repeat answers 200 and does nothing, in the same shape a first call
    answers: this hook cannot authenticate its caller, so telling a stranger
    their call was ignored would tell them a crew is already up.
    """
    global _managed_instance_id
    os.makedirs(STATE_DIR, exist_ok=True)
    if os.path.exists(RUN_MARKER):
        log("run.repeat_ignored")
        return {"ok": True, "repeat": True}

    # The VM's own start, which is what the payload's wall edges are offsets
    # from. Taken HERE because this hook is the platform's first call into a
    # started VM: taking it after the crew boots would move both edges later by
    # however long boot took, and the one edge that must not move is the one in
    # front of a lifetime the platform will not extend.
    _boot["vm_started_at"] = time.time()

    # Through the LANE's own decoder, not a local json.loads. The platform
    # delivers the launcher's payload BASE64-ENCODED -- undocumented, measured
    # measured live -- and a guest that reads only raw JSON fails on every real
    # launch with no hint of why. One decoder, pinned by the lane's own test,
    # means the launcher and the guest cannot disagree about the shape.
    try:
        payload = decode_payload(body)
        encoding = payload_encoding(body)
    except Exception as exc:  # noqa: BLE001
        # Length and the exception TYPE only. The body carries a single-use
        # activation code, so its content must never reach a log -- which is
        # also why the earlier version of this line, logging only the length,
        # left three failed launches with no cause at all. The fix is a better
        # decoder, not a louder log.
        log("run.payload_unreadable", bytes=len(body), error=type(exc).__name__)
        return {"ok": True, "repeat": False}

    ssm_block = payload.get("ssm") or {}
    archive = payload.get("archive") or {}
    region = str(payload.get("region") or _region)
    # What arrived, as shapes and lengths. "The payload arrived intact" is a
    # claim someone will want to check, and the activation code's length checks it
    # without recording it.
    write_state(
        PAYLOAD_SEEN,
        {
            "encoding": encoding,
            "tag": payload.get("tag"),
            "gen": payload.get("gen"),
            "region": region,
            "ssm_id": ssm_block.get("id"),
            "ssm_code_len": len(str(ssm_block.get("code") or "")),
            "secret_ref_present": bool(payload.get("secretRef")),
            "archive_bucket": archive.get("bucket"),
            "archive_key": archive.get("key"),
            "archive_etag": archive.get("etag"),
            "wall": payload.get("wall"),
        },
    )

    log("machine_id.in_effect", machine_id=reset_machine_id())
    register_agent(str(ssm_block.get("id") or ""), str(ssm_block.get("code") or ""), region)
    spawn("ssm-agent", [AGENT_BIN])
    _managed_instance_id = read_managed_instance_id()
    log("run.registered", managed_instance_id=_managed_instance_id, payload_encoding=encoding)

    # The single-shot marker is written HERE, after the registration succeeded,
    # and not on the way in.
    #
    # Written first, a run that failed at the registration could never be retried
    # -- not by the platform and not by a relaunch against the same VM -- so a
    # transient failure became a permanently dark crew, which is exactly what
    # happened live. Written here, the only thing the marker refuses is a
    # SECOND SUCCESSFUL run, which is the property it exists for: resetting the
    # machine id and re-registering the agent is what would sever the owner's own
    # channel.
    #
    # Writing it late does not open a replay: a caller who reaches this route
    # without a version-1 payload is turned away above, and a valid payload
    # carries a live single-use activation code that only the platform was given.
    with open(RUN_MARKER, "w", encoding="ascii") as fh:
        fh.write(str(time.time()))

    threading.Thread(target=boot_crew, args=(payload,), name="boot", daemon=True).start()
    return {"ok": True, "repeat": False}


def crew_is_serving() -> bool:
    """Whether the crew's own processes are still up, by this guest's own look.

    The only liveness question a guest can answer about itself without trusting
    its caller. ``pkill -0`` signals nothing and only reports whether a match
    exists, so this is a question rather than a second terminate.

    Returns ``True`` on any failure to tell. The caller uses this to decide
    whether to keep the crew reachable, and the safe answer when the guest cannot
    see is "it is still serving" -- which leaves the node registered rather than
    cutting a crew off on no information.
    """
    try:
        found = subprocess.run(
            ["pkill", "-0", "-f", "container.supervisor"],
            check=False,
            timeout=10,
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 - cannot tell means do not cut it off
        return True
    return found.returncode == 0


def deregister_self(trigger: str) -> None:
    """Hand the managed node back on the way out.

    Best effort and a backstop: the ``terminate`` hook has no documented timeout.
    It is here because a terminated VM otherwise leaves its ``mi-`` node
    registered and reporting ``Online`` forever -- one leaked per launch, measured live -- and that node is the one resource neither the lane's teardown
    nor its sweeper reclaims today.
    """
    mi = _managed_instance_id or read_managed_instance_id()
    if not mi:
        return
    try:
        import boto3
        from botocore.config import Config

        boto3.client(
            "ssm",
            region_name=_region,
            config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1}),
        ).deregister_managed_instance(InstanceId=mi)
        log("deregister.ok", managed_instance_id=mi, trigger=trigger)
    except Exception as exc:  # noqa: BLE001
        log("deregister.failed", managed_instance_id=mi, trigger=trigger, error=repr(exc))


# ── HTTP ─────────────────────────────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):
    """Six hook paths. Everything else gets one constant answer.

    ``server_version`` and ``sys_version`` are blanked and ``send_response`` is
    narrowed to the status line, so no reply carries a ``Server`` header: the
    default reads ``BaseHTTP/0.6 Python/3.12.x`` and hands anyone who can reach
    this port the guest's interpreter version for free.
    """

    server_version = ""
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def send_response(self, code: int, message: Optional[str] = None) -> None:  # noqa: A003
        self.send_response_only(code, message)

    def log_message(self, fmt: str, *args: Any) -> None:
        # Hook events are logged by their handlers through ``log``. An access log
        # here would let anyone who can reach the endpoint fill the guest's log
        # destination by choosing paths.
        return

    def _reply(self, code: int, body: "dict[str, Any]") -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return b""
        # The payload budget is 4 KiB; this cap is generous against it and bounds
        # what a stranger can make this process allocate.
        return self.rfile.read(min(length, 65536)) if length > 0 else b""

    def _route(self) -> str:
        path = (self.path or "").split("?", 1)[0].rstrip("/")
        return path[len(HOOK_PREFIX) :].lstrip("/") if path.startswith(HOOK_PREFIX) else ""

    def do_GET(self) -> None:  # noqa: N802
        # No hook is a GET, so every GET -- including the front and dashboard
        # paths a probe will try -- gets the one constant answer.
        self._reply(404, {"ok": False})

    def do_POST(self) -> None:  # noqa: N802
        name = self._route()
        body = self._read_body()
        if name == "run":
            self._reply(200, handle_run(body))
            return
        if name in ("ready", "validate", "resume", "suspend"):
            # Fixed replies. ``ready`` answering 200 is what tells the build
            # service to snapshot, so a successful image build is itself proof
            # that this listener starts. ``resume`` and ``suspend`` have nothing
            # to do: the platform snapshots and restores the whole VM, and the
            # lane's own idle verdict is computed on the owner's gateway from the
            # crew's chat slots -- not here, where a guest cannot see whether the
            # owner is waiting on a turn.
            log(f"hook.{name}")
            self._reply(200, {"ok": True})
            return
        if name == "terminate":
            log("hook.terminate")
            # The hook listener cannot authenticate its caller -- that is why
            # every reply here is a constant -- so a terminate must not be able to
            # sever a RUNNING crew's only route in. Deregistering the SSM node is
            # exactly that: the owner reaches this crew through a port-forward over
            # that node and nothing else, so a spurious call would leave a crew
            # that serves, bills, and cannot be reached or torn down from the
            # dashboard.
            #
            # So the guest checks the one thing it can see for itself: whether the
            # crew is still serving. A VM the platform is really taking down has
            # had its processes stopped; one that is still answering turns is not
            # going anywhere.
            #
            # The cost of being wrong in this direction is a managed node left
            # registered for a VM that did go away -- which ``sweeper.py`` reports
            # and an operator can delete. The cost in the other direction is a
            # live crew nobody can reach again. Those are not comparable.
            if crew_is_serving():
                log("hook.terminate.ignored", reason="the crew is still serving")
            else:
                deregister_self("terminate-hook")
            # 200 either way. The platform reads a non-200 as a hook that failed,
            # and this hook's job is to be answered; what it DID is the guest's
            # business and is in the log above.
            self._reply(200, {"ok": True})
            return
        self._reply(404, {"ok": False})

    def do_PUT(self) -> None:  # noqa: N802
        self._reply(404, {"ok": False})

    def do_DELETE(self) -> None:  # noqa: N802
        self._reply(404, {"ok": False})

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


def on_signal(signum: int, _frame: Any) -> None:
    """Deregister, stop the children, and leave.

    The signal path and not the ``terminate`` hook is what makes deregistration
    reliable: a signal cannot be sent from the internet, and the hook's budget is
    undocumented. Both firing is harmless -- the deregister is idempotent.
    """
    log("signal", signum=signum)
    deregister_self(f"signal-{signum}")
    with _lock:
        for name, proc in _children.items():
            if proc.poll() is None:
                log("child.terminate", name=name)
                proc.terminate()
    time.sleep(2)
    os._exit(0)


class HookServer(ThreadingHTTPServer):
    """The hook listener's server, with its address-reuse choice STATED.

    ``SO_REUSEADDR`` is wanted here, and that is why it is written down rather
    than inherited. This process is PID 1 on a fixed port the image declared to
    ``CreateMicrovmImage``, so a restart has to rebind that exact port
    immediately: without reuse, a socket still in ``TIME_WAIT`` from the previous
    listener makes the bind fail and the VM answers no hook at all, which the
    platform reads as a guest that never came up.

    Inheriting the stdlib's ``True`` would reach the same behaviour by accident.
    A fixed-port listener that never names the flag is one nobody can tell apart
    from a listener that did not think about it, which is what the repo's own
    address-reuse rule is for -- and it is the opposite choice from the app
    backends, which bind loopback and must NOT silently take over a port.
    """

    allow_reuse_address = True


def main() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    # 0.0.0.0 is required rather than chosen: the platform's hook caller is not
    # inside this container's loopback. It is also exactly why every reply above
    # is a constant. The crew's own listener binds 127.0.0.1.
    server = HookServer(("0.0.0.0", HOOK_PORT), Handler)  # noqa: S104
    log("hooks.listening", port=HOOK_PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
