"""The lifecycle tick and the sweep: one crew's failure never stops the next, and
the tick refuses by name rather than running against half a dependency set."""

from __future__ import annotations

import pytest

from kiro_crew.cloud import config as cloud_config_mod
from kiro_crew.cloud.microvm import states, tick
from kiro_crew.cloud.microvm.lifecycle import GuestState
from kiro_crew.cloud.microvm.pack import ARCHIVE_RETENTION_SECONDS, PackConflict
from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore

NOW = 1_700_000_000.0


def _guest(*, running_slots: int = 0) -> GuestState:
    return GuestState(
        ready=True,
        running_slots=running_slots,
        idle_for_seconds=0.0,
        restarts=0,
        generation=0,
    )


def _record(tag: str, *, state: str = states.RUNNING, stopped_at: float = 0.0) -> CrewRecord:
    return CrewRecord(tag=tag, state=state, stopped_at=stopped_at, created_at=NOW)


class FakeLifecycle:
    """A scripted lifecycle that records the ORDER it was asked to do things.

    Hand-built rather than a mock, like the sweeper's fakes: the point of these
    tests is which collaborator the tick calls and in what order, so a fake that
    appends to one list reads better than a stack of ``assert_called``.
    """

    def __init__(
        self,
        store: CrewStore,
        *,
        wall_due=(),
        idle=(),
        unreachable=(),
        pack_raises=None,
    ) -> None:
        self.store = store
        self.wall_due_tags = set(wall_due)
        self.idle_tags = set(idle)
        self.unreachable_tags = set(unreachable)
        self.pack_raises = dict(pack_raises or {})
        self.calls: list[tuple[str, str]] = []

    def wall_pack_due(self, tag: str, *, now=None) -> bool:
        self.calls.append(("wall_pack_due", tag))
        return tag in self.wall_due_tags

    def resume(self, tag: str) -> CrewRecord:
        self.calls.append(("resume", tag))
        return self.store.get(tag)

    def pack(self, tag: str) -> CrewRecord:
        self.calls.append(("pack", tag))
        exc = self.pack_raises.get(tag)
        if exc is not None:
            raise exc
        return self.store.get(tag)

    def poll(self, tag: str):
        self.calls.append(("poll", tag))
        if tag in self.unreachable_tags:
            return None
        return _guest()

    def idle_verdict(self, tag: str, *, now=None) -> bool:
        self.calls.append(("idle_verdict", tag))
        return tag in self.idle_tags

    def suspend(self, tag: str) -> CrewRecord:
        self.calls.append(("suspend", tag))
        return self.store.get(tag)


def _lifecycle(tmp_path, records=(), **kwargs) -> FakeLifecycle:
    store = CrewStore(tmp_path / "crews.json")
    for record in records:
        store.put(record)
    return FakeLifecycle(store, **kwargs)


class _FakePlan:
    def describe(self) -> str:
        return "no MicroVM orphans found"


class _FakeSweeper:
    def __init__(self, plan=None) -> None:
        self._plan = plan or _FakePlan()
        self.ran = False

    def run(self):
        self.ran = True
        return self._plan


class _FakeCloud:
    """What ``CloudConfig.load()`` returns, with only what the tick reads."""

    def __init__(self, *, microvm=None, profile="prof", region="us-east-1") -> None:
        self._microvm = microvm
        self.profile = profile
        self.region = region

    def microvm_config(self):
        return self._microvm


class _FakeMicrovmConfig:
    def launch_spec(self):
        class _Spec:
            endpoint_url = "https://microvm.example/"

        return _Spec()


class TestCronSpecs:
    def test_the_two_entries_are_distinct_jobs_on_distinct_schedules(self):
        """Published as data so the guide and the installer cannot disagree."""
        names = [name for name, _schedule, _why in tick.CRON_SPECS]
        schedules = [schedule for _name, schedule, _why in tick.CRON_SPECS]
        assert names == ["microvm-lifecycle-tick", "microvm-sweep"]
        assert len(set(names)) == 2
        assert schedules[0] != schedules[1]


class TestTickReport:
    def test_describe_names_the_positive_signal_and_every_count(self):
        report = tick.TickReport(
            polled=1, suspended=2, packed=3, expired=4, conflicts=5, failures=6
        )
        text = report.describe()
        assert text.startswith(tick.TICK_OK)
        assert "polled=1" in text
        assert "suspended=2" in text
        assert "packed=3" in text
        assert "expired=4" in text
        assert "conflicts=5" in text
        assert "failures=6" in text

    def test_a_fresh_report_is_all_zero(self):
        assert "polled=0 suspended=0 packed=0" in tick.TickReport().describe()


class TestRunTick:
    def test_a_stopped_crew_past_its_window_is_expired(self, tmp_path):
        lifecycle = _lifecycle(
            tmp_path,
            [_record("a", state=states.STOPPED, stopped_at=NOW - ARCHIVE_RETENTION_SECONDS)],
        )
        report = tick.run_tick(lifecycle, now=NOW)
        assert report.expired == 1
        assert lifecycle.store.get("a").state == states.EXPIRED

    def test_a_stopped_crew_within_its_window_is_left_alone(self, tmp_path):
        """Keeping an archive costs tens of kilobytes; expiring it early costs the crew."""
        lifecycle = _lifecycle(tmp_path, [_record("a", state=states.STOPPED, stopped_at=NOW - 60)])
        report = tick.run_tick(lifecycle, now=NOW)
        assert report.expired == 0
        assert lifecycle.store.get("a").state == states.STOPPED

    def test_a_terminal_crew_is_never_touched(self, tmp_path):
        """A LAUNCH_FAILED crew is neither RUNNING nor SUSPENDED, so the wall check
        is never even asked about it."""
        lifecycle = _lifecycle(tmp_path, [_record("a", state=states.LAUNCH_FAILED)])
        report = tick.run_tick(lifecycle, now=NOW)
        assert lifecycle.calls == []
        assert report.describe().endswith("conflicts=0 failures=0")

    def test_a_running_crew_at_its_wall_is_packed_without_a_resume(self, tmp_path):
        lifecycle = _lifecycle(tmp_path, [_record("a")], wall_due={"a"})
        report = tick.run_tick(lifecycle, now=NOW)
        assert report.packed == 1
        assert ("resume", "a") not in lifecycle.calls
        assert ("pack", "a") in lifecycle.calls

    def test_a_suspended_crew_at_its_wall_is_resumed_before_it_is_packed(self, tmp_path):
        """Resume before packing: a suspended VM cannot run the tar the archive is."""
        lifecycle = _lifecycle(tmp_path, [_record("a", state=states.SUSPENDED)], wall_due={"a"})
        report = tick.run_tick(lifecycle, now=NOW)
        assert report.packed == 1
        assert lifecycle.calls == [
            ("wall_pack_due", "a"),
            ("resume", "a"),
            ("pack", "a"),
        ]

    def test_a_suspended_crew_not_at_its_wall_is_not_polled(self, tmp_path):
        """A suspended crew below its wall has nothing the tick does to it: polling
        and the idle verdict are for RUNNING crews only."""
        lifecycle = _lifecycle(tmp_path, [_record("a", state=states.SUSPENDED)])
        report = tick.run_tick(lifecycle, now=NOW)
        assert lifecycle.calls == [("wall_pack_due", "a")]
        assert report.polled == 0 and report.suspended == 0

    def test_a_running_crew_that_answers_is_counted_polled(self, tmp_path):
        lifecycle = _lifecycle(tmp_path, [_record("a")])
        report = tick.run_tick(lifecycle, now=NOW)
        assert report.polled == 1
        assert ("idle_verdict", "a") in lifecycle.calls

    def test_an_unreachable_running_crew_is_not_counted_polled(self, tmp_path):
        """A failed poll must not read as an observation, but the idle verdict is
        still asked -- and answers "not idle" for a crew it cannot reach."""
        lifecycle = _lifecycle(tmp_path, [_record("a")], unreachable={"a"})
        report = tick.run_tick(lifecycle, now=NOW)
        assert report.polled == 0
        assert report.suspended == 0
        assert ("idle_verdict", "a") in lifecycle.calls

    def test_an_idle_running_crew_is_suspended(self, tmp_path):
        lifecycle = _lifecycle(tmp_path, [_record("a")], idle={"a"})
        report = tick.run_tick(lifecycle, now=NOW)
        assert report.polled == 1
        assert report.suspended == 1
        assert ("suspend", "a") in lifecycle.calls

    def test_a_pack_conflict_is_counted_and_leaves_the_crew_running(self, tmp_path):
        """Two writers for one archive is a defect report, not a retry: the crew is
        left running with its home intact."""
        lifecycle = _lifecycle(
            tmp_path,
            [_record("a")],
            wall_due={"a"},
            pack_raises={"a": PackConflict("raced", held_etag='"e9"')},
        )
        report = tick.run_tick(lifecycle, now=NOW)
        assert report.conflicts == 1
        assert report.packed == 0
        assert report.failures == 0
        assert lifecycle.store.get("a").state == states.RUNNING

    def test_a_pack_conflict_with_no_etag_still_reports(self, tmp_path):
        """The log falls back to "unknown" rather than printing an empty etag."""
        lifecycle = _lifecycle(
            tmp_path,
            [_record("a")],
            wall_due={"a"},
            pack_raises={"a": PackConflict("raced")},
        )
        assert tick.run_tick(lifecycle, now=NOW).conflicts == 1

    def test_one_crews_failure_does_not_stop_the_next(self, tmp_path):
        """Each crew is handled inside its own try; the first crew's failure is
        already in its own state, so the second crew's pack still runs."""
        lifecycle = _lifecycle(
            tmp_path,
            [_record("a"), _record("b")],
            wall_due={"a", "b"},
            pack_raises={"a": ValueError("boom")},
        )
        report = tick.run_tick(lifecycle, now=NOW)
        assert report.failures == 1
        assert report.packed == 1
        assert ("pack", "b") in lifecycle.calls

    def test_an_empty_store_is_a_quiet_all_zero_pass(self, tmp_path):
        report = tick.run_tick(_lifecycle(tmp_path))
        assert report.describe() == (
            f"{tick.TICK_OK} polled=0 suspended=0 packed=0 expired=0 conflicts=0 failures=0"
        )


class TestRunSweep:
    def test_run_sweep_returns_the_plan_the_sweeper_produced(self):
        plan = _FakePlan()
        sweeper = _FakeSweeper(plan)
        assert tick.run_sweep(sweeper) is plan
        assert sweeper.ran is True


class TestMain:
    def test_an_unconfigured_lane_is_a_quiet_success(self, monkeypatch):
        """A gateway whose microvm block was deleted still has the jobs installed;
        every run shouting about a gone config would be noise."""
        monkeypatch.setattr(
            cloud_config_mod.CloudConfig, "load", staticmethod(lambda: _FakeCloud(microvm=None))
        )
        assert tick.main(["tick"]) == 0
        assert tick.main(["sweep"]) == 0

    def test_the_sweep_pass_reports_the_plan_and_succeeds(self, monkeypatch):
        monkeypatch.setattr(
            cloud_config_mod.CloudConfig,
            "load",
            staticmethod(lambda: _FakeCloud(microvm=_FakeMicrovmConfig())),
        )
        sweeper = _FakeSweeper()
        monkeypatch.setattr(tick, "_sweeper_for", lambda config: sweeper)
        assert tick.main(["sweep"]) == 0
        assert sweeper.ran is True

    def test_the_tick_pass_runs_the_production_lifecycle(self, monkeypatch, tmp_path):
        """Zero on a pass with nothing to do, and never a nonzero on a schedule.

        This job runs every minute. An entry point that exited nonzero when there
        was no work would be a failing process once a minute forever, and the real
        failure it exists to report would be buried under its own noise.
        """
        monkeypatch.setattr(
            cloud_config_mod.CloudConfig,
            "load",
            staticmethod(lambda: _FakeCloud(microvm=_FakeMicrovmConfig())),
        )
        built: list[object] = []

        def fake_lifecycle(config, **_kw):
            built.append(config)
            return _lifecycle(tmp_path)

        monkeypatch.setattr("kiro_crew.cloud.microvm.wiring.production_lifecycle", fake_lifecycle)
        assert tick.main(["tick"]) == 0
        assert built, "the tick never built a lifecycle"

    def test_an_unknown_pass_name_is_refused_by_the_parser(self):
        with pytest.raises(SystemExit):
            tick.main(["bogus"])


class TestSweeperFactory:
    def test_the_scheduled_sweeper_is_always_a_dry_run(self, monkeypatch):
        """A scheduled sweeper that deleted would be an unattended process removing
        cloud resources on a judgement nobody read."""
        monkeypatch.setattr(
            cloud_config_mod.CloudConfig, "load", staticmethod(lambda: _FakeCloud())
        )
        sweeper = tick._sweeper_for(_FakeMicrovmConfig())
        assert sweeper.dry_run is True
        assert isinstance(sweeper.deps.store, CrewStore)
