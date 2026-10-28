"""Sprint followup-context-deck — B1 (preserve deliverable intent) + P0-C
(resolve idempotency) for the clarification routes."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx2
import pytest
from bowhead_core.clarification_store import (
    ClarificationCandidate,
    ClarificationStore,
)
from bowhead_gateway.main import create_app
from bowhead_gateway.routes.clarifications import _merge_clarified_user_text
from bowhead_gateway.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path

# ── B1: deliverable intent survives the clarification (pure helper) ──────────


def test_b1_appends_pick_preserving_ppt_intent() -> None:
    # B1 (clarification-true-continuation): helper now takes the full answer LIST.
    merged = _merge_clarified_user_text("好你帮我生成一个分析ppt？", ["销售业绩分析（整体/区域/产品线）"])
    # the original "ppt" ask must survive (so Lead routes to consultant/deck)
    assert "ppt" in merged
    assert "销售业绩分析" in merged
    # not the bare topic (the bug)
    assert merged != "销售业绩分析（整体/区域/产品线）"


def test_b1_empty_original_falls_back_to_pick() -> None:
    assert _merge_clarified_user_text("", ["最新营业额"]) == "最新营业额"
    assert _merge_clarified_user_text("   ", ["最新营业额"]) == "最新营业额"


def test_b1_no_pick_keeps_original() -> None:
    assert _merge_clarified_user_text("生成ppt", []) == "生成ppt"


# ── P0-C: resolve idempotency (route) ───────────────────────────────────────


@pytest.fixture
async def app_ctx(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "dev")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "1")
    clarify = ClarificationStore(tmp_path / "c.db")
    await clarify.initialize()
    with sqlite3.connect(tmp_path / "c.db") as connection:
        connection.execute(
            "CREATE TABLE clarification_resolution_outbox ("
            "request_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, "
            "state TEXT NOT NULL, channel TEXT NOT NULL, work_json TEXT NOT NULL, "
            "payload_json TEXT, lease_owner TEXT, lease_expires_at TEXT, "
            "attempt_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
    from bowhead_core.user_memory import ProjectMemoryStore, UserMemoryStore

    user_mem = UserMemoryStore(tmp_path / "m.db")
    await user_mem.initialize()
    proj_mem = ProjectMemoryStore(tmp_path / "m.db")
    await proj_mem.initialize()
    app = create_app()
    app.state.settings = Settings()
    app.state.settings.auth_mode = "dev"
    app.state.clarification_store = clarify
    app.state.user_memory_store = user_mem
    app.state.project_memory_store = proj_mem
    return app


@pytest.fixture
async def http(app_ctx):
    transport = httpx2.ASGITransport(app=app_ctx)
    async with httpx2.AsyncClient(transport=transport, base_url="http://gw") as c:
        yield c


@pytest.mark.integration
async def test_p0c_resolve_is_idempotent(http, app_ctx, monkeypatch) -> None:
    class _ReadySessionStore:
        async def is_turn_resume_ready(self, _turn_id: str) -> bool:
            return True

    async def _resume_stub(request_id, _request, _response, _clarify):
        return SimpleNamespace(
            request_id=request_id,
            stream_url="/api/v1/chat/stream/t_ppt",
            execution_generation=2,
        )

    async def _durable_event_stub(*_args, **_kwargs) -> None:
        return None

    app_ctx.state.orchestrator = SimpleNamespace(
        session_store=_ReadySessionStore(),
    )
    monkeypatch.setattr(
        "bowhead_gateway.routes.clarifications.resume_turn",
        _resume_stub,
    )
    monkeypatch.setattr(
        "bowhead_gateway.routes.clarifications._ensure_clarification_resolved_event",
        _durable_event_stub,
    )
    store: ClarificationStore = app_ctx.state.clarification_store
    req = await store.put(
        turn_id="t_ppt",
        user_id="u_dev_alice",
        workspace_id="42",
        token="",
        rationale="主题不明",
        kind="options",
        candidates=[
            ClarificationCandidate(value="销售业绩分析", label="销售业绩分析"),
            ClarificationCandidate(value="客户与渠道分析", label="客户与渠道分析"),
        ],
        resume_context={"user_text": "好你帮我生成一个分析ppt？", "session_id": "s1"},
    )

    # First resolve — picks 销售业绩分析.
    r1 = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_values": ["销售业绩分析"]},
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["chosen_value"] == "销售业绩分析"
    assert r1.json()["action"] == "turn_resume"

    # Second resolve — user clicks a DIFFERENT option 19s later (the trace).
    # Must be idempotent: returns the ORIGINAL pick, does NOT switch to the new
    # value, and the store is unchanged.
    r2 = await http.post(
        f"/api/v1/clarifications/{req.request_id}/resolve",
        json={"chosen_values": ["客户与渠道分析"]},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["chosen_value"] == "销售业绩分析", "second resolve must NOT switch the answer"
    assert r2.json()["action"] == "turn_resume"  # FE can still reattach to resume

    post = await store.get(req.request_id)
    assert post.status == "resolved"
    assert post.chosen_value == "销售业绩分析"
