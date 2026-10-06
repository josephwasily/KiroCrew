"""One record per MicroVM crew, in the gateway's own product-owned state store.

The reference implementation for this lane keeps the equivalent record in
DynamoDB with optimistic concurrency on an integer version, and the reason it
needs that is stated in its own source: two stateless control-plane functions
must not both pack one crew. This lane's control plane is the owner's single
local gateway, so there is exactly one writer and the version guard buys nothing.
What is left is a small JSON document beside ``cloud_launch_state.json``, written
with the same atomic-publish discipline the rest of ``cloud`` uses.

It is a FILE OF ITS OWN rather than a key inside ``cloud_launch_state.json``
because the two hold different shapes: that file is one record of three fields
whose reader refuses anything else, and this is a collection that grows by crew.
Both are product-owned state under ``config_dir()`` and neither is the
operator-owned ``cloud.json``, which this lane reads and never writes.

What the record must carry is decided by what a recovery needs, not by what is
convenient to store. ``archive_key`` and ``archive_etag`` together are the only
thing that makes a reopen safe, and an ETag the gateway failed to learn leaves a
crew archived and unrestorable -- so the pack sequence writes it back before it
reports success. ``activation_id`` is kept because a launch that failed after
minting an activation leaked one, and the sweeper reads records to tell a leak
from a live crew. ``wall_at`` is kept because the platform's maximum lifetime is
not adjustable and not extendable, so the only way to keep a crew's work is to
know when its VM will be taken.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Iterator, Optional

from kiro_crew.atomic_write import atomic_write
from kiro_crew.cloud.microvm import states
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

_FILENAME = "microvm_crews.json"

#: Ceiling on the whole file, checked BEFORE it is parsed, for the reason
#: ``cloud/config.py`` gives for its own: ``json.loads`` builds the document in
#: memory, so a bound applied to the parsed result runs after the damage. This
#: store is read on every lifecycle tick, so an oversized file is a repeated cost
#: and not a one-off.
_MAX_FILE_BYTES = 1 << 21

#: Ceiling on how many crews one install may track. Reached only by a leak: the
#: regional memory quota stops a real fleet long before this, and the cap exists
#: so a runaway writer cannot grow an unbounded document the tick then re-reads.
_MAX_RECORDS = 256

#: Ceiling on any one string read back from the file.
_MAX_STRING_LEN = 2048


@dataclass(frozen=True)
class CrewRecord:
    """Everything the control plane knows about one MicroVM crew.

    Frozen, so a caller holding a record cannot mutate the store's copy by
    accident; :meth:`evolve` returns a new one. Every field defaults, so a
    document written by an older build reads back with the new fields absent
    rather than refusing to load -- the store is the owner's own state and a
    forward-incompatible read would cost them a crew.
    """

    #: The launch tag. The record's identity, and the same tag ``run_launch``
    #: makes before preflight and passes to ``provision`` and ``teardown`` -- so a
    #: rollback can find this crew without knowing the MicroVM id.
    tag: str = ""
    state: str = states.PENDING
    #: The platform's id for the VM, empty until ``RunMicrovm`` answers.
    microvm_id: str = ""
    #: The SSM managed-node id the guest registered as, empty until it does.
    mi_id: str = ""
    #: The hybrid activation minted for this VM. Kept after the VM is gone,
    #: because an activation with no registrations is what the sweeper reaps.
    activation_id: str = ""
    #: The VM's own HTTPS endpoint, as the platform returned it.
    endpoint: str = ""
    #: An ARN or local reference to the per-crew control secret. NEVER the value:
    #: this file is read by the lifecycle tick on a schedule and a secret in it
    #: would be a secret in every backup of the owner's config directory.
    control_secret_ref: str = ""
    #: S3 bucket and key holding this crew's archive.
    archive_bucket: str = ""
    archive_key: str = ""
    #: The ETag the last successful pack returned. The next pack sends it as
    #: ``If-Match``; a reopen refuses an archive whose ETag disagrees.
    archive_etag: str = ""
    #: Epoch seconds. When the platform will terminate the VM whatever its state.
    wall_at: float = 0.0
    #: The lifetime this VM was launched with, in seconds, as asked for.
    wall_seconds: int = 0
    #: Epoch seconds of the last observation that reached the guest. Zero means
    #: never, which :func:`states.effective_state` reads as unknown.
    last_observed_at: float = 0.0
    #: Epoch seconds the crew last had a running chat slot. The idle verdict is
    #: computed from this and not from endpoint traffic.
    last_active_at: float = 0.0
    #: Epoch seconds the VM entered :data:`states.STOPPED`, which starts the
    #: retention clock.
    stopped_at: float = 0.0
    #: Epoch seconds of the last pack that failed, so a second failure reads as a
    #: pattern rather than as a first.
    last_pack_failed_at: float = 0.0
    #: The guest's own restart counter at the last observation. A climbing value
    #: inside one generation is a crash loop, which no single health check sees.
    restarts_seen: int = 0
    #: Incremented on every launch and reopen. A readiness observation from an
    #: earlier generation must not satisfy a later one, or the roster offers
    #: "Open crew" seconds after a reopen using the previous VM's answer.
    generation: int = 0
    profile: str = ""
    region: str = ""
    created_at: float = field(default_factory=time.time)

    def evolve(self, **changes: Any) -> "CrewRecord":
        """A copy with *changes* applied. The only way to change a record."""
        return replace(self, **changes)

    def effective_state(self, *, now: Optional[float] = None) -> str:
        """What to report for this crew, degrading a stale live state to unknown."""
        moment = time.time() if now is None else now
        age = None if not self.last_observed_at else moment - self.last_observed_at
        return states.effective_state(self.state, age_seconds=age)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: object) -> Optional["CrewRecord"]:
        """One record, or ``None`` for anything that is not one.

        Unknown keys are DROPPED rather than refused, so a document written by a
        newer build still loads on an older one. A known key of the wrong type
        drops the whole record: a ``wall_at`` that is a string would otherwise
        reach the wall arithmetic and raise there, on a schedule, with no owner
        watching.
        """
        if not isinstance(data, dict):
            return None
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in data:
                continue
            value = data[f.name]
            annotation = f.type if isinstance(f.type, str) else getattr(f.type, "__name__", "")
            if annotation == "str":
                if not isinstance(value, str) or len(value) > _MAX_STRING_LEN:
                    return None
            elif annotation == "int":
                # ``bool`` is an ``int`` subclass, so ``true`` would otherwise read
                # as a count of one.
                if isinstance(value, bool) or not isinstance(value, int):
                    return None
            elif annotation == "float":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return None
                value = float(value)
            else:
                return None
            kwargs[f.name] = value
        if not kwargs.get("tag"):
            return None
        if kwargs.get("state", states.PENDING) not in states.STORED_STATES:
            return None
        return cls(**kwargs)


def store_path() -> Path:
    """Where the records live. Under the data home, beside the launch record."""
    return config_dir() / _FILENAME


class CrewStoreUnreadable(RuntimeError):
    """A write was refused because the store could not be read first.

    Its own type rather than a bare ``RuntimeError`` so a caller can tell this
    apart from a failed write: nothing was written, and the file on disk is
    whatever it already was. The fix is to repair or move the file, not to retry.
    """


class CrewStore:
    """Read and publish the whole record set.

    Whole-document writes, not per-record ones. The set is small, one gateway
    writes it, and a partial write of a collection is the failure mode a reopen
    cannot survive -- so the file is replaced atomically or not at all.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or store_path()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, CrewRecord]:
        """Every record by tag. An unusable file reads as no records.

        Tolerant for the reason ``CloudConfig.load`` is: this runs on a timer and
        from every request that lists crews, so raising would turn one bad byte
        into a dashboard that shows nothing. A record that fails to parse is
        dropped and logged; the rest still load, because one unreadable crew must
        not hide the others.
        """
        try:
            with open(self._path, "rb") as fh:
                raw = fh.read(_MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            logger.warning("microvm crew store %s could not be read: %s", self._path, exc)
            return {}
        if len(raw) > _MAX_FILE_BYTES:
            logger.warning(
                "microvm crew store %s is larger than %d bytes", self._path, _MAX_FILE_BYTES
            )
            return {}
        try:
            document = json.loads(raw.decode("utf-8") or "null")
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            logger.warning("microvm crew store %s is not readable JSON: %s", self._path, exc)
            return {}
        if not isinstance(document, dict):
            return {}
        entries = document.get("crews")
        if not isinstance(entries, list) or len(entries) > _MAX_RECORDS:
            return {}
        out: dict[str, CrewRecord] = {}
        for entry in entries:
            record = CrewRecord.from_json(entry)
            if record is None:
                logger.warning("microvm crew store %s holds an unreadable record", self._path)
                continue
            out[record.tag] = record
        return out

    def get(self, tag: str) -> Optional[CrewRecord]:
        return self.load().get(tag)

    def publish(self, records: dict[str, CrewRecord]) -> None:
        """Replace the file with *records*, atomically.

        Refuses to write more than :data:`_MAX_RECORDS`, so a leak cannot grow a
        document the tick then re-reads on every pass.
        """
        if len(records) > _MAX_RECORDS:
            raise ValueError(
                f"refusing to store {len(records)} MicroVM crews: the cap is {_MAX_RECORDS}, "
                "and reaching it means crews are being created and not reaped"
            )
        payload = {
            "version": 1,
            "crews": [records[tag].to_json() for tag in sorted(records)],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self._path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def load_for_write(self) -> dict[str, CrewRecord]:
        """Every record, or a REFUSAL if the file could not be read.

        The strict twin of :meth:`load`, and the difference is which mistake each
        one is allowed to make. A READER that raises turns one bad byte into a
        dashboard showing nothing, so :meth:`load` is tolerant. A WRITER that
        reads tolerantly gets an empty dict from an unreadable file, adds its one
        record to it, and publishes that -- erasing every crew the file held, and
        with them the ETags their archives now require. The archives survive and
        nothing can restore them.

        Distinguished by RE-READING rather than by a flag, because the tolerant
        path cannot tell "no records" from "unreadable": both are ``{}``. Here the
        same failures are raised instead.
        """
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            # A store that has never existed genuinely holds no crews, which is
            # the one empty answer that is true.
            return {}
        except OSError as exc:
            raise CrewStoreUnreadable(
                f"the microvm crew store {self._path} could not be read ({exc}), so a write "
                "would publish a store with every existing crew missing. Refusing: those "
                "records hold the ETags their archives require."
            ) from exc
        if len(raw) > _MAX_FILE_BYTES:
            raise CrewStoreUnreadable(
                f"the microvm crew store {self._path} is larger than {_MAX_FILE_BYTES} bytes, "
                "so it was not parsed; a write now would drop every record in it"
            )
        try:
            document = json.loads(raw.decode("utf-8") or "null")
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise CrewStoreUnreadable(
                f"the microvm crew store {self._path} is not readable JSON ({exc}), so a write "
                "would replace it with one record and lose the rest"
            ) from exc
        if document is None:
            return {}
        entries = document.get("crews")
        if not isinstance(document, dict) or not isinstance(entries, list):
            raise CrewStoreUnreadable(
                f"the microvm crew store {self._path} is not the shape this lane writes, so a "
                "write would discard whatever it does hold"
            )
        # PER-RECORD too, not only per-file. The tolerant read DROPS an entry it
        # cannot parse and keeps the rest, which is right for a dashboard: one
        # unreadable crew must not hide the others. But a write publishes exactly
        # what loaded, so the dropped entry does not come back -- and what it held
        # was that crew's archive ETag, which is the only thing that can read its
        # archive. The bytes survive and nothing can restore them.
        unreadable = sum(1 for entry in entries if CrewRecord.from_json(entry) is None)
        if unreadable:
            raise CrewStoreUnreadable(
                f"{unreadable} record(s) in the microvm crew store {self._path} could not be "
                "parsed. A write would publish the file without them, and an archive whose "
                "ETag is gone cannot be restored. Repair or move the file first."
            )
        return self.load()

    def put(self, record: CrewRecord) -> CrewRecord:
        """Write one record, leaving every other alone. Returns what was stored."""
        records = self.load_for_write()
        records[record.tag] = record
        self.publish(records)
        return record

    def delete(self, tag: str) -> bool:
        """Remove one record. ``False`` when there was none."""
        records = self.load_for_write()
        if tag not in records:
            return False
        del records[tag]
        self.publish(records)
        return True

    def apply_event(self, tag: str, event: str, **changes: Any) -> CrewRecord:
        """Move one crew through :func:`states.transition` and store the result.

        The state and the facts that came with it are written in ONE publish.
        Writing the state first and the ETag second is the sequence that leaves a
        crew archived and unrestorable, because the window between the two is
        exactly where a gateway restart loses the ETag that the archive now
        requires.

        Raises :class:`states.IllegalTransition` for an event the table has no row
        for, which is a caller bug and must not be stored as a state.
        """
        records = self.load_for_write()
        current = records.get(tag)
        next_state = states.transition(current.state if current else None, event)
        if current is None:
            current = CrewRecord(tag=tag)
        records[tag] = current.evolve(state=next_state, **changes)
        self.publish(records)
        return records[tag]

    def iter_records(self) -> Iterator[CrewRecord]:
        """Every record, for a sweep that only reads."""
        yield from self.load().values()
