"""Chat with a HEADLESS remote crew, through the gateway rather than the browser.

A remote Kiro Crew GATEWAY is reached by embedding its own dashboard: the hub
opens a tunnel, mints a dashboard token, and the iframe does the rest. A headless
crew has no dashboard to embed. It serves one turn route -- the OpenAI-shaped
``POST /v1/chat/completions`` of
``apps/builtins/aws_control/crew/runtime/container/front`` -- and nothing else,
so the hub has to be the client.

That is what this module is: the hub's side of a conversation with a crew that
cannot draw its own.

WHY THE GATEWAY AND NOT THE BROWSER
-----------------------------------
The turn route authenticates every caller with the deployment's per-crew control
secret. Three reasons that secret never leaves this process:

* it lives in the owner's Secrets Manager and is read with the owner's AWS
  credentials, which the browser does not have and must not be given;
* it is the SAME secret that gates the crew's control surface, so a copy in a
  browser tab is a copy in every page that tab ever loads;
* the crew is reachable only on a loopback port of THIS machine (the far end of
  an SSM port-forward), which a browser on another machine cannot dial anyway.

So the browser talks to this gateway same-origin, with its own dashboard token,
and this gateway talks to the crew. One hop, two credentials, neither crossing.

LANE-NEUTRAL BY CONSTRUCTION
----------------------------
Nothing here knows about MicroVMs. What it needs is a connected instance whose
far end is a headless crew front, which is true of the Fargate lane
(``connection_method="fargate"``, an ECS task target) and of the MicroVM lane
(``connection_method="ssm"``, an ``mi-`` node) alike. The tunnel's own status is
what says so: it carries a ``turn_url`` exactly when the far end is a turn route
rather than a dashboard.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any, Optional

import aiohttp
from aiohttp import web

from kiro_crew.cloud.connect import FARGATE_TURN_PATH
from kiro_crew.cloud.microvm.engine import MICROVM_PROVISIONER_ID
from kiro_crew.instances.registry import HEADLESS_CREW_PROVISIONERS as _REGISTRY_HEADLESS
from kiro_crew.platform.defaults import FARGATE_PROVISIONER_ID

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: The header the crew's front process reads its control secret from. One
#: definition, in the container's ``common``; named here as a constant so the
#: value cannot drift, and asserted against the container's own constant by a
#: test rather than copied by eye.
CONTROL_SECRET_HEADER = "X-SMC-Control-Secret"

#: Where a crew's control secret lives, by crew name. The same path the lane's
#: own launch spec builds (``MicroVmLaunchSpec.control_secret_name``) and the same
#: one the Fargate task definition references, so the hub reads what the deploy
#: wrote instead of being told a path by the caller.
#: The shape a per-crew control secret's name has, as the lane writes it. Used
#: only to DERIVE an id when the lane recorded none; the recorded reference is
#: always preferred, because it is what the launch actually minted.
_SECRET_PATH = "{prefix}/{crew}/CONTROL_SECRET"


#: The environment name the crew container reads its control secret from, and
#: therefore the entry to pick out of a Fargate deployment's secret list.
_CONTROL_SECRET_ENV = "SMC_CONTROL_SECRET"


def _fargate_control_secret(inst: Any) -> str:
    """The Fargate lane's control secret ARN, from the operator's config.

    ``""`` for anything that is not a Fargate crew, or for a config that names no
    such entry -- in which case the caller falls through and ultimately reports a
    crew it cannot authenticate to, which is the honest answer. Guessing a name
    on this lane cannot work: the secret's last path segment is the ENV NAME, and
    the binding crew in the middle is the operator's choice, so neither is
    recoverable from the instance row.
    """
    if str(getattr(inst, "provisioner_id", "") or "") != "aws_fargate":
        return ""
    try:
        from kiro_crew.cloud.config import CloudConfig

        config = CloudConfig.load().fargate_config()
    except Exception:  # noqa: BLE001 - no config, no reference
        return ""
    if config is None:
        return ""
    # ``(canonical name, ARN)``, and the env name is derived FROM the name by the
    # lane's own ``secret_env_name`` -- called rather than re-derived, because two
    # functions reading one reference to different strictness is how a document
    # delivers one secret's value under another secret's variable.
    try:
        from kiro_crew.cloud.fargate.identity import SecretRef, secret_env_name
    except Exception:  # noqa: BLE001 - no lane, no reference
        return ""
    for secret_name, arn in getattr(config, "secrets", ()) or ():
        try:
            env_name = secret_env_name(SecretRef(name=str(secret_name), arn=str(arn)))
        except Exception:  # noqa: BLE001 - a reference this lane refuses is not ours
            continue
        if env_name == _CONTROL_SECRET_ENV:
            return str(arn or "")
    return ""


def control_secret_id(inst: Any) -> str:
    """This crew's control secret, by NAME, or ``""``.

    Read from the lane's own record rather than built from the instance's display
    name. The display name is a label: the Fargate lane registers crews as
    ``Kiro Crew Cloud (<tag>)``, which is not a valid secret name at all, so
    deriving an id from it makes every such turn fail to read a secret that
    exists. The record holds the reference the LAUNCH minted, which is the only
    value guaranteed to name the right secret.

    Falls back to deriving one from the launch tag and the operator's configured
    prefix -- not the hardcoded default -- so a crew recorded before the lane
    stored references still works, and an operator who moved the prefix is
    honoured. Empty when neither is available, which the caller reports as a
    crew that cannot be authenticated to rather than guessing a name.
    """
    try:
        from kiro_crew.cloud.config import CloudConfig
        from kiro_crew.cloud.microvm.record import CrewStore
    except Exception:  # noqa: BLE001 - no lane, no reference
        return ""
    # FARGATE names its secrets itself, in the operator's own config: each entry
    # is (arn, env_name), and the control secret is the one whose env name is
    # SMC_CONTROL_SECRET. Derived names do not work on that lane at all -- it uses
    # the env name as the last path segment, not CONTROL_SECRET -- so the config's
    # ARN is the only value that names the right secret.
    fargate = _fargate_control_secret(inst)
    if fargate:
        return fargate
    name = str(getattr(inst, "name", "") or "")
    tag = str(getattr(inst, "provisioner_tag", "") or "") or _tag_in(name)
    for record in CrewStore().iter_records():
        if record.tag and tag and record.tag == tag and record.control_secret_ref:
            return record.control_secret_ref
    if not tag:
        return ""
    config = CloudConfig.load().microvm_config()
    prefix = config.secret_path_prefix if config else "kirocrew/crew"
    return _SECRET_PATH.format(prefix=prefix, crew=tag)


#: A launch tag's shape, which is also the one segment a secret name may carry.
#: Anything else is a display label, and a label interpolated into a secret path
#: names a secret nobody minted.
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _tag_in(name: str) -> str:
    """The launch tag inside an instance's display name, or ``""``.

    The Fargate lane registers crews as ``Kiro Crew Cloud (<tag>)``, so the tag is
    recoverable from the label even when no record holds a reference. A name that
    yields nothing tag-shaped resolves to nothing at all, which the caller reports
    as a crew it cannot authenticate to -- better than interpolating a label with
    spaces and brackets into a secret path and reporting that the secret is
    missing.
    """
    text = name.strip()
    if text.endswith(")") and "(" in text:
        text = text[text.rfind("(") + 1 : -1].strip()
    return text if _TAG_RE.match(text) else ""


#: Longest message the hub will forward. The crew's own front caps the body it
#: accepts; this is the hub's cap, so an oversized prompt is refused here rather
#: than after a tunnel round trip.
_MAX_MESSAGE_CHARS = 32_000

#: A turn can legitimately run for minutes -- it is a model call that may use
#: tools. Only the CONNECT is short, so a dead tunnel fails fast instead of
#: holding the pane for the whole read budget.
_TURN_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=10, sock_read=600)


def _refuse(code: str, detail: str, status: int = 400) -> web.Response:
    return web.json_response({"code": code, "detail": detail}, status=status)


async def _read_control_secret(secret_id: str, profile: str, region: str) -> Optional[str]:
    """The crew's control secret, from the owner's own Secrets Manager.

    Through the gateway's one ``aws`` CLI chokepoint, so this read is subject to
    the same agent-session allowlist as every other AWS call the product makes:
    an agent driving the dashboard cannot turn this route into a way to print a
    secret, because the chokepoint refuses the pair.

    Returns ``None`` rather than raising when the secret is absent or unreadable.
    The caller turns that into one refusal, because the two cases are the same
    thing to a user -- this crew cannot be chatted with until its deploy is
    complete -- and distinguishing them in the reply would report whether a
    named secret exists.
    """
    import asyncio

    from kiro_crew.cloud.aws import AWSError, checked_json

    def _read() -> Optional[str]:
        try:
            data = checked_json(
                [
                    "secretsmanager",
                    "get-secret-value",
                    "--secret-id",
                    secret_id,
                ],
                profile,
                region,
                action="secretsmanager:GetSecretValue",
                timeout=30,
            )
        except (AWSError, Exception) as exc:  # noqa: BLE001 - one refusal either way
            # The VALUE is never logged. This records the crew's NAME and the
            # exception's TYPE, which is what a reader needs to tell a secret that
            # does not exist from a Secrets Manager that could not be reached. The
            # message text names the thing that could not be read, which is not
            # the same as printing it.
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logger.info("crew control secret %s unreadable: %s", secret_id, type(exc).__name__)
            return None
        value = data.get("SecretString") if isinstance(data, dict) else None
        return str(value) if value else None

    return await asyncio.to_thread(_read)


#: The provisioners whose crews are HEADLESS: they serve a turn route and have
#: no dashboard to embed. The set, not the connection method, is what decides
#: whether this pane applies -- two lanes already share the ``ssm`` method with
#: the EC2 lane, whose crews DO run a full gateway, so keying on the method would
#: offer a chat pane for a crew that has a dashboard and vice versa.
#: Re-exported from the registry, which OWNS this set. Two copies would drift,
#: and the two readers decide different things from the same answer: the tunnel
#: manager whether to mint a dashboard token, this route whether a chat pane may
#: be offered. A crew that is headless for one and not the other is a crew whose
#: pane is offered and whose forward holds no credential.
HEADLESS_CREW_PROVISIONERS = _REGISTRY_HEADLESS

# The registry holds the ids as LITERALS, because importing a lane's module from
# there would close a cycle. Checked against the real constants here instead, at
# import, so a renamed provisioner id is an immediate failure rather than a
# headless crew the registry fails to recognise.
assert HEADLESS_CREW_PROVISIONERS == {FARGATE_PROVISIONER_ID, MICROVM_PROVISIONER_ID}, (
    "the registry's headless set and the lanes' own provisioner ids disagree: "
    f"{sorted(HEADLESS_CREW_PROVISIONERS)} vs "
    f"{sorted({FARGATE_PROVISIONER_ID, MICROVM_PROVISIONER_ID})}"
)


async def _resume_if_suspended(inst: Any) -> str:
    """Wake a suspended MicroVM crew. Returns "" when there is nothing to do.

    Scoped to the one provisioner that suspends. Every other lane either has no
    suspend at all or leaves it to the platform, so asking their records about it
    would be a question with no answer.

    A failure is returned as a STRING rather than raised, so the caller answers
    the pane with a named refusal instead of a 500. The crew is left suspended:
    the tick will not have made it worse, and the operator can see why.
    """
    if str(getattr(inst, "provisioner_id", "") or "") != "microvm":
        return ""
    try:
        from kiro_crew.cloud.config import CloudConfig
        from kiro_crew.cloud.microvm import states
        from kiro_crew.cloud.microvm.record import CrewStore
    except Exception:  # pragma: no cover - the lane's modules ship with it
        return ""

    def _resume() -> str:
        # The same resolution ``control_secret_id`` uses, so the two cannot
        # disagree about which record is this instance's. A display name is a
        # label and is not the tag.
        name = str(getattr(inst, "name", "") or "")
        tag = str(getattr(inst, "provisioner_tag", "") or "") or _tag_in(name)
        if not tag:
            return ""
        record = CrewStore().get(tag)
        if record is None or record.state != states.SUSPENDED:
            return ""
        config = CloudConfig.load().microvm_config()
        if config is None:
            return (
                "this crew is suspended and the microvm block in cloud.json is gone, "
                "so the gateway cannot resume it"
            )
        from kiro_crew.cloud.microvm.wiring import production_lifecycle

        production_lifecycle(config).resume(tag)
        return ""

    try:
        return await asyncio.to_thread(_resume)
    except Exception as exc:  # noqa: BLE001 - a named refusal beats a 500
        logger.info("crew resume failed: %s", type(exc).__name__)
        return f"this crew is suspended and could not be resumed: {type(exc).__name__}"


def _turn_target(status: dict, provisioner_id: str = "") -> str:
    """The loopback turn URL for a connected headless crew, or ``""``.

    Two sources, in order. The tunnel manager populates ``turn_url`` for the
    ``fargate`` method, and that value is preferred because it is the far end's
    own statement about itself. Otherwise the URL is composed from the connected
    local port, but ONLY when the instance's provisioner is a headless-crew lane
    -- never from a port alone. A composed URL would also "work" against a
    forward pointing at a remote gateway, and posting a crew turn at a gateway's
    own port is a request that fails in a way nobody can read.
    """
    if status.get("state") != "connected":
        return ""
    url = str(status.get("turn_url") or "")
    if url:
        return url
    port = status.get("local_port")
    if isinstance(port, int) and port > 0 and provisioner_id in HEADLESS_CREW_PROVISIONERS:
        return f"http://127.0.0.1:{port}{FARGATE_TURN_PATH}"
    return ""


async def api_crew_turn(request: web.Request) -> web.StreamResponse:
    """POST /api/instances/{id}/crew-turn — one turn against a headless crew.

    Body: ``{"thread": "<id>", "message": "<text>", "stream": <bool>}``.

    ``thread`` is the crew's slot id, which is what makes a conversation a
    conversation: the crew's front serializes per slot and restores that slot's
    transcript before the turn, so the same thread id after a suspend and resume
    continues rather than starts. It is the caller's to choose and is forwarded
    as given.

    Streams by default, as Server-Sent Events relayed from the crew's own
    OpenAI-shaped chunks. The relay is deliberately thin -- it re-frames nothing
    and interprets nothing -- so the pane renders what the crew said.
    """
    from kiro_crew.dashboard.handlers._shared import _owner_denial_response
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
    from kiro_crew.dashboard.handlers_instances import _guard, _registry, _status_for

    denied = _guard(request, "crew_turn")
    if denied is not None:
        return denied
    # Owner-only, the same bar as the proxy and the capability reads: this runs
    # with the owner's AWS credentials and sends the crew's control secret, so an
    # authenticated non-owner (a Slack-minted dashboard subject) must not reach it.
    if not is_owner_dashboard_request(request):
        return _owner_denial_response(request, "remote-crew chat is owner-only")

    state: "DashboardState" = request.app["state"]
    instance_id = request.match_info.get("id", "")
    reg = _registry(state)
    if reg is None:
        return _refuse("instances_unavailable", "remote crews are not available", 503)
    # OFF the loop. ``registry.get`` reads and parses the instances file, and
    # this handler runs on the gateway's sole event loop -- the one serving chat,
    # websockets, timers and the heartbeat. A synchronous read here stalls all of
    # them for the duration, and this route is hit on every turn the pane sends.
    inst = await asyncio.to_thread(reg.get, instance_id)
    if inst is None:
        return _refuse("instance_unknown", "no such remote crew", 404)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _refuse("bad_request", "request body must be JSON")
    if not isinstance(body, dict):
        return _refuse("bad_request", "request body must be a JSON object")
    message = body.get("message")
    thread = body.get("thread")
    if not isinstance(message, str) or not message.strip():
        return _refuse("bad_request", "message must be a non-empty string")
    if len(message) > _MAX_MESSAGE_CHARS:
        return _refuse("message_too_large", f"message exceeds {_MAX_MESSAGE_CHARS} characters", 413)
    if not isinstance(thread, str) or not thread.strip():
        return _refuse("bad_request", "thread must be a non-empty string")
    stream = body.get("stream", True)
    if not isinstance(stream, bool):
        return _refuse("bad_request", '"stream" must be a boolean')

    status = _status_for(state, instance_id)
    target = _turn_target(status, str(getattr(inst, "provisioner_id", "") or ""))
    if not target:
        # Not connected, or connected to something that is not a headless crew.
        # One refusal for both, with the state named, because the pane's next
        # action is the same either way: connect the crew first.
        return _refuse(
            "crew_not_connected",
            f"this crew is not connected as a headless crew (state: {status.get('state')})",
            409,
        )

    secret_id = await asyncio.to_thread(control_secret_id, inst)
    if not secret_id:
        return _refuse(
            "crew_secret_unavailable",
            "this crew's control secret reference is not recorded, so the gateway "
            "cannot authenticate to it. Relaunch the crew, or chat with it through "
            "its own turn URL.",
            503,
        )
    secret = await _read_control_secret(
        secret_id, str(inst.aws_profile or ""), str(inst.aws_region or "")
    )
    if not secret:
        return _refuse(
            "crew_secret_unavailable",
            "this crew's control secret could not be read, so the gateway cannot "
            "authenticate to it. Check the deploy completed and that this machine's "
            "AWS credentials can read it.",
            503,
        )

    # A suspended crew is woken HERE, by the gateway, before the turn is
    # forwarded.
    #
    # The platform can wake a MicroVM on inbound traffic itself, and this lane
    # does not use that: AWS measures inbound only on the VM's own proxy endpoint,
    # while a turn arrives over an SSM port-forward to a front that binds
    # loopback. So the platform would see no traffic to wake on -- and, left to
    # measure idleness the same way, would have suspended a crew in the middle of
    # a turn. The lane turns its idle policy off and takes both halves itself: the
    # lifecycle tick suspends on the gateway's own slot accounting, and this is
    # the resume.
    #
    # Before the forward rather than after a failure, because a suspended VM's
    # port HANGS rather than refusing: a turn sent first would wait out the
    # client's timeout and report an unreachable crew.
    resume_error = await _resume_if_suspended(inst)
    if resume_error:
        return _refuse("crew_resume_failed", resume_error, 503)

    payload = {
        # The crew's display name, which is what the turn route echoes back as
        # the model. A label, deliberately: the SECRET is named by the lane's own
        # recorded reference, and the two are different values.
        "model": str(inst.name or ""),
        "id": thread,
        "stream": stream,
        "messages": [{"role": "user", "content": message}],
    }
    headers = {CONTROL_SECRET_HEADER: secret, "Content-Type": "application/json"}

    if not stream:
        try:
            async with aiohttp.ClientSession(timeout=_TURN_TIMEOUT) as session:
                async with session.post(target, json=payload, headers=headers) as resp:
                    text = await resp.text()
                    return web.Response(
                        body=text.encode(),
                        status=resp.status,
                        content_type="application/json",
                    )
        except Exception as exc:  # noqa: BLE001
            logger.info("crew turn to %s failed: %s", instance_id, type(exc).__name__)
            return _refuse("crew_unreachable", "the crew did not answer", 502)

    # Streamed: relay the crew's SSE frames straight through. The hub does not
    # buffer the turn, so a long answer appears as it is produced rather than at
    # the end -- which is the whole reason the pane streams.
    out = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            # The pane reads this with fetch + a reader, not EventSource, but a
            # proxy in between must still be told not to buffer.
            "X-Accel-Buffering": "no",
        },
    )
    await out.prepare(request)
    try:
        async with aiohttp.ClientSession(timeout=_TURN_TIMEOUT) as session:
            async with session.post(target, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    detail = (await resp.text())[:400]
                    await out.write(
                        b"data: "
                        + json.dumps(
                            {
                                "error": {
                                    "code": "crew_refused",
                                    "status": resp.status,
                                    "detail": detail,
                                }
                            }
                        ).encode()
                        + b"\n\n"
                    )
                    await out.write(b"data: [DONE]\n\n")
                    return out
                async for chunk in resp.content.iter_any():
                    if chunk:
                        await out.write(chunk)
    except Exception as exc:  # noqa: BLE001
        logger.info("crew turn stream to %s failed: %s", instance_id, type(exc).__name__)
        # The stream is already open, so the error has to be a FRAME: a status
        # code cannot be sent any more, and closing silently would leave the pane
        # waiting for a reply that is never coming.
        try:
            await out.write(
                b"data: " + json.dumps({"error": {"code": "crew_unreachable"}}).encode() + b"\n\n"
            )
            await out.write(b"data: [DONE]\n\n")
        except Exception:  # noqa: BLE001
            pass
    return out


def is_headless_crew(status: Any, provisioner_id: str) -> bool:
    """Whether this instance is a headless crew the pane can chat with.

    Exported for the instances list, so the frontend decides between "embed its
    dashboard" and "open the chat pane" from ONE field rather than re-deriving it
    from the connection method -- which would be wrong the moment a third lane
    reuses an existing method, and is already wrong for the EC2 lane, whose crews
    share the ``ssm`` method and do run a full gateway.

    Answers for a DISCONNECTED instance too: the pane has to be offered before
    the crew is connected, or the user has no way to reach the thing that
    connects it. So the provisioner alone decides the KIND, and the status
    decides whether a turn can be sent right now.
    """
    if provisioner_id in HEADLESS_CREW_PROVISIONERS:
        return True
    return bool(isinstance(status, dict) and status.get("turn_url"))
