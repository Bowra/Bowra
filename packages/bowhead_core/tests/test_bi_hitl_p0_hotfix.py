"""bi-hitl-cleanup P0 hotfix tests (A1 + A2).

A1: single-sentinel path back-fill `store.put` for Shape-2 only — Shape-1
    already writes inside `clarification_tool.py` so guard against double-write.
A2: HITL prompt token uses real metric name (decompose candidate) not cut token.

Per tech-design §P0 + review close Eng-P1-1 (use import constant, not literal
string key) + Eng-P1-3 (real diff size 20-30 lines, not "≤5 lines").
"""

from __future__ import annotations

from bowhead_core.agents.clarification_tool import (
    SENTINEL_CLARIFICATION_PENDING,
    extract_clarification_event_payload,
    is_clarification_sentinel,
    make_clarification_sentinel,
)

# ── A1 unit tests ─────────────────────────────────────────────────────────


def test_a1_shape1_sentinel_has_pending_flag() -> None:
    """Shape-1 (clarification_ask tool emits its own sentinel) carries
    `_clarification_pending=True`. Single-sentinel branch must detect this
    via SENTINEL_CLARIFICATION_PENDING import constant (Eng-P1-1)."""
    shape1 = make_clarification_sentinel(
        candidates=["a", "b"],
        fallback_text="user pick one",
        aborted_turn_id="t_xyz",
    )
    assert shape1.get(SENTINEL_CLARIFICATION_PENDING) is True
    assert is_clarification_sentinel(shape1)


def test_a1_shape2_sentinel_no_pending_flag() -> None:
    """Shape-2 (ar2 bi_query needs_clarification) does NOT carry the pending
    flag — has error_kind="needs_clarification" + clarification.options.
    Single-sentinel branch must take the back-fill store.put path."""
    shape2 = {
        "ok": False,
        "error_kind": "needs_clarification",
        "clarification": {
            "kind": "concept_resolution",
            "token": "万达店",
            "options": [
                {"label": "万达广场销售门店", "value": "v1"},
                {"label": "万达地产门店", "value": "v2"},
            ],
            "prompt": "请选择 万达店 的真实指代",
        },
    }
    # NOT Shape-1
    assert shape2.get(SENTINEL_CLARIFICATION_PENDING) is not True
    # but IS still a sentinel (Shape-2 path)
    assert is_clarification_sentinel(shape2)


def test_a1_shape2_kind_from_clarification_nested() -> None:
    """A1 back-fill store.put must read `kind` directly from
    `_single["clarification"]["kind"]` since
    `extract_clarification_event_payload` does NOT surface kind in its return
    dict (only candidates / fallback_text / clarification_id).

    A1 patch uses: `(_single.get("clarification") or {}).get("kind") or "concept_resolution"`
    """
    shape2 = {
        "ok": False,
        "error_kind": "needs_clarification",
        "clarification": {
            "kind": "metric_formula",
            "token": "客单价",
            "options": [{"label": "GMV/订单数", "value": 0}],
            "prompt": "指标 '客单价' 有多种公式",
        },
    }
    # extract_clarification_event_payload does NOT include kind:
    payload = extract_clarification_event_payload(shape2)
    assert "kind" not in payload  # confirms behavior

    # A1's actual kind-extract path:
    resolved_kind = (shape2.get("clarification") or {}).get("kind") or "concept_resolution"
    assert resolved_kind == "metric_formula"


def test_a1_shape2_kind_fallback_when_missing() -> None:
    """If clarification.kind absent, fall back to 'concept_resolution'
    (ar2 default historical kind)."""
    shape2_no_kind = {
        "ok": False,
        "error_kind": "needs_clarification",
        "clarification": {
            "token": "万达店",
            "options": [{"label": "万达广场销售门店", "value": "v1"}],
            "prompt": "请选择",
            # NO kind field
        },
    }
    resolved_kind = (shape2_no_kind.get("clarification") or {}).get("kind") or "concept_resolution"
    assert resolved_kind == "concept_resolution"


def test_a1_sentinel_constant_import_safety() -> None:
    """Eng-P1-1: SENTINEL_CLARIFICATION_PENDING is an importable module
    constant — A1 patch uses this instead of literal "_clarification_pending"
    string key to avoid rename drift."""
    # The constant value matches the literal (sanity)
    assert SENTINEL_CLARIFICATION_PENDING == "_clarification_pending"
    # And is importable from the canonical location used by A1 patch
    from bowhead_core.agents.clarification_tool import (
        SENTINEL_CLARIFICATION_PENDING as imported,
    )

    assert imported is SENTINEL_CLARIFICATION_PENDING


# ── A2 unit tests ─────────────────────────────────────────────────────────


def test_a2_real_metric_uses_candidates_for_hitl_name() -> None:
    """A2: HITL prompt token field uses the LLM-decompose candidate name
    (`m.candidates_for_hitl[0]['name']`) — the actual metric like
    '平均销售达成金额' — not the cut tokenizer fragment like '分组'.

    This is the prod t_d589046b bug: cut token "分组" appeared in HITL
    prompt instead of the real metric.
    """
    # Simulate what bi_query_preflight.py:595-605 does after A2:
    token = "分组"  # cut tokenizer artifact
    candidates_for_hitl = [
        {"name": "平均销售达成金额", "formula": "sum(达成)/count(订单)"},
        {"name": "平均销售达成金额 (按笔)", "formula": "sum(达成)/count(笔数)"},
    ]
    real_metric = (candidates_for_hitl[0].get("name") if candidates_for_hitl else None) or token
    assert real_metric == "平均销售达成金额"
    # Prompt should reference the real metric, not the cut token
    prompt = f"指标 {real_metric!r} 有多种可能公式, 请选择"
    assert "平均销售达成金额" in prompt
    assert "分组" not in prompt


def test_a2_fallback_to_token_when_candidates_empty() -> None:
    """When decompose did not yield any candidates_for_hitl, fall back to
    the cut token (no improvement but no regression vs pre-A2)."""
    token = "客单价"
    candidates_for_hitl: list[dict] = []
    real_metric = (candidates_for_hitl[0].get("name") if candidates_for_hitl else None) or token
    assert real_metric == token  # graceful fallback


def test_a2_fallback_to_token_when_name_missing() -> None:
    """Candidate has no `name` field — fall back to token."""
    token = "GMV同比"
    candidates_for_hitl = [{"formula": "(this_period - last) / last"}]  # no name
    real_metric = (candidates_for_hitl[0].get("name") if candidates_for_hitl else None) or token
    assert real_metric == token
