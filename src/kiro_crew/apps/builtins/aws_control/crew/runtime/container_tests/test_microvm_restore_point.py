"""Whether there is an archive to restore is told to the guest, not guessed.

The guest's AWS identity holds ``s3:GetObject`` on its own crew prefix and
deliberately not ``s3:ListBucket``. S3 answers a GET for a key that does not
exist with ``AccessDenied`` rather than ``NoSuchKey`` unless the caller may list,
so the error code cannot distinguish a crew that has never been packed from a
grant that is broken.

A guest that read "no archive yet" out of the error code therefore got it wrong
in the one direction that matters: every fresh crew's FIRST launch -- the normal
case, since no crew has an archive before its first pack -- ended at
``failed:restore`` with the supervisor never started.

So the launcher states it. It is the only party that can: the ledger holds the
crew's archive ETag, and whether that is set is whether a pack has ever
completed. These tests pin both halves of that contract -- the skip when there
is no restore point, and the refusal to step over one that is expected.
"""

from __future__ import annotations

import sys
import types

import pytest
from container.microvm import hooks

BUCKET = "the-bucket"
KEY = "crews/demo/home.tar.gz"
REGION = "us-east-1"


class _Boom(Exception):
    """Stands in for ``botocore``'s ``ClientError`` carrying a chosen code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def _install_failing_client(monkeypatch: pytest.MonkeyPatch, code: str) -> list[str]:
    """Point the guest's late-imported AWS modules at a client that fails.

    ``restore_data_home`` imports ``boto3`` and ``botocore`` INSIDE the function
    -- deliberate, because only that path needs an AWS client -- so the stand-ins
    go into ``sys.modules``, which is what a late import actually reads. Patching
    an attribute on the module would leave the real import untouched.

    Returns the list the fake records calls in, so a caller can assert that no
    call was made at all.
    """
    calls: list[str] = []

    class _Client:
        def get_object(self, **kwargs: object) -> object:
            calls.append("get_object")
            raise _Boom(code)

    boto3_module = types.ModuleType("boto3")
    boto3_module.client = lambda *a, **k: _Client()  # type: ignore[attr-defined]

    config_module = types.ModuleType("botocore.config")
    config_module.Config = lambda **k: object()  # type: ignore[attr-defined]

    exceptions_module = types.ModuleType("botocore.exceptions")
    exceptions_module.ClientError = _Boom  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "boto3", boto3_module)
    monkeypatch.setitem(sys.modules, "botocore.config", config_module)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exceptions_module)
    return calls


class TestALaunchWithNoRestorePoint:
    """``expected=False`` means the launcher said this crew has no archive."""

    def test_nothing_is_fetched_at_all(self, tmp_path, monkeypatch):
        """Not even a call.

        A fetch that cannot succeed is not worth making, and on this grant its
        failure is indistinguishable from a real fault -- which is how the bug
        happened. Asserted against a client that records its calls, so this
        measures the absence rather than assuming it.
        """
        calls = _install_failing_client(monkeypatch, "AccessDenied")
        result = hooks.restore_data_home(BUCKET, KEY, "", REGION, str(tmp_path), expected=False)
        assert result["restored"] is False
        assert calls == []

    def test_the_reason_says_the_launch_carried_no_restore_point(self, tmp_path):
        """A reason a reader can tell apart from a failure. This is the normal
        first launch, so it must not read like something went wrong."""
        result = hooks.restore_data_home(BUCKET, KEY, "", REGION, str(tmp_path), expected=False)
        assert result == {"restored": False, "reason": "the launch carried no restore point"}


class TestALaunchThatExpectsAnArchive:
    """``expected=True`` means the ledger believes an archive exists."""

    @pytest.mark.parametrize("code", ["AccessDenied", "NoSuchKey", "PreconditionFailed", "403"])
    def test_every_way_of_failing_to_read_it_is_fatal(self, code, tmp_path, monkeypatch):
        """Including ``NoSuchKey``.

        The launcher already said the archive exists, so a missing object means
        the ledger and the bucket disagree about this crew's history. Booting
        empty over that would serve a crew's conversations as absent and then
        overwrite them on the first turn. ``AccessDenied`` is in the list because
        it is what a real fresh-key GET returns on this grant, so reading it as
        "no archive yet" is exactly the mistake this parametrization forbids.
        """
        _install_failing_client(monkeypatch, code)
        with pytest.raises(RuntimeError) as caught:
            hooks.restore_data_home(BUCKET, KEY, "etag-1", REGION, str(tmp_path), expected=True)
        message = str(caught.value)
        assert "could not be read" in message
        assert code in message

    def test_the_refusal_names_the_object_it_could_not_read(self, tmp_path, monkeypatch):
        """The operator's next step is to look at that object, so name it."""
        _install_failing_client(monkeypatch, "AccessDenied")
        with pytest.raises(RuntimeError) as caught:
            hooks.restore_data_home(BUCKET, KEY, "etag-1", REGION, str(tmp_path), expected=True)
        assert f"s3://{BUCKET}/{KEY}" in str(caught.value)

    def test_missing_coordinates_are_reported_rather_than_guessed(self, tmp_path, monkeypatch):
        """An expected archive with no bucket or key is a launcher bug. The guest
        reports absence rather than inventing a location to fetch."""
        calls = _install_failing_client(monkeypatch, "AccessDenied")
        result = hooks.restore_data_home("", "", "etag-1", REGION, str(tmp_path), expected=True)
        assert result["restored"] is False
        assert calls == []


class TestTheDefault:
    def test_expecting_an_archive_is_the_default(self):
        """So a caller that omits the argument gets the strict behaviour, and the
        silent skip is never reached by forgetting to ask for it."""
        import inspect

        assert inspect.signature(hooks.restore_data_home).parameters["expected"].default is True


class TestTheModelCredentialReference:
    """The guest reads the reference it was GIVEN, never one it derives.

    Both secrets were once named from the launch tag. That tag is minted by the
    launcher rather than chosen by the operator -- a launch with no tag is given
    ``kc-<random hex>`` -- so a derived path named a secret nobody could have
    created, and the read of it ended every launch at its secrets stage with the
    VM billing to its wall and serving nothing.
    """

    def test_the_derived_helper_is_gone(self):
        """Its absence is the fix. A helper left in place is one a later change
        falls back to, and the fallback IS the failure."""
        assert not hasattr(hooks, "identity_ref_beside")

    def test_a_payload_with_no_reference_is_refused_by_name(self, monkeypatch, tmp_path):
        """Refused rather than defaulted.

        The operator's remedy is a config value, so the error names it. Falling
        back to a derived path would restore the original failure and report it as
        a missing secret instead of a missing setting.

        The stages before this one are stubbed, so what the test measures is the
        identity gate and not the host's AWS credentials.
        """
        monkeypatch.setattr(hooks, "wait_for_shared_credentials", lambda timeout=0: True)
        monkeypatch.setattr(hooks, "restore_data_home", lambda *a, **k: {"restored": False})
        monkeypatch.setattr(hooks, "read_secret", lambda ref, region: "stub-secret")
        monkeypatch.setattr(hooks, "write_state", lambda path, data: None)
        monkeypatch.setenv("SMC_DATA_HOME", str(tmp_path))

        hooks._boot.clear()
        hooks.boot_crew({"region": "us-east-1", "secretRef": "kirocrew/crew/c/CONTROL_SECRET"})
        assert hooks._boot.get("stage") == "failed:secrets"
        assert "identity_secret_ref" in str(hooks._boot.get("error", ""))

    def test_a_payload_with_a_reference_reaches_the_supervisor(self, monkeypatch, tmp_path):
        """The positive half: a configured reference is the one read, verbatim."""
        read: "list[str]" = []
        monkeypatch.setattr(hooks, "wait_for_shared_credentials", lambda timeout=0: True)
        monkeypatch.setattr(hooks, "restore_data_home", lambda *a, **k: {"restored": False})

        def _read(ref, region):
            read.append(ref)
            return "stub-secret"

        monkeypatch.setattr(hooks, "read_secret", _read)
        monkeypatch.setattr(hooks, "write_state", lambda path, data: None)
        monkeypatch.setattr(hooks, "spawn", lambda *a, **k: None)
        monkeypatch.setattr(hooks, "arm_wall_watchdog", lambda *a, **k: None)
        monkeypatch.setenv("SMC_DATA_HOME", str(tmp_path))

        hooks._boot.clear()
        hooks.boot_crew(
            {
                "region": "us-east-1",
                "secretRef": "kirocrew/crew/c/CONTROL_SECRET",
                "identityRef": "kirocrew/identity/chosen-by-the-operator",
            }
        )
        assert hooks._boot.get("stage") == "started", hooks._boot.get("error")
        assert "kirocrew/identity/chosen-by-the-operator" in read
