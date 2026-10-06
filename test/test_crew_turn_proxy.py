"""The headless-crew turn proxy: what it refuses, and what it never discloses.

``POST /api/instances/{id}/crew-turn`` exists because a headless crew has no
dashboard to embed, so the hub has to be the crew's chat client. That puts a
credential in the hub's hands, and these tests pin the three properties that
follow from it rather than the forwarding mechanics:

* the crew's control secret reaches the CREW and nothing else -- not the
  response, not an error message, not a log line;
* the route refuses before it reaches for the secret at all when the crew is not
  a connected headless crew, so a misdirected turn never causes a secret read;
* "headless" is decided from the provisioner, not from the connection method,
  because the EC2 lane and the MicroVM lane share ``ssm`` and only one of them is
  headless.

Unit-level on purpose: the live end-to-end path is exercised against a real crew
in a real MicroVM, and what a test can add is the refusal matrix, which a live
run gets to see only one row of.
"""

from __future__ import annotations

import pytest

from kiro_crew.cloud.microvm import states as states_mod
from kiro_crew.dashboard import handlers_crew_turn as mod


def test_a_disconnected_crew_has_no_turn_target():
    """No target means the route refuses before any secret is read."""
    assert mod._turn_target({"state": "disconnected", "local_port": 18081}, "microvm") == ""
    assert mod._turn_target({"state": "connecting", "local_port": 18081}, "microvm") == ""


def test_a_connected_gateway_crew_has_no_turn_target():
    """A full-gateway crew is embedded, not chatted with through this route.

    The EC2 lane reaches its crews with ``connection_method="ssm"`` -- the same
    method the MicroVM lane uses -- and its crews DO serve a dashboard. So a
    connected instance with a local port and no headless provisioner must not
    produce a target: posting a crew turn at a gateway's own port is a request
    that fails in a way nobody can read.
    """
    status = {"state": "connected", "local_port": 18081}
    assert mod._turn_target(status, "") == ""
    assert mod._turn_target(status, "ec2") == ""


def test_a_connected_headless_crew_gets_a_loopback_target():
    status = {"state": "connected", "local_port": 18081}
    for provisioner in sorted(mod.HEADLESS_CREW_PROVISIONERS):
        target = mod._turn_target(status, provisioner)
        assert target == "http://127.0.0.1:18081/v1/chat/completions", provisioner


def test_the_far_ends_own_turn_url_wins_over_a_composed_one():
    """``turn_url`` is the far end's statement about itself, so it is preferred."""
    status = {
        "state": "connected",
        "local_port": 18081,
        "turn_url": "http://127.0.0.1:19999/v1/chat/completions",
    }
    assert mod._turn_target(status, "microvm") == "http://127.0.0.1:19999/v1/chat/completions"


def test_headless_is_true_before_the_crew_is_connected():
    """Or the pane never appears, and the user has no way to reach what connects it."""
    assert mod.is_headless_crew({"state": "disconnected"}, "microvm") is True
    assert mod.is_headless_crew({"state": "disconnected"}, "") is False


def test_the_secret_path_is_the_one_the_deploy_wrote():
    """Derived from the crew name, never supplied by the caller.

    A caller-supplied secret path would let any request name a secret for the
    gateway to read with the owner's credentials and send to an endpoint of the
    caller's choosing -- which is a read-any-secret primitive wearing a chat
    route's clothes.
    """
    assert (
        mod._SECRET_PATH.format(prefix="kirocrew/crew", crew="l2crew")
        == "kirocrew/crew/l2crew/CONTROL_SECRET"
    )
    from kiro_crew.cloud.microvm.engine import MicroVmLaunchSpec

    spec = MicroVmLaunchSpec(
        image_identifier="arn:aws:lambda:us-east-1:1:microvm-image:x",
        image_version="1.0",
        archive_bucket="b",
        kms_key_id="",
    )
    # The lane's launcher and this route must name the SAME secret, or the hub
    # authenticates with a value the guest never read.
    assert spec.control_secret_name("l2crew") == mod._SECRET_PATH.format(
        prefix="kirocrew/crew", crew="l2crew"
    )


def test_the_control_header_matches_the_containers_own_constant():
    """One definition, asserted rather than copied by eye.

    The container is a separate source tree that ships inside an image, so a
    rename there cannot break this import -- which is exactly why the agreement
    needs a test instead of a shared symbol.
    """
    import importlib.util
    import pathlib

    config = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src/kiro_crew/apps/builtins/aws_control/crew/runtime/container/common/config.py"
    )
    spec = importlib.util.spec_from_file_location("_container_config_probe", config)
    assert spec is not None and spec.loader is not None
    text = config.read_text()
    # Read as source rather than imported: the container package has its own
    # import root and loading it here would pull its dependencies.
    assert f'CONTROL_SECRET_HEADER = "{mod.CONTROL_SECRET_HEADER}"' in text


@pytest.mark.parametrize(
    "body,code",
    [
        ({}, "bad_request"),
        ({"message": "", "thread": "t"}, "bad_request"),
        ({"message": "hi"}, "bad_request"),
        ({"message": "hi", "thread": ""}, "bad_request"),
        ({"message": "hi", "thread": "t", "stream": "yes"}, "bad_request"),
        ({"message": "x" * (mod._MAX_MESSAGE_CHARS + 1), "thread": "t"}, "message_too_large"),
    ],
)
def test_the_body_contract_is_refused_by_shape(body, code):
    """Shape refusals happen before the crew is looked up, let alone the secret.

    Checked through the module's own validation order rather than a live request:
    what matters is that an unusable body cannot reach the secret read, and the
    order of the checks in ``api_crew_turn`` is what guarantees it.
    """
    import inspect

    source = inspect.getsource(mod.api_crew_turn)
    body_checks = source.index("message must be a non-empty string")
    secret_read = source.index("_read_control_secret")
    assert body_checks < secret_read, (
        "the body contract must be checked before the control secret is read, or a "
        "malformed request causes a credential read"
    )
    # And every refusal above is a code this route actually produces.
    assert code in source or code == "bad_request"


class TestAHeadlessCrewIsNotMintedFor:
    """A crew with no dashboard has no dashboard token.

    Skipping the mint on the `fargate` TRANSPORT is not enough. A MicroVM crew is
    reached over `ssm`, the same method the EC2 lane uses, so a transport-keyed
    skip lets it through to the mint -- which fails against a crew serving no
    dashboard. The connect path then returns an error status and tears down the
    forward it had already installed, so the pane reports a crew that will not
    answer about a forward that is fine.

    The decision is a property of the CREW, which is why it keys on the
    provisioner id.
    """

    def test_the_two_headless_lanes_are_recognised(self):
        from kiro_crew.instances.registry import is_headless_provisioner

        assert is_headless_provisioner("microvm")
        assert is_headless_provisioner("aws_fargate")

    def test_a_crew_with_a_dashboard_is_not_headless(self):
        """The EC2 lane shares the `ssm` method and DOES run a full gateway, so a
        transport-keyed answer would be wrong in both directions."""
        from kiro_crew.instances.registry import is_headless_provisioner

        assert not is_headless_provisioner("aws_ec2")
        assert not is_headless_provisioner("builtin")
        assert not is_headless_provisioner("")

    def test_the_registry_and_the_lanes_agree_on_the_ids(self):
        """One set, asserted against the lanes' own constants, so a rename is a
        failure rather than a crew the registry stops recognising."""
        from kiro_crew.cloud.microvm.engine import MICROVM_PROVISIONER_ID
        from kiro_crew.instances.registry import HEADLESS_CREW_PROVISIONERS
        from kiro_crew.platform.defaults import FARGATE_PROVISIONER_ID

        assert HEADLESS_CREW_PROVISIONERS == {FARGATE_PROVISIONER_ID, MICROVM_PROVISIONER_ID}

    def test_the_pane_and_the_tunnel_read_the_same_set(self):
        from kiro_crew.dashboard import handlers_crew_turn
        from kiro_crew.instances.registry import HEADLESS_CREW_PROVISIONERS

        assert handlers_crew_turn.HEADLESS_CREW_PROVISIONERS is HEADLESS_CREW_PROVISIONERS

    def test_the_mint_is_skipped_on_headlessness_not_on_transport(self):
        """Read from the source: the condition is what decides a live connect, and
        that seam is owned elsewhere, so the change is one condition per site."""
        import pathlib

        import kiro_crew.instances.ssh_tunnel_manager as mod

        source = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        assert source.count("not is_headless_provisioner(") == 2, (
            "both the connect mint and the self-heal prime must ask about the CREW; "
            "leaving one on the transport puts that path back where this one was"
        )
        assert 'if params.method != "fargate":' not in source, (
            "the mint still keys on the transport, so an ssm-reached headless crew " "is minted for"
        )

    def test_the_microvm_registration_targets_the_guests_front_port(self):
        """And not the gateway dashboard port the registry defaults to, which a
        headless crew serves nothing on."""
        from kiro_crew.cloud.microvm import recipe as recipe_mod
        from kiro_crew.instances.registry import DEFAULT_REMOTE_PORT

        assert recipe_mod.GUEST_FRONT_PORT != DEFAULT_REMOTE_PORT


class TestTheSecretIsNamedByTheLaneNotTheDisplayName:
    """A crew's display name is a label, and labels are not secret names.

    The Fargate lane registers crews as ``Kiro Crew Cloud (<tag>)``, which is not
    a valid Secrets Manager name at all, so deriving an id from it makes every
    turn report a secret it never looked for. An operator who moved
    ``microvm.secret_path_prefix`` is ignored by a hardcoded path for the same
    reason.
    """

    def test_a_recorded_reference_is_preferred(self, tmp_path, monkeypatch):
        from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore
        from kiro_crew.dashboard.handlers_crew_turn import control_secret_id

        store = CrewStore(tmp_path / "crews.json")
        store.put(
            CrewRecord(tag="l2crew", control_secret_ref="kirocrew/crew/l2crew/CONTROL_SECRET")
        )
        monkeypatch.setattr("kiro_crew.cloud.microvm.record.CrewStore", lambda *a, **k: store)

        class Inst:
            name = "Kiro Crew Cloud (l2crew)"

        assert control_secret_id(Inst()) == "kirocrew/crew/l2crew/CONTROL_SECRET"

    def test_a_display_name_is_never_used_as_the_secret_name(self, tmp_path, monkeypatch):
        from kiro_crew.cloud.microvm.record import CrewStore
        from kiro_crew.dashboard.handlers_crew_turn import control_secret_id

        monkeypatch.setattr(
            "kiro_crew.cloud.microvm.record.CrewStore",
            lambda *a, **k: CrewStore(tmp_path / "empty.json"),
        )

        class Inst:
            name = "Kiro Crew Cloud (l2crew)"

        assert "Kiro Crew Cloud" not in control_secret_id(Inst())

    def test_an_instance_with_no_name_resolves_nothing(self, tmp_path, monkeypatch):
        """Reported as a crew that cannot be authenticated to, rather than a
        guessed name that reads some other crew's secret."""
        from kiro_crew.cloud.microvm.record import CrewStore
        from kiro_crew.dashboard.handlers_crew_turn import control_secret_id

        monkeypatch.setattr(
            "kiro_crew.cloud.microvm.record.CrewStore",
            lambda *a, **k: CrewStore(tmp_path / "empty.json"),
        )

        class Inst:
            name = ""

        assert control_secret_id(Inst()) == ""

    def test_the_path_shape_takes_the_configured_prefix(self):
        """A hardcoded prefix ignores an operator who moved theirs."""
        from kiro_crew.dashboard.handlers_crew_turn import _SECRET_PATH

        assert "{prefix}" in _SECRET_PATH
        assert _SECRET_PATH.startswith("{prefix}")


# ======================================================================
# Driving the turn handler itself.
#
# ``api_crew_turn`` is the hub's side of the conversation: it validates the
# body, refuses before it reads a secret when the crew is not a connected
# headless crew, reads the control secret, and relays the crew's answer -- as a
# single JSON response, or as the SSE frames of a streamed turn. The refusals
# above pin the ORDER of the checks from the source; the tests below drive the
# handler through each branch with the far end faked, so no turn ever leaves the
# process.
#
# The upstream crew is a fake aiohttp ``ClientSession``: the handler's own
# forward (the control-secret header, the OpenAI-shaped payload, the SSE relay)
# is real, only the socket is not. ``web.StreamResponse`` is replaced with a
# recorder so the streamed path needs no transport.
# ======================================================================


class _FakeContent:
    """``resp.content`` for the stream path: yields chunks, then maybe raises."""

    def __init__(self, chunks, raise_at_end=None):
        self._chunks = list(chunks)
        self._raise_at_end = raise_at_end

    async def iter_any(self):
        for chunk in self._chunks:
            yield chunk
        if self._raise_at_end is not None:
            raise self._raise_at_end


class _FakeResp:
    """The upstream crew's response: a status, a body, and streamable content."""

    def __init__(self, *, status=200, text="", chunks=(), raise_at_end=None):
        self.status = status
        self._text = text
        self.content = _FakeContent(chunks, raise_at_end)

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePost:
    """The context manager ``session.post`` returns."""

    def __init__(self, resp, raise_on_enter):
        self._resp = resp
        self._raise_on_enter = raise_on_enter

    async def __aenter__(self):
        if self._raise_on_enter is not None:
            raise self._raise_on_enter
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """A stand-in for ``aiohttp.ClientSession`` that never opens a socket.

    Records every ``post`` so a test can assert the control secret reaches the
    crew and nothing but the crew, and so it can read back the exact OpenAI-shaped
    payload the hub composed.
    """

    def __init__(self, calls, *, resp=None, raise_on_post=None, raise_on_enter=None):
        self._calls = calls
        self._resp = resp
        self._raise_on_post = raise_on_post
        self._raise_on_enter = raise_on_enter

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, *, json=None, headers=None):
        self._calls.append({"url": url, "json": json, "headers": headers})
        if self._raise_on_post is not None:
            raise self._raise_on_post
        return _FakePost(self._resp, self._raise_on_enter)


class _CapturingStream:
    """A ``web.StreamResponse`` replacement that records frames, not a socket.

    The streamed path calls ``prepare`` then ``write`` repeatedly; recording
    those lets a test read the relayed SSE frames without a prepared transport.
    """

    def __init__(self, *, status=200, headers=None):
        self.status = status
        self.headers = dict(headers or {})
        self.frames = []
        self.prepared = False

    async def prepare(self, request):
        self.prepared = True

    async def write(self, data):
        self.frames.append(bytes(data))


def _install_session(monkeypatch, calls, **kwargs):
    """Replace the module's ``aiohttp.ClientSession`` with the recording fake."""

    def _factory(*args, **_kwargs):
        return _FakeSession(calls, **kwargs)

    monkeypatch.setattr(mod.aiohttp, "ClientSession", _factory)


def _capture_stream(monkeypatch):
    """Make the handler build a recording stream rather than a real one."""
    monkeypatch.setattr(mod.web, "StreamResponse", _CapturingStream)


def _wire_handler(
    monkeypatch,
    *,
    denied=None,
    owner=True,
    reg_none=False,
    inst="__present__",
    status=None,
    secret_id="kirocrew/crew/l2crew/CONTROL_SECRET",
    secret="s3cret-value",
):
    """Stub the handler's lazily-imported collaborators and secret reads.

    Everything the route reaches OUT to -- the owner gate, the registry, the live
    status, the two secret steps -- is replaced so each test drives one branch of
    ``api_crew_turn`` with the rest held fixed. Returns the instance the registry
    will hand back, so a test can read the name the payload must echo.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from aiohttp import web

    import kiro_crew.dashboard.handlers._shared as sh
    import kiro_crew.dashboard.handlers.source_providers as sp
    import kiro_crew.dashboard.handlers_instances as hi

    monkeypatch.setattr(hi, "_guard", lambda request, operation: denied)
    monkeypatch.setattr(sp, "is_owner_dashboard_request", lambda request: owner)
    monkeypatch.setattr(
        sh,
        "_owner_denial_response",
        lambda request, detail="": web.json_response(
            {"code": "owner_only", "detail": detail}, status=403
        ),
    )

    if inst == "__present__":
        inst = SimpleNamespace(
            name="Kiro Crew Cloud (l2crew)",
            provisioner_id="microvm",
            aws_profile="prof",
            aws_region="us-east-1",
        )
    reg = None if reg_none else SimpleNamespace(get=lambda instance_id: inst)
    monkeypatch.setattr(hi, "_registry", lambda state: reg)
    monkeypatch.setattr(hi, "_status_for", lambda state, instance_id: status or {})
    monkeypatch.setattr(mod, "control_secret_id", lambda i: secret_id)
    monkeypatch.setattr(mod, "_read_control_secret", AsyncMock(return_value=secret))
    return inst


def _connected(**extra):
    """A tunnel status that yields a loopback turn target for a headless crew."""
    base = {
        "state": "connected",
        "turn_url": "http://127.0.0.1:18081/v1/chat/completions",
    }
    base.update(extra)
    return base


def _turn_request(body, *, instance_id="i1", bad_json=False):
    """A mocked ``POST /api/instances/{id}/crew-turn`` whose ``json()`` is *body*."""
    from unittest.mock import AsyncMock

    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    app = web.Application()
    app["state"] = object()
    request = make_mocked_request(
        "POST",
        f"/api/instances/{instance_id}/crew-turn",
        match_info={"id": instance_id},
        app=app,
    )
    if bad_json:
        request.json = AsyncMock(side_effect=ValueError("not json"))  # type: ignore[method-assign]
    else:
        request.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return request


def _code(response):
    """The refusal code a non-streaming JSON response carries."""
    import json as _json

    return _json.loads(response.body)["code"]


class TestTheTurnHandlerRefusesBeforeItForwards:
    """Each early exit of ``api_crew_turn``, driven through the real control flow.

    These are the refusals the body-shape matrix above asserts the ORDER of; here
    the handler actually runs them, so the status and code each one returns are
    pinned rather than inferred from the source.
    """

    @pytest.mark.asyncio
    async def test_the_guards_denial_short_circuits_the_route(self, monkeypatch):
        """When the shared gate denies, nothing else -- no registry, no secret -- runs."""
        from aiohttp import web

        sentinel = web.json_response({"code": "denied"}, status=403)
        _wire_handler(monkeypatch, denied=sentinel)
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))
        assert resp is sentinel

    @pytest.mark.asyncio
    async def test_an_authenticated_non_owner_is_refused(self, monkeypatch):
        """The route runs with the owner's credentials, so a non-owner subject
        must not reach it even once the shared gate has let them through."""
        _wire_handler(monkeypatch, owner=False)
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))
        assert resp.status == 403
        assert _code(resp) == "owner_only"

    @pytest.mark.asyncio
    async def test_an_unavailable_registry_is_a_service_error(self, monkeypatch):
        _wire_handler(monkeypatch, reg_none=True)
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))
        assert resp.status == 503
        assert _code(resp) == "instances_unavailable"

    @pytest.mark.asyncio
    async def test_an_unknown_instance_is_a_not_found(self, monkeypatch):
        _wire_handler(monkeypatch, inst=None)
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))
        assert resp.status == 404
        assert _code(resp) == "instance_unknown"

    @pytest.mark.asyncio
    async def test_a_body_that_is_not_json_is_refused(self, monkeypatch):
        _wire_handler(monkeypatch)
        resp = await mod.api_crew_turn(_turn_request(None, bad_json=True))
        assert resp.status == 400
        assert _code(resp) == "bad_request"

    @pytest.mark.asyncio
    async def test_a_body_that_is_not_an_object_is_refused(self, monkeypatch):
        _wire_handler(monkeypatch)
        resp = await mod.api_crew_turn(_turn_request(["not", "a", "dict"]))
        assert resp.status == 400
        assert _code(resp) == "bad_request"

    @pytest.mark.asyncio
    async def test_an_empty_message_is_refused(self, monkeypatch):
        _wire_handler(monkeypatch)
        resp = await mod.api_crew_turn(_turn_request({"message": "   ", "thread": "t"}))
        assert resp.status == 400
        assert _code(resp) == "bad_request"

    @pytest.mark.asyncio
    async def test_an_oversized_message_is_refused_without_a_round_trip(self, monkeypatch):
        """The hub caps the body here so an oversized prompt never crosses the tunnel."""
        _wire_handler(monkeypatch)
        body = {"message": "x" * (mod._MAX_MESSAGE_CHARS + 1), "thread": "t"}
        resp = await mod.api_crew_turn(_turn_request(body))
        assert resp.status == 413
        assert _code(resp) == "message_too_large"

    @pytest.mark.asyncio
    async def test_an_empty_thread_is_refused(self, monkeypatch):
        _wire_handler(monkeypatch)
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "  "}))
        assert resp.status == 400
        assert _code(resp) == "bad_request"

    @pytest.mark.asyncio
    async def test_a_non_boolean_stream_flag_is_refused(self, monkeypatch):
        _wire_handler(monkeypatch)
        body = {"message": "hi", "thread": "t", "stream": "yes"}
        resp = await mod.api_crew_turn(_turn_request(body))
        assert resp.status == 400
        assert _code(resp) == "bad_request"

    @pytest.mark.asyncio
    async def test_a_crew_that_is_not_connected_is_refused_before_any_secret(self, monkeypatch):
        """No turn target means the route stops before it reaches for the secret.

        The ``_read_control_secret`` stub is an ``AsyncMock``; asserting it was
        never awaited is the property the module's docstring names -- a
        misdirected turn causes no credential read.
        """
        _wire_handler(monkeypatch, status={"state": "disconnected"})
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))
        assert resp.status == 409
        assert _code(resp) == "crew_not_connected"
        assert mod._read_control_secret.await_count == 0

    @pytest.mark.asyncio
    async def test_an_unrecorded_secret_reference_is_a_service_error(self, monkeypatch):
        """No recorded reference, so the gateway has nothing to authenticate with."""
        _wire_handler(monkeypatch, status=_connected(), secret_id="")
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))
        assert resp.status == 503
        assert _code(resp) == "crew_secret_unavailable"
        assert mod._read_control_secret.await_count == 0

    @pytest.mark.asyncio
    async def test_an_unreadable_secret_is_a_service_error(self, monkeypatch):
        """A reference that resolves to nothing readable is one refusal, not two."""
        _wire_handler(monkeypatch, status=_connected(), secret=None)
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))
        assert resp.status == 503
        assert _code(resp) == "crew_secret_unavailable"


class TestTheTurnHandlerForwardsToTheCrew:
    """The forward itself: the secret reaches the crew, and only the crew."""

    @pytest.mark.asyncio
    async def test_a_non_streamed_turn_relays_the_crews_answer(self, monkeypatch):
        inst = _wire_handler(monkeypatch, status=_connected(), secret="S3C")
        calls = []
        _install_session(
            monkeypatch,
            calls,
            resp=_FakeResp(status=200, text='{"ok": true}'),
        )
        body = {"message": "hello crew", "thread": "slot-1", "stream": False}
        resp = await mod.api_crew_turn(_turn_request(body))

        assert resp.status == 200
        assert resp.body == b'{"ok": true}'
        assert resp.content_type == "application/json"
        # The control secret went to the crew and lives in no part of the reply.
        assert len(calls) == 1
        assert calls[0]["url"] == "http://127.0.0.1:18081/v1/chat/completions"
        assert calls[0]["headers"][mod.CONTROL_SECRET_HEADER] == "S3C"
        assert b"S3C" not in resp.body
        # And the payload is the OpenAI shape the crew's front expects.
        sent = calls[0]["json"]
        assert sent["model"] == inst.name
        assert sent["id"] == "slot-1"
        assert sent["stream"] is False
        assert sent["messages"] == [{"role": "user", "content": "hello crew"}]

    @pytest.mark.asyncio
    async def test_a_non_streamed_turn_passes_the_crews_own_status_through(self, monkeypatch):
        """A crew that answers 4xx/5xx is relayed with its own status, not masked."""
        _wire_handler(monkeypatch, status=_connected())
        calls = []
        _install_session(monkeypatch, calls, resp=_FakeResp(status=503, text="busy"))
        body = {"message": "hi", "thread": "t", "stream": False}
        resp = await mod.api_crew_turn(_turn_request(body))
        assert resp.status == 503
        assert resp.body == b"busy"

    @pytest.mark.asyncio
    async def test_a_non_streamed_turn_to_an_unreachable_crew_is_a_gateway_error(self, monkeypatch):
        """A transport failure is reported as a 502 and never leaks its type to the body."""
        _wire_handler(monkeypatch, status=_connected())
        calls = []
        _install_session(monkeypatch, calls, raise_on_post=RuntimeError("connection refused"))
        body = {"message": "hi", "thread": "t", "stream": False}
        resp = await mod.api_crew_turn(_turn_request(body))
        assert resp.status == 502
        assert _code(resp) == "crew_unreachable"
        assert b"connection refused" not in resp.body

    @pytest.mark.asyncio
    async def test_a_streamed_turn_relays_the_crews_frames_verbatim(self, monkeypatch):
        """The relay is thin: the crew's SSE bytes reach the pane unframed."""
        _wire_handler(monkeypatch, status=_connected(), secret="HDR")
        _capture_stream(monkeypatch)
        calls = []
        chunks = [b"data: {}\n\n", b"", b"data: [DONE]\n\n"]
        _install_session(monkeypatch, calls, resp=_FakeResp(status=200, chunks=chunks))
        body = {"message": "hi", "thread": "t"}  # stream defaults to True
        out = await mod.api_crew_turn(_turn_request(body))

        assert isinstance(out, _CapturingStream)
        assert out.prepared is True
        assert out.headers["Content-Type"] == "text/event-stream"
        assert out.headers["X-Accel-Buffering"] == "no"
        # The empty chunk is dropped; the two real frames are relayed as-is.
        assert out.frames == [b"data: {}\n\n", b"data: [DONE]\n\n"]
        assert calls[0]["json"]["stream"] is True
        assert calls[0]["headers"][mod.CONTROL_SECRET_HEADER] == "HDR"

    @pytest.mark.asyncio
    async def test_a_streamed_turn_reports_a_crew_refusal_as_a_frame(self, monkeypatch):
        """The head is already sent, so a non-200 upstream becomes an error FRAME.

        A status code cannot be sent any more; the pane learns of the refusal from
        a ``crew_refused`` event carrying the crew's own status and a bounded slice
        of its body, then a ``[DONE]`` so the reader stops waiting.
        """
        import json as _json

        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        calls = []
        _install_session(
            monkeypatch,
            calls,
            resp=_FakeResp(status=401, text="nope " * 200),
        )
        out = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        assert out.status == 200  # the stream head was 200 before the upstream answered
        assert out.frames[-1] == b"data: [DONE]\n\n"
        first = _json.loads(out.frames[0][len(b"data: ") :].decode())
        assert first["error"]["code"] == "crew_refused"
        assert first["error"]["status"] == 401
        assert len(first["error"]["detail"]) <= 400

    @pytest.mark.asyncio
    async def test_a_streamed_turn_that_breaks_mid_body_closes_with_an_error_frame(
        self, monkeypatch
    ):
        """A mid-stream failure cannot be a status, so it is a frame plus ``[DONE]``.

        Closing silently would leave the pane waiting for a reply that is never
        coming, so the relay writes a ``crew_unreachable`` event even though some
        frames already went out.
        """
        import json as _json

        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        calls = []
        _install_session(
            monkeypatch,
            calls,
            resp=_FakeResp(
                status=200,
                chunks=[b"data: partial\n\n"],
                raise_at_end=RuntimeError("socket died"),
            ),
        )
        out = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        assert out.frames[0] == b"data: partial\n\n"
        assert out.frames[-1] == b"data: [DONE]\n\n"
        error_frame = _json.loads(out.frames[-2][len(b"data: ") :].decode())
        assert error_frame["error"]["code"] == "crew_unreachable"
        assert not any(b"socket died" in frame for frame in out.frames)


class TestReadingTheControlSecret:
    """``_read_control_secret`` turns a Secrets Manager read into a value or ``None``.

    It never raises and never logs the VALUE: an absent, unreadable, or empty
    secret is a single ``None``, which the caller reports as one refusal, because
    distinguishing them would report whether a named secret exists.
    """

    @pytest.mark.asyncio
    async def test_a_present_secret_string_is_returned(self, monkeypatch):
        import kiro_crew.cloud.aws as aws

        monkeypatch.setattr(aws, "checked_json", lambda *a, **k: {"SecretString": "the-secret"})
        assert await mod._read_control_secret("sid", "prof", "us-east-1") == "the-secret"

    @pytest.mark.asyncio
    async def test_an_unreadable_secret_becomes_none_without_raising(self, monkeypatch):
        import kiro_crew.cloud.aws as aws

        def _boom(*a, **k):
            raise RuntimeError("access denied")

        monkeypatch.setattr(aws, "checked_json", _boom)
        assert await mod._read_control_secret("sid", "prof", "us-east-1") is None

    @pytest.mark.asyncio
    async def test_a_response_with_no_secret_string_is_none(self, monkeypatch):
        import kiro_crew.cloud.aws as aws

        monkeypatch.setattr(aws, "checked_json", lambda *a, **k: {})
        assert await mod._read_control_secret("sid", "prof", "us-east-1") is None

    @pytest.mark.asyncio
    async def test_a_non_dict_response_is_none(self, monkeypatch):
        import kiro_crew.cloud.aws as aws

        monkeypatch.setattr(aws, "checked_json", lambda *a, **k: "unexpected")
        assert await mod._read_control_secret("sid", "prof", "us-east-1") is None

    @pytest.mark.asyncio
    async def test_the_secret_value_is_never_logged(self, monkeypatch, caplog):
        import logging

        import kiro_crew.cloud.aws as aws

        def _boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(aws, "checked_json", _boom)
        with caplog.at_level(logging.INFO, logger=mod.logger.name):
            await mod._read_control_secret("secret/path/CONTROL", "prof", "us-east-1")
        # The crew's NAME and the exception TYPE may be logged; the value may not,
        # and here the read raised before any value existed to leak.
        assert "RuntimeError" in caplog.text
        assert "boom" not in caplog.text


class TestTheFargateControlSecretReference:
    """``_fargate_control_secret`` reads the ARN from the operator's own config.

    The Fargate lane names its secrets itself, keyed by the ENV the container
    reads -- ``SMC_CONTROL_SECRET`` -- so the reference cannot be derived from the
    instance row. A derived name on this lane names a secret nobody minted.
    """

    def test_a_non_fargate_instance_has_no_fargate_reference(self):
        from types import SimpleNamespace

        assert mod._fargate_control_secret(SimpleNamespace(provisioner_id="microvm")) == ""
        assert mod._fargate_control_secret(SimpleNamespace()) == ""

    def test_a_missing_config_yields_no_reference(self, monkeypatch):
        from types import SimpleNamespace

        import kiro_crew.cloud.config as cfg

        monkeypatch.setattr(
            cfg,
            "CloudConfig",
            SimpleNamespace(load=lambda: SimpleNamespace(fargate_config=lambda: None)),
        )
        assert mod._fargate_control_secret(SimpleNamespace(provisioner_id="aws_fargate")) == ""

    def test_a_config_that_cannot_be_loaded_yields_no_reference(self, monkeypatch):
        from types import SimpleNamespace

        import kiro_crew.cloud.config as cfg

        def _raise():
            raise RuntimeError("no config file")

        monkeypatch.setattr(cfg, "CloudConfig", SimpleNamespace(load=_raise))
        assert mod._fargate_control_secret(SimpleNamespace(provisioner_id="aws_fargate")) == ""

    def test_the_entry_whose_env_is_the_control_secret_wins(self, monkeypatch):
        """The reference is the ARN of the entry mapping to ``SMC_CONTROL_SECRET``.

        An entry whose env name is something else is skipped, and an entry the
        lane refuses to parse at all is skipped rather than fatal -- a reference
        this lane rejects is not the control secret.
        """
        from types import SimpleNamespace

        import kiro_crew.cloud.config as cfg
        import kiro_crew.cloud.fargate.identity as identity

        secrets = [
            ("OTHER", "arn:aws:secretsmanager:::secret:other"),
            ("BROKEN", "arn:aws:secretsmanager:::secret:broken"),
            ("CONTROL", "arn:aws:secretsmanager:::secret:control"),
        ]
        env_by_arn = {
            "arn:aws:secretsmanager:::secret:other": "SOME_OTHER_ENV",
            "arn:aws:secretsmanager:::secret:control": "SMC_CONTROL_SECRET",
        }

        def _env_name(ref, **kwargs):
            if ref.arn not in env_by_arn:
                raise ValueError("this lane refuses that reference")
            return env_by_arn[ref.arn]

        monkeypatch.setattr(identity, "secret_env_name", _env_name)
        monkeypatch.setattr(
            cfg,
            "CloudConfig",
            SimpleNamespace(
                load=lambda: SimpleNamespace(
                    fargate_config=lambda: SimpleNamespace(secrets=secrets)
                )
            ),
        )
        got = mod._fargate_control_secret(SimpleNamespace(provisioner_id="aws_fargate"))
        assert got == "arn:aws:secretsmanager:::secret:control"

    def test_no_matching_entry_yields_no_reference(self, monkeypatch):
        from types import SimpleNamespace

        import kiro_crew.cloud.config as cfg
        import kiro_crew.cloud.fargate.identity as identity

        monkeypatch.setattr(identity, "secret_env_name", lambda ref, **k: "NOT_THE_ONE")
        monkeypatch.setattr(
            cfg,
            "CloudConfig",
            SimpleNamespace(
                load=lambda: SimpleNamespace(
                    fargate_config=lambda: SimpleNamespace(
                        secrets=[("X", "arn:aws:secretsmanager:::secret:x")]
                    )
                )
            ),
        )
        assert mod._fargate_control_secret(SimpleNamespace(provisioner_id="aws_fargate")) == ""


class TestControlSecretIdLanePrecedence:
    """``control_secret_id`` prefers the Fargate config, then a record, then a derive."""

    def test_a_fargate_reference_is_preferred_over_any_derivation(self, monkeypatch):
        """When the Fargate config names the control secret, that ARN is returned
        whole -- a derived path is never consulted on this lane."""
        from types import SimpleNamespace

        monkeypatch.setattr(
            mod, "_fargate_control_secret", lambda inst: "arn:aws:secretsmanager:::secret:ctl"
        )
        inst = SimpleNamespace(name="Kiro Crew Cloud (l2crew)", provisioner_id="aws_fargate")
        assert mod.control_secret_id(inst) == "arn:aws:secretsmanager:::secret:ctl"

    def test_a_non_matching_record_tag_does_not_satisfy_the_lookup(self, tmp_path, monkeypatch):
        """A record for a DIFFERENT tag is not this crew's reference, so the lookup
        falls through to the configured-prefix derivation rather than borrowing it."""
        from types import SimpleNamespace

        from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore

        store = CrewStore(tmp_path / "crews.json")
        store.put(
            CrewRecord(
                tag="someone-else", control_secret_ref="kirocrew/crew/someone-else/CONTROL_SECRET"
            )
        )
        monkeypatch.setattr("kiro_crew.cloud.microvm.record.CrewStore", lambda *a, **k: store)
        monkeypatch.setattr(mod, "_fargate_control_secret", lambda inst: "")

        inst = SimpleNamespace(name="Kiro Crew Cloud (l2crew)", provisioner_id="microvm")
        got = mod.control_secret_id(inst)
        assert "someone-else" not in got
        assert got.endswith("/l2crew/CONTROL_SECRET")


def test_a_connected_crew_with_a_turn_url_is_headless_even_without_a_headless_lane():
    """``is_headless_crew`` also answers yes when the status itself carries a
    ``turn_url`` -- the far end's own statement that it serves a turn route."""
    assert mod.is_headless_crew({"state": "connected", "turn_url": "http://x/v1"}, "ec2") is True
    assert mod.is_headless_crew({"state": "connected"}, "ec2") is False
    assert mod.is_headless_crew("not-a-dict", "ec2") is False


class _ExplodingStream(_CapturingStream):
    """A stream whose every ``write`` fails, modelling a client already gone."""

    async def write(self, data):
        raise RuntimeError("broken pipe")


@pytest.mark.asyncio
async def test_a_streamed_turn_whose_error_frame_also_fails_does_not_raise(monkeypatch):
    """When even the error frame cannot be written, the handler returns, not raises.

    The relay's inner guard: the client has already dropped, so writing the
    ``crew_unreachable`` frame fails too. Letting that propagate would turn a
    gone client into a 500 and an unhandled task error; the handler swallows it
    and returns the (useless but closed) response.
    """
    _wire_handler(monkeypatch, status=_connected())
    monkeypatch.setattr(mod.web, "StreamResponse", _ExplodingStream)
    calls = []
    _install_session(
        monkeypatch,
        calls,
        resp=_FakeResp(status=200, chunks=[b"data: x\n\n"]),
    )
    out = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))
    assert isinstance(out, _ExplodingStream)


def _wire_resume(monkeypatch, *, state, raises=None, on_resume=None, on_lookup=None):
    """Stub the lane record lookup and the lifecycle resume the handler reaches.

    The handler imports both lazily inside a thread, so the stubs go on the
    modules it imports rather than on the handler. ``on_lookup`` records that the
    record store was consulted at all, which is how a test proves another lane is
    never asked about a suspend it does not have.
    """
    from types import SimpleNamespace

    import kiro_crew.cloud.config as cloud_config_mod
    from kiro_crew.cloud.microvm import record as record_mod
    from kiro_crew.cloud.microvm import wiring as wiring_mod

    class _Store:
        def get(self, tag):
            if on_lookup is not None:
                on_lookup(tag)
            return SimpleNamespace(tag=tag, state=state)

    class _Lifecycle:
        def resume(self, tag):
            if on_resume is not None:
                on_resume(tag)
            if raises is not None:
                raise raises
            return SimpleNamespace(tag=tag)

    monkeypatch.setattr(record_mod, "CrewStore", _Store)
    monkeypatch.setattr(wiring_mod, "production_lifecycle", lambda config, **kw: _Lifecycle())
    monkeypatch.setattr(
        cloud_config_mod.CloudConfig,
        "load",
        staticmethod(lambda: SimpleNamespace(microvm_config=lambda: SimpleNamespace())),
    )


class TestASuspendedCrewIsWokenBeforeTheTurn:
    """The gateway owns the resume, because the platform cannot own it here.

    AWS can wake a MicroVM on inbound traffic, measured on the VM's own proxy
    endpoint. A turn on this lane arrives over an SSM port-forward to a front
    bound to loopback, so that measurement sees nothing -- which is also why the
    lane turns the platform's idle policy off rather than letting it suspend a
    crew mid-turn. Both halves are the gateway's: the tick suspends, this resumes.

    Before the forward and not after a failure, because a suspended VM's port
    hangs rather than refusing. A turn sent first waits out the client's timeout
    and reports an unreachable crew.
    """

    @pytest.mark.asyncio
    async def test_a_suspended_crew_is_resumed_and_then_served(self, monkeypatch):
        resumed: list[str] = []
        calls: list[dict] = []
        _wire_handler(monkeypatch, status=_connected())
        _wire_resume(monkeypatch, state=states_mod.SUSPENDED, on_resume=resumed.append)
        _install_session(monkeypatch, calls, resp=_FakeResp(status=200, text='{"ok": true}'))
        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )
        assert resp.status == 200
        assert resumed == ["l2crew"], "the suspended crew was not resumed"
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_a_running_crew_is_not_resumed(self, monkeypatch):
        """Resume costs an API call per turn. A crew already running must not pay
        for one."""
        resumed: list[str] = []
        calls: list[dict] = []
        _wire_handler(monkeypatch, status=_connected())
        _wire_resume(monkeypatch, state=states_mod.RUNNING, on_resume=resumed.append)
        _install_session(monkeypatch, calls, resp=_FakeResp(status=200, text='{"ok": true}'))
        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )
        assert resp.status == 200
        assert resumed == []

    @pytest.mark.asyncio
    async def test_a_resume_that_fails_is_a_named_refusal_not_a_crash(self, monkeypatch):
        """And the turn is NOT forwarded: a hang is what forwarding would buy."""
        calls: list[dict] = []
        _wire_handler(monkeypatch, status=_connected())
        _wire_resume(monkeypatch, state=states_mod.SUSPENDED, raises=RuntimeError("boom"))
        _install_session(monkeypatch, calls, resp=_FakeResp(status=200, text="{}"))
        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )
        assert resp.status == 503
        assert _code(resp) == "crew_resume_failed"
        assert calls == []

    @pytest.mark.asyncio
    async def test_another_lane_is_never_asked_about_suspend(self, monkeypatch):
        """Fargate has no suspend, so reading its records for one would be a
        question with no answer. The provisioner decides whether to ask."""
        asked: list[str] = []
        calls: list[dict] = []
        inst = _wire_handler(monkeypatch, status=_connected())
        inst.provisioner_id = "aws_fargate"
        _wire_resume(
            monkeypatch, state=states_mod.SUSPENDED, on_resume=asked.append, on_lookup=asked.append
        )
        _install_session(monkeypatch, calls, resp=_FakeResp(status=200, text="{}"))
        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )
        assert resp.status == 200
        assert asked == []
