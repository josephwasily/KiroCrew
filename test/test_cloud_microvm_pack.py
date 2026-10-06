"""The archive: its conditional-write lineage, its key guard, and what goes in it."""

from __future__ import annotations

import pytest

from kiro_crew.cloud.aws import AWSError
from kiro_crew.cloud.microvm import pack


class TestPutCondition:
    def test_the_first_pack_of_a_crews_life_requires_absence(self):
        ref = pack.ArchiveRef(bucket="b", key="crews/a/home.tar.gz")
        assert ref.put_condition() == ["--if-none-match", "*"]

    def test_every_later_pack_names_the_etag_it_saw(self):
        ref = pack.ArchiveRef(bucket="b", key="crews/a/home.tar.gz", etag='"e1"')
        assert ref.put_condition() == ["--if-match", '"e1"']

    def test_there_is_no_unconditional_form(self):
        """An unconditional put is last-write-wins, which is what this prevents."""
        for etag in ("", '"e1"'):
            condition = pack.ArchiveRef(bucket="b", key="k", etag=etag).put_condition()
            assert condition and condition[0].startswith("--if-")


class TestConditionalWriteHandling:
    def test_a_412_raises_pack_conflict_and_does_not_retry(self, monkeypatch):
        calls = []

        def fake(args, profile="", region="", *, action="", timeout=0):
            calls.append(action)
            raise AWSError("An error occurred (PreconditionFailed)", action=action)

        monkeypatch.setattr(pack, "checked_json", fake)
        ref = pack.ArchiveRef(bucket="b", key="crews/a/home.tar.gz", etag='"old"')
        with pytest.raises(pack.PackConflict) as exc:
            pack.put_archive(ref, "/dev/null")
        assert exc.value.held_etag == '"old"'
        assert len(calls) == 1, "a refused conditional write must not be retried"

    def test_a_bare_412_status_is_recognised(self, monkeypatch):
        def fake(args, profile="", region="", *, action="", timeout=0):
            raise AWSError("An error occurred (412) when calling PutObject", action=action)

        monkeypatch.setattr(pack, "checked_json", fake)
        with pytest.raises(pack.PackConflict):
            pack.put_archive(pack.ArchiveRef(bucket="b", key="k"), "/dev/null")

    def test_any_other_error_is_not_a_conflict(self, monkeypatch):
        def fake(args, profile="", region="", *, action="", timeout=0):
            raise AWSError("An error occurred (AccessDenied)", action=action)

        monkeypatch.setattr(pack, "checked_json", fake)
        with pytest.raises(AWSError):
            pack.put_archive(pack.ArchiveRef(bucket="b", key="k"), "/dev/null")

    def test_a_successful_put_returns_the_new_etag(self, monkeypatch):
        monkeypatch.setattr(
            pack,
            "checked_json",
            lambda *a, **k: {"ETag": '"new-etag"'},
        )
        etag = pack.put_archive(pack.ArchiveRef(bucket="b", key="k"), "/dev/null")
        assert etag == '"new-etag"'

    def test_a_put_that_returns_no_etag_is_a_failed_pack(self, monkeypatch):
        """A pack with no ETag leaves the next write with no condition to send."""
        monkeypatch.setattr(pack, "checked_json", lambda *a, **k: {})
        with pytest.raises(pack.PackRefused, match="no ETag"):
            pack.put_archive(pack.ArchiveRef(bucket="b", key="k"), "/dev/null")

    def test_the_kms_key_is_named_on_the_put(self, monkeypatch):
        """The bucket policy denies a put that does not name the key."""
        captured: list[list[str]] = []

        def fake(args, profile="", region="", *, action="", timeout=0):
            captured.append(args)
            return {"ETag": '"e"'}

        monkeypatch.setattr(pack, "checked_json", fake)
        pack.put_archive(
            pack.ArchiveRef(bucket="b", key="k", kms_key_id="arn:aws:kms:::key/x"),
            "/dev/null",
        )
        assert "--ssekms-key-id" in captured[0]
        assert "aws:kms" in captured[0]

    def test_a_restore_whose_etag_disagrees_is_refused(self, monkeypatch):
        def fake(args, profile="", region="", *, action="", timeout=0):
            raise AWSError("An error occurred (PreconditionFailed)", action=action)

        monkeypatch.setattr(pack, "checked_json", fake)
        with pytest.raises(pack.PackConflict):
            pack.get_archive(pack.ArchiveRef(bucket="b", key="k", etag='"recorded"'), "/tmp/out")


class TestKeyGuard:
    def test_a_crews_key_is_derived_from_its_tag(self):
        assert pack.archive_key_for("kc-abc") == "crews/kc-abc/home.tar.gz"

    def test_a_key_without_a_tag_is_refused(self):
        with pytest.raises(ValueError):
            pack.archive_key_for("")

    def test_a_mismatched_key_is_refused_rather_than_redirected(self):
        """The ETag condition cannot catch this: a first write to an unused key
        succeeds under ``If-None-Match: *`` exactly as a legitimate one does."""
        ref = pack.ArchiveRef(bucket="b", key="crews/other/home.tar.gz")
        with pytest.raises(pack.PackRefused, match="refusing rather than redirecting"):
            pack.assert_key_matches(ref, "kc-abc")

    def test_the_matching_key_passes(self):
        ref = pack.ArchiveRef(bucket="b", key=pack.archive_key_for("kc-abc"))
        pack.assert_key_matches(ref, "kc-abc")


class TestArchiveShape:
    def test_the_tar_uses_a_builtin_compressor(self):
        """The crew image installs no external compressor, so ``--zstd`` dies at exec."""
        argv = pack.archive_argv("/home/kirocrew/.kiro/crew")
        assert "-czf" in argv
        assert not any("zstd" in part for part in argv)

    def test_every_denylisted_name_is_excluded_anchored_at_the_root(self):
        argv = pack.archive_argv("/h")
        for name in pack.ARCHIVE_DENYLIST:
            assert f"./{name}" in argv, name

    def test_the_credentials_and_the_socket_are_excluded(self):
        """A restored crew must not inherit a dead generation's listener secret."""
        assert "run" in pack.ARCHIVE_DENYLIST
        assert ".local_secret" in pack.ARCHIVE_DENYLIST
        assert "dashboard.sock" in pack.ARCHIVE_DENYLIST

    def test_the_embedding_model_is_excluded(self):
        assert "models" in pack.ARCHIVE_DENYLIST

    def test_the_lock_file_is_deliberately_not_excluded(self):
        """Excluding it deletes the crash-loop ladder structurally."""
        assert "gateway.lock" not in pack.ARCHIVE_DENYLIST

    def test_the_archive_set_carries_both_session_halves_and_their_join(self):
        """Export's set excludes these three, and for this lane they ARE the crew."""
        for required in ("sessions", "session_map.json", "uploads", "artifacts"):
            assert required in pack.ARCHIVE_PATHS, required

    def test_the_sqlite_sidecars_are_in_the_set(self):
        """A db without its -wal restores EMPTY and still passes integrity_check."""
        assert "memory.db-wal" in pack.ARCHIVE_PATHS
        assert "memory_index.db-wal" in pack.ARCHIVE_PATHS

    def test_a_complete_member_list_reports_nothing_missing(self):
        members = {f"./{name}" for name in pack.ARCHIVE_PATHS}
        assert pack.missing_archive_members(members) == ()

    def test_a_directory_member_covers_its_children(self):
        members = {"./sessions/a.jsonl"}
        assert "sessions" not in pack.missing_archive_members(members)

    def test_a_dropped_path_is_named(self):
        members = {f"./{n}" for n in pack.ARCHIVE_PATHS if n != "uploads"}
        assert pack.missing_archive_members(members) == ("uploads",)


class TestCompressorGuard:
    def test_a_present_program_is_not_reported_missing(self):
        assert pack.missing_compressor("sh") is False

    def test_an_absent_program_is_reported(self):
        assert pack.missing_compressor("definitely-not-a-real-binary-xyz") is True


class TestRetention:
    def test_fourteen_days_is_the_window(self):
        assert pack.ARCHIVE_RETENTION_SECONDS == 14 * 86_400

    def test_a_crew_inside_the_window_is_not_due(self):
        assert not pack.expiry_due(1_000_000.0, now=1_000_000.0 + 13 * 86_400)

    def test_a_crew_past_the_window_is_due(self):
        assert pack.expiry_due(1_000_000.0, now=1_000_000.0 + 15 * 86_400)

    def test_a_crew_that_never_stopped_is_never_due(self):
        assert not pack.expiry_due(0.0, now=10**12)


class TestEncryptionHeaderIsAlwaysSent:
    """Every put names an algorithm, because the bucket's deny reads the HEADER.

    Measured against a real SSE-S3 bucket: a put with no encryption header at all
    is refused by the bucket's own deny-unencrypted-put statement, because that
    statement tests the request header and a bucket default does not set one. moto
    accepts a headerless put, so the local harness could not see this.

    So the question is never whether to send the header, only which algorithm it
    names.
    """

    @pytest.fixture()
    def captured(self, monkeypatch):
        calls: list[list[str]] = []

        def fake(args, profile="", region="", *, action="", timeout=0):
            calls.append(list(args))
            return {"ETag": '"e"'}

        monkeypatch.setattr(pack, "checked_json", fake)
        return calls

    def test_a_cmk_bucket_names_the_key(self, captured):
        pack.put_archive(
            pack.ArchiveRef(bucket="b", key="k", kms_key_id="arn:aws:kms:::key/x"), "/dev/null"
        )
        argv = captured[0]
        assert argv[argv.index("--server-side-encryption") + 1] == "aws:kms"
        assert argv[argv.index("--ssekms-key-id") + 1] == "arn:aws:kms:::key/x"

    def test_a_bucket_with_no_key_still_names_an_algorithm(self, captured):
        """The bug: with no key the lane sent no header, and the put was denied."""
        pack.put_archive(pack.ArchiveRef(bucket="b", key="k"), "/dev/null")
        argv = captured[0]
        assert "--server-side-encryption" in argv
        assert argv[argv.index("--server-side-encryption") + 1] == "AES256"

    def test_a_keyless_put_names_no_kms_key(self, captured):
        pack.put_archive(pack.ArchiveRef(bucket="b", key="k"), "/dev/null")
        assert "--ssekms-key-id" not in captured[0]

    @pytest.mark.parametrize("kms_key_id", ["", "arn:aws:kms:::key/x"])
    def test_there_is_no_path_that_sends_no_header(self, captured, kms_key_id):
        """Both branches, so a future edit cannot reintroduce the headerless put."""
        pack.put_archive(pack.ArchiveRef(bucket="b", key="k", kms_key_id=kms_key_id), "/dev/null")
        assert "--server-side-encryption" in captured[-1]

    def test_the_conditional_write_survives_the_header(self, captured):
        """The condition is what makes the archive safe; the header must not displace it."""
        pack.put_archive(pack.ArchiveRef(bucket="b", key="k", etag='"e1"'), "/dev/null")
        assert "--if-match" in captured[0]
        assert "--server-side-encryption" in captured[0]


class TestGetArchiveSuccess:
    """The 412 path is pinned elsewhere; this is the restore that lands bytes."""

    def test_a_successful_get_returns_the_served_etag(self, monkeypatch):
        monkeypatch.setattr(pack, "checked_json", lambda *a, **k: {"ETag": '"served"'})
        etag = pack.get_archive(pack.ArchiveRef(bucket="b", key="k"), "/tmp/out")
        assert etag == '"served"'

    def test_a_recorded_etag_is_sent_as_if_match(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(
            pack, "checked_json", lambda args, *a, **k: calls.append(list(args)) or {"ETag": '"e"'}
        )
        pack.get_archive(pack.ArchiveRef(bucket="b", key="k", etag='"recorded"'), "/tmp/out")
        argv = calls[0]
        assert argv[argv.index("--if-match") + 1] == '"recorded"'

    def test_the_endpoint_url_is_passed_to_the_harness(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(
            pack, "checked_json", lambda args, *a, **k: calls.append(list(args)) or {"ETag": '"e"'}
        )
        pack.get_archive(
            pack.ArchiveRef(bucket="b", key="k"), "/tmp/out", endpoint_url="http://127.0.0.1:9"
        )
        argv = calls[0]
        assert argv[argv.index("--endpoint-url") + 1] == "http://127.0.0.1:9"


class TestPutArchiveEndpoint:
    def test_the_endpoint_url_is_passed_to_the_harness(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(
            pack, "checked_json", lambda args, *a, **k: calls.append(list(args)) or {"ETag": '"e"'}
        )
        pack.put_archive(
            pack.ArchiveRef(bucket="b", key="k"), "/dev/null", endpoint_url="http://127.0.0.1:9"
        )
        argv = calls[0]
        assert argv[argv.index("--endpoint-url") + 1] == "http://127.0.0.1:9"


class TestHeadArchive:
    def test_the_metadata_is_returned_when_the_object_exists(self, monkeypatch):
        monkeypatch.setattr(
            pack, "checked_json", lambda *a, **k: {"ContentLength": 54_000, "ETag": '"e"'}
        )
        data = pack.head_archive(pack.ArchiveRef(bucket="b", key="k"))
        assert data is not None
        assert data["ContentLength"] == 54_000

    def test_a_missing_object_reports_none(self, monkeypatch):
        def fake(args, profile="", region="", *, action="", timeout=0):
            raise AWSError("An error occurred (404) Not Found", action=action)

        monkeypatch.setattr(pack, "checked_json", fake)
        assert pack.head_archive(pack.ArchiveRef(bucket="b", key="k")) is None

    def test_a_no_such_key_reports_none(self, monkeypatch):
        def fake(args, profile="", region="", *, action="", timeout=0):
            raise AWSError("NoSuchKey", action=action)

        monkeypatch.setattr(pack, "checked_json", fake)
        assert pack.head_archive(pack.ArchiveRef(bucket="b", key="k")) is None

    def test_any_other_error_propagates(self, monkeypatch):
        def fake(args, profile="", region="", *, action="", timeout=0):
            raise AWSError("AccessDenied", action=action)

        monkeypatch.setattr(pack, "checked_json", fake)
        with pytest.raises(AWSError):
            pack.head_archive(pack.ArchiveRef(bucket="b", key="k"))

    def test_a_non_object_answer_reports_none(self, monkeypatch):
        monkeypatch.setattr(pack, "checked_json", lambda *a, **k: ["nope"])
        assert pack.head_archive(pack.ArchiveRef(bucket="b", key="k")) is None

    def test_the_endpoint_url_is_passed_to_the_harness(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(
            pack, "checked_json", lambda args, *a, **k: calls.append(list(args)) or {}
        )
        pack.head_archive(pack.ArchiveRef(bucket="b", key="k"), endpoint_url="http://127.0.0.1:9")
        argv = calls[0]
        assert argv[argv.index("--endpoint-url") + 1] == "http://127.0.0.1:9"


class TestDeleteArchive:
    def test_it_names_the_object_to_delete(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(
            pack, "checked_json", lambda args, *a, **k: calls.append(list(args)) or {}
        )
        pack.delete_archive(pack.ArchiveRef(bucket="b", key="crews/a/home.tar.gz"))
        argv = calls[0]
        assert "delete-object" in argv
        assert argv[argv.index("--bucket") + 1] == "b"
        assert argv[argv.index("--key") + 1] == "crews/a/home.tar.gz"

    def test_the_endpoint_url_is_passed_to_the_harness(self, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(
            pack, "checked_json", lambda args, *a, **k: calls.append(list(args)) or {}
        )
        pack.delete_archive(pack.ArchiveRef(bucket="b", key="k"), endpoint_url="http://127.0.0.1:9")
        argv = calls[0]
        assert argv[argv.index("--endpoint-url") + 1] == "http://127.0.0.1:9"


class TestSummariseArchive:
    def test_no_archive_summarises_as_a_plain_phrase(self):
        assert pack.summarise_archive(None) == "no archive"

    def test_an_archive_summary_carries_its_size_and_unquoted_etag(self):
        import json

        line = pack.summarise_archive({"ContentLength": 54_000, "ETag": '"abc"'})
        assert json.loads(line) == {"bytes": 54_000, "etag": "abc"}
