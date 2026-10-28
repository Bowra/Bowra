"""散点能自动标点这件事，**必须让模型看得见**（chart-config-ux 批1 后续）。

116 实测（1.2.35，`t_cb232511e437`）：批1 上线后原生散点已经会自动标十几个点，
但同一句「每个点标上门店名」，agent 仍然 `add_image` 一次、`add_chart` 零次——
它按老经验判「原生标不了」，跑去 matplotlib 了。

**能力 ≠ SOP**：改渲染改不动模型行为，得改模型读得到的那几处文本。
而「`add_image` 只留给原生画不了的」这份清单在仓里有**四处**副本，
改一处等于没改（`REPORT_WRITING.md` 记过同一个教训：那次改动面是
「工具描述 + 4 处运行时 hint + runtime prompt + 两个 SOP skill」）。

本闸盯住四处都提到散点例外。
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_SURFACES = {
    "add_chart / add_image 工具描述": _ROOT / "packages/bowhead_core/src/bowhead_core/canvas_tools.py",
    "compute 系统提示词": _ROOT / "packages/bowhead_core/src/bowhead_core/agents/prompts/system/compute.md",
    "python_sandbox SKILL": _ROOT / "packages/bowhead_skill/catalog/python_sandbox/SKILL.md",
    "analyst-sop-compute SKILL": _ROOT / "packages/bowhead_skill/catalog/analyst-sop-compute/SKILL.md",
}


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_SURFACES))
def test_surface_file_exists(name: str) -> None:
    """正对照：路径写错了就该红，而不是让下面几条静默变成假闸。"""
    assert _SURFACES[name].is_file(), _SURFACES[name]


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_SURFACES))
def test_scatter_exception_is_stated(name: str) -> None:
    """四处**都**要说「散点仍走 add_chart」，不能只改一处。"""
    text = _SURFACES[name].read_text(encoding="utf-8")
    assert "散点" in text, f"{name} 没提散点"
    assert "add_chart" in text, f"{name} 没指回 add_chart"
    # The "标名字" request must appear, or the model cannot tell which request this exception addresses.
    assert "标" in text and "名" in text, f"{name} 没说清是「标名字」这个诉求"


@pytest.mark.unit
def test_statistical_diagnostic_loophole_closed_in_compute_prompt() -> None:
    """★ 「统计诊断图」是个口子：带回归线的散点完全读得成它。

    compute 提示词那份清单里已经摘掉，别再加回去（其余几处保留该措辞是可以的
    ——它们讲的是箱线图/QQ plot 那类真的画不了的，且都紧跟着散点例外）。
    """
    text = _SURFACES["compute 系统提示词"].read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if "`add_image` 只留给" in ln)
    assert "统计诊断图" not in line, line
