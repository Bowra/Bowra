"""Summarize the current concept-result fields rather than a retired response shape.

Match counts and verdicts come from the declared wire contract, including batches.
"""

from __future__ import annotations

from typing import Any

import pytest
from bowhead_core.agents.bi_concept_resolve import ConceptResult
from bowhead_core.subagent import _CONCEPT_VERDICT_LABEL, _prettify_output_preview

# Synthetic result shape with omitted match bodies; summaries count entries.
SYNTHETIC_RESULT: dict[str, Any] = {
    "results": [
        {
            "token": "示例产品甲",
            "verdict": "ambiguous",
            "matches": [{"type": "dim_value", "dim": f"d{i}"} for i in range(5)],
            "truncated": 0,
            "next_action_hint": "call_clarification_ask",
            "clarification_payload": None,
            "error": None,
        }
    ]
}


def _s(result: Any, lang: str = "zh-CN") -> str:
    return _prettify_output_preview("bi_concept_resolve", result, None, ui_language=lang)


class TestDriftGuard:
    """★ 本文件的正主：摘要读的键必须真的在对外形状里。"""

    def test_keys_the_summary_reads_exist_in_the_wire_shape(self) -> None:
        fields = set(ConceptResult.__annotations__)
        for key in ("token", "verdict", "matches"):
            assert key in fields, f"摘要读 {key!r}，但 ConceptResult 里没有 —— 又一次读死字段"

    def test_the_dead_keys_are_really_gone(self) -> None:
        """负对照：`candidates`/`kind` 若哪天回来了，本文件的前提就变了。

        它们**不该**存在——写死这条是为了让「有人把旧字段加回去」变成一次
        显式的红，而不是悄悄让两套形状并存。
        """
        fields = set(ConceptResult.__annotations__)
        assert "candidates" not in fields
        assert "kind" not in fields

    def test_every_label_key_is_a_real_verdict(self) -> None:
        """标签表不能有幽灵条目（拼错的 verdict 永远匹配不上，静默失效）。"""
        from bowhead_core.concept_recall import Verdict

        known = set(Verdict.__args__)
        assert set(_CONCEPT_VERDICT_LABEL) <= known, f"标签表里有不存在的 verdict: {set(_CONCEPT_VERDICT_LABEL) - known}"


class TestSummary:
    def test_nonempty_matches_are_counted(self) -> None:
        """A synthetic result with five matches must report five candidates."""
        out = _s(SYNTHETIC_RESULT)
        assert "5" in out, f"真召回 5 条却报了 {out!r}"
        assert "0 候选" not in out
        assert "示例产品甲" in out and "有歧义" in out

    def test_english(self) -> None:
        out = _s(SYNTHETIC_RESULT, "en-US")
        assert "5 candidates" in out and "ambiguous" in out

    def test_all_tokens_are_summarised_not_just_the_first(self) -> None:
        """旧代码只看 `results[0]`。批量解析是常态（后端合批），只报第一个等于骗人。"""
        payload = {
            "results": [
                {"token": "示例产品甲", "verdict": "unique", "matches": [{}] * 3},
                {"token": "LINE_A", "verdict": "ambiguous", "matches": [{}] * 5},
            ]
        }
        out = _s(payload)
        assert "示例产品甲" in out and "LINE_A" in out

    def test_zero_matches_still_says_zero(self) -> None:
        """正对照：真的 0 条时**就该**显示 0 —— 否则这个修复只是把 0 换成了别的谎。"""
        out = _s({"results": [{"token": "华东", "verdict": "not_found", "matches": []}]})
        assert "0" in out and "没找到" in out

    def test_unknown_verdict_does_not_leak_the_enum_name(self) -> None:
        """未登记的 verdict 不显示标签，而不是把内部枚举名漏给用户。"""
        out = _s({"results": [{"token": "T", "verdict": "brand_new_state", "matches": [{}] * 2}]})
        assert "2" in out
        assert "brand_new_state" not in out

    @pytest.mark.parametrize("payload", [{}, {"results": []}, {"results": [None]}, {"results": "nope"}])
    def test_malformed_payloads_do_not_raise(self, payload: Any) -> None:
        _s(payload)
