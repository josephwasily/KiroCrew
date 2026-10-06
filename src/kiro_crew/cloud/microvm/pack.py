"""Archive a crew's home to S3 under a conditional write, and restore it on reopen.

A MicroVM's disk goes with the VM, and the VM goes at eight hours whatever it is
doing. So the archive is not a backup -- it is the crew's continuity, and the one
thing that makes a reopen safe is that every write is conditional on the ETag the
last write returned.

The lineage rule, stated once:

- the FIRST pack of a crew's life sends ``If-None-Match: *``, which succeeds only
  if the object does not exist;
- every later pack sends ``If-Match <the ETag the previous pack returned>``;
- a ``412`` is NEVER retried, and never retried with a fresh ``GET`` of the
  current ETag. Re-reading the ETag and writing again is last-write-wins wearing a
  conditional write's clothes, and the whole point of the condition is to refuse
  the write whose base the caller did not see.

A 412 on this lane means two writers raced for one crew's archive, which one
gateway should make impossible. So it is reported as a defect rather than
smoothed over, and the crew is left ``RUNNING`` with its home intact rather than
terminated with an archive nobody can trust.

Two riders that each cost a crew in the reference implementation:

``tar`` must not compress with an external binary.
    The published crew image installs no ``zstd``, so ``tar --zstd`` fails at exec
    time -- on every image built, discovered only at the first pack that got past
    the earlier checks. :func:`archive_argv` uses gzip, which ``tar`` has built
    in, and :func:`missing_compressor` is the explicit check for anything else.

The SQLite sidecars are part of the data.
    A ``memory.db`` of four kilobytes can sit beside a ``-wal`` of three hundred,
    and an archive that takes the first and not the second restores two EMPTY
    databases that pass ``integrity_check``. So the archive set names the
    directories and the denylist names what to leave out, rather than the other
    way round.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from typing import Optional

from kiro_crew.cloud.aws import AWSError, checked_json

logger = logging.getLogger(__name__)

#: Everything in a crew's data home that must survive a VM replacement, relative
#: to the home.
#:
#: This is deliberately NOT the set ``portability.create_export_zip`` writes.
#: Export is a document an owner carries between installs and it excludes session
#: transcripts, ``uploads/`` and ``session_map.json`` on purpose. For this lane
#: those three ARE the crew: a reopened crew with its settings and none of its
#: conversations is a new crew wearing the old one's name.
ARCHIVE_PATHS: tuple[str, ...] = (
    "config.json",
    "config.local.json",
    "ui-prefs.json",
    "notification_settings.json",
    "hooks.json",
    "crons.json",
    "notifications.jsonl",
    # The join between the two halves of a session. Export excludes it; without
    # it a restored transcript cannot be matched to the kiro-cli replay log that
    # makes the session RESUME rather than replay as a lossy prefix.
    "session_map.json",
    "sessions",
    "uploads",
    "artifacts",
    "skills",
    "crons",
    "lessons",
    "workspace",
    "plan_memory",
    "crew-teams",
    # The memory databases and their SQLite sidecars. Named as a directory-free
    # glob rather than three literal names so a journal mode change does not
    # silently drop the file that holds the recent writes.
    "memory.db",
    "memory.db-wal",
    "memory.db-shm",
    "memory_index.db",
    "memory_index.db-wal",
    "memory_index.db-shm",
)

#: What must NOT go in the archive, relative to the home.
#:
#: The first four are credentials and a socket. A restored crew that inherited the
#: packed generation's listener secret would hold a credential minted for a VM that
#: is already gone, and a unix socket is not a file type ``tar`` can carry.
#:
#: ``models`` is the embedding model, measured at 639 MB against a 54 KB archive
#: without it. Excluded here and skipped at boot by
#: ``KIROCREW_SKIP_MODEL_DOWNLOAD=1`` in the run payload, so the crew neither
#: archives it nor re-fetches it.
#:
#: ``gateway.lock`` is deliberately absent from this list even though it looks
#: like residue. Excluding it in the reference implementation deleted the crash
#: ladder structurally: a restored crew reported zero restarts across three VMs
#: because the counter it is derived from never travelled.
ARCHIVE_DENYLIST: tuple[str, ...] = (
    "run",
    ".local_secret",
    "dashboard.sock",
    "models",
    "snapshots",
    "outbox",
)


class PackRefused(RuntimeError):
    """The pack did not happen, and the reason is one a caller must branch on."""


class PackConflict(PackRefused):
    """The conditional write was refused: another writer holds this archive.

    Separate from :class:`PackRefused` because the response to it is different.
    Every other refusal is a retryable or fixable condition; this one is a report.
    Two writers for one crew's archive should be impossible with a single local
    gateway, so reaching it means something else is writing -- and a retry would
    overwrite whatever that is.
    """

    def __init__(self, message: str, *, held_etag: str = "") -> None:
        super().__init__(message)
        self.held_etag = held_etag


class GatewayAlive(PackRefused):
    """A pack was asked for while the crew's gateway process was still running.

    Refused rather than attempted. The archive is a tar of a quiesced home, and a
    gateway mid-write produces an archive of a moving target -- which is how a
    ``tar`` exits non-zero with "file changed as we read it" and leaves a
    half-written object that the next restore reads as the crew.
    """


def missing_compressor(program: str) -> bool:
    """Whether *program* is absent from ``PATH``, so ``tar`` could not exec it.

    Exists so a caller can produce ``zstd_missing`` as its own diagnosis instead
    of a ``tar`` exit code. The published crew image installs ``sudo``,
    ``iproute2``, ``procps``, ``less`` and ``ca-certificates`` and nothing else, so
    any external compressor is absent until someone adds it to the image.
    """
    return shutil.which(program) is None


def archive_argv(home: str, *, denylist: tuple[str, ...] = ARCHIVE_DENYLIST) -> list[str]:
    """``tar`` argv that writes this crew's archive to stdout.

    gzip, through ``-z``, which ``tar`` implements itself. Every other
    compressor ``tar`` offers is an external binary it execs, and the published
    crew image has none of them.

    The exclusions are passed as ``--exclude`` patterns anchored at the archive
    root, so a denylisted name matches the crew's own ``run/`` and not a
    ``workspace/run/`` a user created.
    """
    argv = ["tar", "-czf", "-", "-C", home]
    for name in denylist:
        argv += ["--exclude", f"./{name}"]
    argv.append(".")
    return argv


def missing_archive_members(names: "set[str] | frozenset[str]") -> tuple[str, ...]:
    """Which of :data:`ARCHIVE_PATHS` are absent from an archive's member list.

    This is what gives the positive set a job rather than leaving it a comment.
    :func:`archive_argv` tars the home MINUS the denylist, because that is the one
    form that cannot silently drop a file nobody thought to list -- a SQLite
    sidecar, an attachment directory, a new settings document. The positive list
    is then the assertion: after a pack, every name here that the crew actually
    has must be inside the archive, and a name that is missing is a denylist
    pattern that reached further than it was meant to.

    Returns the names in :data:`ARCHIVE_PATHS` order, so the report is stable.
    """
    present = {n.lstrip("./") for n in names}
    out: list[str] = []
    for wanted in ARCHIVE_PATHS:
        if wanted in present:
            continue
        if any(member == wanted or member.startswith(wanted + "/") for member in present):
            continue
        out.append(wanted)
    return tuple(out)


@dataclass(frozen=True)
class ArchiveRef:
    """Where a crew's archive lives, and the ETag that authorises the next write."""

    bucket: str
    key: str
    #: Empty for a crew that has never been packed.
    etag: str = ""
    kms_key_id: str = ""

    def put_condition(self) -> list[str]:
        """The conditional-write argument for the next pack of this archive.

        One function, so the two cases cannot be spelled differently in two
        places. The empty-ETag case is not a missing condition -- it is
        ``If-None-Match: *``, the condition that the object does not yet exist,
        and a pack with no condition at all is the last-write-wins this module
        exists to prevent.
        """
        if not self.etag:
            return ["--if-none-match", "*"]
        return ["--if-match", self.etag]


def put_archive(
    ref: ArchiveRef,
    body_path: str,
    *,
    profile: str = "",
    region: str = "",
    endpoint_url: str = "",
    timeout: int = 600,
) -> str:
    """Write *body_path* to *ref* conditionally. Returns the new ETag.

    Raises :class:`PackConflict` on a 412 and does NOT retry. The ETag it returns
    must be written to the crew's record before the caller reports success: a pack
    whose ETag the ledger never learned leaves the crew archived and
    unrestorable, because the next write's condition names an ETag nobody holds.
    """
    args = [
        "s3api",
        "put-object",
        "--bucket",
        ref.bucket,
        "--key",
        ref.key,
        "--body",
        body_path,
        *ref.put_condition(),
    ]
    if ref.kms_key_id:
        # Stated rather than omitted, because a bucket created by this lane's own
        # template denies a put that names no key: the policy requires the CMK, so
        # an omitted key is a 403 and not a default-encrypted object.
        args += ["--server-side-encryption", "aws:kms", "--ssekms-key-id", ref.kms_key_id]
    else:
        # An archive bucket on S3-managed encryption instead of a CMK. Measured
        # against a real SSE-S3 bucket: with NO header at all this
        # put is refused by the bucket's own deny-unencrypted-put statement --
        #   AccessDenied ... with an explicit deny in a resource-based policy
        # -- because that statement tests the request header and a bucket default
        # does not set one. So the header is always sent; only which algorithm it
        # names depends on whether a key was chosen.
        args += ["--server-side-encryption", "AES256"]
    if endpoint_url:
        args += ["--endpoint-url", endpoint_url]
    try:
        data = checked_json(args, profile, region, action="s3:PutObject", timeout=timeout)
    except AWSError as exc:
        if _is_precondition_failed(exc):
            raise PackConflict(
                "the archive write was refused because its precondition failed: another "
                "writer holds this crew's archive. Not retrying -- re-reading the ETag and "
                "writing again would overwrite work this gateway never saw.",
                held_etag=ref.etag,
            ) from exc
        raise
    etag = ""
    if isinstance(data, dict):
        etag = str(data.get("ETag", "") or "")
    if not etag:
        raise PackRefused(
            "the archive write returned no ETag, so the next pack would have no condition "
            "to send; treating it as a failed pack rather than recording an unknown lineage"
        )
    return etag


def get_archive(
    ref: ArchiveRef,
    dest_path: str,
    *,
    profile: str = "",
    region: str = "",
    endpoint_url: str = "",
    timeout: int = 600,
) -> str:
    """Fetch *ref* to *dest_path*. Returns the ETag the service served.

    Raises :class:`PackConflict` when the stored ETag disagrees with the record's.
    A restore onto a home from an archive whose lineage the record does not
    recognise is the one case where continuing is worse than failing: the crew
    would come back as someone else's bytes under its own name.
    """
    args = ["s3api", "get-object", "--bucket", ref.bucket, "--key", ref.key, dest_path]
    if ref.etag:
        args += ["--if-match", ref.etag]
    if endpoint_url:
        args += ["--endpoint-url", endpoint_url]
    try:
        data = checked_json(args, profile, region, action="s3:GetObject", timeout=timeout)
    except AWSError as exc:
        if _is_precondition_failed(exc):
            raise PackConflict(
                "the archive this crew's record names does not match what the bucket holds, "
                "so the restore was refused rather than laying down bytes from an unknown "
                "lineage",
                held_etag=ref.etag,
            ) from exc
        raise
    return str(data.get("ETag", "") or "") if isinstance(data, dict) else ""


def head_archive(
    ref: ArchiveRef,
    *,
    profile: str = "",
    region: str = "",
    endpoint_url: str = "",
    timeout: int = 60,
) -> Optional[dict]:
    """The archive's metadata, or ``None`` when there is no object."""
    args = ["s3api", "head-object", "--bucket", ref.bucket, "--key", ref.key]
    if endpoint_url:
        args += ["--endpoint-url", endpoint_url]
    try:
        data = checked_json(args, profile, region, action="s3:HeadObject", timeout=timeout)
    except AWSError as exc:
        if "404" in str(exc) or "Not Found" in str(exc) or "NoSuchKey" in str(exc):
            return None
        raise
    return data if isinstance(data, dict) else None


def delete_archive(
    ref: ArchiveRef,
    *,
    profile: str = "",
    region: str = "",
    endpoint_url: str = "",
    timeout: int = 60,
) -> None:
    """Delete the archive object. Used only by expiry."""
    args = ["s3api", "delete-object", "--bucket", ref.bucket, "--key", ref.key]
    if endpoint_url:
        args += ["--endpoint-url", endpoint_url]
    checked_json(args, profile, region, action="s3:DeleteObject", timeout=timeout)


def archive_key_for(tag: str) -> str:
    """This crew's archive key.

    One key per crew for its whole lifetime, so the ETag lineage is continuous
    across reopens. A body naming a different key than the record holds is a
    caller trying to redirect an established crew's archive mid-lifecycle, which
    :func:`assert_key_matches` refuses.
    """
    if not tag:
        raise ValueError("an archive key needs a crew tag")
    return f"crews/{tag}/home.tar.gz"


def assert_key_matches(ref: ArchiveRef, expected_tag: str) -> None:
    """Refuse a reference whose key is not this crew's.

    Silently accepting a different key would let one crew's pack land on another
    crew's archive, and the ETag condition would not catch it: a first write to an
    unused key succeeds under ``If-None-Match: *`` exactly as a legitimate first
    pack does.
    """
    expected = archive_key_for(expected_tag)
    if ref.key != expected:
        raise PackRefused(
            f"this crew's archive key is {expected!r} and the write names {ref.key!r}; "
            "refusing rather than redirecting an established archive"
        )


#: Epoch seconds an archive is kept after its crew stopped.
#:
#: Fourteen days. Long enough that a crew parked over a holiday comes back, short
#: enough that an abandoned crew's bytes do not accumulate: the archive is tens of
#: kilobytes, so the cost being bounded here is attention rather than storage.
ARCHIVE_RETENTION_SECONDS = 14 * 86_400


def expiry_due(stopped_at: float, *, now: Optional[float] = None) -> bool:
    """Whether a crew stopped at *stopped_at* is past its retention window."""
    import time

    moment = time.time() if now is None else now
    return stopped_at > 0 and moment - stopped_at >= ARCHIVE_RETENTION_SECONDS


def _is_precondition_failed(exc: Exception) -> bool:
    """Whether *exc* is S3 refusing a conditional write or read.

    Matched on the service's own error name and status in the CLI's stderr,
    because the seam hands back text rather than a response object. Both spellings
    are checked: the CLI renders the error name for a ``put-object`` and the bare
    status for some ``get-object`` paths.
    """
    text = str(exc)
    return "PreconditionFailed" in text or "412" in text


def summarise_archive(data: Optional[dict]) -> str:
    """One line about an archive, for a log or a dashboard caption."""
    if not data:
        return "no archive"
    size = data.get("ContentLength")
    etag = str(data.get("ETag", "") or "").strip('"')
    return json.dumps({"bytes": size, "etag": etag}, sort_keys=True)
