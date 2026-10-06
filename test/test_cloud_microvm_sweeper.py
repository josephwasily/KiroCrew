"""The sweeper: two orphan classes, dry-run by default, and an oracle it refuses to guess."""

from __future__ import annotations

from kiro_crew.cloud.microvm import api, states
from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore
from kiro_crew.cloud.microvm.sweeper import (
    MIN_ORPHAN_AGE_SECONDS,
    MicroVmSweeper,
    SweepDeps,
)

NOW = 1_700_000_000.0


def _vm(microvm_id: str, *, state: str = "RUNNING", age: float = 10_000.0) -> api.MicroVm:
    from datetime import datetime, timezone

    started = datetime.fromtimestamp(NOW - age, tz=timezone.utc).isoformat()
    return api.MicroVm(
        microvm_id=microvm_id,
        state=state,
        endpoint=f"https://{microvm_id}.example/",
        started_at=started,
    )


def _sweeper(
    tmp_path,
    *,
    records=(),
    live=(),
    activations=None,
    dry_run=True,
    list_error: Exception | None = None,
):
    store = CrewStore(tmp_path / "crews.json")
    for record in records:
        store.put(record)
    terminated: list[str] = []
    deleted: list[str] = []

    def list_microvms():
        if list_error is not None:
            raise list_error
        return list(live)

    sweeper = MicroVmSweeper(
        SweepDeps(
            store=store,
            list_microvms=list_microvms,
            terminate_microvm=terminated.append,
            describe_activation=lambda a: (activations or {}).get(a),
            delete_activation=deleted.append,
            now=lambda: NOW,
        ),
        dry_run=dry_run,
    )
    return sweeper, terminated, deleted


class TestOrphanVms:
    def test_a_live_vm_on_no_record_is_an_orphan(self, tmp_path):
        """The expensive class: it bills for eight hours and nothing names it."""
        sweeper, _t, _d = _sweeper(tmp_path, live=[_vm("mvm-ghost")])
        plan = sweeper.plan()
        assert [o.identifier for o in plan.orphans] == ["mvm-ghost"]
        assert plan.confirmed is True

    def test_a_recorded_vm_is_not_an_orphan(self, tmp_path):
        sweeper, _t, _d = _sweeper(
            tmp_path,
            records=[CrewRecord(tag="a", microvm_id="mvm-1", state=states.RUNNING)],
            live=[_vm("mvm-1")],
        )
        assert sweeper.plan().orphans == ()

    def test_a_young_vm_is_never_an_orphan(self, tmp_path):
        """A launch in flight has a VM and not yet a complete record, by design."""
        sweeper, _t, _d = _sweeper(tmp_path, live=[_vm("mvm-new", age=MIN_ORPHAN_AGE_SECONDS - 1)])
        assert sweeper.plan().orphans == ()

    def test_a_vm_at_the_age_bound_is_an_orphan(self, tmp_path):
        sweeper, _t, _d = _sweeper(tmp_path, live=[_vm("mvm-old", age=MIN_ORPHAN_AGE_SECONDS)])
        assert len(sweeper.plan().orphans) == 1

    def test_a_terminated_vm_is_not_swept(self, tmp_path):
        sweeper, _t, _d = _sweeper(tmp_path, live=[_vm("mvm-dead", state="TERMINATED")])
        assert sweeper.plan().orphans == ()

    def test_an_unreadable_start_time_leaves_the_vm_alone_and_unconfirms_the_plan(self, tmp_path):
        """An unreadable timestamp must not read as "brand new" by accident."""
        broken = api.MicroVm(
            microvm_id="mvm-x", state="RUNNING", endpoint="x", started_at="yesterday"
        )
        sweeper, _t, _d = _sweeper(tmp_path, live=[broken])
        plan = sweeper.plan()
        assert plan.orphans == ()
        assert plan.confirmed is False
        assert "mvm-x" in plan.warning


class TestOrphanActivations:
    def test_an_expired_activation_with_no_registrations_is_an_orphan(self, tmp_path):
        from datetime import datetime, timezone

        expired = datetime.fromtimestamp(NOW - 60, tz=timezone.utc).isoformat()
        sweeper, _t, _d = _sweeper(
            tmp_path,
            records=[CrewRecord(tag="a", activation_id="act-1", state=states.LAUNCH_FAILED)],
            activations={"act-1": {"RegistrationsCount": 0, "ExpirationDate": expired}},
        )
        assert [o.kind for o in sweeper.plan().orphans] == ["activation"]

    def test_an_activation_that_registered_a_node_is_not_an_orphan(self, tmp_path):
        from datetime import datetime, timezone

        expired = datetime.fromtimestamp(NOW - 60, tz=timezone.utc).isoformat()
        sweeper, _t, _d = _sweeper(
            tmp_path,
            records=[CrewRecord(tag="a", activation_id="act-1", state=states.RUNNING)],
            activations={"act-1": {"RegistrationsCount": 1, "ExpirationDate": expired}},
        )
        assert sweeper.plan().orphans == ()

    def test_an_unexpired_activation_is_not_an_orphan(self, tmp_path):
        from datetime import datetime, timezone

        future = datetime.fromtimestamp(NOW + 3600, tz=timezone.utc).isoformat()
        sweeper, _t, _d = _sweeper(
            tmp_path,
            records=[CrewRecord(tag="a", activation_id="act-1")],
            activations={"act-1": {"RegistrationsCount": 0, "ExpirationDate": future}},
        )
        assert sweeper.plan().orphans == ()

    def test_an_unreadable_expiry_reads_as_not_expired(self, tmp_path):
        """Keeping a dead activation costs nothing; deleting a live one costs a launch."""
        sweeper, _t, _d = _sweeper(
            tmp_path,
            records=[CrewRecord(tag="a", activation_id="act-1")],
            activations={"act-1": {"RegistrationsCount": 0}},
        )
        assert sweeper.plan().orphans == ()

    def test_an_activation_the_service_has_forgotten_is_not_an_orphan(self, tmp_path):
        sweeper, _t, _d = _sweeper(
            tmp_path, records=[CrewRecord(tag="a", activation_id="act-1")], activations={}
        )
        assert sweeper.plan().orphans == ()


class TestSafety:
    def test_an_unreadable_oracle_plans_nothing(self, tmp_path, monkeypatch):
        """An unreadable store looks exactly like an empty one, and against an
        empty one every running crew is an orphan."""
        sweeper, _t, _d = _sweeper(tmp_path, live=[_vm("mvm-1")])

        def boom():
            raise OSError("permission denied")

        monkeypatch.setattr(sweeper.deps.store, "load", boom)
        plan = sweeper.plan()
        assert plan.orphans == ()
        assert plan.confirmed is False
        assert "unreadable" in plan.warning

    def test_a_partial_vm_list_cannot_support_a_deletion(self, tmp_path):
        sweeper, _t, _d = _sweeper(tmp_path, list_error=RuntimeError("throttled"))
        plan = sweeper.plan()
        assert plan.confirmed is False
        assert "could not be read in full" in plan.warning

    def test_a_dry_run_changes_nothing(self, tmp_path):
        sweeper, terminated, deleted = _sweeper(tmp_path, live=[_vm("mvm-ghost")], dry_run=True)
        plan = sweeper.run()
        assert len(plan.orphans) == 1
        assert terminated == [] and deleted == []
        assert sweeper.acted == ()

    def test_dry_run_is_the_default(self, tmp_path):
        store = CrewStore(tmp_path / "c.json")
        sweeper = MicroVmSweeper(
            SweepDeps(
                store=store,
                list_microvms=lambda: [],
                terminate_microvm=lambda _i: None,
                describe_activation=lambda _a: None,
                delete_activation=lambda _a: None,
            )
        )
        assert sweeper.dry_run is True

    def test_an_unconfirmed_plan_is_never_executed(self, tmp_path):
        """Both guards, because they answer different questions."""
        sweeper, terminated, _d = _sweeper(
            tmp_path, list_error=RuntimeError("throttled"), dry_run=False
        )
        sweeper.run()
        assert terminated == []

    def test_a_confirmed_plan_acts_when_not_a_dry_run(self, tmp_path):
        sweeper, terminated, _d = _sweeper(tmp_path, live=[_vm("mvm-ghost")], dry_run=False)
        sweeper.run()
        assert terminated == ["mvm-ghost"]
        assert [o.identifier for o in sweeper.acted] == ["mvm-ghost"]

    def test_one_failed_removal_does_not_stop_the_rest(self, tmp_path):
        store = CrewStore(tmp_path / "c.json")
        removed: list[str] = []

        def terminate(identifier: str) -> None:
            if identifier == "mvm-a":
                raise RuntimeError("throttled")
            removed.append(identifier)

        sweeper = MicroVmSweeper(
            SweepDeps(
                store=store,
                list_microvms=lambda: [_vm("mvm-a"), _vm("mvm-b")],
                terminate_microvm=terminate,
                describe_activation=lambda _a: None,
                delete_activation=lambda _a: None,
                now=lambda: NOW,
            ),
            dry_run=False,
        )
        sweeper.run()
        assert removed == ["mvm-b"]


class TestReport:
    def test_an_empty_plan_says_so(self, tmp_path):
        sweeper, _t, _d = _sweeper(tmp_path)
        assert sweeper.plan().describe() == "no MicroVM orphans found"

    def test_a_plan_names_each_orphan_and_its_reason(self, tmp_path):
        """A plan an operator cannot read is a plan they cannot refuse."""
        sweeper, _t, _d = _sweeper(tmp_path, live=[_vm("mvm-ghost")])
        text = sweeper.plan().describe()
        assert "mvm-ghost" in text
        assert "no crew record names it" in text

    def test_an_unconfirmed_plan_says_nothing_will_be_deleted(self, tmp_path):
        sweeper, _t, _d = _sweeper(tmp_path, list_error=RuntimeError("x"))
        assert "NOT confirmed" in sweeper.plan().describe() or not sweeper.plan().orphans
