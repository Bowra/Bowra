from __future__ import annotations

import json
from pathlib import Path

import pytest
from bowhead_core.subagent_purpose import (
    PurposeEvidence,
    PurposeGateOutcome,
    evaluate_purpose_evidence,
)


def _evidence(
    *,
    source_or_object: bool,
    action: bool,
    expected_result: bool,
) -> PurposeEvidence:
    return PurposeEvidence(
        source_or_object=source_or_object,
        action=action,
        expected_result=expected_result,
    )


def test_absent_purpose_normalizes_full_width_generic_terms() -> None:
    decision = evaluate_purpose_evidence(
        "  我想创建一个 Ａｇｅｎｔ  ",
        _evidence(
            source_or_object=False,
            action=False,
            expected_result=False,
        ),
    )

    assert decision.outcome is PurposeGateOutcome.PURPOSE_REQUIRED
    assert decision.normalized_purpose == "我想创建一个 Agent"


def test_clear_business_purpose_passes_two_of_three_rule() -> None:
    decision = evaluate_purpose_evidence(
        "每周分析门店销售异常并生成趋势图",
        _evidence(
            source_or_object=True,
            action=True,
            expected_result=True,
        ),
    )

    assert decision.outcome is PurposeGateOutcome.SUFFICIENT


def test_non_empty_purpose_is_insufficient_without_evidence() -> None:
    decision = evaluate_purpose_evidence(
        "帮我处理业务",
        _evidence(
            source_or_object=True,
            action=False,
            expected_result=False,
        ),
    )

    assert decision.outcome is PurposeGateOutcome.PURPOSE_INSUFFICIENT


def test_short_english_articles_do_not_strip_business_words() -> None:
    decision = evaluate_purpose_evidence(
        "analyze customer data",
        _evidence(
            source_or_object=True,
            action=True,
            expected_result=False,
        ),
    )

    assert decision.outcome is PurposeGateOutcome.SUFFICIENT


def test_action_or_expected_result_is_required() -> None:
    decision = evaluate_purpose_evidence(
        "销售数据和门店信息",
        _evidence(
            source_or_object=True,
            action=False,
            expected_result=False,
        ),
    )

    assert decision.outcome is PurposeGateOutcome.PURPOSE_INSUFFICIENT


def test_manual_and_cancel_are_deterministic() -> None:
    evidence = _evidence(
        source_or_object=False,
        action=False,
        expected_result=False,
    )

    assert evaluate_purpose_evidence("我想自己配置", evidence).outcome is PurposeGateOutcome.MANUAL
    assert evaluate_purpose_evidence("算了，不创建了", evidence).outcome is PurposeGateOutcome.CANCELLED


def test_over_limit_purpose_is_rejected_without_truncation() -> None:
    decision = evaluate_purpose_evidence(
        "x" * 2001,
        _evidence(
            source_or_object=True,
            action=True,
            expected_result=True,
        ),
    )

    assert decision.outcome is PurposeGateOutcome.TOO_LONG
    assert len(decision.normalized_purpose) == 2001


_CORPUS_PATH = Path(__file__).parent / "fixtures" / "subagent_purpose_v1.json"
_CORPUS = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "case",
    _CORPUS,
    ids=lambda case: f"{case['locale']}:{case['purpose']}",
)
def test_purpose_v1_golden_corpus(case: dict[str, object]) -> None:
    source_or_object, action, expected_result = case["evidence"]
    decision = evaluate_purpose_evidence(
        str(case["purpose"]),
        _evidence(
            source_or_object=bool(source_or_object),
            action=bool(action),
            expected_result=bool(expected_result),
        ),
    )

    assert decision.outcome.value == case["expected"]
