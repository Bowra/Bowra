"""跨语言对拍：后端归一出的形状，必须正是前端测试钉着「渲得出图」的那批形状。

单一真源在 TS 测试文件里（`figure-anchor-normalized.test.ts` 的 `NORMALIZED_CASES`），
本文件把它抠出来逐条跑后端 normalizer。两边各守一半：
- 前端：这些形状能提升成 figure_ref、无字面量残留、往返字节不变；
- 后端（本文件）：normalizer 对给定输入确实产出这些形状。

只改一边就会红——这正是 `test_figure_caption_parity.py` 立下的做法（那边两份 golden
互不相干，单边改正则+单边改 golden 会全绿，而两侧行为真的分叉过）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from bowhead_core.figure_anchor_layout import normalize_figure_anchors

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TS_SOURCE = _REPO_ROOT / "packages/bowhead_frontend/lib/canvas-pm-adapter/__tests__/figure-anchor-normalized.test.ts"


def _load_cases() -> list[dict]:
    text = _TS_SOURCE.read_text(encoding="utf-8")
    # Tolerate TS type annotation (`: NormalizedCase[]`), but extraction failure must error, never silently exercise nothing.
    match = re.search(r"export const NORMALIZED_CASES[^=]*= (\[.*?\]);", text, re.DOTALL)
    assert match, "没在 TS 测试里找到 NORMALIZED_CASES —— 抠取失效，这条闸会假绿"
    cases = json.loads(match.group(1))
    assert cases, "用例数组是空的"
    return cases


def test_ts_fixture_is_reachable() -> None:
    """抠取本身要有正对照：TS 文件挪走/改名时必须红，而不是静默跳过。"""
    assert _TS_SOURCE.exists(), f"TS 单一真源不在了：{_TS_SOURCE}"
    assert len(_load_cases()) >= 5


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["label"])
def test_backend_produces_the_shape_frontend_pins(case: dict) -> None:
    out, stats = normalize_figure_anchors(case["input"], dict(case["figures"]))
    assert out == case["normalized"], case["label"]
    if case["input"] != case["normalized"]:
        assert stats["changed"] == 1
    # Fixed point: frontend-pinned shape stays unchanged on another pass, or each write changes user-visible bytes.
    again, stats2 = normalize_figure_anchors(case["normalized"], dict(case["figures"]))
    assert again == case["normalized"]
    assert stats2["changed"] == 0
