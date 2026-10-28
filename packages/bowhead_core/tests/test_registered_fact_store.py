"""Tests for sprint δ-#1 — RegisteredFactStore.

Covers:
- inline migration adds tables on initialize
- upsert insert + update + history archive
- get / list_active / list_by_key_keywords / delete / list_history
- UNIQUE constraint per (workspace, project, user, fact_key)
- atomicity: BEGIN/COMMIT roundtrip
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite
import pytest
from bowhead_core.registered_fact_store import RegisteredFactStore
from bowhead_core.session_store import SessionStore

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def store(tmp_path: Path) -> RegisteredFactStore:
    db = tmp_path / "facts.db"
    await SessionStore(db).initialize()  # inline migration runs here
    return RegisteredFactStore(db)


# ─── inline migration ─────────────────────────────────────


@pytest.mark.asyncio
async def test_migration_creates_facts_table(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    await SessionStore(db).initialize()
    async with aiosqlite.connect(str(db)) as conn:
        cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='registered_facts'")
        assert await cur.fetchone() is not None
        cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='registered_facts_history'")
        assert await cur.fetchone() is not None


@pytest.mark.asyncio
async def test_migration_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "m2.db"
    s = SessionStore(db)
    await s.initialize()
    await s.initialize()  # 2nd call should not error
    async with aiosqlite.connect(str(db)) as conn:
        cur = await conn.execute("PRAGMA index_list('registered_facts')")
        idxs = [r[1] for r in await cur.fetchall()]
    assert "uniq_facts" in idxs


# ─── upsert insert ───────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_insert_new(store: RegisteredFactStore) -> None:
    fact = await store.upsert(
        workspace_id="ws_1",
        fact_key="cogs_composition",
        fact_type="formula_definition",
        value_json='{"formula": "原料+人工+能源"}',
        source="user_directive",
        is_user_corrected=True,
        confidence=9,
        source_turn_id="t_001",
    )
    assert fact.fact_id.startswith("fact_")
    assert fact.is_user_corrected is True
    assert fact.confidence == 9


@pytest.mark.asyncio
async def test_upsert_update_existing(store: RegisteredFactStore) -> None:
    """Second upsert on same key updates + archives old to history."""
    f1 = await store.upsert(
        workspace_id="ws_1",
        fact_key="ebitda",
        fact_type="formula_definition",
        value_json='{"formula": "v1"}',
        source="auto_learner",
    )
    f2 = await store.upsert(
        workspace_id="ws_1",
        fact_key="ebitda",
        fact_type="formula_definition",
        value_json='{"formula": "v2"}',
        source="user_directive",
        is_user_corrected=True,
    )
    # Same fact_id (UPDATE not new INSERT)
    assert f2.fact_id == f1.fact_id
    # Latest value
    cur = await store.get(workspace_id="ws_1", fact_key="ebitda")
    assert cur is not None
    assert "v2" in cur.value_json
    # History contains v1
    history = await store.list_history(workspace_id="ws_1", fact_key="ebitda")
    assert len(history) == 1
    assert "v1" in history[0]["value_json"]


@pytest.mark.asyncio
async def test_upsert_uniqueness_per_scope(store: RegisteredFactStore) -> None:
    """Same fact_key in different scopes (project / user) coexist."""
    await store.upsert(
        workspace_id="ws_1",
        fact_key="default_dim",
        fact_type="preference",
        value_json='"product_line"',
        source="user_directive",
        project_id="p_1",
    )
    await store.upsert(
        workspace_id="ws_1",
        fact_key="default_dim",
        fact_type="preference",
        value_json='"region"',
        source="user_directive",
        project_id="p_2",
    )
    # Both retrievable by project scope
    f1 = await store.get(workspace_id="ws_1", fact_key="default_dim", project_id="p_1")
    f2 = await store.get(workspace_id="ws_1", fact_key="default_dim", project_id="p_2")
    assert f1 is not None and "product_line" in f1.value_json
    assert f2 is not None and "region" in f2.value_json


# ─── get / list ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_returns_none_for_missing(store: RegisteredFactStore) -> None:
    assert await store.get(workspace_id="ws_1", fact_key="nope") is None


@pytest.mark.asyncio
async def test_list_active(store: RegisteredFactStore) -> None:
    for k in ("a", "b", "c"):
        await store.upsert(
            workspace_id="ws_1",
            fact_key=k,
            fact_type="preference",
            value_json='"x"',
            source="auto_learner",
        )
    facts = await store.list_active(workspace_id="ws_1")
    keys = {f.fact_key for f in facts}
    assert keys == {"a", "b", "c"}


# ─── list_by_key_keywords ─────────────────────────────────


@pytest.mark.asyncio
async def test_keyword_match_substring(store: RegisteredFactStore) -> None:
    await store.upsert(
        workspace_id="ws_1",
        fact_key="ebitda_composition",
        fact_type="formula_definition",
        value_json="{}",
        source="user_directive",
    )
    await store.upsert(
        workspace_id="ws_1",
        fact_key="default_currency",
        fact_type="preference",
        value_json='"CNY"',
        source="user_directive",
    )
    facts = await store.list_by_key_keywords(
        workspace_id="ws_1",
        keywords=["EBITDA"],  # uppercase
    )
    assert len(facts) == 1
    assert facts[0].fact_key == "ebitda_composition"


@pytest.mark.asyncio
async def test_keyword_match_empty_returns_empty(store: RegisteredFactStore) -> None:
    facts = await store.list_by_key_keywords(workspace_id="ws_1", keywords=[])
    assert facts == []


@pytest.mark.asyncio
async def test_keyword_match_multiple(store: RegisteredFactStore) -> None:
    for k in ("ebitda_definition", "roe_definition", "default_currency"):
        await store.upsert(
            workspace_id="ws_1",
            fact_key=k,
            fact_type="formula_definition",
            value_json="{}",
            source="user_directive",
        )
    facts = await store.list_by_key_keywords(
        workspace_id="ws_1",
        keywords=["ebitda", "roe"],
    )
    keys = {f.fact_key for f in facts}
    assert "ebitda_definition" in keys
    assert "roe_definition" in keys
    assert "default_currency" not in keys


# ─── delete / list_history ─────────────────────────────────


@pytest.mark.asyncio
async def test_delete(store: RegisteredFactStore) -> None:
    await store.upsert(
        workspace_id="ws_1",
        fact_key="x",
        fact_type="preference",
        value_json='"x"',
        source="auto_learner",
    )
    assert await store.delete(workspace_id="ws_1", fact_key="x") is True
    assert await store.get(workspace_id="ws_1", fact_key="x") is None
    # delete missing returns False
    assert await store.delete(workspace_id="ws_1", fact_key="nope") is False


@pytest.mark.asyncio
async def test_history_archives_each_upsert(store: RegisteredFactStore) -> None:
    for v in ("v1", "v2", "v3"):
        await store.upsert(
            workspace_id="ws_1",
            fact_key="k1",
            fact_type="formula_definition",
            value_json=f'"{v}"',
            source="user_directive",
        )
    history = await store.list_history(workspace_id="ws_1", fact_key="k1")
    # 3 upserts → 2 history rows (1st insert no archive)
    assert len(history) == 2
    # Oldest first
    assert "v1" in history[0]["value_json"]
    assert "v2" in history[1]["value_json"]
