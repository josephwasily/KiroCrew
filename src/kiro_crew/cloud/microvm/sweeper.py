"""Two classes of orphan this lane can leak, and a sweep that is dry-run by default.

The reference implementation sweeps five classes. Three of them -- per-crew IAM
roles, per-crew Secrets Manager paths, and stale ``mi-`` managed nodes -- are
resources a multi-user roster creates and this lane does not. The two that remain
are the two that cost real money or real limits when missed:

**(a) An activation with zero registrations, past its expiry.** This is the
measured leak from the launch path: minting the activation succeeds, the
``RunMicrovm`` that follows fails, and the activation is left behind having
enrolled nothing. It bills nothing but counts against the account, and it is
invisible unless something looks for it.

**(b) A live MicroVM whose id is on no record.** The real race: ``RunMicrovm``
succeeds and the process that was going to write the id down dies. The VM runs for
up to eight hours, bills the whole time, and nothing in the product knows it
exists. This is the expensive one.

Three rules the sweep follows, each of them load-bearing:

**The record store is the oracle, and it is never ignored.** The first live dry
run of the reference sweeper planned to destroy its own running bench crew. So a
sweep that cannot READ the records refuses to plan anything, rather than planning
against an empty oracle -- an unreadable store looks exactly like "no crews exist"
and would make every VM an orphan.

**Never key liveness on a ping status.** A terminated node still reports a ping
status, so a sweep that trusts it deletes live things and keeps dead ones.

**A VM younger than :data:`MIN_ORPHAN_AGE_SECONDS` is never an orphan.** A launch
in flight has a VM and not yet a complete record, by design -- the id is written
the instant ``RunMicrovm`` answers, and the window between the two is real.

The sweep runs on its own schedule, separate from the lifecycle tick, because it
walks account-wide paginated lists and a bug in that walk must not be able to take
the tick down with it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from kiro_crew.cloud.microvm import api
from kiro_crew.cloud.microvm.record import CrewStore

logger = logging.getLogger(__name__)

#: How old an unrecorded MicroVM must be before the sweep calls it an orphan.
#:
#: Five minutes. A launch writes the VM's id into the record the moment
#: ``RunMicrovm`` answers, so the unrecorded window is seconds -- but an online
#: wait can hold the launch for minutes, and a sweep that fired inside it would
#: terminate the crew the owner is watching provision.
MIN_ORPHAN_AGE_SECONDS = 300


@dataclass(frozen=True)
class Orphan:
    """One thing the sweep would act on, and why.

    Carries the reason as text because the dry-run report is the whole product
    here: a plan an operator cannot read is a plan they cannot refuse.
    """

    kind: str
    identifier: str
    reason: str


@dataclass
class SweepPlan:
    """What a sweep found, and whether it is safe to act on.

    ``confirmed`` is ``False`` for a plan built against an oracle the sweep could
    not fully read. An unconfirmed plan is reported and never executed, which is
    the discipline the Fargate lane already applies to an ambiguous teardown:
    refuse, name what was ambiguous, and leave the resource alone.
    """

    orphans: tuple[Orphan, ...] = ()
    confirmed: bool = True
    warning: str = ""

    def describe(self) -> str:
        if not self.orphans:
            return "no MicroVM orphans found"
        lines = [f"{o.kind} {o.identifier}: {o.reason}" for o in self.orphans]
        head = f"{len(self.orphans)} MicroVM orphan(s)"
        if not self.confirmed:
            head += " (NOT confirmed, so nothing will be deleted)"
        return head + "\n" + "\n".join(lines)


@dataclass
class SweepDeps:
    """Everything the sweep reaches outside itself, so a test replaces all of it."""

    store: CrewStore
    list_microvms: Callable[[], list[api.MicroVm]]
    terminate_microvm: Callable[[str], None]
    #: One activation's record, or ``None`` when it is gone. The sweep reads
    #: ``RegistrationsCount`` and ``ExpirationDate`` from it.
    describe_activation: Callable[[str], Optional[dict]]
    delete_activation: Callable[[str], None]
    now: Callable[[], float] = time.time


@dataclass
class MicroVmSweeper:
    """Plan, and optionally execute, the two orphan classes.

    ``dry_run`` defaults to ``True`` and the cron entry that drives this keeps it
    that way. Turning it off is a deliberate act with a reviewable config change
    behind it, because the first live run of the equivalent sweeper elsewhere
    planned to destroy a running crew.
    """

    deps: SweepDeps
    dry_run: bool = True
    _acted: list[Orphan] = field(default_factory=list)

    def plan(self) -> SweepPlan:
        """What this sweep would act on. Makes no change of any kind."""
        try:
            records = self.deps.store.load()
        except Exception as exc:  # noqa: BLE001 - see docstring: an unreadable oracle refuses
            return SweepPlan(
                confirmed=False,
                warning=(
                    "the MicroVM crew record store could not be read, so no sweep was "
                    f"planned: an unreadable store is indistinguishable from an empty one, "
                    f"and against an empty one every running crew is an orphan ({exc})"
                ),
            )
        known_vms = {r.microvm_id for r in records.values() if r.microvm_id}
        known_activations = {r.activation_id for r in records.values() if r.activation_id}
        orphans: list[Orphan] = []
        warnings: list[str] = []
        confirmed = True

        # (b) live MicroVMs on no record. The expensive class, so it goes first:
        # a sweep that fails halfway should already have reported this one.
        try:
            live = self.deps.list_microvms()
        except Exception as exc:  # noqa: BLE001 - a partial list cannot support a deletion
            confirmed = False
            warnings.append(
                f"the MicroVM list could not be read in full, so no VM was judged ({exc})"
            )
            live = []
        moment = self.deps.now()
        for vm in live:
            if vm.state in api.TERMINAL_MICROVM_STATES:
                continue
            if vm.microvm_id in known_vms:
                continue
            age = _age_seconds(vm.started_at, moment)
            if age is None:
                confirmed = False
                warnings.append(
                    f"MicroVM {vm.microvm_id} reports a start time this sweep could not read "
                    f"({vm.started_at!r}), so its age is unknown and it was left alone"
                )
                continue
            if age < MIN_ORPHAN_AGE_SECONDS:
                continue
            orphans.append(
                Orphan(
                    kind="microvm",
                    identifier=vm.microvm_id,
                    reason=(
                        f"running in state {vm.state} for {int(age)}s and no crew record names "
                        "it, so nothing in the product can reach it and it bills until the "
                        "platform's maximum lifetime"
                    ),
                )
            )

        # (a) expired activations with no registrations.
        for activation_id in sorted(known_activations):
            try:
                detail = self.deps.describe_activation(activation_id)
            except Exception as exc:  # noqa: BLE001
                confirmed = False
                warnings.append(f"activation {activation_id} could not be read ({exc})")
                continue
            if detail is None:
                continue
            registrations = detail.get("RegistrationsCount")
            if not isinstance(registrations, int) or registrations != 0:
                continue
            if not _activation_expired(detail, moment):
                continue
            orphans.append(
                Orphan(
                    kind="activation",
                    identifier=activation_id,
                    reason=(
                        "past its expiry with zero registrations, so the launch that minted it "
                        "never enrolled a node and nothing will use it"
                    ),
                )
            )
        return SweepPlan(orphans=tuple(orphans), confirmed=confirmed, warning=" ".join(warnings))

    def run(self) -> SweepPlan:
        """Plan, then act only when this sweeper is not a dry run AND the plan is confirmed.

        Both conditions, because they guard different things: ``dry_run`` is the
        operator's choice, and ``confirmed`` is the sweep's own statement that it
        could see enough to be sure. An unconfirmed plan is never executed however
        the flag is set.
        """
        plan = self.plan()
        if self.dry_run or not plan.confirmed:
            logger.info("microvm sweep (no action taken): %s", plan.describe())
            return plan
        for orphan in plan.orphans:
            try:
                if orphan.kind == "microvm":
                    self.deps.terminate_microvm(orphan.identifier)
                elif orphan.kind == "activation":
                    self.deps.delete_activation(orphan.identifier)
                else:
                    continue
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the rest
                logger.error("microvm sweep could not remove %s: %s", orphan.identifier, exc)
                continue
            self._acted.append(orphan)
            logger.warning("microvm sweep removed %s %s", orphan.kind, orphan.identifier)
        return plan

    @property
    def acted(self) -> tuple[Orphan, ...]:
        """What this sweeper actually removed, for a report."""
        return tuple(self._acted)


def _age_seconds(started_at: str, now: float) -> Optional[float]:
    """Seconds since *started_at*, or ``None`` when it cannot be read.

    ``None`` rather than zero. Zero would make an unreadable timestamp mean
    "brand new", which is the safe direction by accident; returning ``None`` makes
    the caller say so in the plan and mark it unconfirmed, which is the safe
    direction on purpose.
    """
    if not started_at:
        return None
    from datetime import datetime

    text = started_at.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return now - parsed.timestamp()


def _activation_expired(detail: dict, now: float) -> bool:
    """Whether this activation's expiry has passed.

    An unreadable or absent expiry reads as NOT expired, so the sweep leaves it
    alone. The cost of keeping one dead activation is nothing; the cost of
    deleting a live one is a launch that cannot enroll its node.
    """
    raw = detail.get("ExpirationDate")
    if isinstance(raw, (int, float)):
        return now > float(raw)
    if not isinstance(raw, str) or not raw:
        return False
    age = _age_seconds(raw, now)
    return age is not None and age > 0
