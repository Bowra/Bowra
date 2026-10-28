"""图注前缀规则的**真** FE↔BE 对拍（打法照 test_canvas_rank_parity.py）。

为什么必须有这条：`export_ir.FIGURE_CAPTION_PREFIX_RE` 与 FE 的 `CAPTION_RE` 是
跨语言双份常量，而四处留存物都写着「同字面 golden 对拍钉死，字符串 diff 即红」
——**那个机制根本不存在**：没有任何 Python 读那个 TS 文件，也没有任何 TS 读这个
Python 文件，两份 golden 只是互相独立的字面拷贝，单边改正则 + 单边改 golden 会
全绿（对抗性 review 的实证）。

而且当时两侧行为**真的不等价**：`\\d` 在 Python 匹配全部 Unicode 十进制数字（含
全角 ０-９），在 JS 只匹配 ASCII；`\\s` 的空白集合也有分歧（\\ufeff / \\x85）。986 条
语料里 648 条分叉 —— `图１：` 在画布上是普通正文、前缀照露，在导出件里却被吸附
成图注并剥了前缀，正是本 sprint 要治的「画布与导出不一致」换了个触发条件。

现在两侧都用 ASCII 显式集合（`[0-9]` / `[ \\t]`），本测试**从 TS 源码抠正则字面**
逐字比对，并对同一组样本跑两侧引擎（Python re vs Node RegExp）对比命中结果。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from bowhead_core.export_ir import FIGURE_CAPTION_PREFIX_RE, split_figure_caption

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TS_SOURCE = _REPO_ROOT / "packages/bowhead_frontend/lib/canvas-pm-adapter/extensions/figure-caption.ts"
_TS_GOLDEN = _REPO_ROOT / "packages/bowhead_frontend/lib/canvas-pm-adapter/extensions/figure-caption.test.ts"

# Both sides must use ASCII classes only; divergence came from \d / \s.
_FORBIDDEN_CLASSES = (r"\d", r"\s", r"\w")


def _extract_ts_regex() -> str:
    text = _TS_SOURCE.read_text(encoding="utf-8")
    match = re.search(r"export const CAPTION_RE = /(.+?)/;", text)
    assert match, "没在 figure-caption.ts 里找到 CAPTION_RE —— 抠取失效，这条闸会假绿"
    return match.group(1)


@pytest.mark.unit
def test_caption_regex_literals_match_across_languages() -> None:
    ts_pattern = _extract_ts_regex()
    py_pattern = FIGURE_CAPTION_PREFIX_RE.pattern
    assert ts_pattern == py_pattern, f"图注正则两侧字面不一致 —— 改一处必须同改另一处：\n  TS: {ts_pattern}\n  PY: {py_pattern}"


@pytest.mark.unit
def test_caption_regex_uses_no_language_dependent_classes() -> None:
    pattern = FIGURE_CAPTION_PREFIX_RE.pattern
    found = [cls for cls in _FORBIDDEN_CLASSES if cls in pattern]
    assert not found, (
        f"图注正则含跨语言语义不同的字符类 {found}："
        r"Python 的 \d 含全角数字、\s 的空白集合与 JS 不同。用显式 ASCII 集合。"
    )


@pytest.mark.unit
def test_both_golden_fixtures_stay_in_sync() -> None:
    """两侧 golden 必须是同一组字符串（这次是真读文件比，不是靠注释承诺）。"""
    ts_text = _TS_GOLDEN.read_text(encoding="utf-8")
    ts_hits = re.findall(r'\["([^"]+)", "([^"]+)"\]', ts_text)
    ts_misses = re.findall(r'^\s*"([^"]+)",\s*$', ts_text, re.M)

    from test_export_ir_figure_caption import GOLDEN_HITS, GOLDEN_MISSES  # noqa: PLC0415

    assert [list(pair) for pair in ts_hits] == [list(pair) for pair in GOLDEN_HITS], (
        f"TS 与 PY 的命中 golden 不是同一组：\n  TS={ts_hits}\n  PY={GOLDEN_HITS}"
    )
    assert set(GOLDEN_MISSES) <= set(ts_misses), f"PY 的不命中 golden 有 TS 侧没有的条目：{set(GOLDEN_MISSES) - set(ts_misses)}"


@pytest.mark.unit
def test_two_engines_agree_on_tricky_samples() -> None:
    """同一组样本在 Python re 与 Node RegExp 下命中结果必须一致（真跑两个引擎）。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node on PATH")

    samples = [
        "图1：ASCII 数字",
        "图１：全角数字",  # Divergence: Python \\d matches; JS does not.
        "图１２：全角两位",
        "**图1**：加粗",
        "图﻿1：BOM 分隔",  # Divergence: JS \\s includes ﻿, Python does not.
        "图\x851：NEL 分隔",  # Divergence: Python \\s includes \x85, JS does not.
        "图　1：表意空格",
        "图 1: 半角冒号",
        "图表1：不是图N",
        "如图1所示",
    ]
    pattern = FIGURE_CAPTION_PREFIX_RE.pattern
    py_hits = [bool(FIGURE_CAPTION_PREFIX_RE.match(s)) for s in samples]

    script = (
        "const re = new RegExp(" + json.dumps(pattern) + ");"
        "const samples = " + json.dumps(samples) + ";"
        "process.stdout.write(JSON.stringify(samples.map((s) => re.test(s))));"
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    js_hits = json.loads(result.stdout)

    diverged = [(s, p, j) for s, p, j in zip(samples, py_hits, js_hits, strict=True) if p != j]
    assert not diverged, "两侧引擎对同一正则的命中结果分叉：\n" + "\n".join(f"  {sample!r}: PY={py} JS={js}" for sample, py, js in diverged)


@pytest.mark.unit
def test_full_width_digits_are_not_absorbed_either_side() -> None:
    """全角数字现在两侧都不吸附（收窄后的既定行为，钉住防回退）。"""
    caption, rest = split_figure_caption("图１：全角数字图注")
    assert caption is None
    assert rest == "图１：全角数字图注"
