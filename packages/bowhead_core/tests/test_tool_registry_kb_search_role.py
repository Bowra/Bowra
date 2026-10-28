"""V7 kb-agentic-fix Day 3: ctx.kb_role → ScopeContext.role 透传.

Tech design v1.1 §2.6 — kb_role propagates from TurnRequest → ToolExecutionContext
→ ScopeContext for retriever invocation.
"""

from __future__ import annotations

import pytest
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry


class _MockKbRetriever:
    def __init__(self) -> None:
        self.last_scope_role: str | None = None

    async def invoke(  # type: ignore[no-untyped-def]
        self,
        *,
        query: str,
        scope_ctx,
        top_k: int = 5,
        scope_filter=None,
    ):
        from bowhead_core.kb.schemas import KbSearchResult

        self.last_scope_role = scope_ctx.role
        return KbSearchResult(
            chunks=[],
            total_candidates=0,
            rerank_used=False,
            latency_ms=1,
        )


async def _invoke_kb_search(kb_role: str) -> str:
    """Helper: build ctx with given kb_role, dispatch kb.search, return ScopeContext.role."""
    reg = build_default_registry()
    retriever = _MockKbRetriever()
    ctx = ToolExecutionContext(
        workspace_id="ws_test",
        member_uid="m1",
        user_id="u1",
        turn_id="t1",
        datarc=None,  # type: ignore[arg-type]
        kb_retriever=retriever,
        team_id="team_1",
        kb_role=kb_role,
    )
    await reg.execute("kb_search", {"query": "test"}, ctx)
    return retriever.last_scope_role or ""


async def test_kb_role_user_default() -> None:
    """kb_role='user' → ScopeContext.role='user'."""
    role = await _invoke_kb_search("user")
    assert role == "user"


async def test_kb_role_team_admin_propagates() -> None:
    """kb_role='team_admin' → ScopeContext.role='team_admin' (P0-1 修)."""
    role = await _invoke_kb_search("team_admin")
    assert role == "team_admin"


async def test_kb_role_invalid_falls_back_to_user() -> None:
    """无效 kb_role (e.g. 老 ctx 跑过来) → 防御 fallback 'user'."""
    role = await _invoke_kb_search("hacker_role")
    assert role == "user"


# ─────────────────────────────────────────────────────────────
# cca LOAD POINT D1 — KB ScopeContext.team_id under the team_id populate
# ─────────────────────────────────────────────────────────────
class _TeamCaptureRetriever:
    def __init__(self) -> None:
        self.last_scope_team_id: str | None = None

    async def invoke(  # type: ignore[no-untyped-def]
        self,
        *,
        query: str,
        scope_ctx,
        top_k: int = 5,
        scope_filter=None,
    ):
        from bowhead_core.kb.schemas import KbSearchResult

        self.last_scope_team_id = scope_ctx.team_id
        return KbSearchResult(chunks=[], total_candidates=0, rerank_used=False, latency_ms=1)


async def _scope_team_id(*, team_id: str, workspace_id: str) -> str:
    reg = build_default_registry()
    retr = _TeamCaptureRetriever()
    ctx = ToolExecutionContext(
        workspace_id=workspace_id,
        member_uid="m1",
        user_id="u1",
        turn_id="t1",
        datarc=None,  # type: ignore[arg-type]
        kb_retriever=retr,
        team_id=team_id,
        kb_role="user",
    )
    await reg.execute("kb_search", {"query": "x"}, ctx)
    return retr.last_scope_team_id or ""


@pytest.mark.asyncio
async def test_kb_scope_team_id_uses_real_team_id_when_populated() -> None:
    """cca LOAD POINT D1 (confirmed: keep populate): when the turn populates ctx.team_id
    (JWT-derived, != workspace_id), KB team-collection routing + team-scope permission use
    the REAL team_id (aligns with the gateway KB endpoints), NOT the workspace_id fallback."""
    assert await _scope_team_id(team_id="team_real", workspace_id="ws_42") == "team_real"


@pytest.mark.asyncio
async def test_kb_scope_team_id_stays_empty_when_team_less() -> None:
    """批1/c5: team-less turns (ctx.team_id == "") stay fail-closed — the workspace_id
    fallback is retired (a space id never stands in for an team id)."""
    assert await _scope_team_id(team_id="", workspace_id="ws_42") == ""
