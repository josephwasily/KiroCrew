"""The three operations the gateway's lifecycle tick runs inside the guest.

``container.microvm.ops`` is the guest half of one seam; the gateway half is
``kiro_crew/cloud/microvm/wiring.py``, which invokes these verbs over SSM and
parses one line of JSON off stdout. So the properties worth pinning here are the
ones that seam depends on: the answer is JSON on stdout, a failure is JSON on
stdout too rather than a bare traceback, and the idle number is safe on its first
reading.

The verdicts are NOT here. Whether a crew should be suspended is the gateway's
decision, in ``lifecycle.idle_verdict``; this module only reports.
"""

from __future__ import annotations

import json

import pytest
from container.microvm import ops


@pytest.fixture()
def idle_state(tmp_path, monkeypatch):
    """Redirect the guest's idle mark into a temp file.

    Requested by name rather than autouse. An autouse fixture that patches
    through the shared ``monkeypatch`` is lifted by any test calling
    ``monkeypatch.undo()``, and this suite has no independently-undone
    ``_floor_monkeypatch`` to request instead -- so the dependency is explicit.
    """
    path = tmp_path / "idle.json"
    monkeypatch.setattr(ops, "IDLE_STATE", str(path))
    return path


class TestTheIdleMark:
    """``idle_for_seconds`` is persisted, because one observation cannot answer
    "how long has this been idle"."""

    def test_the_first_reading_is_zero_even_with_no_slots(self, idle_state):
        """The safe direction, and the one that matters most.

        A guest whose idle history is gone -- a fresh boot, a resume, a cleared
        state dir -- must not be suspended on its first poll for having no record
        of ever being busy. Zero means "not idle long enough", so the crew lives.
        """
        assert ops.observe_idle(0, now=1_000.0) == 0.0

    def test_idleness_accumulates_from_the_mark(self, idle_state):
        ops.observe_idle(0, now=1_000.0)
        assert ops.observe_idle(0, now=1_060.0) == pytest.approx(60.0)

    def test_a_running_slot_resets_the_mark(self, idle_state):
        """A crew that took a turn is not idle, and its clock starts over."""
        ops.observe_idle(0, now=1_000.0)
        assert ops.observe_idle(1, now=1_060.0) == 0.0
        assert ops.observe_idle(0, now=1_090.0) == pytest.approx(30.0)

    def test_the_mark_survives_a_separate_invocation(self, idle_state):
        """Each poll is its own process over SSM, so the number has to live on
        disk rather than in memory."""
        ops.observe_idle(0, now=1_000.0)
        assert ops.observe_idle(0, now=1_120.0) == pytest.approx(120.0)

    def test_an_unreadable_mark_reads_as_fresh_rather_than_as_idle(self, idle_state):
        """A corrupt state file is the same situation as a missing one."""
        idle_state.write_text("{not json", encoding="utf-8")
        assert ops.observe_idle(0, now=1_000.0) == 0.0


class TestTheStateVerb:
    def test_an_unreadable_slot_probe_reports_busy_and_not_ready(self, monkeypatch, idle_state):
        """The whole reason ``running_slots`` raises instead of returning zero.

        A maintenance pass that read a failed probe as "idle" would suspend a
        crew mid-turn, so a probe that cannot be read is reported as one running
        slot. ``ready`` goes false with it, so the gateway also stops offering
        the crew a turn.
        """
        monkeypatch.setattr(
            ops.hooks, "running_slots", lambda: (_ for _ in ()).throw(RuntimeError("no route"))
        )
        state = ops.guest_state()
        assert state["running_slots"] == 1
        assert state["ready"] is False
        assert state["idle_for_seconds"] == 0.0

    def test_a_crew_that_has_not_started_is_not_ready(self, monkeypatch, tmp_path, idle_state):
        """Readiness is the boot stage AND the probe, because a crew whose boot
        stopped at its secrets stage can still answer a slots route."""
        monkeypatch.setattr(ops.hooks, "running_slots", lambda: 0)
        boot = tmp_path / "boot.json"
        boot.write_text(json.dumps({"stage": "failed:secrets"}), encoding="utf-8")
        monkeypatch.setattr(ops.hooks, "BOOT_STATE", str(boot))
        assert ops.guest_state()["ready"] is False

    def test_a_started_crew_with_a_readable_probe_is_ready(self, monkeypatch, tmp_path, idle_state):
        monkeypatch.setattr(ops.hooks, "running_slots", lambda: 0)
        boot = tmp_path / "boot.json"
        boot.write_text(json.dumps({"stage": "started", "generation": 3}), encoding="utf-8")
        monkeypatch.setattr(ops.hooks, "BOOT_STATE", str(boot))
        state = ops.guest_state()
        assert state["ready"] is True
        assert state["generation"] == 3


class TestTheCliContract:
    """What the gateway's parser depends on."""

    def test_the_state_verb_prints_one_json_object(self, monkeypatch, capsys, idle_state):
        monkeypatch.setattr(ops.hooks, "running_slots", lambda: 0)
        assert ops.main(["state"]) == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert set(payload) == {
            "ready",
            "running_slots",
            "idle_for_seconds",
            "restarts",
            "generation",
            "self_pack_armed",
            "self_packed_etag",
        }

    def test_a_failure_is_json_on_stdout_and_a_nonzero_exit(self, monkeypatch, capsys, idle_state):
        """The gateway's only channel is the command's output, so an exception
        that reached stderr alone would read as an empty answer rather than as a
        failure."""
        monkeypatch.setattr(ops, "guest_state", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert ops.main(["state"]) == 1
        payload = json.loads(capsys.readouterr().out.strip())
        assert "boom" in payload["error"]

    def test_a_missing_verb_is_refused_rather_than_defaulted(self):
        """The three differ in what they may DO, so guessing which was meant is
        guessing whether this invocation may act."""
        with pytest.raises(SystemExit):
            ops.main([])

    def test_an_unknown_verb_is_refused(self):
        with pytest.raises(SystemExit):
            ops.main(["bogus"])

    def test_the_pack_verb_requires_its_coordinates(self):
        """A pack with no bucket or key would archive nowhere."""
        with pytest.raises(SystemExit):
            ops.main(["pack"])

    def test_the_pack_verb_passes_the_gateways_etag_through(self, monkeypatch, capsys, idle_state):
        """The ETag is the gateway's generation, not something the guest re-reads:
        a guest that read it again would turn a compare-and-set into
        last-write-wins."""
        seen: dict[str, object] = {}

        def fake_pack(**kwargs):
            seen.update(kwargs)
            return {"etag": "new-etag", "bytes": 10}

        monkeypatch.setattr(ops.hooks, "pack_data_home", fake_pack)
        code = ops.main(
            [
                "pack",
                "--bucket",
                "b",
                "--key",
                "crews/demo/home.tar.gz",
                "--region",
                "us-east-1",
                "--etag",
                "old-etag",
            ]
        )
        assert code == 0
        assert seen["etag"] == "old-etag"
        assert json.loads(capsys.readouterr().out.strip())["etag"] == "new-etag"


class TestTheSealVerb:
    def test_an_unconfirmed_exit_is_reported_rather_than_assumed(self, monkeypatch):
        """A signal is a request, not an exit.

        The supervisor and the backend flush transcripts and checkpoint SQLite on
        the way down, and archiving while that is in flight stores a torn
        database. So an exit that cannot be confirmed is said out loud, and the
        gateway decides -- it refuses to pack.
        """
        monkeypatch.setattr(ops.hooks, "SEAL_TIMEOUT_SECONDS", 0)
        monkeypatch.setattr(ops.subprocess, "run", lambda *a, **k: None)
        result = ops.seal()
        assert result["sealed"] is False
        assert "did not exit" in result["reason"]

    def test_a_confirmed_exit_is_sealed(self, monkeypatch):
        import subprocess as real_subprocess

        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            if args[0] == "pgrep":
                return real_subprocess.CompletedProcess(args, 1, b"", b"")
            return real_subprocess.CompletedProcess(args, 0, b"", b"")

        monkeypatch.setattr(ops.subprocess, "run", fake_run)
        assert ops.seal() == {"sealed": True}
        assert calls[0][0] == "pkill", "the gateway was never asked to stop"
        assert any(c[0] == "pgrep" for c in calls), "the exit was never confirmed"


class TestTheSelfPackEtagIsReported:
    """The host's only channel to the ETag the guest's own wall pack produced.

    Read out of the watchdog's own state file rather than tracked here, so that
    fact has one writer and this is only a reader of it.
    """

    @pytest.fixture()
    def wall(self, tmp_path, monkeypatch):
        path = tmp_path / "wall.json"
        monkeypatch.setattr(ops.hooks, "WATCHDOG_STATE", str(path))
        return path

    def test_a_completed_pack_reports_its_etag(self, wall, idle_state, monkeypatch):
        monkeypatch.setattr(ops.hooks, "running_slots", lambda: 0)
        wall.write_text(
            json.dumps({"packed": True, "edge": "soft", "result": {"etag": "e-77"}}),
            encoding="utf-8",
        )
        assert ops.guest_state()["self_packed_etag"] == "e-77"

    def test_a_conflicted_pack_reports_nothing(self, wall, idle_state, monkeypatch):
        """The conflict path records no ETag, and inventing one would hand the
        host a generation that is not the archive's."""
        monkeypatch.setattr(ops.hooks, "running_slots", lambda: 0)
        wall.write_text(
            json.dumps({"packed": True, "edge": "hard", "result": {"conflict": True}}),
            encoding="utf-8",
        )
        assert ops.guest_state()["self_packed_etag"] == ""

    def test_an_unpacked_crew_reports_nothing(self, wall, idle_state, monkeypatch):
        monkeypatch.setattr(ops.hooks, "running_slots", lambda: 0)
        wall.write_text(json.dumps({"packed": False}), encoding="utf-8")
        assert ops.guest_state()["self_packed_etag"] == ""

    def test_a_missing_state_file_reports_nothing(self, idle_state, monkeypatch, tmp_path):
        monkeypatch.setattr(ops.hooks, "running_slots", lambda: 0)
        monkeypatch.setattr(ops.hooks, "WATCHDOG_STATE", str(tmp_path / "absent.json"))
        assert ops.guest_state()["self_packed_etag"] == ""
