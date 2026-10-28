"""Tests for sprint δ-#6 — verification_provenance.py pre + post hooks."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bowhead_core.provenance_prompts import reset_cache_for_test
from bowhead_core.registered_fact_store import RegisteredFactStore
from bowhead_core.session_store import SessionStore
from bowhead_core.verification_provenance import (
    ProvenanceVerification,
    post_llm_verify_provenance,
    pre_llm_inject_facts,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
async def fact_store(tmp_path: Path) -> RegisteredFactStore:
    db = tmp_path / "facts.db"
    await SessionStore(db).initialize()
    return RegisteredFactStore(db)


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    reset_cache_for_test()


# ─── pre_llm_inject_facts ──────────────────────────────────


@pytest.mark.asyncio
async def test_pre_llm_empty_when_no_facts(fact_store: RegisteredFactStore) -> None:
    out = await pre_llm_inject_facts(
        workspace_id="ws_1",
        user_text="EBITDA?",
        fact_store=fact_store,
    )
    assert out == ""


@pytest.mark.asyncio
async def test_pre_llm_returns_block(fact_store: RegisteredFactStore) -> None:
    await fact_store.upsert(
        workspace_id="ws_1",
        fact_key="ebitda_definition",
        fact_type="formula_definition",
        value_json='{"formula":"营收-COGS"}',
        source="user_directive",
    )
    out = await pre_llm_inject_facts(
        workspace_id="ws_1",
        user_text="What's EBITDA?",
        fact_store=fact_store,
    )
    assert "Active Facts" in out
    assert "ebitda_definition" in out


# ─── post_llm_verify_provenance ────────────────────────────


@pytest.mark.asyncio
async def test_post_verify_passes_with_proper_tags(
    fact_store: RegisteredFactStore,
) -> None:
    text = "Q3 GMV 是 12.7 亿 [bi:art_001], EBITDA 大约 3.2 亿 [bi:art_002]."
    v = await post_llm_verify_provenance(
        text,
        workspace_id="ws_1",
        fact_store=fact_store,
    )
    assert v.passed is True
    assert v.untagged_claim_count == 0


@pytest.mark.asyncio
async def test_post_verify_flags_untagged_claim(
    fact_store: RegisteredFactStore,
) -> None:
    text = "Q3 GMV 是 12.7 亿. EBITDA 是 3.2 亿."  # No bound tag.
    v = await post_llm_verify_provenance(
        text,
        workspace_id="ws_1",
        fact_store=fact_store,
    )
    assert v.passed is False
    assert v.untagged_claim_count >= 1


@pytest.mark.asyncio
async def test_post_verify_silent_skip_user_corrected(
    fact_store: RegisteredFactStore,
) -> None:
    """[fact:k] where fact.is_user_corrected=True → silent_skip_count++."""
    await fact_store.upsert(
        workspace_id="ws_1",
        fact_key="ebitda_definition",
        fact_type="formula_definition",
        value_json='{"formula":"营收-COGS-外包"}',
        source="user_directive",
        is_user_corrected=True,
    )
    text = "EBITDA 按你们的定义算 [fact:ebitda_definition]."
    v = await post_llm_verify_provenance(
        text,
        workspace_id="ws_1",
        fact_store=fact_store,
    )
    assert v.silent_skip_count == 1


@pytest.mark.asyncio
async def test_post_verify_warns_when_llm_std_ignores_fact(
    fact_store: RegisteredFactStore,
) -> None:
    """If LLM uses [llm-std:] but a registered fact for that concept exists,
    warn (LLM ignored override)."""
    await fact_store.upsert(
        workspace_id="ws_1",
        fact_key="ebitda_definition",
        fact_type="formula_definition",
        value_json='{"formula":"营收-COGS-外包"}',
        source="user_directive",
        is_user_corrected=True,
    )
    text = "EBITDA 通常是营收减成本 [llm-std:]."  # uses standard, ignores fact
    v = await post_llm_verify_provenance(
        text,
        workspace_id="ws_1",
        fact_store=fact_store,
    )
    assert "ebitda_definition" in v.fact_warnings
    assert v.passed is False


@pytest.mark.asyncio
async def test_post_verify_disabled_returns_passed(
    fact_store: RegisteredFactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOWHEAD_FACT_LOOKUP_ENABLED", "false")
    text = "Q3 GMV 是 12.7 亿."  # untagged, would normally fail
    v = await post_llm_verify_provenance(
        text,
        workspace_id="ws_1",
        fact_store=fact_store,
    )
    assert v.passed is True
    assert v.untagged_claim_count == 0


@pytest.mark.asyncio
async def test_provenance_verification_as_dict() -> None:
    v = ProvenanceVerification(
        passed=False,
        fact_warnings=["ebitda_definition"],
        untagged_claim_count=2,
        silent_skip_count=1,
    )
    d = v.as_dict()
    assert d["passed"] is False
    assert d["fact_warnings"] == ["ebitda_definition"]
    assert d["untagged_claim_count"] == 2
    assert d["silent_skip_count"] == 1
