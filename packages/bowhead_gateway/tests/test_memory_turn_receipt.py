"""`GET /api/v1/memory/turn-receipt?turn_id=…`: the per-turn memory receipt (D7/D8).

The receipt is a REST projection of persisted state, so every assertion here reads the
same endpoint a live poll and a refresh use: undo and restore must change the derived
`state`, not a client-side flag.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import httpx2
import pytest
from bowhead_core.memory_auto_capture import CAPTURE_SOURCE
from bowhead_core.memory_turn_requests import MemoryTurnRequest, MemoryTurnRequestStore
from bowhead_core.session_store import SessionStore
from bowhead_core.user_settings_store import UserSettingsStore
from bowhead_core.user_store import UserStore
from bowhead_gateway.main import create_app
from bowhead_gateway.settings import Settings
from bowhead_memory.candidates import SETTINGS_KEY_DREAMING_PAUSED, MemoryCandidateStore, PauseStateCache

from bowhead_core import UserMemoryStore

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration


@pytest.fixture
async def app_ctx(tmp_path: Path, initialize_migrated_core_database):
    db = tmp_path / "t.db"
    initialize_migrated_core_database(db)
    mem = UserMemoryStore(db)
    await mem.initialize()
    settings_store = UserSettingsStore(db)
    await settings_store.init()
    user_store = UserStore(str(db))
    await user_store.initialize()
    sessions = SessionStore(db)
    await sessions.initialize()
    candidates = MemoryCandidateStore(db_path=str(db), fingerprint_key="receipt-test")
    await candidates.initialize()

    app = create_app()
    app.state.settings = Settings()
    app.state.user_memory_store = mem
    app.state.user_settings_store = settings_store
    app.state.user_store = user_store
    app.state.session_store = sessions
    app.state.memory_candidate_store = candidates
    app.state.pause_state_cache = PauseStateCache(settings_store=settings_store)
    app.state.orchestrator = None
    app.state.datarc = None
    return app


def _uid(app) -> str:
    return app.state.settings.dev_default_user


async def _turn(app, turn_id: str, *, user_id: str | None = None) -> None:
    sessions: SessionStore = app.state.session_store
    sess = await sessions.ensure_session(session_id=f"s_for_{turn_id}", workspace_id="ws", user_id=user_id or _uid(app))
    await sessions.record_turn(turn_id=turn_id, session_id=sess.id, user_text="anything")


async def _record(app, turn_id: str, request: MemoryTurnRequest, *, user_id: str | None = None) -> None:
    store = MemoryTurnRequestStore(app.state.user_memory_store.db_path)
    await store.record(user_id=user_id or _uid(app), turn_id=turn_id, request=request)


async def _entry(app, turn_id: str, *, source: str, key: str = "ranking_chart_type"):
    res = await app.state.user_memory_store.commit_memory_write(
        user_id=_uid(app),
        kind="preference",
        key=key,
        value=f"排名图用横向条形图（{key}）",
        source=source,
        created_source_turn_id=turn_id,
    )
    return res.entry


def _client(app):
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw")


async def _receipt(app, turn_id: str) -> httpx2.Response:
    async with _client(app) as c:
        return await c.get("/api/v1/memory/turn-receipt", params={"turn_id": turn_id})


async def test_a_turn_without_records_has_no_explicit_request(app_ctx) -> None:
    """Old turns and ordinary turns: `explicit_request` is null, never a false "not remembered"."""
    await _turn(app_ctx, "t_plain")
    r = await _receipt(app_ctx, "t_plain")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "schema_version": 1,
        "turn_id": "t_plain",
        "explicit_request": None,
        "pending_candidates": [],
        "entries": [],
    }


async def test_another_users_turn_is_not_found(app_ctx) -> None:
    """Only the turn's owner reads its receipt; rows written under the caller's id do not leak through it either."""
    await _turn(app_ctx, "t_other", user_id="usr_not_me")
    await _record(app_ctx, "t_other", MemoryTurnRequest("remember_tool", "not_remembered", reason="one_off"), user_id="usr_not_me")
    await _record(app_ctx, "t_other", MemoryTurnRequest("remember_tool", "remembered"))
    r = await _receipt(app_ctx, "t_other")
    assert r.status_code == 404
    assert (await _receipt(app_ctx, "t_missing")).status_code == 404


async def test_undo_restore_and_purge_derive_the_entry_state(app_ctx) -> None:
    await _turn(app_ctx, "t_1")
    entry = await _entry(app_ctx, "t_1", source="user_directive")
    await _record(app_ctx, "t_1", MemoryTurnRequest("remember_tool", "remembered", entry_id=entry.id))

    body = (await _receipt(app_ctx, "t_1")).json()
    assert body["explicit_request"] == {"source": "remember_tool", "outcome": "remembered", "reason": None}
    assert body["entries"] == [
        {
            "id": entry.id,
            "kind": "preference",
            "key": "ranking_chart_type",
            "display_value": "排名图用横向条形图（ranking_chart_type）",
            "origin": "remember_tool",
            "state": "remembered",
            "undoable": True,
            "candidate_id": None,
        }
    ]

    async with _client(app_ctx) as c:
        assert (await c.delete(f"/api/v1/memory/{entry.id}")).status_code == 200
    undone = (await _receipt(app_ctx, "t_1")).json()["entries"]
    assert [(e["state"], e["undoable"]) for e in undone] == [("undone", False)]

    async with _client(app_ctx) as c:
        assert (await c.post(f"/api/v1/memory/{entry.id}/restore")).status_code == 200
    assert [e["state"] for e in (await _receipt(app_ctx, "t_1")).json()["entries"]] == ["remembered"]

    with sqlite3.connect(app_ctx.state.user_memory_store.db_path) as db:
        db.execute("DELETE FROM memory_entries WHERE id=?", (entry.id,))
    assert (await _receipt(app_ctx, "t_1")).json()["entries"] == []


async def test_entries_name_their_origin_and_skip_unknown_writers(app_ctx) -> None:
    await _turn(app_ctx, "t_1")
    await _entry(app_ctx, "t_1", source=CAPTURE_SOURCE, key="auto_one")
    await _entry(app_ctx, "t_1", source="user_explicit", key="hand_written")
    await _entry(app_ctx, "t_2", source=CAPTURE_SOURCE, key="other_turn")
    entries = (await _receipt(app_ctx, "t_1")).json()["entries"]
    assert [(e["key"], e["origin"]) for e in entries] == [("auto_one", "capture")]


async def test_pending_candidates_of_the_turn_are_included(app_ctx) -> None:
    await _turn(app_ctx, "t_1")
    candidates: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    cand, _ = await candidates.enqueue_if_new_with_outcome(
        user_id=_uid(app_ctx),
        kind="preference",
        key="fiscal_year",
        value="财年从7月开始",
        reason=None,
        source_turn_id="t_1",
    )
    await candidates.enqueue_if_new_with_outcome(
        user_id=_uid(app_ctx), kind="preference", key="other", value="别的轮次", reason=None, source_turn_id="t_2"
    )
    await _record(app_ctx, "t_1", MemoryTurnRequest("nl_intent", "candidate", candidate_id=cand.id))
    body = (await _receipt(app_ctx, "t_1")).json()
    assert [c["id"] for c in body["pending_candidates"]] == [cand.id]
    assert body["explicit_request"] == {"source": "nl_intent", "outcome": "candidate", "reason": None}


async def test_a_skipped_capture_with_an_nl_intent_card_reports_no_refusal(app_ctx) -> None:
    """One sentence: capture skipped it, nl_intent made a card. The receipt must not say 「没有记住」."""
    await _turn(app_ctx, "t_1")
    candidates: MemoryCandidateStore = app_ctx.state.memory_candidate_store
    cand, _ = await candidates.enqueue_if_new_with_outcome(
        user_id=_uid(app_ctx), kind="preference", key="fiscal_year", value="财年从7月开始", reason=None, source_turn_id="t_1"
    )
    await _record(app_ctx, "t_1", MemoryTurnRequest("capture", "not_remembered", reason="draft_says_not_lasting"))
    await _record(app_ctx, "t_1", MemoryTurnRequest("nl_intent", "candidate", candidate_id=cand.id))
    body = (await _receipt(app_ctx, "t_1")).json()
    assert body["explicit_request"] == {"source": "nl_intent", "outcome": "candidate", "reason": None}
    assert [c["id"] for c in body["pending_candidates"]] == [cand.id]


async def test_only_allowlisted_reasons_are_named(app_ctx) -> None:
    await _turn(app_ctx, "t_1")
    await _record(app_ctx, "t_1", MemoryTurnRequest("capture", "not_remembered", reason="draft_failed"))
    assert (await _receipt(app_ctx, "t_1")).json()["explicit_request"] == {
        "source": "capture",
        "outcome": "not_remembered",
        "reason": None,
    }
    await _record(app_ctx, "t_1", MemoryTurnRequest("capture", "not_remembered", reason="rate_limited"))
    assert (await _receipt(app_ctx, "t_1")).json()["explicit_request"]["reason"] == "rate_limited"


async def test_the_language_setting_request_comes_from_the_recorded_row(app_ctx) -> None:
    """The language notice is no longer recomputed from `turn.user_text`; only the recorded capture outcome counts."""
    await _turn(app_ctx, "t_lang")
    assert (await _receipt(app_ctx, "t_lang")).json()["explicit_request"] is None
    await _record(app_ctx, "t_lang", MemoryTurnRequest("language_setting", "not_remembered", reason="trigger_language_setting"))
    assert (await _receipt(app_ctx, "t_lang")).json()["explicit_request"] == {
        "source": "language_setting",
        "outcome": "not_remembered",
        "reason": "trigger_language_setting",
    }


async def test_paused_learning_hides_automatic_requests_but_not_remember(app_ctx) -> None:
    uid = _uid(app_ctx)
    await _turn(app_ctx, "t_1")
    await _record(app_ctx, "t_1", MemoryTurnRequest("language_setting", "not_remembered", reason="trigger_language_setting"))
    await app_ctx.state.user_settings_store.set(uid, SETTINGS_KEY_DREAMING_PAUSED, "true")
    assert (await _receipt(app_ctx, "t_1")).json()["explicit_request"] is None
    await _record(app_ctx, "t_1", MemoryTurnRequest("remember_tool", "not_remembered", reason="one_off"))
    assert (await _receipt(app_ctx, "t_1")).json()["explicit_request"]["source"] == "remember_tool"


async def test_hidden_memory_returns_an_empty_receipt(app_ctx) -> None:
    """While a destructive privacy operation holds the barrier, every memory read fails closed."""
    uid = _uid(app_ctx)
    await _turn(app_ctx, "t_1")
    await _entry(app_ctx, "t_1", source="user_directive")
    await _record(app_ctx, "t_1", MemoryTurnRequest("remember_tool", "remembered"))
    with sqlite3.connect(app_ctx.state.user_memory_store.db_path) as db:
        db.execute(
            "INSERT INTO memory_privacy_state(user_id,barrier_status,updated_at) VALUES(?,'deleting','2026-10-05')",
            (uid,),
        )
    body = (await _receipt(app_ctx, "t_1")).json()
    assert body["explicit_request"] is None
    assert body["entries"] == []


@pytest.mark.unit
@pytest.mark.parametrize("locale", ["zh-CN", "en-US", "de-DE", "fr-FR"])
def test_every_displayable_reason_has_frontend_copy(locale) -> None:
    """The receipt names a reason only when the frontend can say it; the frontend owns the wording."""
    import json
    from pathlib import Path

    from bowhead_core.memory_turn_requests import DISPLAYABLE_REASONS

    catalog = Path(__file__).resolve().parents[2] / "bowhead_frontend" / "edition" / "shared-messages" / f"{locale}.json"
    table = json.loads(catalog.read_text(encoding="utf-8"))["memory_card"]["not_remembered_reason"]
    assert set(table) == DISPLAYABLE_REASONS
