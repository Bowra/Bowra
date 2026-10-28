"""compute 提示词对 `query_explain` 的契约：**只教用法，检测归工具**（G §2.5）。

字段本身由 `test_bi_query_explain_passthrough.py` / `test_bi_query_explain_fallback.py` 钉住「送到模型面前」；
「没按它筛过」「被截成前 N」这类检测由 `test_bi_query_explain_signals.py` 钉在工具侧。
这里钉的是剩下那一半：提示词要告诉模型这份口径是什么、检测结果写在哪、缺字段意味着什么。

★ 为什么提示词不再列检测规则：用户指出「这是 tool 的功能吧为啥又到提示词了」——结构化口径
就在工具手里，`filters` / `limit` 键在不在是确定性事实，写成规则让模型每次自判既重复又更弱。
"""

from __future__ import annotations

import pytest
from bowhead_core.subagent import _build_compute_system_prompt


@pytest.fixture(scope="module")
def prompt() -> str:
    return _build_compute_system_prompt()


def _block(prompt: str) -> str:
    return prompt[prompt.index("query_explain") :][:900]


@pytest.mark.unit
def test_prompt_names_both_explain_fields(prompt: str) -> None:
    """结构化版与只有文本时的退路都要点名——116 实例只有文本版，只提一个等于告诉模型没这东西。"""
    assert "query_explain" in prompt
    assert "query_explain_text" in prompt


@pytest.mark.unit
def test_prompt_points_detection_to_the_tool_hint(prompt: str) -> None:
    """检测结果写在 `_preflight_hint` 里，模型照事实处理，不自己再推断。"""
    assert "_preflight_hint" in _block(prompt)


@pytest.mark.unit
def test_prompt_says_missing_report_is_not_evidence(prompt: str) -> None:
    """该实例不返这份说明时，不能被读成「没筛选」。"""
    assert "不等于没筛选" in _block(prompt)
