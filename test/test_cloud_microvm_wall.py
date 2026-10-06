"""The wall: a crew must not reach the platform's terminate with its home unarchived.

That sentence is the invariant, and the last class in this file is the test of it.
Everything above establishes the pieces the invariant rests on.

Every clock here is fake, and it has to be. The property under test is what
happens at hour seven of an eight-hour lifetime; no real-time test reaches it, so
a watchdog whose only proof was a live run would be a watchdog nobody had ever
seen fire.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from kiro_crew.cloud.microvm.pack import ARCHIVE_DENYLIST as HOST_DENYLIST
from kiro_crew.cloud.microvm.payload import RunHookPayload, compute_wall_leads

#: The guest package, loaded under a name of our own. The repo's ``test/``
#: directory is not a package and the standard library has one of the same name,
#: so a plain absolute import depends on what else the shard imported first.
_GUEST = "kirocrew_crew_container"
_GUEST_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "apps"
    / "builtins"
    / "aws_control"
    / "crew"
    / "runtime"
    / "container"
)


def _load_guest():
    if _GUEST not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            _GUEST, _GUEST_DIR / "__init__.py", submodule_search_locations=[str(_GUEST_DIR)]
        )
        assert spec and spec.loader, f"the container package is not at {_GUEST_DIR}"
        module = importlib.util.module_from_spec(spec)
        sys.modules[_GUEST] = module
        spec.loader.exec_module(module)
    return importlib.import_module(f"{_GUEST}.microvm.watchdog")


wd = _load_guest()


class Clock:
    """A clock a test drives, and a sleep that advances it.

    ``sleep`` MOVES the clock rather than blocking, so ``run_forever`` runs its
    real loop -- the same code a VM runs -- over seven simulated hours in
    milliseconds. A test that patched the loop instead would prove the test's
    loop, not the product's.
    """

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start
        self.slept = 0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds
        self.slept += 1


class Packer:
    """Records every pack, so "packed once" is checkable rather than assumed."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.fail = fail

    def __call__(self, edge: str) -> dict:
        self.calls.append(edge)
        if self.fail is not None:
            raise self.fail
        return {"bytes": 1181179, "etag": "etag-after-pack"}


def _dog(tmp_path, clock, packer, *, slots=lambda: 0, wall=28_800):
    leads = compute_wall_leads(wall)
    return wd.WallWatchdog(
        started_at=clock.t,
        soft_at=leads.soft_at,
        hard_at=leads.hard_at,
        wall_seconds=wall,
        pack=packer,
        running_slots=slots,
        state_path=str(tmp_path / "wall.json"),
        now=clock.now,
        sleep=clock.sleep,
    )


class TestTheEdgesAreTheLanesOwnEdges:
    def test_the_leads_come_from_the_launchers_own_function(self):
        """One definition of the edges, so the guest and the launcher cannot
        disagree about when a crew packs."""
        leads = compute_wall_leads(28_800)
        assert 0 < leads.soft_at < leads.hard_at < 28_800

    def test_an_unordered_pair_is_refused(self, tmp_path):
        """A watchdog with the edges crossed either never fires or fires after the
        VM is gone, and both are silent."""
        with pytest.raises(ValueError, match="not ordered inside"):
            wd.WallWatchdog(
                started_at=0.0,
                soft_at=900,
                hard_at=600,
                wall_seconds=1800,
                pack=Packer(),
                running_slots=lambda: 0,
                state_path=str(tmp_path / "w.json"),
            )

    def test_the_wall_block_the_launcher_writes_arms_a_watchdog(self, tmp_path):
        """Built through the launcher's own encoder, not by hand: a rename of a
        wire key on either side fails here instead of shipping a VM whose
        watchdog silently never armed."""
        payload = RunHookPayload(
            tag="wallcrew",
            activation_id="9f806f70-cffe-447e-97d4-e41e7f61b253",
            activation_code="0123456789abcdefghij",
            region="us-east-1",
            control_secret_ref="kirocrew/crew/wallcrew/CONTROL_SECRET",
            identity_secret_ref="kirocrew/identity/wallcrew",
            archive_bucket="kirocrew-microvm-archive-123456789012-us-east-1",
            archive_key="crews/wallcrew/home.tar.gz",
            archive_etag="",
            archive_restore=False,
            kms_key_id="",
            wall=compute_wall_leads(1800),
            generation=1,
        )
        body = json.loads(payload.encode())
        dog = wd.from_payload(
            body,
            started_at=0.0,
            pack=Packer(),
            running_slots=lambda: 0,
            state_path=str(tmp_path / "w.json"),
        )
        assert dog is not None, "the launcher's own wall block did not arm a watchdog"
        assert dog.soft_deadline < dog.hard_deadline < dog.wall

    def test_a_payload_with_no_wall_block_arms_nothing(self, tmp_path):
        assert (
            wd.from_payload(
                {},
                started_at=0.0,
                pack=Packer(),
                running_slots=lambda: 0,
                state_path=str(tmp_path / "w.json"),
            )
            is None
        )


class TestTheSoftEdgeWaitsForATurnBoundary:
    def test_nothing_happens_before_the_soft_edge(self, tmp_path):
        clock, packer = Clock(), Packer()
        dog = _dog(tmp_path, clock, packer)
        assert dog.run_once() == "waiting"
        assert packer.calls == []

    def test_a_crew_mid_turn_is_not_packed_at_the_soft_edge(self, tmp_path):
        """A pack mid-turn archives a transcript the crew is still writing."""
        clock, packer = Clock(), Packer()
        dog = _dog(tmp_path, clock, packer, slots=lambda: 1)
        clock.t = dog.soft_deadline
        assert dog.run_once() == "waiting-for-turn-boundary"
        assert packer.calls == []

    def test_an_idle_crew_is_packed_at_the_soft_edge(self, tmp_path):
        clock, packer = Clock(), Packer()
        dog = _dog(tmp_path, clock, packer)
        clock.t = dog.soft_deadline
        assert dog.run_once() == "packed-soft"
        assert packer.calls == ["soft"]

    def test_a_probe_that_cannot_answer_counts_as_busy(self, tmp_path):
        """A probe that reported "idle" on failure would pack mid-turn on no
        information. Unreadable means wait, and the hard edge is behind it."""

        def broken() -> int:
            raise RuntimeError("the backend is not answering")

        clock, packer = Clock(), Packer()
        dog = _dog(tmp_path, clock, packer, slots=broken)
        clock.t = dog.soft_deadline
        assert dog.run_once() == "waiting-for-turn-boundary"
        assert packer.calls == []


class TestTheHardEdgePacksRegardless:
    def test_a_crew_still_mid_turn_is_packed_at_the_hard_edge(self, tmp_path):
        """A transcript archived mid-turn beats no transcript at all, because what
        it is compared against is losing the whole home."""
        clock, packer = Clock(), Packer()
        dog = _dog(tmp_path, clock, packer, slots=lambda: 3)
        clock.t = dog.hard_deadline
        assert dog.run_once() == "packed-hard"
        assert packer.calls == ["hard"]

    def test_the_hard_edge_is_checked_before_the_soft_branch(self, tmp_path):
        """Taking the soft branch past the hard deadline would let a crew in a
        long turn wait out the wall, which is the one outcome ruled out."""
        clock, packer = Clock(), Packer()
        dog = _dog(tmp_path, clock, packer, slots=lambda: 1)
        clock.t = dog.hard_deadline + 60
        assert dog.run_once() == "packed-hard"

    def test_the_probe_is_not_even_consulted_at_the_hard_edge(self, tmp_path):
        def must_not_be_called() -> int:
            raise AssertionError("the hard edge does not ask whether the crew is busy")

        clock, packer = Clock(), Packer()
        dog = _dog(tmp_path, clock, packer, slots=must_not_be_called)
        clock.t = dog.hard_deadline
        assert dog.run_once() == "packed-hard"


class TestItPacksExactlyOnce:
    def test_a_second_pass_after_a_pack_does_nothing(self, tmp_path):
        clock, packer = Clock(), Packer()
        dog = _dog(tmp_path, clock, packer)
        clock.t = dog.soft_deadline
        assert dog.run_once() == "packed-soft"
        clock.t = dog.hard_deadline + 1
        assert dog.run_once() == "already-packed"
        assert packer.calls == ["soft"]

    def test_a_fresh_watchdog_reads_the_pack_off_disk(self, tmp_path):
        """A resume restarts this loop with the same payload, so the flag has to
        survive the process. Packing twice is not merely wasteful: the second
        pack's precondition names an ETag the first one replaced."""
        clock, packer = Clock(), Packer()
        first = _dog(tmp_path, clock, packer)
        clock.t = first.soft_deadline
        first.run_once()
        again = _dog(tmp_path, clock, packer)
        assert again.packed()
        assert again.run_once() == "already-packed"
        assert packer.calls == ["soft"]

    def test_a_conflict_is_recorded_and_never_retried(self, tmp_path):
        """Another writer holds the archive, so this crew's home is in someone's
        archive and a retry would overwrite theirs."""
        clock = Clock()
        packer = Packer(fail=wd.PackConflict("another writer holds it"))
        dog = _dog(tmp_path, clock, packer)
        clock.t = dog.soft_deadline
        assert dog.run_once() == "packed-soft"
        clock.t = dog.hard_deadline
        assert dog.run_once() == "already-packed"
        assert packer.calls == ["soft"]

    def test_an_ordinary_failure_is_retried_on_the_next_pass(self, tmp_path):
        """Unlike a conflict: nothing else holds the archive, so another pass is
        the right answer and the hard edge is still ahead."""
        clock = Clock()
        packer = Packer(fail=RuntimeError("s3 timed out"))
        dog = _dog(tmp_path, clock, packer)
        clock.t = dog.soft_deadline
        assert dog.run_once() == "pack-failed"
        assert not dog.packed()
        assert dog.run_once() == "pack-failed"
        assert packer.calls == ["soft", "soft"]

    def test_the_state_file_names_which_edge_packed(self, tmp_path):
        clock, packer = Clock(), Packer()
        dog = _dog(tmp_path, clock, packer, slots=lambda: 1)
        clock.t = dog.hard_deadline
        dog.run_once()
        stored = json.loads((tmp_path / "wall.json").read_text())
        assert stored["packed"] is True
        assert stored["edge"] == "hard"


class TestTheGuestArchiveMatchesTheHosts:
    def test_guest_denylist_matches_the_host(self):
        """The guest's list is a COPY -- ``kiro_crew.cloud`` is not in the crew
        image -- so this is what stops the two archives from differing in what
        they leave out."""
        assert wd.ARCHIVE_DENYLIST == HOST_DENYLIST


class TestTheInvariant:
    """No path lets a launched crew reach the platform's terminate unarchived.

    The reviewers' sentence, as a test. Each case walks a fake clock from launch
    past both edges to the wall and asserts the home was archived before the wall
    arrived.
    """

    @pytest.mark.parametrize("wall", [1800, 7200, 28_800])
    def test_an_idle_crew_is_archived_before_the_wall(self, tmp_path, wall):
        clock, packer = Clock(), Packer()
        dog = _dog(tmp_path, clock, packer, wall=wall)
        dog.run_forever()
        assert packer.calls == ["soft"]
        assert clock.now() < dog.wall, "the pack happened after the platform's terminate"

    @pytest.mark.parametrize("wall", [1800, 7200, 28_800])
    def test_a_crew_busy_for_its_whole_life_is_archived_before_the_wall(self, tmp_path, wall):
        """The case the soft edge alone cannot save: a crew that never reaches a
        turn boundary. The hard edge is what makes this bounded."""
        clock, packer = Clock(), Packer()
        dog = _dog(tmp_path, clock, packer, slots=lambda: 1, wall=wall)
        dog.run_forever()
        assert packer.calls == ["hard"]
        assert clock.now() < dog.wall

    def test_a_crew_whose_probe_is_broken_is_still_archived(self, tmp_path):
        def broken() -> int:
            raise RuntimeError("the backend never came up")

        clock, packer = Clock(), Packer()
        dog = _dog(tmp_path, clock, packer, slots=broken)
        dog.run_forever()
        assert packer.calls == ["hard"]
        assert clock.now() < dog.wall

    def test_a_crew_that_goes_idle_late_is_archived_at_that_boundary(self, tmp_path):
        """Busy across the soft edge, idle before the hard one: it packs at the
        boundary rather than waiting for the hard edge."""
        clock, packer = Clock(), Packer()
        state = {"busy": True}
        dog = _dog(tmp_path, clock, packer, slots=lambda: 1 if state["busy"] else 0)
        clock.t = dog.soft_deadline
        assert dog.run_once() == "waiting-for-turn-boundary"
        state["busy"] = False
        dog.run_forever()
        assert packer.calls == ["soft"]
        assert clock.now() < dog.hard_deadline

    def test_the_loop_stops_at_the_wall_rather_than_running_unbounded(self, tmp_path):
        """A watchdog whose pack keeps failing must stop claiming it is working."""
        clock = Clock()
        packer = Packer(fail=RuntimeError("s3 is gone"))
        dog = _dog(tmp_path, clock, packer, wall=1800)
        assert dog.run_forever() == "pack-failed"
        assert not dog.packed()
        assert clock.now() >= dog.wall

    def test_the_watchdog_is_armed_on_the_boot_path(self):
        """The invariant depends on something CALLING it. Asserted against the
        boot module's source, because importing it needs boto3 and a VM."""
        hooks = (_GUEST_DIR / "microvm" / "hooks.py").read_text(encoding="utf-8")
        assert "arm_wall_watchdog(payload" in hooks, "nothing arms the watchdog at boot"
        assert "def arm_wall_watchdog" in hooks
        assert "threading.Thread(target=dog.run_forever" in hooks


class TestSealingAfterThePack:
    """A packed crew stops serving, or the turns after the pack are lost.

    The archive is a snapshot of a moment. A crew that keeps answering past it
    writes transcripts that are in no archive, and the VM's remaining life is
    minutes -- so those turns are answered, believed, and then taken with the disk.
    """

    def test_a_soft_pack_seals_the_crew(self, tmp_path):
        clock, packer = Clock(), Packer()
        sealed: list[int] = []
        leads = compute_wall_leads(1800)
        dog = wd.WallWatchdog(
            started_at=clock.t,
            soft_at=leads.soft_at,
            hard_at=leads.hard_at,
            wall_seconds=1800,
            pack=packer,
            running_slots=lambda: 0,
            state_path=str(tmp_path / "w.json"),
            seal=lambda: sealed.append(1),
            now=clock.now,
            sleep=clock.sleep,
        )
        clock.t = dog.soft_deadline
        assert dog.run_once() == "packed-soft"
        assert sealed == [1]

    def test_a_hard_pack_seals_the_crew(self, tmp_path):
        clock, packer = Clock(), Packer()
        sealed: list[int] = []
        leads = compute_wall_leads(1800)
        dog = wd.WallWatchdog(
            started_at=clock.t,
            soft_at=leads.soft_at,
            hard_at=leads.hard_at,
            wall_seconds=1800,
            pack=packer,
            running_slots=lambda: 5,
            state_path=str(tmp_path / "w.json"),
            seal=lambda: sealed.append(1),
            now=clock.now,
            sleep=clock.sleep,
        )
        clock.t = dog.hard_deadline
        assert dog.run_once() == "packed-hard"
        assert sealed == [1]

    def test_the_seal_happens_BEFORE_the_pack(self, tmp_path):
        """A turn boundary says no slot is RUNNING, not that the backend finished
        writing: a transcript flush and a SQLite checkpoint outlive the turn. So
        the writers stop first and the snapshot is of a quiesced home."""
        order: list[str] = []
        clock = Clock()
        leads = compute_wall_leads(1800)
        dog = wd.WallWatchdog(
            started_at=clock.t,
            soft_at=leads.soft_at,
            hard_at=leads.hard_at,
            wall_seconds=1800,
            pack=lambda edge: order.append("pack") or {"etag": "e"},
            running_slots=lambda: 0,
            state_path=str(tmp_path / "w.json"),
            seal=lambda: order.append("seal"),
            now=clock.now,
            sleep=clock.sleep,
        )
        clock.t = dog.soft_deadline
        dog.run_once()
        assert order == ["seal", "pack"]

    def test_a_failed_pack_still_leaves_the_crew_sealed(self):
        """The safe direction: the VM is minutes from its wall either way, and a
        crew still taking turns it cannot archive is the loss being prevented."""
        import tempfile

        with tempfile.TemporaryDirectory() as scratch:
            clock = Clock()
            packer = Packer(fail=RuntimeError("s3 timed out"))
            sealed: list[int] = []
            leads = compute_wall_leads(1800)
            dog = wd.WallWatchdog(
                started_at=clock.t,
                soft_at=leads.soft_at,
                hard_at=leads.hard_at,
                wall_seconds=1800,
                pack=packer,
                running_slots=lambda: 0,
                state_path=f"{scratch}/w.json",
                seal=lambda: sealed.append(1),
                now=clock.now,
                sleep=clock.sleep,
            )
            clock.t = dog.soft_deadline
            assert dog.run_once() == "pack-failed"
            assert sealed == [1]

    def test_a_seal_that_fails_does_not_undo_the_pack(self, tmp_path):
        """The home IS archived, which is the property that matters. Treating a
        successful pack as failed would pack again over it."""

        def broken() -> None:
            raise RuntimeError("pkill is not on PATH")

        clock, packer = Clock(), Packer()
        leads = compute_wall_leads(1800)
        dog = wd.WallWatchdog(
            started_at=clock.t,
            soft_at=leads.soft_at,
            hard_at=leads.hard_at,
            wall_seconds=1800,
            pack=packer,
            running_slots=lambda: 0,
            state_path=str(tmp_path / "w.json"),
            seal=broken,
            now=clock.now,
            sleep=clock.sleep,
        )
        clock.t = dog.soft_deadline
        assert dog.run_once() == "packed-soft"
        assert dog.packed()

    def test_the_boot_path_passes_a_seal(self):
        hooks = (_GUEST_DIR / "microvm" / "hooks.py").read_text(encoding="utf-8")
        assert "seal=_seal" in hooks, "the watchdog is armed without a seal"
        assert "container.supervisor" in hooks


class TestTheGuestPackRefusesABrokenArchive:
    """Read from the source, because running it needs boto3, tar and a VM.

    The property is about which outcomes are REFUSED, and a truncated archive
    published as a restore point is worse than a failed pack: a failed pack
    leaves the previous archive intact and the next pass retries.
    """

    @staticmethod
    def _source() -> str:
        return (_GUEST_DIR / "microvm" / "hooks.py").read_text(encoding="utf-8")

    def test_tars_exit_code_is_read(self):
        source = self._source()
        assert "completed.returncode not in (0, 1)" in source, "tar's exit status is not checked"

    def test_an_empty_archive_is_refused(self):
        assert "tar produced an empty archive" in self._source()

    def test_the_crew_name_comes_from_the_bundle_not_the_tag(self):
        """The supervisor compares the name it is given against the bundle's
        manifest and refuses a mismatch, and a launch tag is not the crew's
        name."""
        source = self._source()
        assert "SMC_CREW_NAME=bundle_crew_name()" in source
        assert "def bundle_crew_name" in source


class TestTarIsNotTrustedOnASignal:
    """A tar the kernel stopped must not become a restore point.

    Read from the source, because running it needs tar, boto3 and a VM. The
    property is which exit statuses are ACCEPTED, and the dangerous one is
    negative: a signal-killed tar reports -15 or -9, and a `> 1` comparison lets
    every negative number through.
    """

    def test_only_zero_and_one_are_accepted(self):
        source = (_GUEST_DIR / "microvm" / "hooks.py").read_text(encoding="utf-8")
        assert "completed.returncode not in (0, 1)" in source
        assert (
            "completed.returncode > 1" not in source
        ), "a comparison accepts a signal's negative status"


class TestTheSealWaitsForTheWritersToGo:
    """A signal is a request, not an exit.

    The supervisor and the backend flush transcripts and checkpoint SQLite on the
    way down. Archiving while that is in flight captures a torn database, which
    restores as a crew whose history ends mid-sentence or will not open at all --
    and the whole point of sealing before the pack is that the writers have
    stopped. Only a confirmed exit says they have.

    Read from the source, because running it needs a VM with real processes.
    """

    @staticmethod
    def _source() -> str:
        return (_GUEST_DIR / "microvm" / "hooks.py").read_text(encoding="utf-8")

    def test_it_polls_for_the_exit_rather_than_assuming_it(self):
        source = self._source()
        assert (
            "pkill" in source and '"-0"' in source
        ), "nothing checks whether the crew's processes actually went"
        assert "SEAL_TIMEOUT_SECONDS" in source

    def test_the_wait_is_bounded_and_ends_in_a_kill(self):
        """Unbounded would mean a supervisor that ignores TERM holds the pack off
        past the wall, which loses the home entirely."""
        source = self._source()
        assert '"-KILL"' in source
        assert "wall.seal_timeout" in source

    def test_the_bound_leaves_room_for_the_pack(self):
        """The seal's budget has to fit inside the gap between the two edges, or a
        soft-edge seal eats the window the pack needs."""
        import re as _re

        source = self._source()
        match = _re.search(r"SEAL_TIMEOUT_SECONDS = (\d+)", source)
        assert match, "the seal has no stated budget"
        budget = int(match.group(1))
        leads = compute_wall_leads(1800)
        assert budget < leads.hard_at - leads.soft_at
