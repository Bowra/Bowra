"""bi-hitl-cleanup P5.1 / B5.1 tests — ClarificationKind 8 enum 维护 (deprecated tier).

Per tech-design §P5.1 + PRD §B5 enum 维护:
- VALID_CLARIFICATION_KINDS size = 8 (backward read compat, includes deprecated)
- _DEPRECATED_CLARIFICATION_KINDS = {"concept_resolution", "metric_formula"}
  (`ambiguous_turn` 不入 set per P0-CTO-3)
- Writing a deprecated kind logs WARN via _warn_if_deprecated_kind
- Active-kind writes log NO WARN
- I4 (will be checked in dogfood P10): post-B3 prod logs SHOULD have 0
  WARN lines for these kinds.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pytest
from bowhead_core.clarification_store import (
    _DEPRECATED_CLARIFICATION_KINDS,
    VALID_CLARIFICATION_KINDS,
    ClarificationCandidate,
    ClarificationStore,
    _warn_if_deprecated_kind,
)

# ── Enum size + content ────────────────────────────────────────────────


def test_b51_valid_kinds_includes_9_total() -> None:
    """9 kinds total: keep all (deprecated stay for backward read compat).

    批2 (workspace-space-split PRD §3.3b) 契约演进：+`tool_context_select`
    （北极九章工作区选择——server-emit sentinel，resolve 写 user 偏好）。"""
    assert len(VALID_CLARIFICATION_KINDS) == 9
    expected = {
        "token",
        "options",
        "task_plan",
        "business_rule_review",
        "ambiguous_turn",
        "formula_confirm",
        "tool_context_select",
        "concept_resolution",
        "metric_formula",
    }
    assert expected == VALID_CLARIFICATION_KINDS


def test_b51_deprecated_set_correct() -> None:
    """Per P0-CTO-3: ar2 Shape-2 kinds plus the retired business-rule review are deprecated.
    `ambiguous_turn` is active V4 surface — must NOT be in the deprecated set."""
    assert {
        "concept_resolution",
        "metric_formula",
        "business_rule_review",
    } == _DEPRECATED_CLARIFICATION_KINDS
    assert "ambiguous_turn" not in _DEPRECATED_CLARIFICATION_KINDS
    assert "options" not in _DEPRECATED_CLARIFICATION_KINDS
    assert "task_plan" not in _DEPRECATED_CLARIFICATION_KINDS


def test_b51_deprecated_set_subset_of_valid() -> None:
    """Deprecated kinds remain VALID (writes must succeed for back-compat)."""
    assert _DEPRECATED_CLARIFICATION_KINDS <= VALID_CLARIFICATION_KINDS


# ── _warn_if_deprecated_kind helper ────────────────────────────────────


def test_b51_warn_helper_logs_on_deprecated(caplog) -> None:
    """concept_resolution / metric_formula trigger WARN."""
    with caplog.at_level(logging.WARNING, logger="bowhead_core.clarification_store"):
        _warn_if_deprecated_kind("concept_resolution")
    assert any("clarification_store.deprecated_kind_write" in r.message and "concept_resolution" in r.message for r in caplog.records)


def test_b51_warn_helper_logs_on_metric_formula(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="bowhead_core.clarification_store"):
        _warn_if_deprecated_kind("metric_formula")
    assert any("clarification_store.deprecated_kind_write" in r.message and "metric_formula" in r.message for r in caplog.records)


def test_b51_warn_helper_silent_on_active_kind(caplog) -> None:
    """Active kinds (options / ambiguous_turn / token / etc.) emit NO WARN."""
    with caplog.at_level(logging.WARNING, logger="bowhead_core.clarification_store"):
        _warn_if_deprecated_kind("options")
        _warn_if_deprecated_kind("ambiguous_turn")
        _warn_if_deprecated_kind("token")
        _warn_if_deprecated_kind("task_plan")
        _warn_if_deprecated_kind("formula_confirm")
    warns = [r for r in caplog.records if r.levelno >= logging.WARNING and "deprecated_kind_write" in r.message]
    assert warns == []


# ── Integration: store.put logs WARN for deprecated kind ──────────────


@pytest.mark.asyncio
async def test_b51_put_with_deprecated_concept_resolution_logs_warn(caplog) -> None:
    """ClarificationStore.put with deprecated kind → row written + WARN logged."""
    with tempfile.TemporaryDirectory() as td:
        store = ClarificationStore(db_path=Path(td) / "c.db")
        await store.initialize()
        with caplog.at_level(logging.WARNING, logger="bowhead_core.clarification_store"):
            req = await store.put(
                turn_id="t_p51_1",
                user_id="u_x",
                workspace_id="ws_x",
                token="华东",
                rationale="abstract dim_value",
                candidates=[
                    ClarificationCandidate(value="华东省", label="华东省"),
                    ClarificationCandidate(value="华东集团", label="华东集团"),
                ],
                kind="concept_resolution",
            )
        # Row persists (backward read compat)
        assert req.kind == "concept_resolution"
        # WARN emitted with the deprecated kind
        assert any("deprecated_kind_write" in r.message and "concept_resolution" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_b51_put_with_deprecated_metric_formula_logs_warn(caplog) -> None:
    with tempfile.TemporaryDirectory() as td:
        store = ClarificationStore(db_path=Path(td) / "c.db")
        await store.initialize()
        with caplog.at_level(logging.WARNING, logger="bowhead_core.clarification_store"):
            req = await store.put(
                turn_id="t_p51_2",
                user_id="u_x",
                workspace_id="ws_x",
                token="客单价",
                rationale="derived metric",
                candidates=[
                    ClarificationCandidate(value="GMV/订单数", label="GMV/订单数"),
                    ClarificationCandidate(value="净销售额/订单数", label="净销售额/订单数"),
                ],
                kind="metric_formula",
            )
        assert req.kind == "metric_formula"
        assert any("deprecated_kind_write" in r.message and "metric_formula" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_b51_put_with_active_kind_no_warn(caplog) -> None:
    """Active kind write does NOT log the deprecated WARN."""
    with tempfile.TemporaryDirectory() as td:
        store = ClarificationStore(db_path=Path(td) / "c.db")
        await store.initialize()
        with caplog.at_level(logging.WARNING, logger="bowhead_core.clarification_store"):
            await store.put(
                turn_id="t_p51_3",
                user_id="u_x",
                workspace_id="ws_x",
                token="销售",
                rationale="active",
                candidates=[ClarificationCandidate(value="A", label="A")],
                kind="options",
            )
        deprecated_warns = [r for r in caplog.records if "deprecated_kind_write" in r.message]
        assert deprecated_warns == []


@pytest.mark.asyncio
async def test_b51_put_with_ambiguous_turn_no_warn(caplog) -> None:
    """`ambiguous_turn` is active (V4 surface) — must NOT trigger deprecated WARN."""
    with tempfile.TemporaryDirectory() as td:
        store = ClarificationStore(db_path=Path(td) / "c.db")
        await store.initialize()
        with caplog.at_level(logging.WARNING, logger="bowhead_core.clarification_store"):
            await store.put(
                turn_id="t_p51_4",
                user_id="u_x",
                workspace_id="ws_x",
                token="(turn-scope)",
                rationale="user ambiguous",
                candidates=[ClarificationCandidate(value="X", label="X")],
                kind="ambiguous_turn",
            )
        deprecated_warns = [r for r in caplog.records if "deprecated_kind_write" in r.message]
        assert deprecated_warns == []


# ── Backward read compat: unknown kind still rejected ─────────────────


@pytest.mark.asyncio
async def test_b51_unknown_kind_still_raises() -> None:
    """Schema enforcement preserved — typo / made-up kind still raises."""
    with tempfile.TemporaryDirectory() as td:
        store = ClarificationStore(db_path=Path(td) / "c.db")
        await store.initialize()
        with pytest.raises(ValueError, match="unknown clarification kind"):
            await store.put(
                turn_id="t_p51_5",
                user_id="u_x",
                workspace_id="ws_x",
                token="x",
                rationale="r",
                candidates=[ClarificationCandidate(value="X", label="X")],
                kind="made_up_kind",  # not in VALID set
            )
