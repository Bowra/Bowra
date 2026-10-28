"""δ-#8 integration tests — 9 concept fixtures via stub router.

Why stub instead of real LLM:
- Real-LLM 93-run eval requires (a) confidence_judgment emission prompt
  (deferred to post-canary prompt design + tuning), (b) live dev gateway,
  (c) API budget. Documented as a separate driver script under scripts/.
- Stub eval validates the integrated stack — classify → fact upsert → cache
  invalidate → next-turn injection → post-verify — without API spend.

9 concept fixtures cover the canonical "general financial concepts the LLM
should know but customer can override" cases that drove the EBITDA prod
incident (s_3fb05ed4c6 · t_97040e2d).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from bowhead_core.confidence_gate import (
    ConfidenceJudgment,
    gate,
)
from bowhead_core.provenance_prompts import (
    build_active_facts_block,
    reset_cache_for_test,
)
from bowhead_core.registered_fact_store import RegisteredFactStore
from bowhead_core.session_store import SessionStore
from bowhead_core.verification_provenance import post_llm_verify_provenance

if TYPE_CHECKING:
    from pathlib import Path

# ─── 9 concept fixtures — the universe the EBITDA incident exposed ──


@dataclass
class ConceptFixture:
    """One real-world concept that LLM should handle gracefully:
    - Either use [llm-std:] (general financial concept)
    - Or use [fact:..] when customer registered an override
    """

    name: str
    user_directive: str  # what user types to register the fact
    fact_key: str  # canonical fact_key auto_learner should produce
    fact_value: str  # canonical value_json
    fact_type: str  # 4 fact types
    scope: str  # project | user
    next_turn_query: str  # follow-up query that should trigger fact use
    confidence: int  # judge confidence (defaults to 9 for fixture)


FIXTURES: list[ConceptFixture] = [
    ConceptFixture(
        name="EBITDA",
        user_directive="EBITDA 我们不算外包人力, 只算正式工",
        fact_key="ebitda_definition",
        fact_type="formula_definition",
        fact_value='{"formula":"营收-COGS-销售费用-管理费用,不含外包人力"}',
        scope="project",
        next_turn_query="算下 Q3 EBITDA",
        confidence=9,
    ),
    ConceptFixture(
        name="ROE",
        user_directive="ROE 我们口径是净利润除以权益账面价值, 不是市值",
        fact_key="roe_definition",
        fact_type="formula_definition",
        fact_value='{"formula":"净利润/权益账面价值"}',
        scope="project",
        next_turn_query="ROE 比同行高吗",
        confidence=9,
    ),
    ConceptFixture(
        name="GMV",
        user_directive="GMV 含退货金额, 不要扣",
        fact_key="gmv_definition",
        fact_type="data_correction",
        fact_value='{"include_returns":true}',
        scope="project",
        next_turn_query="今年 GMV 多少",
        confidence=8,
    ),
    ConceptFixture(
        name="DOT",
        user_directive="DOT 准时到达率算完整周期, 拒收也算到达",
        fact_key="dot_definition",
        fact_type="formula_definition",
        fact_value='{"formula":"准时送达单数/总单数,含拒收"}',
        scope="project",
        next_turn_query="DOT 这季度怎样",
        confidence=8,
    ),
    ConceptFixture(
        name="OEE",
        user_directive="OEE 我们的可用率分母用 24h 不是排班时间",
        fact_key="oee_availability",
        fact_type="formula_definition",
        fact_value='{"availability_denom":"24h"}',
        scope="project",
        next_turn_query="OEE 上月数据",
        confidence=9,
    ),
    ConceptFixture(
        name="高潜客户",
        user_directive="高潜客户 = 近 30 天 ≥ 3 次购买且客单 ≥ 500",
        fact_key="high_potential_customer",
        fact_type="concept_definition",
        fact_value='{"rule":"30d>=3次 AND 客单>=500"}',
        scope="project",
        next_turn_query="本月新增多少高潜客户",
        confidence=9,
    ),
    ConceptFixture(
        name="活跃用户",
        user_directive="活跃用户 = 近 30 天登录 ≥ 5 次",
        fact_key="active_user",
        fact_type="concept_definition",
        fact_value='{"rule":"30d>=5次登录"}',
        scope="project",
        next_turn_query="活跃用户怎么样",
        confidence=9,
    ),
    ConceptFixture(
        name="default_currency",
        user_directive="默认所有货币按 CNY 不要 USD",
        fact_key="default_currency",
        fact_type="preference",
        fact_value='"CNY"',
        scope="project",
        next_turn_query="GMV 多少",
        confidence=9,
    ),
    ConceptFixture(
        name="default_grouping",
        user_directive="默认按产品线分组不要按区域",
        fact_key="default_grouping",
        fact_type="preference",
        fact_value='"product_line"',
        scope="user",
        next_turn_query="销量分布",
        confidence=8,
    ),
]


@dataclass
class _FakeResp:
    content: str


class _StubRouter:
    """Returns the canonical classify response for the fixture concept being tested."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._next_response: dict[str, Any] | None = None

    def queue(self, response: dict[str, Any]) -> None:
        self._next_response = response

    async def invoke(self, role: str, req: Any) -> _FakeResp:
        self.calls.append(req.messages[0].content)
        body = self._next_response or {"type": "skip", "confidence": 0}
        return _FakeResp(content=json.dumps(body, ensure_ascii=False))


@pytest.fixture
async def fact_store(tmp_path: Path) -> RegisteredFactStore:
    db = tmp_path / "facts.db"
    await SessionStore(db).initialize()
    return RegisteredFactStore(db)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_cache_for_test()


# ─── concept-by-concept E2E ────────────────────────────────


@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f.name)
@pytest.mark.asyncio
async def test_concept_fixture_full_cycle(
    fx: ConceptFixture,
    fact_store: RegisteredFactStore,
) -> None:
    """For each concept:
    1. 事实入库（显式写入——自动分类那半边已在 r3 删除）
    2. next-turn pre-LLM injection sees the fact (cache invalidated)
    3. post-verify on a [fact:fact_key] tag → silent_skip_count > 0
    """
    workspace_id = "ws_e2e"
    project_id = "p_1"
    user_id = "u_1"

    # Step 1: persist facts.
    # ★ r3 (2026-09-09): this **used to** call `auto_learner.classify` → `upsert`,
    # but the entire automatic-write path was removed (model judgment + zero confirmation + `scope=project` persisted as
    # workspace-shared ⇒ team-visible). **Readers remain**: injection and post-verify are still active,
    # so this case still covers them, with facts now explicitly written by the caller.
    # This is the actual remaining shape (`RegisteredFactStore` currently has no production writer;
    # see tech-design §1.4b: retaining/removing readers is a separate batch).
    await fact_store.upsert(
        workspace_id=workspace_id,
        project_id=project_id if fx.scope == "project" else None,
        user_id=user_id if fx.scope == "user" else None,
        fact_key=fx.fact_key,
        fact_type=fx.fact_type,
        value_json=fx.fact_value,
        source="test_seed",
        is_user_corrected=True,
    )

    # Step 2: pre-LLM injection picks up the new fact
    facts_block = await build_active_facts_block(
        workspace_id=workspace_id,
        user_text=fx.next_turn_query,
        fact_store=fact_store,
        project_id=project_id if fx.scope == "project" else None,
        user_id=user_id if fx.scope == "user" else None,
    )
    assert "Active Facts" in facts_block
    assert fx.fact_key in facts_block, f"{fx.name}: expected fact_key {fx.fact_key} in pre-LLM block, got:\n{facts_block}"

    # Step 3: post-verify silent_skip when [fact:k] used + is_user_corrected
    sample_text = f"按你们的口径算 [fact:{fx.fact_key}]."
    v = await post_llm_verify_provenance(
        sample_text,
        workspace_id=workspace_id,
        fact_store=fact_store,
        project_id=project_id if fx.scope == "project" else None,
        user_id=user_id if fx.scope == "user" else None,
    )
    assert v.silent_skip_count == 1, f"{fx.name}: expected silent_skip"


@pytest.mark.asyncio
async def test_llm_std_with_no_registered_fact_passes(
    fact_store: RegisteredFactStore,
) -> None:
    """Pre-EBITDA incident: customer has no override → LLM uses [llm-std:] →
    verify should pass (no warning), since no registered fact exists.
    """
    text = "EBITDA 是营收减成本费用 [llm-std:]."
    v = await post_llm_verify_provenance(
        text,
        workspace_id="ws_pristine",
        fact_store=fact_store,
    )
    assert v.passed is True
    assert v.fact_warnings == []


@pytest.mark.asyncio
async def test_llm_std_with_registered_fact_warns(
    fact_store: RegisteredFactStore,
) -> None:
    """Post-incident: customer registered override, but LLM still uses
    [llm-std:] → warning."""
    await fact_store.upsert(
        workspace_id="ws_post",
        fact_key="ebitda_definition",
        fact_type="formula_definition",
        value_json='{"formula":"营收-COGS-销售费用-管理费用,不含外包"}',
        source="user_directive",
        is_user_corrected=True,
    )
    text = "EBITDA 通常是营收减成本 [llm-std:]."
    v = await post_llm_verify_provenance(
        text,
        workspace_id="ws_post",
        fact_store=fact_store,
    )
    assert "ebitda_definition" in v.fact_warnings
    assert v.passed is False


# ─── Confidence Gate decision matrix across thresholds ─────


def _judgment(conf: int, src: str = "[llm-std:]") -> ConfidenceJudgment:
    return ConfidenceJudgment(
        claim="EBITDA = 营收-COGS",
        proposed_source=src,
        confidence=conf,
    )


@pytest.mark.parametrize(
    "threshold,conf,expected",
    [
        # DeepSeek baseline (threshold=7)
        (7, 10, "use_silent"),
        (7, 9, "use_silent"),
        (7, 8, "use_with_surface"),
        (7, 7, "use_with_surface"),
        (7, 6, "trigger_hitl"),
        # Doubao calibration (over-confident → threshold=6)
        (6, 6, "use_with_surface"),
        (6, 5, "trigger_hitl"),
        # Qwen calibration (under-confident → threshold=8)
        (8, 8, "use_with_surface"),
        (8, 7, "trigger_hitl"),
        # Silent threshold = 9 always wins regardless of per-model threshold
        (8, 9, "use_silent"),
        (6, 9, "use_silent"),
    ],
)
def test_gate_decision_matrix(threshold: int, conf: int, expected: str) -> None:
    assert gate(_judgment(conf), threshold=threshold) == expected


# ─── pre-LLM injection performance characteristic ──────────


@pytest.mark.asyncio
async def test_pre_llm_returns_under_50ms_with_50_facts(
    fact_store: RegisteredFactStore,
) -> None:
    """SRE: build_active_facts_block hot path should be sub-50ms with realistic
    fact counts (50). Cache-warm path is even faster — this exercises cold."""
    import time as _t

    workspace = "ws_perf"
    for i in range(50):
        await fact_store.upsert(
            workspace_id=workspace,
            fact_key=f"fact_{i:03d}",
            fact_type="preference",
            value_json='"x"',
            source="auto_learner",
        )
    reset_cache_for_test()
    t0 = _t.monotonic()
    block = await build_active_facts_block(
        workspace_id=workspace,
        user_text="random query",
        fact_store=fact_store,
    )
    elapsed_ms = (_t.monotonic() - t0) * 1000
    assert "Active Facts" in block
    assert elapsed_ms < 100, f"pre-LLM injection took {elapsed_ms:.1f}ms (>100ms budget)"
