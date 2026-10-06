"""The lifecycle port: the idle verdict, the pack order, and the generation clause."""

from __future__ import annotations

import pytest

from kiro_crew.cloud.microvm import pack, states
from kiro_crew.cloud.microvm.lifecycle import (
    IDLE_SUSPEND_SECONDS,
    GuestState,
    LifecycleDeps,
    MicroVmLifecycle,
)
from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore

NOW = 1_700_000_000.0


class Platform:
    """A scripted platform that records the ORDER it was asked to do things."""

    def __init__(self, guest: GuestState | None = None, *, vm_status: str | None = "RUNNING"):
        self.calls: list[str] = []
        self.guest = guest
        self.vm_status = vm_status
        self.etag = '"packed"'
        self.pack_error: Exception | None = None
        self.resume_error: Exception | None = None
        #: Called from inside ``terminate_vm``, so a test can observe what the
        #: record held at the exact moment the VM was about to go away.
        self.on_terminate = None

    def read_guest(self, record: CrewRecord):
        self.calls.append("read_guest")
        return self.guest

    def stop_gateway(self, record: CrewRecord) -> None:
        self.calls.append("stop_gateway")

    def pack_home(self, record: CrewRecord) -> str:
        self.calls.append("pack_home")
        if self.pack_error is not None:
            raise self.pack_error
        return self.etag

    def suspend_vm(self, record: CrewRecord) -> None:
        self.calls.append("suspend_vm")

    def resume_vm(self, record: CrewRecord) -> None:
        self.calls.append("resume_vm")
        if self.resume_error is not None:
            raise self.resume_error

    def terminate_vm(self, record: CrewRecord) -> None:
        self.calls.append("terminate_vm")
        if self.on_terminate is not None:
            self.on_terminate()

    def wait_terminated(self, record: CrewRecord) -> None:
        self.calls.append("wait_terminated")

    def read_vm_status(self, record: CrewRecord):
        self.calls.append("read_vm_status")
        return self.vm_status


def _build(tmp_path, record: CrewRecord, platform: Platform, *, now: float = NOW):
    store = CrewStore(tmp_path / "crews.json")
    store.put(record)
    deps = LifecycleDeps(
        store=store,
        read_guest=platform.read_guest,
        stop_gateway=platform.stop_gateway,
        pack_home=platform.pack_home,
        suspend_vm=platform.suspend_vm,
        resume_vm=platform.resume_vm,
        terminate_vm=platform.terminate_vm,
        read_vm_status=platform.read_vm_status,
        wait_terminated=platform.wait_terminated,
        now=lambda: now,
    )
    return MicroVmLifecycle(deps), store


def _running(**overrides) -> CrewRecord:
    base = dict(
        tag="kc-a",
        state=states.RUNNING,
        microvm_id="mvm-1",
        endpoint="https://mvm-1.example",
        archive_bucket="b",
        archive_key="crews/kc-a/home.tar.gz",
        wall_seconds=28_800,
        generation=1,
        created_at=NOW - 60,
        last_observed_at=NOW - 10,
        last_active_at=NOW - 10,
    )
    base.update(overrides)
    return CrewRecord(**base)  # type: ignore[arg-type]


def _guest(**overrides) -> GuestState:
    base = dict(ready=True, running_slots=0, idle_for_seconds=0.0, restarts=0, generation=1)
    base.update(overrides)
    return GuestState(**base)  # type: ignore[arg-type]


class TestIdleVerdict:
    def test_a_crew_with_a_running_slot_is_never_idle(self, tmp_path):
        """Platform idle counts endpoint traffic, and a tunnelled turn sends none."""
        platform = Platform(_guest(running_slots=1, idle_for_seconds=10_000))
        lifecycle, _ = _build(tmp_path, _running(last_active_at=NOW - 10_000), platform)
        assert lifecycle.idle_verdict("kc-a") is False

    def test_a_quiet_crew_past_the_window_is_idle(self, tmp_path):
        platform = Platform(_guest(idle_for_seconds=IDLE_SUSPEND_SECONDS + 1))
        lifecycle, _ = _build(
            tmp_path, _running(last_active_at=NOW - IDLE_SUSPEND_SECONDS - 1), platform
        )
        assert lifecycle.idle_verdict("kc-a") is True

    def test_a_quiet_crew_inside_the_window_is_not_idle(self, tmp_path):
        platform = Platform(_guest(idle_for_seconds=60))
        lifecycle, _ = _build(tmp_path, _running(last_active_at=NOW - 60), platform)
        assert lifecycle.idle_verdict("kc-a") is False

    def test_an_unreachable_crew_is_not_idle(self, tmp_path):
        """It may be mid-turn behind a blip, and a suspend there is a suspend into
        unknown state."""
        platform = Platform(None)
        lifecycle, _ = _build(tmp_path, _running(last_active_at=NOW - 10_000), platform)
        assert lifecycle.idle_verdict("kc-a") is False

    def test_a_stale_record_is_not_idle(self, tmp_path):
        """Its reported state is unknown, so there is nothing to suspend from."""
        platform = Platform(_guest(idle_for_seconds=10_000))
        lifecycle, _ = _build(tmp_path, _running(last_observed_at=NOW - 10_000), platform)
        assert lifecycle.idle_verdict("kc-a") is False

    def test_a_crew_with_no_record_is_not_idle(self, tmp_path):
        platform = Platform(_guest())
        lifecycle, _ = _build(tmp_path, _running(), platform)
        assert lifecycle.idle_verdict("kc-missing") is False

    def test_both_clocks_must_agree(self, tmp_path):
        """The control plane's clock alone can be wrong after a laptop sleeps."""
        platform = Platform(_guest(idle_for_seconds=1))
        lifecycle, _ = _build(tmp_path, _running(last_active_at=NOW - 10_000), platform)
        assert lifecycle.idle_verdict("kc-a") is False


class TestReadiness:
    def test_a_ready_guest_in_the_current_generation_is_ready(self, tmp_path):
        platform = Platform(_guest(generation=1))
        lifecycle, _ = _build(tmp_path, _running(generation=1), platform)
        assert lifecycle.ready("kc-a", platform.guest) is True

    def test_a_ready_answer_from_a_previous_generation_is_not_ready(self, tmp_path):
        """Without this clause a reopen offers "Open crew" for the old VM."""
        platform = Platform(_guest(generation=1))
        lifecycle, _ = _build(tmp_path, _running(generation=2), platform)
        assert lifecycle.ready("kc-a", platform.guest) is False

    def test_a_crew_with_no_coordinates_is_not_ready(self, tmp_path):
        platform = Platform(_guest())
        lifecycle, _ = _build(tmp_path, _running(endpoint=""), platform)
        assert lifecycle.ready("kc-a", platform.guest) is False

    def test_a_stale_observation_is_not_ready(self, tmp_path):
        platform = Platform(_guest())
        lifecycle, _ = _build(tmp_path, _running(last_observed_at=NOW - 10_000), platform)
        assert lifecycle.ready("kc-a", platform.guest) is False

    def test_a_guest_that_says_not_ready_is_not_ready(self, tmp_path):
        platform = Platform(_guest(ready=False))
        lifecycle, _ = _build(tmp_path, _running(), platform)
        assert lifecycle.ready("kc-a", platform.guest) is False


class TestPoll:
    def test_a_failed_poll_does_not_stamp_an_observation(self, tmp_path):
        """Otherwise a crew nobody can reach looks freshly observed."""
        platform = Platform(None)
        lifecycle, store = _build(tmp_path, _running(last_observed_at=NOW - 500), platform)
        assert lifecycle.poll("kc-a") is None
        assert store.get("kc-a").last_observed_at == NOW - 500

    def test_a_successful_poll_records_the_restart_count(self, tmp_path):
        platform = Platform(_guest(restarts=3))
        lifecycle, store = _build(tmp_path, _running(), platform)
        lifecycle.poll("kc-a")
        assert store.get("kc-a").restarts_seen == 3
        assert store.get("kc-a").last_observed_at == NOW

    def test_a_poll_adopts_the_guests_own_pack_etag(self, tmp_path):
        """The guest packing itself is the designed path, and the result lands on
        the guest's disk, so a poll is the only channel that carries it back.

        Without this the record keeps an empty ETag, the next reopen tells the
        guest there is nothing to restore, the crew boots with an empty home, and
        its first pack's ``If-None-Match: *`` is refused against the archive that
        was there all along.
        """
        platform = Platform(_guest(self_packed_etag="etag-the-guest-wrote"))
        lifecycle, store = _build(tmp_path, _running(archive_etag=""), platform)
        lifecycle.poll("kc-a")
        assert store.get("kc-a").archive_etag == "etag-the-guest-wrote"

    def test_a_poll_with_no_guest_etag_leaves_the_recorded_one_alone(self, tmp_path):
        """Forward only. A guest too old to report one must not read as a crew
        whose archive vanished, which an empty answer clearing the field would
        make it."""
        platform = Platform(_guest())
        lifecycle, store = _build(tmp_path, _running(archive_etag="etag-already-known"), platform)
        lifecycle.poll("kc-a")
        assert store.get("kc-a").archive_etag == "etag-already-known"

    def test_a_running_slot_refreshes_the_activity_clock(self, tmp_path):
        platform = Platform(_guest(running_slots=2))
        lifecycle, store = _build(tmp_path, _running(last_active_at=NOW - 9999), platform)
        lifecycle.poll("kc-a")
        assert store.get("kc-a").last_active_at == NOW


class TestSuspendAndResume:
    def test_suspend_moves_a_running_crew(self, tmp_path):
        platform = Platform(_guest())
        lifecycle, store = _build(tmp_path, _running(), platform)
        assert lifecycle.suspend("kc-a").state == states.SUSPENDED
        assert platform.calls == ["suspend_vm"]

    def test_suspend_refuses_a_crew_that_is_not_running(self, tmp_path):
        platform = Platform(_guest())
        lifecycle, _ = _build(tmp_path, _running(state=states.STOPPED), platform)
        with pytest.raises(states.IllegalTransition):
            lifecycle.suspend("kc-a")
        assert "suspend_vm" not in platform.calls

    def test_resume_returns_a_crew_to_running(self, tmp_path):
        platform = Platform(_guest())
        lifecycle, _ = _build(tmp_path, _running(state=states.SUSPENDED), platform)
        assert lifecycle.resume("kc-a").state == states.RUNNING

    def test_a_suspended_vm_the_wall_took_is_resume_target_gone(self, tmp_path):
        """Nothing failed: suspending does not pause the eight-hour clock."""
        platform = Platform(_guest(), vm_status="TERMINATED")
        lifecycle, _ = _build(tmp_path, _running(state=states.SUSPENDED), platform)
        assert lifecycle.resume("kc-a").state == states.RESUME_TARGET_GONE

    def test_a_forgotten_vm_is_also_resume_target_gone(self, tmp_path):
        platform = Platform(_guest(), vm_status=None)
        lifecycle, _ = _build(tmp_path, _running(state=states.SUSPENDED), platform)
        assert lifecycle.resume("kc-a").state == states.RESUME_TARGET_GONE

    def test_a_transient_resume_failure_is_raised_and_not_recorded_as_gone(self, tmp_path):
        """Recording "the wall took it" would tell the owner their work is lost."""
        platform = Platform(_guest(), vm_status="SUSPENDED")
        platform.resume_error = RuntimeError("throttled")
        lifecycle, store = _build(tmp_path, _running(state=states.SUSPENDED), platform)
        with pytest.raises(RuntimeError, match="throttled"):
            lifecycle.resume("kc-a")
        assert store.get("kc-a").state == states.SUSPENDED


class TestPackOrder:
    def test_the_sequence_terminates_last(self, tmp_path):
        platform = Platform(_guest())
        lifecycle, _ = _build(tmp_path, _running(), platform)
        lifecycle.pack("kc-a")
        assert platform.calls == [
            "read_guest",
            "stop_gateway",
            "pack_home",
            "terminate_vm",
            "wait_terminated",
        ]

    def test_the_gateway_is_stopped_before_the_archive_is_written(self, tmp_path):
        platform = Platform(_guest())
        lifecycle, _ = _build(tmp_path, _running(), platform)
        lifecycle.pack("kc-a")
        assert platform.calls.index("stop_gateway") < platform.calls.index("pack_home")

    def test_the_new_etag_is_stored_before_the_terminate(self, tmp_path):
        """A pack the ledger never learned about is archived and unrestorable."""
        seen: list[str] = []
        platform = Platform(_guest())
        lifecycle, store = _build(tmp_path, _running(), platform)
        platform.on_terminate = lambda: seen.append(store.get("kc-a").archive_etag)
        lifecycle.pack("kc-a")
        assert seen == ['"packed"']

    def test_a_running_slot_refuses_the_pack(self, tmp_path):
        platform = Platform(_guest(running_slots=1))
        lifecycle, store = _build(tmp_path, _running(), platform)
        with pytest.raises(pack.GatewayAlive):
            lifecycle.pack("kc-a")
        assert "terminate_vm" not in platform.calls
        assert store.get("kc-a").state == states.RUNNING

    def test_a_failed_archive_ends_terminated_unarchived(self, tmp_path):
        platform = Platform(_guest())
        platform.pack_error = RuntimeError("tar: file changed as we read it")
        lifecycle, store = _build(tmp_path, _running(), platform)
        record = lifecycle.pack("kc-a")
        assert record.state == states.TERMINATED_UNARCHIVED
        assert record.last_pack_failed_at == NOW
        # The VM is still terminated: an eight-hour VM kept running to protect a
        # home that could not be written costs money for a rescue nobody will make.
        assert "terminate_vm" in platform.calls

    def test_a_pack_conflict_leaves_the_vm_alone(self, tmp_path):
        """Two writers for one archive is a defect report, never a retry."""
        platform = Platform(_guest())
        platform.pack_error = pack.PackConflict("held", held_etag='"other"')
        lifecycle, store = _build(tmp_path, _running(), platform)
        with pytest.raises(pack.PackConflict):
            lifecycle.pack("kc-a")
        assert "terminate_vm" not in platform.calls
        assert store.get("kc-a").state == states.RUNNING

    def test_pack_refuses_a_crew_that_is_not_running(self, tmp_path):
        platform = Platform(_guest())
        lifecycle, _ = _build(tmp_path, _running(state=states.SUSPENDED), platform)
        with pytest.raises(states.IllegalTransition):
            lifecycle.pack("kc-a")


class TestWallBackstop:
    def test_a_young_crew_is_not_due(self, tmp_path):
        platform = Platform(_guest())
        lifecycle, _ = _build(tmp_path, _running(created_at=NOW - 60), platform)
        assert lifecycle.wall_pack_due("kc-a") is False

    def test_a_crew_past_its_soft_edge_is_due(self, tmp_path):
        platform = Platform(_guest())
        leads_soft = 7 * 3600 + 30 * 60
        lifecycle, _ = _build(tmp_path, _running(created_at=NOW - leads_soft - 1), platform)
        assert lifecycle.wall_pack_due("kc-a") is True

    def test_a_suspended_crew_is_still_subject_to_the_wall(self, tmp_path):
        """Suspending does not pause the clock, which is why this edge applies."""
        platform = Platform(_guest())
        lifecycle, _ = _build(
            tmp_path,
            _running(state=states.SUSPENDED, created_at=NOW - 7 * 3600 - 31 * 60),
            platform,
        )
        assert lifecycle.wall_pack_due("kc-a") is True

    def test_a_stopped_crew_has_no_wall(self, tmp_path):
        platform = Platform(_guest())
        lifecycle, _ = _build(
            tmp_path, _running(state=states.STOPPED, created_at=NOW - 10**6), platform
        )
        assert lifecycle.wall_pack_due("kc-a") is False

    def test_a_short_lived_crew_gets_edges_inside_its_own_life(self, tmp_path):
        platform = Platform(_guest())
        lifecycle, _ = _build(tmp_path, _running(wall_seconds=900), platform)
        leads = lifecycle.wall_leads(_running(wall_seconds=900))
        assert 0 < leads.soft_at < leads.hard_at < 900
