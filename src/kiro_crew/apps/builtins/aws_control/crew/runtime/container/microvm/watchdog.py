"""The guest's wall watchdog: pack this crew's home before the platform takes the VM.

A MicroVM's lifetime is a hard, non-adjustable eight hours, and the platform does
not warn before it ends. The clock covers suspended time as well as running time,
so a crew nobody touched for a day still reaches the end. What happens at the end
is that the VM and its disk go away together.

**Why the GUEST owns this and not the gateway.** The gateway's own backstop runs
on the owner's machine, and that machine sleeps. A wall timer is only as awake as
the process holding it, so the one process guaranteed to be running when the wall
arrives is the one inside the VM. The gateway's tick stays as a backstop for a
guest too old to have armed a timer; it is not the primary.

**Two edges, and they do different things.**

``soft_at``
    Pack at the next TURN BOUNDARY. A pack mid-turn archives a transcript the
    crew is still writing, so the soft edge waits for no running slot. Waiting is
    safe here because the hard edge is behind it.

``hard_at``
    Pack REGARDLESS. The soft edge can wait forever on a crew in a long turn, and
    a transcript archived mid-turn is strictly better than no transcript at all --
    the thing being compared against is losing the whole home.

The gap between them is the budget for the pack itself, and it is why the two are
separate numbers rather than one deadline with a fudge factor.

**The pack is the host's pack, conditionally.** The same archive layout, the same
denylist, and the same ETag compare-and-set: ``If-None-Match: *`` for a crew that
has never been packed, ``If-Match <etag>`` afterwards. A refused precondition is
NOT retried, because another writer holds the archive and a retry is the
last-write-wins this condition exists to prevent.

**Packing once is the invariant.** Every edge is checked against one state file,
so a watchdog that already packed does nothing on the next pass -- including
after a resume, which restarts this loop with the same payload.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Optional

#: The names left OUT of the archive, which must be the host pack's own list.
#: Copied rather than imported because this package ships inside the crew image
#: and ``kiro_crew.cloud`` is not in it; ``test_guest_denylist_matches_the_host``
#: fails if the two ever differ, so the copy cannot drift silently.
ARCHIVE_DENYLIST: tuple[str, ...] = (
    "run",
    ".local_secret",
    "dashboard.sock",
    "models",
    "snapshots",
    "outbox",
)

#: Seconds between passes. The edges are hours apart and each pass is a clock
#: read plus, near the soft edge, one loopback request -- so a slow loop costs
#: nothing and a fast one would only add wakeups to a VM that is billed for being
#: alive.
POLL_SECONDS = 30

#: How long one pack may take before it is treated as failed. Generous against a
#: measured archive of about 1.2 MB, because the cost of giving up early is the
#: home.
PACK_TIMEOUT_SECONDS = 600


class PackConflict(RuntimeError):
    """The conditional write was refused: another writer holds this archive.

    Separate from every other failure because the response differs. Others are
    worth another pass; this one is a report, since a retry would overwrite
    whatever the other writer put there.
    """


def wall_deadline(started_at: float, wall_seconds: int) -> float:
    """The absolute epoch second at which the platform terminates this VM."""
    return started_at + wall_seconds


class WallWatchdog:
    """Arms the two edges for one VM and packs at whichever arrives first.

    Every moving part is injected -- the clock, the sleep, the slot probe and the
    pack -- so the whole behaviour is testable against a fake clock without a VM,
    an S3 bucket or a crew. That is not a convenience: the thing being proven is
    what happens at hour seven of an eight-hour lifetime, and no real-time test
    can reach it.
    """

    def __init__(
        self,
        *,
        started_at: float,
        soft_at: int,
        hard_at: int,
        wall_seconds: int,
        pack: Callable[[str], dict[str, Any]],
        running_slots: Callable[[], int],
        state_path: str,
        seal: Optional[Callable[[], None]] = None,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 0 < soft_at < hard_at < wall_seconds:
            raise ValueError(
                f"wall edges are not ordered inside the VM's life: soft={soft_at} "
                f"hard={hard_at} wall={wall_seconds}. An unordered pair is a watchdog "
                "that either never fires or fires after the VM is gone."
            )
        self._started_at = started_at
        self._soft_at = soft_at
        self._hard_at = hard_at
        self._wall_seconds = wall_seconds
        self._pack = pack
        self._running_slots = running_slots
        self._seal = seal
        self._state_path = state_path
        self._now = now
        self._sleep = sleep
        self._packed = self._read_packed()

    # ── state ────────────────────────────────────────────────────────────────

    def _read_packed(self) -> bool:
        """Whether a previous pass already packed, read from disk.

        On DISK rather than in memory, because a resume restarts this loop with
        the same payload and an in-memory flag would be gone. Packing twice is
        not merely wasteful: the second pack's precondition names an ETag the
        first pack replaced, so it would be refused and reported as a conflict.
        """
        try:
            with open(self._state_path, encoding="utf-8") as handle:
                return bool(json.load(handle).get("packed"))
        except (OSError, ValueError):
            return False

    def _mark_packed(self, edge: str, result: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self._state_path) or ".", exist_ok=True)
        tmp = f"{self._state_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"packed": True, "edge": edge, "result": result}, handle, default=str)
        os.replace(tmp, self._state_path)
        self._packed = True

    # ── the edges ────────────────────────────────────────────────────────────

    @property
    def soft_deadline(self) -> float:
        return self._started_at + self._soft_at

    @property
    def hard_deadline(self) -> float:
        return self._started_at + self._hard_at

    @property
    def wall(self) -> float:
        return wall_deadline(self._started_at, self._wall_seconds)

    def packed(self) -> bool:
        return self._packed

    def run_once(self) -> str:
        """Evaluate both edges once. Returns what this pass decided.

        ``"already-packed"``, ``"waiting"``, ``"waiting-for-turn-boundary"``,
        ``"packed-soft"``, ``"packed-hard"`` or ``"pack-failed"`` -- one value per
        reachable state, so a test names the state rather than inferring it from
        a side effect.

        The HARD edge is checked first. Taking the soft branch past the hard
        deadline would let a crew in a long turn wait out the wall, which is the
        one outcome this class exists to make impossible.
        """
        if self._packed:
            return "already-packed"
        now = self._now()
        if now >= self.hard_deadline:
            return self._pack_now("hard")
        if now < self.soft_deadline:
            return "waiting"
        # Between the edges: pack at a turn boundary, wait otherwise. A probe that
        # cannot answer counts as BUSY, so a broken probe delays to the hard edge
        # rather than packing mid-turn on no information.
        try:
            busy = self._running_slots() > 0
        except Exception:  # noqa: BLE001 - an unreadable probe is not a boundary
            busy = True
        if busy:
            return "waiting-for-turn-boundary"
        return self._pack_now("soft")

    def _pack_now(self, edge: str) -> str:
        # SEAL FIRST, then snapshot. A turn boundary says no slot is RUNNING; it
        # does not say the backend has finished writing -- a transcript flush and
        # a SQLite checkpoint outlive the turn that caused them, and the hard edge
        # does not even wait for a boundary. Archiving a home with live writers
        # captures a torn database, which restores as a crew whose history ends
        # mid-sentence or will not open at all.
        #
        # This is the order the host lifecycle already uses: stop the gateway,
        # confirm it exited, then pack. Sealing before the write also means a
        # pack that FAILS leaves a stopped crew rather than a running one, which
        # is the safe direction: the VM is minutes from its wall either way, and a
        # crew still taking turns it cannot archive is the loss being prevented.
        self._seal_the_crew(edge)
        try:
            result = self._pack(edge)
        except PackConflict:
            # Another writer holds the archive. Marked packed anyway: this crew's
            # home is in someone's archive and a retry would overwrite theirs.
            self._mark_packed(edge, {"conflict": True})
            return "packed-" + edge
        except Exception:  # noqa: BLE001 - another pass may succeed
            return "pack-failed"
        self._mark_packed(edge, result)
        return "packed-" + edge

    def _seal_the_crew(self, edge: str) -> None:
        """Stop the crew accepting turns, before its home is archived.

        Two things at once. The archive becomes a snapshot of a QUIESCED home
        rather than one with live writers, so it restores as a crew whose history
        ends cleanly. And a crew that kept serving after the pack would write
        transcripts that are in no archive, which the VM's remaining minutes then
        take with the disk.

        Failure here is logged and survived rather than raised: a pack over a
        still-running crew is imperfect, and no pack at all is the loss this whole
        class exists to prevent.
        """
        if self._seal is None:
            return
        try:
            self._seal()
        except Exception:  # noqa: BLE001 - the pack already succeeded
            pass

    def run_forever(self) -> str:
        """Poll until this crew is packed or the wall passes.

        Returns the last decision. The loop ends at the WALL rather than running
        unbounded, so a watchdog whose pack keeps failing stops saying it is
        still working once there is nothing left to work on.
        """
        last = "waiting"
        while True:
            last = self.run_once()
            if self._packed:
                return last
            if self._now() >= self.wall:
                return last
            self._sleep(POLL_SECONDS)


def from_payload(
    payload: dict[str, Any],
    *,
    started_at: float,
    pack: Callable[[str], dict[str, Any]],
    running_slots: Callable[[], int],
    state_path: str,
    seal: Optional[Callable[[], None]] = None,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> Optional[WallWatchdog]:
    """A watchdog armed from the run payload's ``wall`` block, or ``None``.

    ``None`` for a payload that carries no wall block, which is the only case
    where the gateway's backstop is the whole answer. Every payload this lane
    writes carries one.
    """
    wall = payload.get("wall") or {}
    if not isinstance(wall, dict):
        return None
    # The wire names, which are the launcher's: ``secs``/``softAt``/``hardAt``.
    # Read by those names and nothing else -- a guess at a snake_case spelling
    # would silently return None and leave the VM with no watchdog at all, which
    # is the failure this class exists to prevent and the quietest way to cause
    # it. ``test_the_wall_block_the_launcher_writes_arms_a_watchdog`` builds the
    # block through the launcher's own encoder rather than by hand, so a rename
    # on either side fails there.
    try:
        soft_at = int(wall["softAt"])
        hard_at = int(wall["hardAt"])
        wall_seconds = int(wall["secs"])
    except (KeyError, TypeError, ValueError):
        return None
    return WallWatchdog(
        started_at=started_at,
        soft_at=soft_at,
        hard_at=hard_at,
        wall_seconds=wall_seconds,
        pack=pack,
        running_slots=running_slots,
        state_path=state_path,
        seal=seal,
        now=now,
        sleep=sleep,
    )
