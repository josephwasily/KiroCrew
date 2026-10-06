"""The run-hook payload, and the wall-clock arithmetic the guest arms itself from.

The payload is the ONLY channel into a MicroVM at boot. The platform delivers it
once, to a hook whose own budget is sixty seconds, and nothing else reaches the
guest until it has registered itself as a managed node. So what goes in it is
decided by what the guest cannot discover for itself:

- its SSM activation, because an agent registered at image-build time would bake
  one identity and one private key into the snapshot and share them with every VM
  launched from that image;
- a reference to its control secret, never the value;
- its archive coordinates and the ETag it must send as ``If-Match``;
- the two wall-clock edges, so the guest packs ITSELF.

That last one is the design decision worth stating plainly. The control plane on
this lane is the owner's laptop, and a laptop sleeps. A wall timer that lives only
in the control plane means a crew whose owner closed the lid at 23:00 is
terminated by the platform at its eight-hour edge with its work still on a disk
that goes with it. Armed in the guest, a control-plane outage delays the LEDGER
learning about a pack; it cannot lose the pack.

Nothing in this module may put a secret VALUE in the payload. The payload is an
argument to ``RunMicrovm``, and AWS does not document whether ``runHookPayload``
is marked sensitive -- so it may sit in that account's CloudTrail request history.
A reference costs the guest one extra call and costs the owner nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from kiro_crew.cloud.microvm.api import MAX_LIFETIME_SECONDS, MAX_RUN_HOOK_PAYLOAD_BYTES

#: Seconds into a full-length VM's life at which the guest starts packing itself.
#: Seven hours thirty against an eight-hour wall: the measured pack of a crew home
#: without the embedding model is well under a second, and of one with it about
#: eight seconds, so thirty minutes is not a tight budget -- it is room for a pack
#: that has to wait for a turn to finish first.
WALL_SOFT_SECONDS = 7 * 3600 + 30 * 60

#: Seconds at which the guest stops waiting for anything and packs regardless.
#: Ten minutes after the soft edge and twenty before the wall.
WALL_HARD_SECONDS = 7 * 3600 + 40 * 60

#: Largest share of a VM's own lifetime the leads may consume. A 900-second test
#: crew must not inherit the eight-hour crew's 1800/1200-second leads, which sit
#: past its entire life -- it would pack at a moment that never arrives.
_MAX_LEAD_FRACTION = 0.40


@dataclass(frozen=True)
class WallLeads:
    """When a VM of *wall_seconds* should pack itself, in seconds from start.

    Both edges are offsets from the VM's own start rather than absolute times,
    because the guest arms a timer and the control plane compares against a
    deadline, and the two must not be able to disagree about which clock they
    meant.
    """

    wall_seconds: int
    soft_at: int
    hard_at: int

    def __post_init__(self) -> None:
        if not 0 < self.soft_at < self.hard_at < self.wall_seconds:
            raise ValueError(
                f"wall leads are not ordered inside the VM's life: soft={self.soft_at} "
                f"hard={self.hard_at} wall={self.wall_seconds}"
            )


def compute_wall_leads(wall_seconds: int) -> WallLeads:
    """The soft and hard pack edges for a VM whose lifetime is *wall_seconds*.

    For a full-length VM these are :data:`WALL_SOFT_SECONDS` and
    :data:`WALL_HARD_SECONDS` unchanged. For a shorter one the LEADS -- the gaps
    from each edge to the wall -- are clamped to :data:`_MAX_LEAD_FRACTION` of the
    lifetime, so both edges stay inside the VM's own life with the hard edge after
    the soft one.

    Pure arithmetic with no clock in it, so a test can ask for any lifetime and a
    reviewer can check the numbers by hand.
    """
    if not 1 <= wall_seconds <= MAX_LIFETIME_SECONDS:
        raise ValueError(
            f"wall_seconds must be between 1 and {MAX_LIFETIME_SECONDS}; got {wall_seconds}"
        )
    soft_lead = MAX_LIFETIME_SECONDS - WALL_SOFT_SECONDS
    hard_lead = MAX_LIFETIME_SECONDS - WALL_HARD_SECONDS
    cap = int(wall_seconds * _MAX_LEAD_FRACTION)
    soft_lead = min(soft_lead, cap)
    hard_lead = min(hard_lead, cap)
    # The hard edge must come strictly after the soft one, and both strictly
    # before the wall. Clamping can collapse them on a very short lifetime, so the
    # order is restored here rather than left to __post_init__ to reject.
    if hard_lead >= soft_lead:
        hard_lead = max(1, soft_lead // 2)
    soft_at = wall_seconds - soft_lead
    hard_at = wall_seconds - hard_lead
    if soft_at < 1:
        soft_at = 1
    if hard_at <= soft_at:
        hard_at = soft_at + 1
    if hard_at >= wall_seconds:
        # A lifetime too short to hold two ordered edges and a wall. Compress to
        # the only ordering that exists rather than refusing: a three-second crew
        # is a test crew, and a test must be able to make one.
        soft_at = max(1, wall_seconds - 2)
        hard_at = soft_at + 1
    return WallLeads(wall_seconds=wall_seconds, soft_at=soft_at, hard_at=hard_at)


@dataclass(frozen=True)
class RunHookPayload:
    """What the platform hands the guest once, at boot.

    ``control_secret_ref`` is a REFERENCE. There is no field on this class for a
    secret value, so a caller cannot put one in by passing the wrong argument.
    """

    tag: str
    activation_id: str
    activation_code: str
    region: str
    control_secret_ref: str
    #: The crew's MODEL credential, by reference. Carried explicitly rather than
    #: derived from ``control_secret_ref``: the launch tag is minted by the
    #: launcher rather than chosen by the operator, so a name built from it names
    #: a secret nobody could have created, and the guest's read of it ends boot at
    #: its secrets stage. A reference, never a value, for the same reason as the
    #: control secret.
    identity_secret_ref: str
    archive_bucket: str
    archive_key: str
    #: The ETag the guest must send as ``If-Match`` when it packs itself. Empty
    #: for a crew that has never been packed, which the guest turns into
    #: ``If-None-Match: *`` -- the first write of an object's life.
    archive_etag: str
    #: Whether this launch has an archive to restore FROM. Stated by the launcher
    #: rather than discovered by the guest, because the guest cannot discover it:
    #: the guest role holds ``s3:GetObject`` on this crew's prefix and
    #: deliberately not ``s3:ListBucket``, and S3 answers a missing key without
    #: ``ListBucket`` as ``AccessDenied`` rather than ``NoSuchKey``. A guest that
    #: reads "no archive yet" out of the error code therefore cannot tell a fresh
    #: crew from a broken grant, and treating ``AccessDenied`` as "fresh" would
    #: turn a real permission fault into a crew that silently boots empty.
    #:
    #: So the launcher says it. It is the only party that knows: the ledger holds
    #: this crew's archive ETag, and whether that is set IS whether a pack has
    #: ever completed. When this is ``False`` the guest skips the download
    #: entirely -- one fewer call on the common path -- and when it is ``True``
    #: every failure to fetch stays fatal, because an archive the ledger believes
    #: in must not be stepped over.
    archive_restore: bool
    kms_key_id: str
    wall: WallLeads
    #: Bumped on every launch and reopen, and echoed back by the guest, so a
    #: readiness answer from the previous VM cannot satisfy this one.
    generation: int

    def to_dict(self) -> dict[str, Any]:
        """The payload as the guest reads it.

        Short keys, because the budget is 4,096 bytes and the activation code and
        the two ARNs already take most of it. Measured in
        ``test_cloud_microvm_payload.py`` against that bound with worst-case
        values rather than asserted here.
        """
        return {
            "v": 1,
            "tag": self.tag,
            "gen": self.generation,
            "region": self.region,
            "ssm": {"id": self.activation_id, "code": self.activation_code},
            "secretRef": self.control_secret_ref,
            "identityRef": self.identity_secret_ref,
            "archive": {
                "bucket": self.archive_bucket,
                "key": self.archive_key,
                "etag": self.archive_etag,
                "kms": self.kms_key_id,
                # Explicit, and a bool rather than an absence: a guest reading a
                # missing key as False would read a launcher that forgot the
                # field the same way as one that said "fresh crew".
                "restore": self.archive_restore,
            },
            "wall": {
                "secs": self.wall.wall_seconds,
                "softAt": self.wall.soft_at,
                "hardAt": self.wall.hard_at,
            },
            # The guest must not spend ten minutes and 581 MB fetching an
            # embedding model it will not use before it can answer. Carried in the
            # payload rather than baked into the image so the decision is visible
            # at the launch that makes it.
            "env": {"KIROCREW_SKIP_MODEL_DOWNLOAD": "1", "KIROCREW_ALLOW_UNSANDBOXED": "1"},
        }

    def encode(self) -> str:
        """The payload as one compact JSON string, refused if it is over budget.

        Separators without spaces, because the only reason this is compact is the
        4,096-byte ceiling and pretty-printing one of these costs roughly a fifth
        of it.
        """
        text = json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)
        size = len(text.encode("utf-8"))
        if size > MAX_RUN_HOOK_PAYLOAD_BYTES:
            raise ValueError(
                f"run hook payload is {size} bytes, over the {MAX_RUN_HOOK_PAYLOAD_BYTES}-byte "
                "limit; shorten the archive key or the secret reference"
            )
        return text


def decode_payload(text: "str | bytes") -> dict[str, Any]:
    """Read a payload back, as the guest does.

    Here rather than in the guest's own source so the two shapes are pinned by one
    test: a payload the launcher can write and the guest cannot read is a VM that
    boots, registers nothing and bills for eight hours. Which is exactly what
    happened live, three launches in a row, before this function knew
    the shape the guest is actually handed.

    **The platform does not deliver what the launcher wrote.** ``RunMicrovm``
    takes ``runHookPayload`` as a JSON string, and the run hook receives that
    string BASE64-ENCODED. Measured: a 434-byte payload arrived at the hook as a
    576-byte body, and 576 is the base64 length of 432 bytes. Nothing in the API
    reference says so, and a guest that calls ``json.loads`` on the body gets a
    decode error with no hint of why -- which reads exactly like a launcher bug.

    So three forms are accepted, in order, and the one that worked is recorded by
    :func:`payload_encoding` for a caller that wants to log it:

    1. raw JSON, which is what the launcher wrote and what a local harness
       delivers;
    2. base64 of that JSON, which is what the real platform delivers;
    3. a JSON envelope with the payload under ``payload`` or ``runHookPayload``,
       in either of the two forms above -- accepted defensively rather than
       because it was observed, since a platform that wraps once may wrap again.

    Being liberal here is the right direction: every form is checked for
    ``v == 1`` before it is believed, so a body this function cannot read is
    refused rather than guessed at.
    """
    data, _ = _decode_with_encoding(text)
    return data


def payload_encoding(text: "str | bytes") -> str:
    """Which of the accepted forms *text* is: ``json``, ``base64``, ``envelope``.

    Exists so a guest can LOG the form it received without logging the payload
    itself -- the payload carries a single-use activation code, so its content
    must not reach a log, and "which encoding arrived" is the one fact about it
    that is both safe and diagnostic.
    """
    return _decode_with_encoding(text)[1]


def _decode_with_encoding(text: "str | bytes") -> tuple[dict[str, Any], str]:
    raw = text.decode("utf-8", "replace") if isinstance(text, bytes) else text
    candidate = raw.strip()

    def _as_v1(value: object) -> Optional[dict[str, Any]]:
        return value if isinstance(value, dict) and value.get("v") == 1 else None

    direct: object = None
    try:
        direct = json.loads(candidate)
    except (ValueError, TypeError):
        direct = None
    found = _as_v1(direct)
    if found is not None:
        return found, "json"

    import base64
    import binascii

    try:
        # ``validate=True``: without it, base64 silently discards any character
        # outside the alphabet, so a JSON document that failed to parse above
        # would be "decoded" into bytes that are not it.
        decoded = base64.b64decode(candidate, validate=True).decode("utf-8")
        found = _as_v1(json.loads(decoded))
        if found is not None:
            return found, "base64"
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError):
        pass

    if isinstance(direct, dict):
        for key in ("payload", "runHookPayload"):
            inner = direct.get(key)
            if isinstance(inner, (str, bytes)):
                try:
                    nested, _ = _decode_with_encoding(inner)
                except ValueError:
                    continue
                return nested, "envelope"

    raise ValueError(
        "not a version-1 MicroVM run hook payload: the body is neither JSON, nor "
        "base64 of JSON, nor an envelope carrying either. The platform delivers "
        "the launcher's string base64-encoded, so a raw-JSON-only reader fails here "
        "on every real launch"
    )


def wall_deadline(started_at_epoch: float, wall: WallLeads) -> float:
    """Absolute epoch second at which the platform will terminate this VM."""
    return started_at_epoch + wall.wall_seconds


def soft_pack_due(started_at_epoch: float, wall: WallLeads, *, now: Optional[float] = None) -> bool:
    """Whether the control plane's own backstop should pack this crew now.

    The guest arms the same edge and normally gets there first. This exists for a
    guest too old to have armed one, and it reads the SOFT edge rather than the
    hard one: the backstop that waits for the hard edge has twenty minutes to do a
    pack the platform may interrupt.
    """
    import time

    moment = time.time() if now is None else now
    return moment - started_at_epoch >= wall.soft_at
