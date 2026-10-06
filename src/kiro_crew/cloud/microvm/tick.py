"""The lifecycle tick and the sweep: one pass each, driven by the gateway's cron.

Two entries rather than one, and they are deliberately not the same job.

The TICK is cheap and reads only this install's own crews: poll each one, suspend
the idle ones, pack the ones approaching their wall. It runs every minute.

The SWEEP walks account-wide paginated lists looking for things no record names.
It runs every fifteen minutes, dry-run by default, and it is separate because a
bug in that walk must not be able to take the tick down with it -- the tick is
what keeps a crew's work safe, and the sweep is what keeps the bill honest.

The tick emits a positive signal on success, :data:`TICK_OK`. The reason is worth
stating: an alarm that watches for a FAILURE cannot tell a healthy quiet system
from a scheduler that stopped running. An alarm that watches for ``TICK_OK`` going
missing has something to miss.

This is a backstop and not the primary timer. Each guest arms its own wall
watchdog from its run payload, because the control plane here is the owner's own
machine and a machine that sleeps cannot be the only thing standing between a
crew and the platform's eight-hour edge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from kiro_crew.cloud.microvm import states
from kiro_crew.cloud.microvm.lifecycle import MicroVmLifecycle
from kiro_crew.cloud.microvm.pack import PackConflict, expiry_due
from kiro_crew.cloud.microvm.sweeper import MicroVmSweeper, SweepPlan

logger = logging.getLogger(__name__)

#: The positive health signal one completed tick emits. Watch for its ABSENCE.
TICK_OK = "microvm.tick.ok"

#: How often each entry should run, as cron expressions, with the reason.
#:
#: Published as data so the guide and the installer cannot disagree with the
#: code about them, and so a reader can see that the two schedules differ on
#: purpose rather than by accident.
CRON_SPECS: tuple[tuple[str, str, str], ...] = (
    (
        "microvm-lifecycle-tick",
        "* * * * *",
        "poll each MicroVM crew, suspend the idle ones, pack the ones near their wall",
    ),
    (
        "microvm-sweep",
        "*/15 * * * *",
        "report MicroVMs and SSM activations that no crew record names (dry run)",
    ),
)


@dataclass
class TickReport:
    """What one tick did. Counts rather than prose, so a log line stays readable."""

    polled: int = 0
    suspended: int = 0
    packed: int = 0
    expired: int = 0
    conflicts: int = 0
    failures: int = 0

    def describe(self) -> str:
        return (
            f"{TICK_OK} polled={self.polled} suspended={self.suspended} packed={self.packed} "
            f"expired={self.expired} conflicts={self.conflicts} failures={self.failures}"
        )


def run_tick(lifecycle: MicroVmLifecycle, *, now: Optional[float] = None) -> TickReport:
    """One pass over every crew. Never raises for one crew's failure.

    A tick that aborted on the first bad crew would stop the pack that was about
    to save the second crew's work, and the first crew's failure is already
    recorded in its own state. So each crew is handled inside its own try and the
    report carries the count.

    Order inside one crew matters: the wall check comes BEFORE the idle check. An
    idle crew near its wall must be packed, not suspended -- suspending does not
    stop the wall clock, so a suspend here parks the crew until the platform
    terminates it with its home still on the disk.
    """
    report = TickReport()
    store = lifecycle.store
    for record in list(store.load().values()):
        tag = record.tag
        try:
            if record.state == states.STOPPED:
                if expiry_due(record.stopped_at, now=now):
                    store.apply_event(tag, states.EVENT_EXPIRED)
                    report.expired += 1
                continue
            if record.state not in (states.RUNNING, states.SUSPENDED):
                continue
            if lifecycle.wall_pack_due(tag, now=now):
                if record.state == states.SUSPENDED:
                    # Resume before packing: the archive is a tar of the home and a
                    # suspended VM cannot run one. This is why the state table has
                    # no pack edge out of SUSPENDED.
                    lifecycle.resume(tag)
                lifecycle.pack(tag)
                report.packed += 1
                continue
            if record.state != states.RUNNING:
                continue
            if lifecycle.poll(tag) is not None:
                report.polled += 1
            if lifecycle.idle_verdict(tag, now=now):
                lifecycle.suspend(tag)
                report.suspended += 1
        except PackConflict as exc:
            # Two writers for one archive. With a single local gateway this should
            # be impossible, so it is a defect report and not a retry: the crew is
            # left running with its home intact.
            report.conflicts += 1
            logger.error(
                "microvm crew %s: the archive is held by another writer (etag %s). The crew was "
                "left running and nothing was overwritten. This should not happen with one "
                "gateway -- please report it.",
                tag,
                exc.held_etag or "unknown",
            )
        except Exception as exc:  # noqa: BLE001 - one crew must not stop the others
            report.failures += 1
            logger.error("microvm crew %s failed its lifecycle tick: %s", tag, exc)
    logger.info("%s", report.describe())
    return report


def run_sweep(sweeper: MicroVmSweeper) -> SweepPlan:
    """One sweep pass. Returns the plan whether or not it acted.

    Separate entry point from :func:`run_tick`, on its own schedule, for the
    reason in this module's own docstring.
    """
    return sweeper.run()


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m kiro_crew.cloud.microvm.tick {tick|sweep}``.

    The entry point the lane's installed cron jobs invoke, and the reason
    :data:`CRON_SPECS` can be turned into real jobs at all: a schedule needs a
    command, and a command that only exists in a guide is a schedule that does
    nothing.

    Refuses rather than defaulting to a pass when the verb is missing or unknown.
    The two passes differ in what they may DO -- the tick suspends and packs, the
    sweep only reports -- so guessing which one was meant is guessing whether this
    invocation is allowed to act.

    An unconfigured lane is a quiet 0, not an error: a gateway whose `microvm`
    block was deleted still has the jobs installed until the next reconcile, and
    every run of them shouting about a config that is gone is noise rather than
    information.
    """
    import argparse

    # This is a `python -m` entry point, so nothing has configured logging and the
    # root logger sits at WARNING. Both passes report at INFO -- and for the
    # sweep that report is its ONLY output, since a dry run acts on nothing. A
    # scheduled job whose findings are dropped is a job that cannot be read.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(prog="kiro-crew-microvm-tick", description=__doc__)
    parser.add_argument("pass_name", choices=("tick", "sweep"), help="which pass to run")
    args = parser.parse_args(argv)

    from kiro_crew.cloud.config import CloudConfig

    config = CloudConfig.load().microvm_config()
    if config is None:
        logger.info("no usable microvm block in cloud.json; nothing to do")
        return 0

    if args.pass_name == "sweep":
        logger.info("%s", run_sweep(_sweeper_for(config)).describe())
        return 0

    # The three guest-side operations this pass needs -- read the guest's reduced
    # state, stop its gateway child and confirm it exited, and ask it to write its
    # own archive -- are implemented in ``wiring.py`` over SSM commands into the
    # guest. ``run_tick`` handles each crew inside its own try, so one unreachable
    # crew costs its own line in the report rather than the whole pass.
    from kiro_crew.cloud.microvm.wiring import production_lifecycle

    logger.info("%s", run_tick(production_lifecycle(config)).describe())
    return 0


def _sweeper_for(config: Any) -> MicroVmSweeper:
    """The sweeper, DRY RUN, which is the only form a schedule may run.

    A scheduled sweeper that deleted would be an unattended process removing
    cloud resources on a judgement nobody read. The plan is reported; acting on
    it is a human command.
    """
    from kiro_crew.cloud.config import CloudConfig
    from kiro_crew.cloud.microvm import api
    from kiro_crew.cloud.microvm.record import CrewStore
    from kiro_crew.cloud.microvm.sweeper import SweepDeps

    cloud = CloudConfig.load()
    profile, region = cloud.profile, cloud.region
    spec = config.launch_spec()
    return MicroVmSweeper(
        SweepDeps(
            store=CrewStore(),
            list_microvms=lambda: api.list_microvms(
                profile=profile, region=region, endpoint_url=spec.endpoint_url
            ),
            terminate_microvm=lambda vm_id: api.terminate_microvm(
                vm_id, profile=profile, region=region, endpoint_url=spec.endpoint_url
            ),
            describe_activation=lambda act_id: api.describe_activation_registrations(
                act_id, profile=profile, region=region, endpoint_url=spec.endpoint_url
            ),
            delete_activation=lambda act_id: None,
        ),
        dry_run=True,
    )


if __name__ == "__main__":  # pragma: no cover - exercised through `python -m`
    raise SystemExit(main())
