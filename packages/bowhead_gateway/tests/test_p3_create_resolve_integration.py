"""CCA P3 — cross-slice integration: route CREATE → store → SubagentRunner RESOLVE.

The per-slice tests cover create (route → store) and resolve (hand-built TeamSubagent)
separately. This wires them through the SAME UserStore so a record created via the public
API resolves into the SubagentProfile the binding/advertise paths consume — catching any
create↔resolve field-shape drift (the cross-module gap real dogfood would otherwise find).
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.subagent import SubagentRunner
from bowhead_core.team_subagent_store import TeamSubagentStore
from bowhead_core.tool_registry import ToolRegistry
from bowhead_core.user_store import User, UserStore, _now
from bowhead_gateway.routes import team_subagents as osa
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _migrated_user_db(tmp_path, initialize_migrated_core_database):
    """``u.db`` 先复制真实 core ``upgrade head`` 快照：创建校验要读只由 Alembic 建的 ``team_builtin_tool``。"""
    initialize_migrated_core_database(tmp_path / "u.db")


@pytest.fixture(autouse=True)
def _creator_admitted(monkeypatch):
    """Admit only the creator ``u_a`` through the runtime admission seam.

    8e6f85c9a (fix(agents): require current release and active hire for member agents) moved the
    whole runtime decision (active hire, audience including creator_only, live release) into
    ``team_agent_runtime_allowed``; a freshly created definition has no release, so it is refused
    there. These cases cover what the resolver does with route-created records once admission
    answers; the admission criteria, including creator_only refusal, are covered by the core
    test_effective_authorization suite.
    """
    from bowhead_core.agents import plan_admission

    async def allowed(store: Any, *, team_id: str | None, user_id: str | None, agent_id: str) -> bool:
        return user_id == "u_a"

    monkeypatch.setattr(plan_admission, "team_agent_runtime_allowed", allowed)


def _client(store: UserStore) -> TestClient:
    now = _now()
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT OR IGNORE INTO teams(id,name,max_seats,status,created_at) VALUES (?,?,?,?,?)",
            ("team_a", "team_a", 10, "active", now),
        )
        db.execute(
            "INSERT OR IGNORE INTO users(id,team_id,email,status,created_at) VALUES (?,?,?,?,?)",
            ("u_a", "team_a", "a@x.com", "active", now),
        )
        db.execute(
            "INSERT OR IGNORE INTO team_members(id,user_id,team_id,role,status,created_at) VALUES (?,?,?,?,?,?)",
            ("tm_team_a_u_a", "u_a", "team_a", "member", "active", now),
        )
    subagents = TeamSubagentStore(store.db_path)
    _run(subagents.initialize())
    access = AccessControlStore(store.db_path)
    _run(access.initialize())
    app = FastAPI()
    app.include_router(osa.router, prefix="/api/v1/team-subagents")
    app.state.user_store = store
    app.state.team_subagent_schema_store = subagents
    app.state.access_control_store = access
    app.dependency_overrides[osa._current_user] = lambda: User(
        id="u_a",
        team_id="team_a",
        email="a@x.com",
    )
    return TestClient(app)


class _StubRouter:
    async def invoke(self, *a: Any, **kw: Any) -> Any:
        raise NotImplementedError

    def stream(self, *a: Any, **kw: Any) -> Any:
        raise NotImplementedError

    async def close(self) -> None:
        pass


def test_route_created_subagent_resolves_into_profile(tmp_path) -> None:
    store = UserStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    client = _client(store)

    # CREATE via the public API (S2 route + S4 model/output_schema)
    r = client.post(
        "/api/v1/team-subagents",
        json={
            "name": "sales-expert",
            "description": "answers sales questions",
            "system_prompt": "You are a focused sales analyst.",
            "tool_refs": ["bi_query", "bi_schema"],  # native, analyst-allowlisted
            "max_iterations": 12,
            "model": "analyst",
            "output_schema": {"type": "object", "additionalProperties": False, "properties": {"exec_summary": {"type": "string"}}},
        },
    )
    assert r.status_code == 200, r.text

    # RESOLVE through SubagentRunner against the SAME store (S3 team-store branch)
    runner = SubagentRunner(
        router=_StubRouter(),
        tool_registry=ToolRegistry(),  # type: ignore[arg-type]
        team_subagent_store=store,
    )
    # Creation uses route default visibility=creator_only (pinned by test_p3_team_subagents_routes.py);
    # runtime admission enforces that visibility (test_effective_authorization), so resolve as the
    # **creator**. user_id=None resolves as nobody, so should find nothing by design.
    p = _run(runner._resolve_profile("sales-expert", user_id="u_a", team_id="team_a"))

    assert p is not None
    assert p.name == "sales-expert"
    assert p.llm_agent_type == "analyst"  # S4 model clamp
    assert p.max_iterations == 12  # stored value, not dataclass 50
    assert set(p.tool_whitelist) == {"bi_query", "bi_schema", "subagent_return"}  # native + auto terminal
    assert p.team_tool_bundle == ()  # native-only → empty bundle (not None)
    assert p.output_schema == {"type": "object", "additionalProperties": False, "properties": {"exec_summary": {"type": "string"}}}  # S4 carry
    assert "focused sales analyst" in p.system_prompt

    # cross-team isolation: team_b can't resolve team_a's subagent
    assert _run(runner._resolve_profile("sales-expert", user_id="u_a", team_id="team_b")) is None
    # Callers the admission seam refuses (non-creators, anonymous) resolve nothing.
    assert _run(runner._resolve_profile("sales-expert", user_id=None, team_id="team_a")) is None
    assert _run(runner._resolve_profile("sales-expert", user_id="u_other", team_id="team_a")) is None


def test_route_created_disabled_subagent_not_resolved(tmp_path) -> None:
    store = UserStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    client = _client(store)

    created = client.post(
        "/api/v1/team-subagents",
        json={"name": "draft-agent", "system_prompt": "Test operating instructions.", "tool_refs": ["bi_query"]},
    )
    sid = created.json()["id"]
    disabled = client.put(
        f"/api/v1/team-subagents/{sid}/status",
        json={"status": "disabled"},
        headers={"If-Match": created.json()["etag"]},
    )
    assert disabled.status_code == 200

    runner = SubagentRunner(
        router=_StubRouter(),
        tool_registry=ToolRegistry(),  # type: ignore[arg-type]
        team_subagent_store=store,
    )
    # resolve only returns enabled subagents (only_enabled=True) → disabled is unreachable
    assert _run(runner._resolve_profile("draft-agent", user_id=None, team_id="team_a")) is None


def test_runner_without_store_cannot_resolve_team_subagent(tmp_path) -> None:
    """Regression for the dogfood-caught ship-blocker: the boot-time subagent_runner SINGLETON
    was built WITHOUT team_subagent_store (user_store didn't exist yet at ~main.py:825), and
    turn_runner REUSES that singleton for spawn EXECUTION — so team subagents were ADVERTISED to
    the Lead but FAILED to RESOLVE on spawn ('unknown profile: <name>'). This pins the invariant
    the lifespan fix satisfies (wire orch.subagent_runner.team_subagent_store once user_store
    exists): a runner MUST carry the store to resolve an team subagent, and setting it AFTER
    construction (what the fix does on the singleton) works."""
    store = UserStore(str(tmp_path / "u.db"))
    _run(store.initialize())
    created = _client(store).post(
        "/api/v1/team-subagents",
        json={"name": "sales-expert", "system_prompt": "Test operating instructions.", "tool_refs": ["bi_query"]},
    )
    assert created.status_code == 200, created.text

    # WITHOUT the store (the buggy boot-singleton state) → the team subagent is UNRESOLVABLE
    # Resolve as the **creator** (route defaults visibility=creator_only); otherwise None comes from visibility
    # denial rather than the missing-store wiring this test must pin.
    no_store = SubagentRunner(router=_StubRouter(), tool_registry=ToolRegistry())  # type: ignore[arg-type]
    assert _run(no_store._resolve_profile("sales-expert", user_id="u_a", team_id="team_a")) is None

    # set the store AFTER construction (exactly what the lifespan fix does on the singleton) → RESOLVES
    no_store.team_subagent_store = store
    assert _run(no_store._resolve_profile("sales-expert", user_id="u_a", team_id="team_a")) is not None
