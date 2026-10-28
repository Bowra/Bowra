"""bi-answer-correctness A 步骤 1/2：`_parse.unknown_words` 去噪与点名提示。

116 全量空跑：原样清单 95% 非空（空格、「各」「按」这类单字），模型早已不看；去噪后 16.9%，
真漏识别（华东地、万达店、客单、IPT）全保留。去噪只做切分与字符类别判断，不用正则、不用词表。
"""

from __future__ import annotations

import pytest
from bowhead_core.bi_unknown_words import denoise_unknown_words, unknown_words_note


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["华东地", " ", " "], ["华东地"]),  # Original 116 q2 shape
        (["IPT 客单"], ["IPT", "客单"]),  # datarc merges adjacent unknown fragments, including spaces, into one token.
        (["　万达店　"], ["万达店"]),  # Full-width spaces
        (["各", "按", "省"], []),  # Drop single characters (known cost: genuine unrecognized single characters are also lost).
        (["「华东」，"], ["华东"]),  # Leading/trailing punctuation
        (["客单价", "客单价"], ["客单价"]),  # Deduplicate while preserving order
        ([], []),
    ],
)
def test_denoise_unknown_words(raw: list[str], expected: list[str]) -> None:
    assert denoise_unknown_words(raw) == expected


def test_denoise_keeps_punctuation_inside_a_word() -> None:
    assert denoise_unknown_words(["A-B店"]) == ["A-B店"]


def test_note_names_every_word() -> None:
    note = unknown_words_note(["华东地", "客单"])
    assert "「华东地」" in note and "「客单」" in note


def test_note_states_both_outcomes_without_claiming_unfiltered() -> None:
    """措辞不许断言「没按它筛」——2026-09-20 116 dogfood 逮到反例。

    「省 包含 江西」：`江西` 不在字典里 ⇒ 进了 `unknown_words`，而引擎自报的执行口径里
    **带着**「省包含江西」（`$like` 按字面走）。旧措辞在这一档是假陈述。
    真正「没筛」只由 `unfiltered_fact_note` 按结构化口径的 `filters` 键判定。
    """
    note = unknown_words_note(["江西"])
    assert "未过滤" in note, "「整段被忽略」这一档要说到"
    assert "匹配不到" in note, "「按字面进了查询却匹配不到」这一档要说到"
    assert "没有按它筛选" not in note, "不许无条件断言没筛过"
