"""Tests for sprint δ-#5 — provenance_prompts.py + provenance_flags.py."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bowhead_core.provenance_flags import (
    is_feature_enabled,
    is_provenance_hitl_enabled,
)
from bowhead_core.provenance_prompts import (
    _extract_keywords,
    build_active_facts_block,
    build_provenance_block,
    invalidate_cache,
    reset_cache_for_test,
)
from bowhead_core.registered_fact_store import RegisteredFactStore
from bowhead_core.session_store import SessionStore

if TYPE_CHECKING:
    from pathlib import Path

# ─── flags ─────────────────────────────────────────────────


def test_master_flag_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOWHEAD_PROVENANCE_HITL_ENABLED", raising=False)
    assert is_provenance_hitl_enabled() is True


def test_master_flag_explicit_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PROVENANCE_HITL_ENABLED", "false")
    assert is_provenance_hitl_enabled() is False
    monkeypatch.setenv("BOWHEAD_FACT_LOOKUP_ENABLED", "true")
    assert is_feature_enabled("FACT_LOOKUP") is False


def test_sub_flag_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PROVENANCE_HITL_ENABLED", "true")
    monkeypatch.setenv("BOWHEAD_FACT_LOOKUP_ENABLED", "false")
    monkeypatch.setenv("BOWHEAD_PROVENANCE_TAGS_ENABLED", "true")
    assert is_feature_enabled("FACT_LOOKUP") is False
    assert is_feature_enabled("PROVENANCE_TAGS") is True


# ─── build_provenance_block ────────────────────────────────


def test_provenance_block_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOWHEAD_PROVENANCE_HITL_ENABLED", raising=False)
    monkeypatch.delenv("BOWHEAD_PROVENANCE_TAGS_ENABLED", raising=False)
    block = build_provenance_block()
    assert "Provenance Rules" in block
    assert "[bi:art_id]" in block
    assert "[llm-rsn:" in block


def test_provenance_block_disabled_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PROVENANCE_TAGS_ENABLED", "false")
    assert build_provenance_block() == ""


def test_provenance_block_master_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_PROVENANCE_HITL_ENABLED", "false")
    assert build_provenance_block() == ""


# ─── _extract_keywords ─────────────────────────────────────


def test_keywords_basic_english() -> None:
    kws = _extract_keywords("What's the EBITDA trend for 2024?")
    assert "ebitda" in kws
    assert "trend" in kws
    assert "2024" in kws
    # stop-words filtered
    assert "what" not in kws
    assert "the" not in kws
    assert "for" not in kws


def test_keywords_chinese() -> None:
    kws = _extract_keywords("EBITDA 我们不算外包人力, 默认按产品线分组")
    assert "ebitda" in kws
    assert any("外包" in k or "人力" in k for k in kws)
    # stop-word filtered
    assert "我们" not in kws


def test_keywords_dedup() -> None:
    kws = _extract_keywords("ebitda EBITDA ebitda trend trend")
    assert kws.count("ebitda") == 1
    assert kws.count("trend") == 1


def test_keywords_short_filtered() -> None:
    kws = _extract_keywords("a b c GMV ROE Q1")
    # 1-char tokens dropped, ≥2-char kept
    assert "a" not in kws
    assert "gmv" in kws
    assert "roe" in kws
    assert "q1" in kws


def test_keywords_cap() -> None:
    text = " ".join(f"kw{i}" for i in range(50))
    kws = _extract_keywords(text, limit=5)
    assert len(kws) == 5


# ─── build_active_facts_block (real DB) ────────────────────


@pytest.fixture
async def fact_store(tmp_path: Path) -> RegisteredFactStore:
    db = tmp_path / "facts.db"
    await SessionStore(db).initialize()
    return RegisteredFactStore(db)


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    reset_cache_for_test()


@pytest.mark.asyncio
async def test_facts_block_empty_when_no_facts(fact_store: RegisteredFactStore) -> None:
    out = await build_active_facts_block(
        workspace_id="ws_1",
        user_text="EBITDA trend",
        fact_store=fact_store,
    )
    assert out == ""


@pytest.mark.asyncio
async def test_facts_block_keyword_match(fact_store: RegisteredFactStore) -> None:
    await fact_store.upsert(
        workspace_id="ws_1",
        fact_key="ebitda_definition",
        fact_type="formula_definition",
        value_json='{"formula":"营收-COGS-外包"}',
        source="user_directive",
    )
    await fact_store.upsert(
        workspace_id="ws_1",
        fact_key="default_currency",
        fact_type="preference",
        value_json='"CNY"',
        source="auto_learner",
    )
    out = await build_active_facts_block(
        workspace_id="ws_1",
        user_text="What is our EBITDA?",
        fact_store=fact_store,
    )
    assert "Active Facts" in out
    assert "ebitda_definition" in out
    # default_currency might also appear via recency fill, both OK


@pytest.mark.asyncio
async def test_facts_block_recency_fill_when_no_keyword_hits(
    fact_store: RegisteredFactStore,
) -> None:
    await fact_store.upsert(
        workspace_id="ws_1",
        fact_key="default_currency",
        fact_type="preference",
        value_json='"CNY"',
        source="auto_learner",
    )
    out = await build_active_facts_block(
        workspace_id="ws_1",
        user_text="random query about something else",
        fact_store=fact_store,
    )
    # No keyword hit → recency fills
    assert "default_currency" in out


@pytest.mark.asyncio
async def test_facts_block_truncates_long_value(fact_store: RegisteredFactStore) -> None:
    long_value = '"' + ("x" * 1000) + '"'
    await fact_store.upsert(
        workspace_id="ws_1",
        fact_key="big",
        fact_type="preference",
        value_json=long_value,
        source="auto_learner",
    )
    out = await build_active_facts_block(
        workspace_id="ws_1",
        user_text="big",
        fact_store=fact_store,
    )
    # Truncated with ellipsis
    assert "…" in out
    assert "x" * 1000 not in out  # original full value should not appear


@pytest.mark.asyncio
async def test_facts_block_disabled_returns_empty(
    fact_store: RegisteredFactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await fact_store.upsert(
        workspace_id="ws_1",
        fact_key="x",
        fact_type="preference",
        value_json='"y"',
        source="auto_learner",
    )
    monkeypatch.setenv("BOWHEAD_FACT_LOOKUP_ENABLED", "false")
    out = await build_active_facts_block(
        workspace_id="ws_1",
        user_text="x",
        fact_store=fact_store,
    )
    assert out == ""


@pytest.mark.asyncio
async def test_facts_block_master_off_returns_empty(
    fact_store: RegisteredFactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await fact_store.upsert(
        workspace_id="ws_1",
        fact_key="x",
        fact_type="preference",
        value_json='"y"',
        source="auto_learner",
    )
    monkeypatch.setenv("BOWHEAD_PROVENANCE_HITL_ENABLED", "false")
    out = await build_active_facts_block(
        workspace_id="ws_1",
        user_text="x",
        fact_store=fact_store,
    )
    assert out == ""


@pytest.mark.asyncio
async def test_facts_block_cache_invalidation(
    fact_store: RegisteredFactStore,
) -> None:
    """auto_learner write 后 invalidate_cache → 立即可见, 不等 60s TTL."""
    await fact_store.upsert(
        workspace_id="ws_1",
        fact_key="initial",
        fact_type="preference",
        value_json='"v1"',
        source="auto_learner",
    )
    out1 = await build_active_facts_block(
        workspace_id="ws_1",
        user_text="random query",
        fact_store=fact_store,
    )
    assert "initial" in out1

    # Add a new fact (would be cached out)
    await fact_store.upsert(
        workspace_id="ws_1",
        fact_key="brand_new_fact",
        fact_type="preference",
        value_json='"v2"',
        source="auto_learner",
    )
    invalidate_cache(workspace_id="ws_1")

    out2 = await build_active_facts_block(
        workspace_id="ws_1",
        user_text="random query",
        fact_store=fact_store,
    )
    assert "brand_new_fact" in out2
