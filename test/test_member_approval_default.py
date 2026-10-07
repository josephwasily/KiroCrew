"""A crewmate's approval mode: trust by default, the user's own choice preserved.

The mode lives on the crew record (``agents.<name>.approval_mode``), which is
what the Crewmates-page profile card writes. ``""`` means the user never chose
one, and only then does the crewmate's thread open in ``trust``. Any stored
choice wins, and a grant already on the live slot is never overwritten.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from test_members_dm_thread import CREW, _fake_config, _make_members_app

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.config.sections import coerce_member_approval_mode
from kiro_crew.members import member_slot_key


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _config_with(mode: str):
    cfg = _fake_config([CREW])
    cfg.agents[CREW] = KiroCrewAgentConfig(kiro_agent="kirocrew", approval_mode=mode)
    return cfg


async def _open_thread(state, cfg, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
    async with TestClient(TestServer(_make_members_app(state))) as client:
        resp = await client.post(f"/api/members/{CREW}/thread")
        assert resp.status == 200, await resp.text()
        return state._slots[(await resp.json())["slot_key"]]


def _policy_spy(state, monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        state.sessions, "set_approval_policy", lambda key, policy: calls.append((key, policy))
    )
    return calls


class TestThreadOpenSeedsTheProfileMode:
    @pytest.mark.asyncio
    async def test_a_mate_with_no_choice_opens_in_trust(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        calls = _policy_spy(state, monkeypatch)
        slot = await _open_thread(state, _config_with(""), monkeypatch)
        assert slot._trust is True
        assert slot._trust_reads is False
        assert calls and calls[-1][1] == "auto"

    @pytest.mark.asyncio
    async def test_a_stored_normal_is_kept(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        calls = _policy_spy(state, monkeypatch)
        slot = await _open_thread(state, _config_with("normal"), monkeypatch)
        assert slot._trust is False
        assert slot._trust_reads is False
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_stored_trust_reads_is_kept(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        _policy_spy(state, monkeypatch)
        slot = await _open_thread(state, _config_with("trust_reads"), monkeypatch)
        assert slot._trust is False
        assert slot._trust_reads is True

    @pytest.mark.asyncio
    async def test_a_grant_already_on_the_slot_is_not_overwritten(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        calls = _policy_spy(state, monkeypatch)
        live = state.get_or_create_slot(
            member_slot_key(CREW), agent=CREW, mode="member", workspace="default"
        )
        live._trust_reads = True
        slot = await _open_thread(state, _config_with(""), monkeypatch)
        assert slot is live
        assert slot._trust is False
        assert slot._trust_reads is True
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_change_after_the_seed_survives_a_reopen(self, tmp_path, monkeypatch):
        """The seed runs once per live slot: the user turning trust off on the
        thread is not undone the next time the page opens it."""
        state = _make_state(tmp_path)
        _policy_spy(state, monkeypatch)
        cfg = _config_with("")
        slot = await _open_thread(state, cfg, monkeypatch)
        assert slot._trust is True
        slot._trust = False
        again = await _open_thread(state, cfg, monkeypatch)
        assert again is slot
        assert again._trust is False


class TestRosterCarriesTheProfileSettings:
    @pytest.mark.asyncio
    async def test_the_row_names_mode_and_effort(self, tmp_path, monkeypatch):
        cfg = _config_with("trust_reads")
        cfg.agents[CREW].reasoning_effort = "high"
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_members_app(state))) as client:
            resp = await client.get("/api/members")
            assert resp.status == 200
            row = (await resp.json())["members"][0]
        assert row["approval_mode"] == "trust_reads"
        assert row["reasoning_effort"] == "high"


class TestTheRecordField:
    @pytest.mark.parametrize("mode", ["normal", "trust_reads", "trust"])
    def test_known_modes_survive(self, mode):
        assert coerce_member_approval_mode(mode) == mode

    @pytest.mark.parametrize("junk", [None, "", "yolo", "Trust", 1, ["trust"]])
    def test_anything_else_reads_as_never_chosen(self, junk):
        assert coerce_member_approval_mode(junk) == ""

    def test_a_saved_choice_round_trips_through_the_config_file(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["mate"] = KiroCrewAgentConfig(kiro_agent="kirocrew", approval_mode="normal")
        cfg.save()
        assert KiroCrewConfig.load().agents["mate"].approval_mode == "normal"


def _update_app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_agent_update

    app = web.Application()
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    return app


@pytest.fixture()
def seeded_mate():
    cfg = KiroCrewConfig.load()
    cfg.agents["mate"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", workspace="default", memory_store="default"
    )
    cfg.save()
    return "mate"


class TestProfileWrite:
    @pytest.mark.asyncio
    async def test_the_profile_stores_the_choice(self, seeded_mate):
        async with TestClient(TestServer(_update_app())) as client:
            resp = await client.put(f"/api/agents/{seeded_mate}", json={"approval_mode": "normal"})
            assert resp.status == 200, await resp.text()
        assert KiroCrewConfig.load().agents[seeded_mate].approval_mode == "normal"

    @pytest.mark.asyncio
    async def test_an_unrelated_edit_keeps_the_stored_choice(self, seeded_mate):
        async with TestClient(TestServer(_update_app())) as client:
            assert (
                await client.put(f"/api/agents/{seeded_mate}", json={"approval_mode": "normal"})
            ).status == 200
            assert (
                await client.put(f"/api/agents/{seeded_mate}", json={"description": "hi"})
            ).status == 200
        assert KiroCrewConfig.load().agents[seeded_mate].approval_mode == "normal"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["yolo", "", "TRUST", 1])
    async def test_yolo_unset_and_junk_are_refused(self, seeded_mate, bad):
        async with TestClient(TestServer(_update_app())) as client:
            resp = await client.put(
                f"/api/agents/{seeded_mate}", json={"workspace": "other", "approval_mode": bad}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_approval_mode"
        stored = KiroCrewConfig.load().agents[seeded_mate]
        assert stored.approval_mode == ""
        assert stored.workspace == "default"
