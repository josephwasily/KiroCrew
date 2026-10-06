"""The crew record store: what it keeps, what it refuses, and what it writes atomically."""

from __future__ import annotations

import json

import pytest

from kiro_crew.cloud.microvm import states
from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore


@pytest.fixture()
def store(tmp_path):
    return CrewStore(tmp_path / "microvm_crews.json")


class TestRoundTrip:
    def test_an_absent_file_reads_as_no_crews(self, store):
        assert store.load() == {}
        assert store.get("anything") is None

    def test_one_record_survives_a_round_trip(self, store):
        store.put(CrewRecord(tag="kc-abc123", microvm_id="mvm-1", archive_etag='"e1"'))
        back = store.get("kc-abc123")
        assert back is not None
        assert back.microvm_id == "mvm-1"
        assert back.archive_etag == '"e1"'

    def test_put_leaves_siblings_alone(self, store):
        store.put(CrewRecord(tag="a"))
        store.put(CrewRecord(tag="b"))
        store.put(CrewRecord(tag="a", microvm_id="mvm-a"))
        assert set(store.load()) == {"a", "b"}
        assert store.get("b").microvm_id == ""

    def test_delete_reports_whether_there_was_one(self, store):
        store.put(CrewRecord(tag="a"))
        assert store.delete("a") is True
        assert store.delete("a") is False

    def test_the_file_is_sorted_so_a_diff_is_readable(self, store):
        store.put(CrewRecord(tag="zeta"))
        store.put(CrewRecord(tag="alpha"))
        document = json.loads(store.path.read_text())
        assert [c["tag"] for c in document["crews"]] == ["alpha", "zeta"]


class TestTolerance:
    def test_a_corrupt_file_reads_as_no_crews(self, store):
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text("{not json")
        assert store.load() == {}

    def test_a_json_scalar_reads_as_no_crews(self, store):
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text('"hello"')
        assert store.load() == {}

    def test_one_unreadable_record_does_not_hide_the_others(self, store):
        """One bad crew must not take the owner's whole roster down."""
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "crews": [
                        {"tag": "good", "state": states.RUNNING},
                        {"tag": "bad", "wall_at": "not a number"},
                    ],
                }
            )
        )
        loaded = store.load()
        assert set(loaded) == {"good"}

    def test_an_oversized_file_reads_as_no_crews(self, store, monkeypatch):
        from kiro_crew.cloud.microvm import record as record_module

        monkeypatch.setattr(record_module, "_MAX_FILE_BYTES", 10)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text(json.dumps({"version": 1, "crews": []}) + " " * 100)
        assert store.load() == {}


class TestFieldTypes:
    def test_a_record_with_no_tag_is_refused(self):
        assert CrewRecord.from_json({"state": states.RUNNING}) is None

    def test_an_unknown_state_is_refused(self):
        """A state the edge table has no row for cannot be moved out of."""
        assert CrewRecord.from_json({"tag": "a", "state": "sleepy"}) is None

    def test_a_string_field_of_the_wrong_type_drops_the_record(self):
        assert CrewRecord.from_json({"tag": "a", "microvm_id": 7}) is None

    def test_a_boolean_is_not_a_count(self):
        """``bool`` is an ``int`` subclass, so ``true`` would read as one restart."""
        assert CrewRecord.from_json({"tag": "a", "restarts_seen": True}) is None

    def test_an_unknown_key_is_dropped_not_refused(self):
        """A document from a newer build must still load on an older one."""
        record = CrewRecord.from_json({"tag": "a", "something_new": 1})
        assert record is not None and record.tag == "a"

    def test_an_int_is_accepted_where_a_float_is_declared(self):
        record = CrewRecord.from_json({"tag": "a", "wall_at": 1700000000})
        assert record is not None and record.wall_at == 1700000000.0


class TestApplyEvent:
    def test_a_launch_creates_the_record(self, store):
        record = store.apply_event("kc-new", states.EVENT_LAUNCH_STARTED)
        assert record.state == states.PENDING
        assert store.get("kc-new") is not None

    def test_an_illegal_event_is_not_stored(self, store):
        store.put(CrewRecord(tag="a", state=states.STOPPED))
        with pytest.raises(states.IllegalTransition):
            store.apply_event("a", states.EVENT_SUSPENDED)
        assert store.get("a").state == states.STOPPED

    def test_the_state_and_its_facts_land_in_one_write(self, store):
        """The window between two writes is where an ETag is lost for good."""
        store.put(CrewRecord(tag="a", state=states.RUNNING))
        record = store.apply_event(
            "a", states.EVENT_PACK_OK, archive_etag='"new"', stopped_at=1234.0
        )
        assert record.state == states.STOPPED
        on_disk = store.get("a")
        assert on_disk.archive_etag == '"new"'
        assert on_disk.stopped_at == 1234.0


class TestCaps:
    def test_publishing_past_the_cap_is_refused(self, store, monkeypatch):
        from kiro_crew.cloud.microvm import record as record_module

        monkeypatch.setattr(record_module, "_MAX_RECORDS", 2)
        with pytest.raises(ValueError, match="cap is 2"):
            store.publish({str(i): CrewRecord(tag=str(i)) for i in range(3)})


class TestEffectiveState:
    def test_a_never_observed_crew_is_unknown(self):
        assert (
            CrewRecord(tag="a", state=states.RUNNING).effective_state(now=1000.0)
            == states.EFFECTIVE_UNKNOWN
        )

    def test_a_freshly_observed_crew_reports_its_state(self):
        record = CrewRecord(tag="a", state=states.RUNNING, last_observed_at=990.0)
        assert record.effective_state(now=1000.0) == states.RUNNING

    def test_evolve_does_not_mutate_the_original(self):
        record = CrewRecord(tag="a")
        other = record.evolve(microvm_id="mvm-1")
        assert record.microvm_id == ""
        assert other.microvm_id == "mvm-1"


class TestAWriteNeverErasesWhatItCouldNotRead:
    """An unreadable ledger must refuse the write, not publish one record over it.

    The tolerant read is right for a READER: raising would turn one bad byte into
    a dashboard showing nothing. It is wrong before a WRITE, because an
    unreadable file reads as ``{}``, and a writer that adds its one record to that
    publishes a store with every other crew missing -- taking with them the ETags
    their archives require. The archives survive and nothing can restore them.
    """

    def test_an_unparseable_store_refuses_a_put(self, tmp_path):
        from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore, CrewStoreUnreadable

        path = tmp_path / "crews.json"
        path.write_text("{ not json at all")
        with pytest.raises(CrewStoreUnreadable, match="not readable JSON"):
            CrewStore(path).put(CrewRecord(tag="newcrew"))

    def test_the_file_is_left_exactly_as_it_was(self, tmp_path):
        from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore, CrewStoreUnreadable

        path = tmp_path / "crews.json"
        path.write_text("{ not json at all")
        before = path.read_bytes()
        with pytest.raises(CrewStoreUnreadable):
            CrewStore(path).put(CrewRecord(tag="newcrew"))
        assert path.read_bytes() == before

    def test_an_unparseable_store_refuses_an_event(self, tmp_path):
        from kiro_crew.cloud.microvm.record import CrewStore, CrewStoreUnreadable

        path = tmp_path / "crews.json"
        path.write_text('{"crews": "not a list"}')
        with pytest.raises(CrewStoreUnreadable, match="not the shape"):
            CrewStore(path).apply_event("c", "launch_recorded")

    def test_an_unparseable_store_refuses_a_delete(self, tmp_path):
        from kiro_crew.cloud.microvm.record import CrewStore, CrewStoreUnreadable

        path = tmp_path / "crews.json"
        path.write_text("\x00\x01 binary")
        with pytest.raises(CrewStoreUnreadable):
            CrewStore(path).delete("c")

    def test_a_store_that_never_existed_is_the_one_true_empty(self, tmp_path):
        """A file that was never written genuinely holds no crews, so a first
        write must not be refused."""
        from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore

        store = CrewStore(tmp_path / "crews.json")
        assert store.put(CrewRecord(tag="first")).tag == "first"
        assert set(store.load()) == {"first"}

    def test_a_readable_store_still_writes_and_keeps_the_others(self, tmp_path):
        from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore

        store = CrewStore(tmp_path / "crews.json")
        store.put(CrewRecord(tag="one"))
        store.put(CrewRecord(tag="two"))
        assert set(store.load()) == {"one", "two"}

    def test_the_reader_stays_tolerant(self, tmp_path):
        """Unchanged, and deliberately: one bad byte must not blank the dashboard."""
        from kiro_crew.cloud.microvm.record import CrewStore

        path = tmp_path / "crews.json"
        path.write_text("{ not json at all")
        assert CrewStore(path).load() == {}


class TestAWriteRefusesAPartiallyUnreadableLedger:
    """One dropped record takes that crew's archive ETag with it.

    The tolerant read drops an entry it cannot parse and keeps the rest, which is
    right for a dashboard. A WRITE publishes exactly what loaded, so the dropped
    entry does not come back -- and what it held was the only thing that can read
    that crew's archive.
    """

    def test_one_unparseable_record_refuses_a_put(self, tmp_path):
        import json as _json

        from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore, CrewStoreUnreadable

        path = tmp_path / "crews.json"
        good = CrewStore(path)
        good.put(CrewRecord(tag="keeper"))
        document = _json.loads(path.read_text())
        document["crews"].append({"not": "a record"})
        path.write_text(_json.dumps(document))
        with pytest.raises(CrewStoreUnreadable, match="could not be parsed"):
            CrewStore(path).put(CrewRecord(tag="newcomer"))

    def test_the_keeper_is_still_on_disk_afterwards(self, tmp_path):
        import json as _json

        from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore, CrewStoreUnreadable

        path = tmp_path / "crews.json"
        CrewStore(path).put(CrewRecord(tag="keeper", archive_etag="etag-keeper"))
        document = _json.loads(path.read_text())
        document["crews"].append({"not": "a record"})
        path.write_text(_json.dumps(document))
        with pytest.raises(CrewStoreUnreadable):
            CrewStore(path).put(CrewRecord(tag="newcomer"))
        assert "etag-keeper" in path.read_text()

    def test_the_reader_still_shows_the_readable_ones(self, tmp_path):
        """Unchanged: one unreadable crew must not hide the others."""
        import json as _json

        from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore

        path = tmp_path / "crews.json"
        CrewStore(path).put(CrewRecord(tag="keeper"))
        document = _json.loads(path.read_text())
        document["crews"].append({"not": "a record"})
        path.write_text(_json.dumps(document))
        assert set(CrewStore(path).load()) == {"keeper"}
