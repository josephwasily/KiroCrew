"""Installing the lane's cron jobs: exactly those, once, and gone when the lane is.

A schedule nothing installed is documentation. ``CRON_SPECS`` said what should
run for several rounds while nothing ran it, which is the defect these tests
exist to keep closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kiro_crew.cloud.microvm import schedule
from kiro_crew.cloud.microvm.tick import CRON_SPECS

#: The specs that can actually be installed, which is all of them: both passes
#: have production entry points, so the lane declares nothing it cannot run.
INSTALLABLE = [name for name, _expr, _why in CRON_SPECS]


@dataclass
class FakeJob:
    id: str
    name: str
    cron_expr: str = ""
    command: str = ""
    created_by: str = ""


@dataclass
class FakeCron:
    """Enough of ``CronService`` to hold jobs, with its absence check intact.

    ``add_job_if_absent`` applies the caller's predicate exactly as the real
    service does -- under one lock, after a sync. Reproducing the PREDICATE path
    rather than a name set is what lets these tests catch a predicate that
    matches the wrong thing.
    """

    jobs: list[FakeJob] = field(default_factory=list)
    adds: int = 0
    removes: int = 0
    _next: int = 1

    def add_job_if_absent(self, predicate, **kwargs):
        if any(predicate(job) for job in self.jobs):
            return None
        job = FakeJob(
            id=f"job-{self._next}",
            name=kwargs["name"],
            cron_expr=kwargs.get("cron_expr", ""),
            command=kwargs.get("command", ""),
            created_by=kwargs.get("created_by", ""),
        )
        self._next += 1
        self.jobs.append(job)
        self.adds += 1
        return job

    def list_jobs(self):
        return list(self.jobs)

    def remove_job(self, job_id: str) -> bool:
        before = len(self.jobs)
        self.jobs = [job for job in self.jobs if job.id != job_id]
        removed = len(self.jobs) != before
        self.removes += int(removed)
        return removed


class TestRegistrationInstallsExactlyTheLanesJobs:
    def test_it_installs_one_job_per_spec(self):
        cron = FakeCron()
        installed = schedule.install(cron)
        assert installed == INSTALLABLE
        assert cron.adds == len(INSTALLABLE)

    def test_each_job_carries_its_specs_own_schedule(self):
        """Derived from ``CRON_SPECS``, so the guide, the installer and what the
        gateway runs cannot disagree about how often."""
        cron = FakeCron()
        schedule.install(cron)
        by_name = {job.name: job for job in cron.list_jobs()}
        for name, expr, _why in CRON_SPECS:
            if name not in INSTALLABLE:
                continue
            assert by_name[name].cron_expr == expr

    def test_each_job_runs_the_lanes_own_entry_point(self):
        cron = FakeCron()
        schedule.install(cron)
        for job in cron.list_jobs():
            assert f" -m {schedule.TICK_MODULE} " in job.command

    def test_each_job_names_this_interpreter_and_not_a_bare_python(self):
        """These run through the sandboxed command path, where ``python`` need not
        be the environment Kiro Crew is installed in.

        A job whose interpreter cannot import the package fails on every fire,
        and silently, because the lane installs them with ``silent: True``.
        """
        import shlex
        import sys

        cron = FakeCron()
        schedule.install(cron)
        for job in cron.list_jobs():
            assert job.command.startswith(shlex.quote(sys.executable) + " -m ")
            assert not job.command.startswith("python ")

    def test_the_two_passes_get_different_verbs(self):
        """They differ in what they may DO -- the tick suspends and packs, the
        sweep only reports -- so one command for both would be one of them
        running with the other's permissions."""
        assert schedule.command_for("microvm-lifecycle-tick").endswith(" tick")
        assert schedule.command_for("microvm-sweep").endswith(" sweep")

    def test_it_installs_nothing_else(self):
        cron = FakeCron()
        schedule.install(cron)
        assert len(cron.list_jobs()) == len(INSTALLABLE)

    def test_every_job_is_owned_by_the_lane(self):
        cron = FakeCron()
        schedule.install(cron)
        assert {job.created_by for job in cron.list_jobs()} == {schedule.OWNER}


class TestASecondRegistrationInstallsNothingNew:
    def test_a_second_install_adds_no_job(self):
        cron = FakeCron()
        schedule.install(cron)
        adds_after_first = cron.adds
        second = schedule.install(cron)
        assert cron.adds == adds_after_first
        assert second == INSTALLABLE
        assert len(cron.list_jobs()) == len(INSTALLABLE)

    def test_ten_registrations_leave_one_set(self):
        """Registration runs on configuration reads, so idempotence has to hold
        for repetition rather than merely for a second call."""
        cron = FakeCron()
        for _ in range(10):
            schedule.install(cron)
        assert len(cron.list_jobs()) == len(INSTALLABLE)
        assert cron.adds == len(INSTALLABLE)

    def test_the_absence_check_is_the_stores_own(self):
        """Not a read-then-add here: two registrars racing would both see the
        name as absent and persist duplicates."""
        import inspect

        source = inspect.getsource(schedule.install)
        assert "add_job_if_absent" in source
        assert "list_jobs" not in source.split("present = ")[0]


class TestRemovalWhenTheLaneGoesAway:
    def test_uninstall_removes_every_job_the_lane_owns(self):
        cron = FakeCron()
        schedule.install(cron)
        removed = schedule.uninstall(cron)
        assert sorted(removed) == sorted(INSTALLABLE)
        assert cron.list_jobs() == []

    def test_it_leaves_a_users_own_job_of_the_same_name_alone(self):
        """By OWNER, not by name. A lane being turned off must not delete a job
        the user wrote and named the same thing."""
        cron = FakeCron()
        mine = FakeJob(id="mine", name="microvm-sweep", created_by="user:raymond")
        cron.jobs.append(mine)
        schedule.install(cron)
        schedule.uninstall(cron)
        assert cron.list_jobs() == [mine]

    def test_uninstalling_twice_removes_nothing_the_second_time(self):
        cron = FakeCron()
        schedule.install(cron)
        schedule.uninstall(cron)
        assert schedule.uninstall(cron) == []


class TestReconcile:
    def test_a_configured_lane_gets_the_jobs(self):
        cron = FakeCron()
        assert schedule.reconcile(cron, lane_configured=True) == INSTALLABLE

    def test_an_unconfigured_lane_gets_none_and_loses_any_it_had(self):
        """Installing on registration and never removing leaves a tick polling a
        lane whose block was deleted, and every pass of it is an error about a
        config that is gone."""
        cron = FakeCron()
        schedule.install(cron)
        assert schedule.reconcile(cron, lane_configured=False) == []
        assert cron.list_jobs() == []

    def test_turning_the_lane_off_and_on_again_leaves_one_set(self):
        cron = FakeCron()
        schedule.reconcile(cron, lane_configured=True)
        schedule.reconcile(cron, lane_configured=False)
        schedule.reconcile(cron, lane_configured=True)
        assert len(cron.list_jobs()) == len(INSTALLABLE)


class TestItNeverTakesTheGatewayDown:
    def test_an_unreadable_cron_store_is_reported_and_survived(self):
        """A gateway must come up whether or not a schedule could be written: a
        cron store that is unreadable is a reason to report, not to refuse every
        request."""

        def broken():
            raise OSError("the cron store is not readable")

        assert schedule.reconcile_safely(broken, lane_configured=True) == []

    def test_a_gateway_with_no_cron_service_is_the_quiet_case(self):
        assert schedule.reconcile_safely(lambda: None, lane_configured=True) == []

    def test_a_working_service_still_gets_its_jobs(self):
        cron = FakeCron()
        assert schedule.reconcile_safely(lambda: cron, lane_configured=True) == INSTALLABLE


class TestTheEntryPointTheJobsRun:
    def test_the_module_exposes_a_main_the_command_can_reach(self):
        from kiro_crew.cloud.microvm import tick

        assert callable(tick.main)

    def test_an_unknown_verb_is_refused_rather_than_defaulted(self):
        """The two passes differ in what they may DO, so guessing which was meant
        is guessing whether this invocation may act."""
        import pytest

        from kiro_crew.cloud.microvm import tick

        with pytest.raises(SystemExit):
            tick.main([])
        with pytest.raises(SystemExit):
            tick.main(["suspend-everything"])


class TestLaneRegistrationReconcilesTheSchedule:
    """The read that decides whether the lane exists is what installs its jobs.

    One place, because the two have to agree: a schedule installed where the lane
    is not offered is a tick polling a config that is gone, and an offered lane
    with no schedule is the defect these tests exist for.
    """

    def _provider(self, monkeypatch, cron, *, configured):
        from kiro_crew.cloud.microvm.config import MicroVmConfig
        from kiro_crew.platform import defaults

        defaults._forget_microvm_schedule_state()
        block = {
            "base_image_arn": "arn:aws:lambda:us-east-1:123456789012:microvm-image/al2023",
            "build_role_arn": "arn:aws:iam::123456789012:role/kirocrew-microvm-build",
            "recipe_bucket": "kirocrew-microvm-recipes-123456789012-us-east-1",
            "bundle_dir": "/srv/kirocrew/bundles/demo",
            "archive_bucket": "kirocrew-microvm-archive-123456789012-us-east-1",
            "kms_key_id": (
                "arn:aws:kms:us-east-1:123456789012:key/" "11111111-2222-3333-4444-555555555555"
            ),
            "activation_role_arn": "arn:aws:iam::123456789012:role/kirocrew-microvm-crew",
            "identity_secret_ref": "kirocrew/identity/demo-crew",
        }
        config = MicroVmConfig.from_mapping(block) if configured else None
        monkeypatch.setattr(
            defaults.DefaultRemoteProvisionerProvider,
            "_microvm_config",
            staticmethod(lambda: config),
        )
        monkeypatch.setattr(
            defaults.DefaultRemoteProvisionerProvider,
            "_fargate_config",
            staticmethod(lambda: None),
        )
        monkeypatch.setattr(
            defaults.DefaultRemoteProvisionerProvider,
            "_cron_service",
            staticmethod(lambda: cron),
        )
        return defaults

    def test_offering_the_lane_installs_its_jobs(self, monkeypatch):
        cron = FakeCron()
        defaults = self._provider(monkeypatch, cron, configured=True)
        rows = {row.id for row in defaults.DefaultRemoteProvisionerProvider().provisioners()}
        assert defaults.MICROVM_PROVISIONER_ID in rows
        assert sorted(job.name for job in cron.list_jobs()) == sorted(INSTALLABLE)

    def test_a_second_read_installs_nothing_new(self, monkeypatch):
        """``provisioners()`` serves every GET, and the cron store's constructor is
        file I/O, so the steady state has to be free."""
        cron = FakeCron()
        defaults = self._provider(monkeypatch, cron, configured=True)
        provider = defaults.DefaultRemoteProvisionerProvider()
        provider.provisioners()
        adds = cron.adds
        for _ in range(5):
            provider.provisioners()
        assert cron.adds == adds
        assert len(cron.list_jobs()) == len(INSTALLABLE)

    def test_an_unoffered_lane_installs_nothing(self, monkeypatch):
        cron = FakeCron()
        defaults = self._provider(monkeypatch, cron, configured=False)
        rows = {row.id for row in defaults.DefaultRemoteProvisionerProvider().provisioners()}
        assert defaults.MICROVM_PROVISIONER_ID not in rows
        assert cron.list_jobs() == []

    def test_removing_the_block_removes_the_jobs(self, monkeypatch):
        cron = FakeCron()
        defaults = self._provider(monkeypatch, cron, configured=True)
        defaults.DefaultRemoteProvisionerProvider().provisioners()
        assert cron.list_jobs()
        self._provider(monkeypatch, cron, configured=False)
        defaults.DefaultRemoteProvisionerProvider().provisioners()
        assert cron.list_jobs() == []


class TestAnUnwiredSpecIsNotInstalled:
    """A job whose command cannot work is worse than no job at all.

    The `tick` spec runs every minute. Installed while its verb refuses, it is a
    failing process once a minute indefinitely, and the real failure it exists to
    report is buried under its own noise.
    """

    def test_the_tick_spec_is_declared(self):
        """It stays in ``CRON_SPECS``, which is the lane's statement of what
        SHOULD run."""
        assert any(name.endswith("lifecycle-tick") for name, _e, _w in CRON_SPECS)

    def test_the_tick_job_is_not_installed(self):
        cron = FakeCron()
        installed = schedule.install(cron)
        assert [name for name in installed if name.endswith("lifecycle-tick")]
        assert [job for job in cron.list_jobs() if job.name.endswith("lifecycle-tick")]

    def test_the_sweep_job_is_installed_too(self):
        """Both passes, on their own schedules. They are separate jobs so a bug in
        the account-wide sweep walk cannot take down the tick that keeps a crew's
        work safe."""
        cron = FakeCron()
        assert [name for name in schedule.install(cron) if name.endswith("sweep")]

    def test_registration_installs_the_tick_on_every_minute(self):
        """The idle suspend the lane promises is noticed rather than requested, so
        the cron expression is the mechanism and not a detail."""
        cron = FakeCron()
        schedule.install(cron)
        tick_jobs = [j for j in cron.list_jobs() if j.name.endswith("lifecycle-tick")]
        assert len(tick_jobs) == 1
        assert tick_jobs[0].cron_expr == "* * * * *"
        assert tick_jobs[0].command.endswith(" tick")

    def test_every_installed_job_names_a_verb_the_module_accepts(self):
        """A job whose command the entry point refuses is a failing process on a
        schedule, which buries the real failure it exists to report."""
        cron = FakeCron()
        schedule.install(cron)
        for job in cron.list_jobs():
            assert job.command.rsplit(" ", 1)[-1] in ("tick", "sweep")
