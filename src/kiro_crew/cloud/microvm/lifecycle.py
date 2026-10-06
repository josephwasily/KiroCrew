"""The ``CrewLifecycle`` port, and the MicroVM lane's implementation of it.

``LaunchEngine`` has five methods and not one of them is ``suspend``, ``resume``,
``pack`` or ``poll``. Growing it by four would oblige the EC2 and Fargate lanes to
answer questions they have no answer to: a Fargate task has no suspend, and an EC2
instance's stop is a different thing with a different cost. So the lifecycle is a
SECOND port, beside the first, which only this lane implements. A lane without one
is simply a lane with no lifecycle loop, which is exactly what the other two are
today.

Two decisions in here are worth reading before changing anything.

**The idle verdict is slot-based, not traffic-based.** The platform measures idle
as inbound traffic on the VM's own proxy endpoint, and a crew reached through an
SSM port-forward sends none of it -- so a crew mid-turn looks completely idle to
the platform and would be suspended out from under the turn. The platform's idle
policy is therefore disabled at launch, and the verdict here is computed from the
gateway's own chat slots: no slot running AND nothing active for fifteen minutes.

**Resume is not the resume call returning.** A resumed guest's SSM agent needs
roughly ten seconds to re-register, and a command sent inside that window comes
back undeliverable rather than queueing. So :meth:`MicroVmLifecycle.resume` waits
on the GUEST answering, and the one hard-won consequence is that SSM reporting a
node ``Online`` is not the guest answering either -- a crew that passed an online
wait and then failed its first real call was stamped a pack failure when it was
merely not ready.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

from kiro_crew.cloud.microvm import api, pack, states
from kiro_crew.cloud.microvm.payload import WallLeads, compute_wall_leads, soft_pack_due
from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore

logger = logging.getLogger(__name__)

#: How long a crew must have no running slot before the idle verdict suspends it.
#:
#: Fifteen minutes. Short enough that a crew left open over lunch is not billing
#: compute; long enough that a owner reading a long answer, or thinking between
#: turns, does not come back to a suspended crew. Resume is measured in tens of
#: milliseconds locally and a few seconds on the platform, so the cost of being
#: wrong in this direction is small.
IDLE_SUSPEND_SECONDS = 15 * 60


@dataclass(frozen=True)
class GuestState:
    """What the guest says about itself, read as ONE reduction rather than rebuilt.

    Readiness is the guest's own answer and not something this side re-derives
    from a health check plus a slot list. Two derivations of one predicate drift,
    and the drift shows up as a roster offering "Open crew" for a crew that cannot
    take a turn.
    """

    ready: bool
    #: Chat slots currently running a turn.
    running_slots: int
    #: Seconds since the guest last had a running slot, by the GUEST's clock.
    idle_for_seconds: float
    #: The guest's supervisor restart counter. Climbing inside one generation is a
    #: crash loop, which no single observation can see.
    restarts: int
    #: The generation this guest was launched with, echoed back from its payload.
    generation: int
    #: Whether the guest has armed its own wall watchdog.
    self_pack_armed: bool = False
    #: The ETag the guest's OWN wall pack produced, or ``""``.
    #:
    #: Reported because the guest packing itself is the designed path -- the
    #: control plane is the owner's laptop and a laptop sleeps -- and the result
    #: lands on the guest's disk. Without carrying it back, the record's ETag
    #: stays empty, the next reopen tells the guest there is nothing to restore,
    #: the crew boots with an empty home, and its first pack's ``If-None-Match:
    #: *`` is refused against the archive that was there all along.
    self_packed_etag: str = ""


class CrewLifecycle(Protocol):
    """Suspend, resume, pack and poll one crew. Implemented only by this lane.

    Deliberately NOT part of ``LaunchEngine``. Every method here needs a crew
    whose compute can be paused and whose home can be written somewhere durable,
    and a lane without both would have to raise from four methods to conform.
    """

    def poll(self, tag: str) -> Optional[GuestState]: ...

    def idle_verdict(self, tag: str, *, now: Optional[float] = None) -> bool: ...

    def suspend(self, tag: str) -> CrewRecord: ...

    def resume(self, tag: str) -> CrewRecord: ...

    def pack(self, tag: str) -> CrewRecord: ...


@dataclass(frozen=True)
class LifecycleDeps:
    """Everything :class:`MicroVmLifecycle` reaches outside itself.

    One dataclass rather than five constructor arguments, so the local harness
    swaps the whole set and a test cannot accidentally leave one real.
    """

    store: CrewStore
    #: Reads the guest's reduced state. Returns ``None`` when the guest cannot be
    #: reached at all, which is a different answer from a guest that answers
    #: "not ready".
    read_guest: Callable[[CrewRecord], Optional[GuestState]]
    #: Stops the crew's gateway child and confirms it EXITED. Not a shutdown
    #: request: an ack that the process will exit soon is not evidence it has, and
    #: a bare shutdown reads as a crash to the guest's supervisor, which would
    #: respawn a gateway into the home the pack is about to archive.
    stop_gateway: Callable[[CrewRecord], None]
    #: Asks the guest to write its own archive under the record's ETag, and
    #: returns the new ETag. The guest does the write because the home is on the
    #: guest's disk; the control plane only learns the result.
    pack_home: Callable[[CrewRecord], str]
    suspend_vm: Callable[[CrewRecord], None]
    resume_vm: Callable[[CrewRecord], None]
    terminate_vm: Callable[[CrewRecord], None]
    #: The VM's platform state, or ``None`` when the platform has forgotten it.
    #: Both answers mean "not resumable", which is why the caller does not have to
    #: tell them apart -- but they are kept distinct here because a sweep deciding
    #: what to delete does.
    read_vm_status: Callable[[CrewRecord], Optional[str]]
    #: Blocks until the VM reports terminated, so the record is not written
    #: ``STOPPED`` while the VM is still billing.
    wait_terminated: Callable[[CrewRecord], None]
    #: Current wall-clock seconds. Injected so the wall arithmetic is testable
    #: without sleeping for seven and a half hours.
    now: Callable[[], float] = time.time


class MicroVmLifecycle:
    """The lane's :class:`CrewLifecycle`.

    Every method reads the record, acts, and writes the record through
    :meth:`CrewStore.apply_event` -- so a state this object produced is a state
    :data:`states.EDGES` has a row for, and a bug in here raises
    :class:`states.IllegalTransition` instead of storing a crew somewhere the
    reopen path cannot read.
    """

    def __init__(self, deps: LifecycleDeps) -> None:
        self._deps = deps

    @property
    def store(self) -> CrewStore:
        """The record store this lifecycle writes through.

        Exposed because the tick drives this object and needs the same records it
        does; reading them from a second store would let the two disagree about a
        crew's state within one pass.
        """
        return self._deps.store

    # ── observation ──────────────────────────────────────────────────────────

    def poll(self, tag: str) -> Optional[GuestState]:
        """Read the guest once and fold the answer into the record.

        Records the observation time only when the guest ANSWERED. A failed poll
        that stamped ``last_observed_at`` would make a crew nobody can reach look
        freshly observed, and :func:`states.effective_state` would then repeat a
        stale ``RUNNING`` as fact.
        """
        record = self._deps.store.get(tag)
        if record is None:
            return None
        guest = self._deps.read_guest(record)
        if guest is None:
            return None
        moment = self._deps.now()
        # Annotated, because the fields folded in here are not all the same type:
        # two clocks and a counter, plus the archive ETag adopted below.
        changes: dict[str, Any] = {
            "last_observed_at": moment,
            "restarts_seen": guest.restarts,
        }
        if guest.running_slots > 0:
            changes["last_active_at"] = moment
        elif not record.last_active_at:
            # A crew that has never had a running slot still needs a start point
            # for the idle clock, or its first idle verdict is computed against
            # zero and fires immediately.
            changes["last_active_at"] = moment
        # ADOPT the guest's own pack. It happened on the guest's disk, so this is
        # the only channel by which the host learns the generation to condition
        # the next write on. Only forward: an empty answer never clears a recorded
        # ETag, because a guest too old to report one must not look like a crew
        # whose archive vanished.
        if guest.self_packed_etag and guest.self_packed_etag != record.archive_etag:
            changes["archive_etag"] = guest.self_packed_etag
            logger.info(
                "microvm crew %s packed itself at its wall; adopting etag %s into the record",
                tag,
                guest.self_packed_etag,
            )
        if guest.restarts > record.restarts_seen and guest.generation == record.generation:
            logger.warning(
                "microvm crew %s restarted %d times in generation %d: the guest's gateway "
                "is crash-looping, so a ready answer may not survive the next turn",
                tag,
                guest.restarts,
                guest.generation,
            )
        self._deps.store.put(record.evolve(**changes))
        return guest

    def ready(self, tag: str, guest: Optional[GuestState]) -> bool:
        """Whether the roster may offer "Open crew" for this crew right now.

        Four clauses, and the fourth is the one that is easy to drop: the
        observation must come from the CURRENT generation. Without it a reopen
        satisfies readiness with the previous VM's ``ready=True`` for as long as
        the old observation is fresh, and the owner's first few clicks go to a
        crew that is not there.
        """
        record = self._deps.store.get(tag)
        if record is None or guest is None:
            return False
        if record.effective_state(now=self._deps.now()) != states.RUNNING:
            return False
        if not (record.microvm_id and record.endpoint):
            return False
        return guest.ready and guest.generation == record.generation

    def idle_verdict(self, tag: str, *, now: Optional[float] = None) -> bool:
        """Whether this crew should be suspended for being idle.

        Computed from the gateway's own slots, NOT from the platform's idle
        measurement. The platform counts only inbound traffic on the VM's proxy
        endpoint, and an SSM port-forward produces none -- so a crew running a
        forty-minute turn is maximally idle by that measure. Suspending on it
        would kill the turn.
        """
        record = self._deps.store.get(tag)
        if record is None:
            return False
        # The clock comes from the DEPS, never from ``time.time`` behind this
        # object's back. A caller that injected a clock did so because the whole
        # verdict is about elapsed time, and a mix of the two reads the record's
        # observation age against one clock and its activity age against another.
        moment = self._deps.now() if now is None else now
        if record.effective_state(now=moment) != states.RUNNING:
            return False
        guest = self._deps.read_guest(record)
        if guest is None:
            # Unreachable is not idle. A crew that cannot be asked may be mid-turn
            # behind a network blip, and a suspend here would be a suspend into
            # unknown state.
            return False
        if guest.running_slots > 0:
            return False
        since_active = moment - (record.last_active_at or record.created_at)
        return (
            since_active >= IDLE_SUSPEND_SECONDS and guest.idle_for_seconds >= IDLE_SUSPEND_SECONDS
        )

    # ── movement ─────────────────────────────────────────────────────────────

    def suspend(self, tag: str) -> CrewRecord:
        """Suspend the VM. The home stays on its disk and is NOT archived.

        Cheap and reversible, and the wall clock keeps running against the crew
        the whole time -- which is why :meth:`wall_pack_due` still applies to a
        suspended crew and why :data:`states.RESUME_TARGET_GONE` exists.
        """
        record = self._require(tag, states.RUNNING)
        self._deps.suspend_vm(record)
        return self._deps.store.apply_event(tag, states.EVENT_SUSPENDED)

    def resume(self, tag: str) -> CrewRecord:
        """Resume the VM and wait for the GUEST, not for the call.

        A suspended VM whose platform state has become terminal is not resumable:
        the wall took it while it was parked. That is recorded as
        :data:`states.RESUME_TARGET_GONE` rather than as a resume failure, because
        nothing failed -- the crew was parked past a limit suspending does not
        pause, and the owner needs to be told that and not told to retry.
        """
        record = self._require(tag, states.SUSPENDED)
        try:
            self._deps.resume_vm(record)
        except Exception:
            # A resume that failed is only RESUME_TARGET_GONE when the VM really
            # is gone. Any other failure is re-raised, because recording "the wall
            # took it" for a transient API error would tell the owner their work
            # is unrecoverable when it is sitting on a disk that is still there.
            if record.microvm_id and self._vm_is_gone(record, api.TERMINAL_MICROVM_STATES):
                return self._deps.store.apply_event(tag, states.EVENT_RESUME_TARGET_GONE)
            raise
        if self._vm_is_gone(record, api.TERMINAL_MICROVM_STATES):
            return self._deps.store.apply_event(tag, states.EVENT_RESUME_TARGET_GONE)
        return self._deps.store.apply_event(
            tag, states.EVENT_RESUMED, last_observed_at=self._deps.now()
        )

    def pack(self, tag: str) -> CrewRecord:
        """Stop the crew's gateway, archive its home, then terminate the VM.

        **The order is the correctness argument and it does not have a cheaper
        form.** Each step is here because the previous one is not enough:

        1. stop the gateway and confirm it EXITED -- an archive of a home with a
           live writer in it is an archive of a moving target;
        2. write the archive under the record's ETag, and learn the new one;
        3. write the new ETag into the record BEFORE terminating, so a crash
           between the write and the terminate leaves a crew whose record can
           still authorise the next write;
        4. terminate the VM, and only now -- the platform sends no ``SIGTERM`` and
           the terminate call returns a second or two before the guest's own hook
           fires, so nothing inside the VM runs after this;
        5. wait for terminated, so the record is not ``STOPPED`` while the VM
           bills.

        A failed archive write ends at :data:`states.TERMINATED_UNARCHIVED` and
        the VM is still terminated, because leaving an eight-hour VM running to
        protect a home that could not be written costs the owner money for a
        rescue nobody is coming to make. A :class:`pack.PackConflict` is the one
        exception: it means another writer holds the archive, so the VM is left
        alone and the crew stays ``RUNNING``.
        """
        record = self._require(tag, states.RUNNING)
        guest = self._deps.read_guest(record)
        if guest is not None and guest.running_slots > 0:
            raise pack.GatewayAlive(
                f"crew {tag} has {guest.running_slots} running chat slot(s); packing now would "
                "archive a home that is still being written"
            )
        self._deps.stop_gateway(record)
        try:
            new_etag = self._deps.pack_home(record)
        except pack.PackConflict:
            # The VM is untouched and the home is intact. Re-raised so the caller
            # reports a defect rather than a stopped crew: two writers for one
            # archive should be impossible with a single gateway.
            raise
        except Exception as exc:
            logger.error("microvm crew %s could not be archived: %s", tag, exc)
            self._terminate_and_wait(record)
            return self._deps.store.apply_event(
                tag,
                states.EVENT_PACK_FAILED,
                last_pack_failed_at=self._deps.now(),
            )
        # The ETag BEFORE the terminate. A self-pack the ledger never learned
        # about leaves the crew archived and unrestorable, because the next write's
        # condition names an ETag nobody holds.
        record = self._deps.store.put(record.evolve(archive_etag=new_etag))
        self._terminate_and_wait(record)
        return self._deps.store.apply_event(
            tag,
            states.EVENT_PACK_OK,
            archive_etag=new_etag,
            stopped_at=self._deps.now(),
        )

    # ── the wall ─────────────────────────────────────────────────────────────

    def wall_leads(self, record: CrewRecord) -> WallLeads:
        return compute_wall_leads(record.wall_seconds or api.MAX_LIFETIME_SECONDS)

    def wall_pack_due(self, tag: str, *, now: Optional[float] = None) -> bool:
        """Whether the control plane's backstop should pack this crew now.

        A BACKSTOP. The guest arms the same edge from its run payload and normally
        reaches it first, and that is the design: the control plane here is the
        owner's laptop, so a timer that lives only on this side loses a crew every
        time the lid closes. This exists for a guest too old to have armed one, and
        for the case where the guest's own pack failed.
        """
        record = self._deps.store.get(tag)
        if record is None or record.state not in (states.RUNNING, states.SUSPENDED):
            return False
        if not record.created_at:
            return False
        moment = self._deps.now() if now is None else now
        return soft_pack_due(record.created_at, self.wall_leads(record), now=moment)

    # ── internals ────────────────────────────────────────────────────────────

    def _require(self, tag: str, expected: str) -> CrewRecord:
        record = self._deps.store.get(tag)
        if record is None:
            raise KeyError(f"no MicroVM crew record for {tag!r}")
        if record.state != expected:
            raise states.IllegalTransition(
                f"crew {tag} is {record.state!r}, and this operation needs {expected!r}"
            )
        return record

    def _terminate_and_wait(self, record: CrewRecord) -> None:
        self._deps.terminate_vm(record)
        self._deps.wait_terminated(record)

    def _vm_is_gone(self, record: CrewRecord, terminal: frozenset[str]) -> bool:
        """Whether the platform has taken this VM.

        ``None`` from the status probe counts as gone: the platform having
        forgotten the VM and the platform remembering a dead one are the same
        answer to "can this be resumed".
        """
        try:
            status = self._deps.read_vm_status(record)
        except Exception as exc:  # noqa: BLE001 - a failed probe is not evidence of a dead VM
            logger.warning("could not read MicroVM status for crew %s: %s", record.tag, exc)
            return False
        return status is None or status in terminal
