"""The last run of a streamed Slack answer is settled when its message ends.

The live stream holds back the trailing run of each chunk until more text shows
where it ends. These tests drive the native handler and pin what happens to that
held run at each point a message ends: the end of the turn, a ``wait`` boundary,
and a tool card. Clean text is sent, so the last word is not lost; text that
redacts differently as a whole is dropped and replaced by the final copy.
"""

from __future__ import annotations

import asyncio
import importlib

from conftest import MockSlackClient
from kiro_crew.acp.types import EVENT_TOOL_CALL
from kiro_crew.providers.base import LLMEvent
from kiro_crew.slack.handler import handle_message

_handler_tests = importlib.import_module("test_slack_handler")
FakeSessionManager = _handler_tests.FakeSessionManager
FakeProvider = _handler_tests.FakeProvider


class _StreamingSlack(MockSlackClient):
    def __init__(self) -> None:
        super().__init__()
        self._stream_enabled = True


class _TrackSessions(FakeSessionManager):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.calls = {"success": 0, "failure": 0}

    def record_success(self, key) -> None:
        self.calls["success"] += 1

    async def record_failure(self, key):
        self.calls["failure"] += 1
        return False


def _run(events: list[LLMEvent]) -> tuple[_StreamingSlack, _TrackSessions]:
    slack = _StreamingSlack()
    sessions = _TrackSessions(FakeProvider(events))
    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))
    return slack, sessions


def _appends(slack: _StreamingSlack) -> list[tuple[str, str]]:
    return [
        (payload["ts"], payload["text"])
        for name, payload in slack.actions
        if name == "append_stream"
    ]


def _text(kind_text: str) -> LLMEvent:
    return LLMEvent(kind="text_chunk", text=kind_text)


def test_the_last_word_of_a_clean_answer_reaches_the_stream():
    slack, sessions = _run([_text("The answer is "), _text("forty2")])

    assert "".join(text for _ts, text in _appends(slack)) == "The answer is forty2"
    assert sessions.calls == {"success": 1, "failure": 0}


def test_a_one_word_answer_is_delivered_and_counted():
    slack, sessions = _run([_text("done")])

    assert [text for _ts, text in _appends(slack)] == ["done"]
    assert sessions.calls == {"success": 1, "failure": 0}


def test_a_wait_boundary_ends_the_first_message_with_its_own_last_word():
    wait = LLMEvent(kind=EVENT_TOOL_CALL, title="wait", tool_name="wait", tool_call_id="t-wait")
    slack, _sessions = _run([_text("first part 42"), wait, _text("second part")])

    by_ts: dict[str, str] = {}
    for ts, text in _appends(slack):
        by_ts[ts] = by_ts.get(ts, "") + text
    messages = list(by_ts.values())
    assert len(messages) == 2
    assert messages[0].endswith("first part 42")
    assert "42" not in messages[1]
    assert messages[1].endswith("second part")


def test_text_before_a_tool_card_is_sent_before_the_card():
    tool = LLMEvent(
        kind=EVENT_TOOL_CALL, title="Running: read", tool_name="read", tool_call_id="t1"
    )
    slack, _sessions = _run([_text("Reading file42"), tool, _text("Done")])

    order = [
        (name, payload.get("text", ""))
        for name, payload in slack.actions
        if name in ("append_stream", "append_task")
    ]
    first_task = next(i for i, (name, _t) in enumerate(order) if name == "append_task")
    before = "".join(text for name, text in order[:first_task] if name == "append_stream")
    assert before == "Reading file42"


def test_a_held_run_is_replaced_by_the_final_copy_when_the_whole_text_redacts():
    slack, _sessions = _run([_text("The access key is AKIA"), _text("IOSFODNN7"), _text("EXAMPLE")])

    streamed = "".join(text for _ts, text in _appends(slack))
    assert "IOSFODNN7EXAMPLE" not in streamed
    final = [
        payload.get("text", "") for name, payload in slack.actions if name in ("update", "post")
    ]
    assert any("[REDACTED: credential]" in text for text in final)
