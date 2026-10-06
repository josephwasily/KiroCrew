"""The production :class:`LifecycleDeps`, built from a real cloud config.

``MicroVmLifecycle`` is written against a dependency set so the local harness can
swap the whole thing. This module is the other implementation: the one that talks
to AWS and to a real guest, and the reason the lifecycle tick has callers rather
than only tests.

**Three of the dependencies reach into the guest, and SSM is the channel.** The
home is on the guest's disk and the slot accounting is the backend's, so reading
state, sealing the gateway and writing the archive are all commands run INSIDE
the VM. They go through ``ssm.run_command`` as the crew user, against the managed
instance the guest registered itself as. That channel is authenticated by the
node's own IAM identity, which is why these are not front routes: a front route
would require the gateway to carry the per-crew control secret into a maintenance
pass that has no business holding it.

**The other six are plain control-plane calls** -- suspend, resume, terminate,
read status, wait for terminated -- and are the same API wrappers the launcher
uses.

**A crew with no managed instance id is not reachable, and that is reported as
unreachable rather than as an error.** ``read_guest`` returning ``None`` is a
first-class answer that the lifecycle reads as "may be mid-turn behind a blip",
and it never suspends on it. A crew whose node never registered is in exactly
that position.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from kiro_crew.cloud.microvm import api
from kiro_crew.cloud.microvm.lifecycle import GuestState, LifecycleDeps, MicroVmLifecycle
from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore

logger = logging.getLogger(__name__)

#: The guest module the three guest-side operations are invoked through.
#:
#: Named here rather than spelled into each command so the three cannot drift
#: onto different entry points, and so a reader can find the other half of this
#: seam from this side of it.
GUEST_OPS_MODULE = "container.microvm.ops"

#: The user the maintenance commands run as inside the guest. ROOT, and the crew
#: user would not work for three independent reasons:
#:
#: 1. ``crew`` is created with ``--shell /usr/sbin/nologin``, and
#:    ``ssm.run_command`` wraps every command in ``sudo -u <user> -i bash`` --
#:    which runs the login shell, so the call exits non-zero before reaching the
#:    verb.
#: 2. The guest-state and restore directories are created root-owned, so ``ops``
#:    as ``crew`` could write neither its idle mark nor the archive it builds.
#: 3. The SSM agent's ``ShareCreds`` publishes the node's credentials to ROOT's
#:    shared credentials file, so ``crew`` has no AWS identity for the S3 put.
#:
#: This does not privilege the crew: the crew's own processes still run as
#: ``crew`` under the supervisor. What runs as root is the maintenance the hook
#: process already performs as root from inside the VM -- reading state, stopping
#: the gateway, writing the archive -- so this makes the remote path and the local
#: one the same identity rather than granting a new one.
#:
#: The archive stays readable by the crew because the restore chowns the home to
#: ``crew`` after unpacking, which is where that property is enforced.
GUEST_RUN_AS = "root"

#: Where the guest's own package sits, so ``python3 -m`` can find it. The image
#: puts the container package at this path and nothing adds it to the default
#: path for a login shell.
GUEST_APP_DIR = "/app"

#: How long to wait for a terminate to be visible, and how often to look.
#: Bounded because the record must not be written ``STOPPED`` on an assumption,
#: and a tick that waited forever would stop every crew behind it.
TERMINATE_TIMEOUT_SECONDS = 300
TERMINATE_POLL_SECONDS = 5


def _ops_command(verb: str, *args: str) -> str:
    """One guest command line, with every argument quoted.

    ``shlex.quote`` on every piece, because these strings carry a bucket name, an
    object key and an ETag that came from a record on disk. None of them is user
    input today and all of them are interpolated into a shell command that runs
    as the crew user, so the quoting is the thing that keeps it that way.
    """
    import shlex

    parts = [
        "cd",
        GUEST_APP_DIR,
        "&&",
        "python3",
        "-m",
        GUEST_OPS_MODULE,
        verb,
    ]
    return " ".join([*parts, *(shlex.quote(a) for a in args)])


class GuestUnreachable(RuntimeError):
    """The guest could not be asked. Distinct from the guest answering badly."""


def _run_guest(
    record: CrewRecord,
    verb: str,
    *args: str,
    profile: str,
    region: str,
) -> "dict[str, Any]":
    """Run one ops verb in the guest and return its parsed answer.

    Raises :class:`GuestUnreachable` when the command could not be run or its
    output is not the one JSON line the verb promises. The caller turns that into
    whichever answer its own contract needs -- ``None`` for a read, a raise for a
    write -- and that choice is deliberately not made here.
    """
    from kiro_crew.cloud import ssm

    if not record.mi_id:
        raise GuestUnreachable(f"crew {record.tag} has no managed instance id yet")
    result = ssm.run_command(
        record.mi_id,
        _ops_command(verb, *args),
        profile=profile,
        region=region,
        run_as=GUEST_RUN_AS,
    )
    if not result.ok:
        raise GuestUnreachable(
            f"crew {record.tag}: the guest's {verb} command failed: "
            f"{(getattr(result, 'stderr', '') or '').strip()[:300]}"
        )
    text = (getattr(result, "stdout", "") or "").strip().splitlines()
    for line in reversed(text):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            if payload.get("error"):
                raise GuestUnreachable(
                    f"crew {record.tag}: the guest's {verb} failed: {payload['error']}"
                )
            return payload
    raise GuestUnreachable(f"crew {record.tag}: the guest's {verb} printed no JSON answer")


def read_guest(record: CrewRecord, *, profile: str, region: str) -> Optional[GuestState]:
    """The guest's reduced state, or ``None`` when it cannot be reached.

    ``None`` rather than a raise, and the difference matters: the lifecycle reads
    unreachable as "not idle" and leaves the crew alone, which is the safe answer
    for a crew that might be mid-turn behind a network blip.
    """
    try:
        payload = _run_guest(record, "state", profile=profile, region=region)
    except Exception as exc:  # noqa: BLE001 - unreachable is an answer, not a failure
        logger.info("microvm crew %s could not be read: %s", record.tag, exc)
        return None
    try:
        return GuestState(
            ready=bool(payload.get("ready")),
            running_slots=int(payload.get("running_slots") or 0),
            idle_for_seconds=float(payload.get("idle_for_seconds") or 0.0),
            restarts=int(payload.get("restarts") or 0),
            generation=int(payload.get("generation") or 0),
            self_pack_armed=bool(payload.get("self_pack_armed")),
            self_packed_etag=str(payload.get("self_packed_etag") or ""),
        )
    except (TypeError, ValueError) as exc:
        # A malformed answer is also unreachable rather than a crash: the tick
        # must survive one crew's bad reply.
        logger.warning("microvm crew %s answered an unreadable state: %s", record.tag, exc)
        return None


def stop_gateway(record: CrewRecord, *, profile: str, region: str) -> None:
    """Stop the crew's gateway and confirm it exited, or raise.

    A raise here, where ``read_guest`` returns ``None``, because this is the step
    that makes the archive trustworthy. A pack over a gateway that is still
    writing captures a torn database, so a seal that cannot be confirmed must
    stop the pack rather than let it proceed on an assumption.
    """
    payload = _run_guest(record, "seal", profile=profile, region=region)
    if not payload.get("sealed"):
        raise RuntimeError(
            f"crew {record.tag} was not sealed: {payload.get('reason') or 'unknown'}. "
            "The archive is not written, because packing a home whose writers are "
            "still running stores a torn database."
        )


def pack_home(record: CrewRecord, *, profile: str, region: str, kms_key_id: str) -> str:
    """Ask the guest to write its archive, and return the new ETag."""
    payload = _run_guest(
        record,
        "pack",
        "--bucket",
        record.archive_bucket,
        "--key",
        record.archive_key,
        "--region",
        record.region or region,
        "--etag",
        record.archive_etag,
        "--kms-key-id",
        kms_key_id,
        profile=profile,
        region=region,
    )
    etag = str(payload.get("etag") or "")
    if not etag:
        raise RuntimeError(
            f"crew {record.tag} reported a pack with no ETag, so the gateway has no "
            "generation to condition the next write on"
        )
    return etag


def production_deps(config: Any, *, store: Optional[CrewStore] = None) -> LifecycleDeps:
    """The real dependency set for one cloud config's lane.

    Every closure takes the record it acts on, so one set serves every crew the
    store holds rather than being rebuilt per crew.
    """
    from kiro_crew.cloud.config import CloudConfig

    cloud = CloudConfig.load()
    profile, region = cloud.profile, cloud.region
    spec = config.launch_spec()
    endpoint = spec.endpoint_url
    kms_key_id = spec.kms_key_id

    def _vm(method: Any) -> Any:
        return lambda record: method(
            record.microvm_id, profile=profile, region=region, endpoint_url=endpoint
        )

    def _wait_terminated(record: CrewRecord) -> None:
        """Poll until the platform agrees the VM is gone.

        A loop rather than one call, because the API has no blocking terminate and
        the record must not be written ``STOPPED`` while the VM is still billing.
        ``None`` counts as terminal: a VM the platform has forgotten is not one
        this gateway should keep waiting for.
        """
        import time

        deadline = time.time() + TERMINATE_TIMEOUT_SECONDS
        while time.time() < deadline:
            status = api.microvm_status(
                record.microvm_id, profile=profile, region=region, endpoint_url=endpoint
            )
            if status is None or status in api.TERMINAL_MICROVM_STATES:
                return
            time.sleep(TERMINATE_POLL_SECONDS)
        raise TimeoutError(
            f"MicroVM {record.microvm_id} for crew {record.tag} did not reach a terminal state"
        )

    return LifecycleDeps(
        store=store if store is not None else CrewStore(),
        read_guest=lambda record: read_guest(record, profile=profile, region=region),
        stop_gateway=lambda record: stop_gateway(record, profile=profile, region=region),
        pack_home=lambda record: pack_home(
            record, profile=profile, region=region, kms_key_id=kms_key_id
        ),
        suspend_vm=_vm(api.suspend_microvm),
        resume_vm=_vm(api.resume_microvm),
        terminate_vm=_vm(api.terminate_microvm),
        read_vm_status=_vm(api.microvm_status),
        wait_terminated=_wait_terminated,
    )


def production_lifecycle(config: Any, *, store: Optional[CrewStore] = None) -> MicroVmLifecycle:
    """:class:`MicroVmLifecycle` over :func:`production_deps`."""
    return MicroVmLifecycle(production_deps(config, store=store))
