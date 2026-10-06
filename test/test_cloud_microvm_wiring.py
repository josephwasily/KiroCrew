"""The production dependency set, which is what gives the lifecycle tick callers.

``MicroVmLifecycle`` is written against ``LifecycleDeps`` so the local harness can
swap the whole set. ``wiring.py`` is the other implementation -- the one that
talks to AWS and to a real guest -- and these tests pin the two properties the
tick depends on and the one that keeps an archive trustworthy.

Nothing here opens a socket or runs a command. The SSM channel is replaced, and
what is asserted is the command the lane WOULD run and how it reads the answer.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.cloud.microvm import wiring
from kiro_crew.cloud.microvm.record import CrewRecord


def _record(**overrides) -> CrewRecord:
    fields = {
        "tag": "demo",
        "state": "running",
        "profile": "p",
        "region": "us-east-1",
        "mi_id": "mi-0123456789abcdef0",
        "microvm_id": "mvm-1",
        "archive_bucket": "kirocrew-archive",
        "archive_key": "crews/demo/home.tar.gz",
        "archive_etag": "etag-1",
    }
    fields.update(overrides)
    return CrewRecord(**fields)  # type: ignore[arg-type]


class _Result:
    """Stands in for ``ssm.CommandResult``."""

    def __init__(self, stdout: str = "", *, ok: bool = True, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.ok = ok
        self.status = "Success" if ok else "Failed"
        self.exit_code = 0 if ok else 1


def _install_ssm(monkeypatch, result, calls=None):
    from kiro_crew.cloud import ssm

    def fake(instance_id, command, profile="", region="", *, run_as="ec2-user", total_wait=0):
        if calls is not None:
            calls.append({"instance_id": instance_id, "command": command, "run_as": run_as})
        return result() if callable(result) else result

    monkeypatch.setattr(ssm, "run_command", fake)
    return calls


class TestTheGuestChannel:
    def test_the_command_runs_as_the_configured_user(self, monkeypatch):
        """One identity for the whole seam, asserted against the constant so a
        change to it cannot leave a call site behind. Which identity, and why it
        is not ``crew``, is pinned by ``TestMaintenanceRunsAsRoot``."""
        calls: list[dict] = []
        _install_ssm(monkeypatch, _Result(json.dumps({"ready": True})), calls)
        wiring.read_guest(_record(), profile="p", region="us-east-1")
        assert calls[0]["run_as"] == wiring.GUEST_RUN_AS

    def test_the_command_invokes_the_guest_ops_module(self, monkeypatch):
        """One entry point on the guest side, named in one place, so the two
        halves of the seam cannot drift onto different modules."""
        calls: list[dict] = []
        _install_ssm(monkeypatch, _Result(json.dumps({"ready": True})), calls)
        wiring.read_guest(_record(), profile="p", region="us-east-1")
        assert wiring.GUEST_OPS_MODULE in calls[0]["command"]
        assert calls[0]["command"].endswith("state")

    def test_every_pack_argument_is_shell_quoted(self, monkeypatch):
        """These carry a bucket name, an object key and an ETag read off disk, and
        they are interpolated into a command that runs as the crew user. The
        quoting is what keeps that safe as the values' origins change."""
        calls: list[dict] = []
        _install_ssm(monkeypatch, _Result(json.dumps({"etag": "e2"})), calls)
        wiring.pack_home(
            _record(archive_etag="a b; rm -rf /"),
            profile="p",
            region="us-east-1",
            kms_key_id="",
        )
        assert "rm -rf /" not in calls[0]["command"].replace("'a b; rm -rf /'", "")

    def test_a_crew_with_no_managed_instance_is_unreachable(self, monkeypatch):
        """Reported as unreachable rather than attempted: a node that never
        registered has no channel, and the lifecycle reads unreachable as
        "leave it alone"."""
        _install_ssm(monkeypatch, _Result(json.dumps({"ready": True})))
        assert wiring.read_guest(_record(mi_id=""), profile="p", region="us-east-1") is None


class TestReadingTheGuest:
    def test_a_well_formed_answer_becomes_a_guest_state(self, monkeypatch):
        _install_ssm(
            monkeypatch,
            _Result(
                json.dumps(
                    {
                        "ready": True,
                        "running_slots": 2,
                        "idle_for_seconds": 31.5,
                        "restarts": 1,
                        "generation": 4,
                        "self_pack_armed": True,
                    }
                )
            ),
        )
        state = wiring.read_guest(_record(), profile="p", region="us-east-1")
        assert state is not None
        assert (state.running_slots, state.generation) == (2, 4)
        assert state.idle_for_seconds == pytest.approx(31.5)
        assert state.self_pack_armed is True

    @pytest.mark.parametrize(
        "result",
        [
            _Result("", ok=False, stderr="connection refused"),
            _Result("not json at all"),
            _Result(json.dumps({"error": "RuntimeError('no route')"})),
            _Result(json.dumps(["a", "list"])),
        ],
        ids=["command-failed", "no-json", "guest-reported-error", "wrong-json-shape"],
    )
    def test_every_way_of_not_getting_an_answer_is_unreachable(self, monkeypatch, result):
        """``None``, never a raise.

        Unreachable is a first-class answer the lifecycle reads as "may be
        mid-turn behind a blip", and it never suspends on it. A raise here would
        instead end that crew's tick as a failure.
        """
        _install_ssm(monkeypatch, result)
        assert wiring.read_guest(_record(), profile="p", region="us-east-1") is None

    def test_the_last_json_line_wins(self, monkeypatch):
        """A login shell can print a banner before the command's own output, so
        the parse reads from the end rather than assuming line one."""
        _install_ssm(monkeypatch, _Result("Last login: whenever\n" + json.dumps({"ready": True})))
        assert wiring.read_guest(_record(), profile="p", region="us-east-1") is not None


class TestSealingBeforeAPack:
    def test_an_unsealed_crew_raises_so_the_pack_does_not_happen(self, monkeypatch):
        """The one place a raise is right.

        ``read_guest`` returns ``None`` for unreachable because the safe answer is
        to leave the crew alone. Here the safe answer is to STOP: packing a home
        whose writers are still running stores a torn database, which restores as
        a crew whose history ends mid-sentence or will not open.
        """
        _install_ssm(monkeypatch, _Result(json.dumps({"sealed": False, "reason": "timed out"})))
        with pytest.raises(RuntimeError, match="not sealed"):
            wiring.stop_gateway(_record(), profile="p", region="us-east-1")

    def test_a_sealed_crew_returns_quietly(self, monkeypatch):
        _install_ssm(monkeypatch, _Result(json.dumps({"sealed": True})))
        assert wiring.stop_gateway(_record(), profile="p", region="us-east-1") is None


class TestPacking:
    def test_the_recorded_etag_is_what_the_guest_is_given(self, monkeypatch):
        """The gateway's generation, passed in. A guest that re-read it would turn
        a compare-and-set into last-write-wins."""
        calls: list[dict] = []
        _install_ssm(monkeypatch, _Result(json.dumps({"etag": "e2"})), calls)
        wiring.pack_home(_record(), profile="p", region="us-east-1", kms_key_id="k")
        assert "etag-1" in calls[0]["command"]

    def test_a_pack_with_no_etag_back_is_refused(self, monkeypatch):
        """Without a new ETag the gateway has no generation to condition the next
        write on, so accepting it would break the next pack rather than this one."""
        _install_ssm(monkeypatch, _Result(json.dumps({"bytes": 10})))
        with pytest.raises(RuntimeError, match="no ETag"):
            wiring.pack_home(_record(), profile="p", region="us-east-1", kms_key_id="k")

    def test_the_new_etag_is_returned(self, monkeypatch):
        _install_ssm(monkeypatch, _Result(json.dumps({"etag": "e2", "bytes": 10})))
        got = wiring.pack_home(_record(), profile="p", region="us-east-1", kms_key_id="k")
        assert got == "e2"


class TestTheDependencySetIsComplete:
    def test_every_lifecycle_dependency_is_supplied(self, monkeypatch, tmp_path):
        """A set with one field left at its default is a tick that reaches a real
        AWS call where a test expected a fake, so completeness is the property."""
        from types import SimpleNamespace

        import kiro_crew.cloud.config as cloud_config_mod
        from kiro_crew.cloud.microvm.record import CrewStore

        monkeypatch.setattr(
            cloud_config_mod.CloudConfig,
            "load",
            staticmethod(lambda: SimpleNamespace(profile="p", region="us-east-1")),
        )
        config = SimpleNamespace(
            launch_spec=lambda: SimpleNamespace(endpoint_url="", kms_key_id="k")
        )
        deps = wiring.production_deps(config, store=CrewStore(tmp_path / "crews.json"))
        for field in (
            "read_guest",
            "stop_gateway",
            "pack_home",
            "suspend_vm",
            "resume_vm",
            "terminate_vm",
            "read_vm_status",
            "wait_terminated",
        ):
            assert callable(getattr(deps, field)), field


class TestMaintenanceRunsAsRoot:
    """``crew`` cannot do this work, for three independent reasons.

    The user is created with ``--shell /usr/sbin/nologin`` and
    ``ssm.run_command`` wraps everything in ``sudo -u <user> -i bash``, so the
    login shell exits before the verb runs. The guest-state and restore
    directories are created root-owned. And the SSM agent's ``ShareCreds``
    publishes the node's credentials to root's shared credentials file, so
    ``crew`` has no AWS identity for the archive's S3 put.

    This grants nothing new: the hook process already performs the same three
    operations as root from inside the VM, so running them as root remotely makes
    the two paths one identity. The crew's own processes stay unprivileged.
    """

    def test_the_ops_commands_run_as_root(self, monkeypatch):
        calls: list[dict] = []
        _install_ssm(monkeypatch, _Result(json.dumps({"ready": True})), calls)
        wiring.read_guest(_record(), profile="p", region="us-east-1")
        assert wiring.GUEST_RUN_AS == "root"
        assert calls[0]["run_as"] == "root"

    def test_the_crew_user_is_not_used_for_maintenance(self):
        """Named explicitly, because 'crew' is the plausible-looking wrong answer
        and the failure it produces names neither the shell nor the ownership."""
        assert wiring.GUEST_RUN_AS != "crew"


class TestTheGuestsOwnPackReachesTheRecord:
    """The guest packing itself is the DESIGNED path, so its result has to land.

    The control plane here is the owner's laptop and a laptop sleeps, which is
    why the guest arms its own wall watchdog. That pack happens on the guest's
    disk, so unless the ETag travels back the record keeps an empty one -- and
    then the next reopen tells the guest there is nothing to restore, the crew
    boots with an empty home, and its first pack's ``If-None-Match: *`` is
    refused against the archive that was there the whole time.
    """

    def test_the_etag_is_read_off_the_guests_answer(self, monkeypatch):
        _install_ssm(
            monkeypatch,
            _Result(json.dumps({"ready": True, "self_packed_etag": "etag-from-the-guest"})),
        )
        state = wiring.read_guest(_record(), profile="p", region="us-east-1")
        assert state is not None
        assert state.self_packed_etag == "etag-from-the-guest"

    def test_an_absent_field_reads_as_empty_rather_than_none(self, monkeypatch):
        """A guest too old to report one must not look like a crew whose archive
        vanished, so the absence is an empty string and the host leaves the
        recorded ETag alone."""
        _install_ssm(monkeypatch, _Result(json.dumps({"ready": True})))
        state = wiring.read_guest(_record(), profile="p", region="us-east-1")
        assert state is not None
        assert state.self_packed_etag == ""
