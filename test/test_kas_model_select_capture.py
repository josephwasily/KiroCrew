"""KAS reads its model list from the ``configOptions`` ``model`` select.

KAS sends no ``models`` object on ``session/new``; its served list is only the
``model`` select. These tests lock in that the capture reads it, so an account
that does not serve ``auto`` neither gets ``auto`` sent nor a "capacity" error
that advises setting ``auto``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from kiro_crew.acp.session_handle import (
    AcpSessionHandle,
    WatchdogSettings,
    models_from_config_options,
)
from kiro_crew.acp.transport_errors import _format_acp_error, _is_transient_raw_error
from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO

_FIXTURE = Path(__file__).parent / "fixtures" / "acp_frames" / "kas" / "session.jsonl"


def _make_handle(backend: str = ACP_BACKEND_KAS) -> AcpSessionHandle:
    runtime = MagicMock()
    runtime.acp_backend = backend
    runtime.is_alive.return_value = True
    runtime.send_request = AsyncMock(return_value=1)
    runtime.send_notification = AsyncMock()
    return AcpSessionHandle(
        session_id="sess-kas",
        queue=asyncio.Queue(),
        runtime=runtime,
        watchdog=WatchdogSettings(),
    )


def _kas_session_new(values: list[str], current: str) -> dict:
    """A KAS ``session/new`` result: no ``models``, list only in the select."""
    return {
        "sessionId": "sess-kas",
        "configOptions": [
            {
                "type": "select",
                "id": "model",
                "name": "Model",
                "category": "model",
                "currentValue": current,
                "options": [{"value": v, "name": v, "description": ""} for v in values],
            }
        ],
    }


def _recorded_session_new() -> dict:
    for line in _FIXTURE.read_text(encoding="utf-8").splitlines():
        frame = json.loads(line)
        result = frame.get("result") if isinstance(frame, dict) else None
        if isinstance(result, dict) and "sessionId" in result:
            return result
    raise AssertionError("no session/new result in the KAS fixture")


def test_recorded_kas_session_new_captures_the_select() -> None:
    resp = _recorded_session_new()
    assert resp.get("models") is None
    handle = _make_handle()
    handle.store_session_config(resp)
    assert handle._advertised_model_ids() == ["auto"]


def test_select_fold_answers_for_kas_and_not_for_kiro() -> None:
    resp = _kas_session_new(["claude-sonnet-4.5"], "claude-sonnet-4.5")
    envelope = models_from_config_options(resp, ACP_BACKEND_KAS)
    assert envelope is not None
    assert [m["modelId"] for m in envelope["availableModels"]] == ["claude-sonnet-4.5"]
    assert envelope["currentModelId"] == "claude-sonnet-4.5"
    assert models_from_config_options(resp, ACP_BACKEND_KIRO) is None


def test_unserved_auto_is_not_sent_to_kas() -> None:
    handle = _make_handle()
    handle.store_session_config(_kas_session_new(["claude-sonnet-4.5"], "claude-sonnet-4.5"))
    asyncio.run(handle.set_model("auto"))
    handle._runtime.send_request.assert_not_awaited()  # type: ignore[attr-defined]


def test_auto_rejection_on_kas_is_worded_as_no_access() -> None:
    handle = _make_handle()
    handle.store_session_config(_kas_session_new(["claude-sonnet-4.5"], "claude-sonnet-4.5"))
    error = {
        "code": -32603,
        "message": "Internal error",
        "data": "The model 'auto' is not available (request_id: 84d1abe6-6cb6-4ddf-956c-f33133f0ad14)",
    }
    advertised = handle._advertised_model_ids()
    text = _format_acp_error(error, advertised, backend=ACP_BACKEND_KAS)
    assert "does not have access to model 'auto'" in text
    assert "claude-sonnet-4.5" in text
    assert "capacity throttle" not in text
    assert _is_transient_raw_error(error, advertised) is False
