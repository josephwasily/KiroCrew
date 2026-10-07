"""Resuming a closed chat from Slack must say so, not claim "Session resumed".

``DashboardState.link_slack`` reports a missing slot (False + a warning), and
``_handle_resume_choice`` turns that into an honest reply and drops the link.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog
from kiro_crew.slack import interactions as ix


def _make_state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.get_slack_link = MagicMock(return_value=(None, None))
    sessions.get_mirror_link = MagicMock(return_value=None)
    sessions.mirror_accepts_inbound = MagicMock(return_value=False)
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )


class TestLinkSlackSignal:
    def test_missing_slot_returns_false_and_warns(self, tmp_path, caplog) -> None:
        state = _make_state(tmp_path)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.state"):
            assert state.link_slack("chat-gone", "1.2", "C1") is False
        assert any("chat-gone" in r.getMessage() for r in caplog.records)
        state.sessions.set_slack_link.assert_not_called()

    def test_present_slot_links_and_returns_true(self, tmp_path, caplog) -> None:
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.state"):
            assert state.link_slack("s1", "1.2", "C1") is True
        assert slot._slack_linked is True
        assert slot._slack_thread_ts == "1.2"
        assert not any("s1" in r.getMessage() for r in caplog.records)


@pytest.fixture
def aiohttp_sess():
    sess = AsyncMock()
    sess.__aenter__ = AsyncMock(return_value=sess)
    sess.__aexit__ = AsyncMock(return_value=None)
    sess.post = AsyncMock(return_value=MagicMock(status=200))
    with patch("aiohttp.ClientSession", return_value=sess):
        yield sess


@pytest.fixture
def orch(monkeypatch: pytest.MonkeyPatch, tmp_path) -> MagicMock:
    o = MagicMock()
    o.slack.post_message = AsyncMock(return_value="ts1")
    o.slack.update_message = AsyncMock()
    o.slack.open_dm = AsyncMock(return_value="D1")
    o.sessions.get_slack_link = MagicMock(return_value=("", ""))
    o.dashboard_state = MagicMock()
    monkeypatch.setattr(ix, "_orch", o)
    monkeypatch.setattr(ix, "is_allowed_user", lambda uid: True)
    monkeypatch.setattr(ix, "is_owner", lambda uid: True)
    monkeypatch.setattr("kiro_crew.slack.handler.is_owner", lambda uid: True)
    monkeypatch.setattr(ix, "channel_inbound_permitted", AsyncMock(return_value=True))
    monkeypatch.setattr("kiro_crew.config.loader.data_home", lambda: tmp_path)
    monkeypatch.setattr(ix, "_resume_locks", {})
    return o


async def _resume(mode: str, key: str = "dashboard:s1") -> None:
    payload = {
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "message": {"ts": "m1", "blocks": []},
        "response_url": "https://hooks.slack.com/z",
    }
    action = {
        "action_id": f"mc_resume_{mode}_x",
        "value": json.dumps({"key": key, "title": "My chat", "src_channel": "C5"}),
    }
    await ix._handle_resume_choice(payload, action, "C1", "m1", "U1", mode=mode)


class TestResumeReply:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode,channel", [("thread", "C5"), ("dm", "D1")])
    async def test_closed_chat_gets_honest_reply(
        self, orch: MagicMock, aiohttp_sess: AsyncMock, mode: str, channel: str
    ) -> None:
        orch.dashboard_state.link_slack = MagicMock(return_value=False)
        await _resume(mode)
        orch.dashboard_state.link_slack.assert_called_once_with("s1", "ts1", channel)
        text = aiohttp_sess.post.await_args.kwargs["json"]["text"]
        assert text == ix._RESUME_CLOSED_MSG
        assert "resumed" not in text.lower().replace("could not be resumed", "")
        orch.slack.update_message.assert_awaited_once_with(channel, "ts1", ix._RESUME_CLOSED_MSG)
        orch.sessions.clear_slack_link.assert_called_once_with("dashboard:s1")

    @pytest.mark.asyncio
    async def test_live_chat_still_says_resumed(
        self, orch: MagicMock, aiohttp_sess: AsyncMock
    ) -> None:
        orch.dashboard_state.link_slack = MagicMock(return_value=True)
        await _resume("thread")
        orch.slack.post_message.assert_any_await(
            "C5", "🧵 *My chat*\nSession resumed. Continue the conversation in this thread."
        )
        text = aiohttp_sess.post.await_args.kwargs["json"]["text"]
        assert text == "▶️ Resumed *My chat* in thread."
        orch.slack.update_message.assert_not_awaited()
        orch.sessions.clear_slack_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_without_dashboard_slot_still_resumes(
        self, orch: MagicMock, aiohttp_sess: AsyncMock
    ) -> None:
        # A task-runner session never has a dashboard slot, so False is not "closed".
        orch.dashboard_state.link_slack = MagicMock(return_value=False)
        await _resume("thread", key="taskrunner_run_t1")
        text = aiohttp_sess.post.await_args.kwargs["json"]["text"]
        assert text == "▶️ Resumed *My chat* in thread."
        orch.slack.update_message.assert_not_awaited()
        orch.sessions.clear_slack_link.assert_not_called()
