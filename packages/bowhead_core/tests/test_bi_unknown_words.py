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


def test_note_identifies_parser_output_without_deciding_query_semantics() -> None:
    note = unknown_words_note(["江西"])
    assert "parser reported unrecognized words: 「江西」" in note
    assert "does not establish whether those words were ignored or used as literal conditions" in note
