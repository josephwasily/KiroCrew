"""The guest's slot probe authenticates, and its header is the backend's own.

``running_slots`` asks the backend how many chat slots are mid-turn. The wall
watchdog reads the answer to decide whether the SOFT pack edge may fire -- and
reads a failure as BUSY, deliberately, because packing mid-turn archives a
transcript the crew is still writing.

That fail-safe has a cost if the probe can never succeed: the soft edge never
fires, so every pack lands at the HARD edge, which is the one that does not wait
for a turn to finish. An unauthenticated probe is exactly that case, because the
backend requires a token on every route and does not exempt loopback -- a proxy
in front of it can make remote traffic appear local.

``hooks.py`` runs as PID 1 and deliberately imports nothing from the crew's own
package tree, so it spells the header and the file name itself. These tests are
what keep the two spellings equal.
"""

from __future__ import annotations

import http.client
import io
import urllib.error
import urllib.request

import pytest
from container import common
from container.microvm import hooks


class TestTheSpellingsMatchTheBackend:
    """``hooks.py`` cannot import ``common``, so the agreement is pinned here."""

    def test_the_header_name_is_the_backends_own(self):
        assert hooks.BACKEND_SECRET_HEADER == common.HEADER

    def test_the_secret_file_name_is_the_backends_own(self, tmp_path):
        """Compared against ``common.secret_path`` rather than restated, so a
        rename there fails here instead of returning this probe to being
        unauthenticated."""
        port = 8765
        expected = common.secret_path(tmp_path, port)
        got = tmp_path / hooks.BACKEND_SECRET_FILE.format(port=port)
        assert got == expected


class TestTheHeaderIsRead:
    @pytest.fixture()
    def run_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SMC_BACKEND_RUN_DIR", str(tmp_path))
        monkeypatch.setenv("SMC_BACKEND_PORT", "8765")
        return tmp_path

    def test_a_written_secret_becomes_the_header(self, run_dir):
        common.secret_path(run_dir, 8765).write_text("s3cret\n", encoding="utf-8")
        assert hooks._backend_auth_header() == {common.HEADER: "s3cret"}

    def test_the_value_is_stripped(self, run_dir):
        """The backend writes a trailing newline, and a header carrying one is a
        header the backend does not match."""
        common.secret_path(run_dir, 8765).write_text("  s3cret  \n", encoding="utf-8")
        assert hooks._backend_auth_header()[common.HEADER] == "s3cret"

    def test_the_port_selects_the_file(self, tmp_path, monkeypatch):
        """One run directory can hold more than one backend's secret."""
        monkeypatch.setenv("SMC_BACKEND_RUN_DIR", str(tmp_path))
        monkeypatch.setenv("SMC_BACKEND_PORT", "9999")
        common.secret_path(tmp_path, 9999).write_text("nine", encoding="utf-8")
        common.secret_path(tmp_path, 8765).write_text("eight", encoding="utf-8")
        assert hooks._backend_auth_header()[common.HEADER] == "nine"

    @pytest.mark.parametrize("state", ["missing", "empty"])
    def test_an_unreadable_secret_yields_no_header_rather_than_raising(self, run_dir, state):
        """The caller's contract already turns an unreadable probe into BUSY.

        Raising here would move the same outcome to a different layer and a less
        diagnosable error -- the request is refused and ``running_slots`` raises
        there, which is where the fail-safe lives.
        """
        if state == "empty":
            common.secret_path(run_dir, 8765).write_text("", encoding="utf-8")
        assert hooks._backend_auth_header() == {}

    def test_the_secret_is_read_per_call_and_never_cached(self, run_dir):
        """The backend rotates it. A cached value is the failure the secret
        module exists to prevent."""
        path = common.secret_path(run_dir, 8765)
        path.write_text("first", encoding="utf-8")
        assert hooks._backend_auth_header()[common.HEADER] == "first"
        path.write_text("second", encoding="utf-8")
        assert hooks._backend_auth_header()[common.HEADER] == "second"


class TestTheProbeSendsIt:
    def test_the_request_carries_the_header(self, tmp_path, monkeypatch):
        """The property that matters: the header reaches the wire, not merely
        that it can be computed."""
        monkeypatch.setenv("SMC_BACKEND_RUN_DIR", str(tmp_path))
        monkeypatch.setenv("SMC_BACKEND_PORT", "8765")
        common.secret_path(tmp_path, 8765).write_text("s3cret", encoding="utf-8")

        seen: dict[str, object] = {}

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b'{"slots": [{"running": true}, {"running": false}]}'

        class _Opener:
            def open(self, request, timeout=0):
                seen["url"] = request.full_url
                seen["header"] = request.get_header(common.HEADER.capitalize())
                return _Response()

        monkeypatch.setattr(hooks, "_no_redirect_opener", lambda: _Opener())
        assert hooks.running_slots() == 1
        assert seen["header"] == "s3cret"
        assert str(seen["url"]).startswith("http://127.0.0.1:")

    def test_the_opener_refuses_a_redirect_rather_than_following_it(self):
        """The reason this is an opener and not a bare ``urlopen``.

        A default opener follows a 3xx and re-sends the request's headers to
        wherever it points, so a loopback call answered with a redirect carries
        the backend's secret off the machine. The gateway has
        ``loopback_http.loopback_urlopen`` for this; the guest cannot import it,
        so it builds the same refusal and this is what proves it did.
        """
        opener = hooks._no_redirect_opener()
        handler = next(
            h for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)
        )
        request = urllib.request.Request("http://127.0.0.1:8765/api/chat/slots")
        with pytest.raises(urllib.error.HTTPError):
            handler.redirect_request(
                request,
                io.BytesIO(b""),
                302,
                "Found",
                http.client.HTTPMessage(),
                "http://evil.example/collect",
            )

    def test_a_refused_probe_still_raises_so_the_watchdog_reads_busy(self, tmp_path, monkeypatch):
        """The fail-safe is unchanged by authenticating: a probe that cannot be
        read is BUSY, never idle, so the soft edge does not pack mid-turn."""
        monkeypatch.setenv("SMC_BACKEND_RUN_DIR", str(tmp_path))

        class _Refusing:
            def open(self, request, timeout=0):
                raise OSError("403")

        # Patched on the OPENER, not on ``urlopen``: the probe goes through the
        # no-redirect opener, so patching ``urlopen`` would leave the real call to
        # run and the test would pass on a connection error instead of on this.
        monkeypatch.setattr(hooks, "_no_redirect_opener", lambda: _Refusing())
        with pytest.raises(OSError):
            hooks.running_slots()
