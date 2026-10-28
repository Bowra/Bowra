"""比率形状能声明这件事，**必须让模型看得见**（tech-design-percent-shape 批2）。

★★ 能力 ≠ SOP。批1（散点标注）实测过一次：1.2.35 把原生散点的自动标注做出来了，
同一句「每个点标上门店名」agent 仍然 `add_image` 一次、`add_chart` 零次——
它按老经验判「原生标不了」，跑去 matplotlib 了。**改渲染改不动模型行为，
得改它读得到的那几处文本。**

这次同理：`column_types` 的新值域加了，但如果只写在工具描述里，
在沙箱里算比率的那条路（116 实测 687 个百分比列的出身）不一定读得到。
所以三处都要说，而这份清单**改一处等于没改**——
`add_image` 那份就是四处副本，栽过（见 `test_scatter_labels_are_advertised.py`）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_SURFACES = {
    "add_chart 工具描述": _ROOT / "packages/bowhead_core/src/bowhead_core/canvas_tools.py",
    "compute 系统提示词": _ROOT / "packages/bowhead_core/src/bowhead_core/agents/prompts/system/compute.md",
    "python_sandbox SKILL": _ROOT / "packages/bowhead_skill/catalog/python_sandbox/SKILL.md",
}


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_SURFACES))
def test_surface_file_exists(name: str) -> None:
    """正对照：路径写错就该红，而不是让下面几条静默变成假闸。"""
    assert _SURFACES[name].is_file(), _SURFACES[name]


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_SURFACES))
def test_both_percent_shape_values_are_stated(name: str) -> None:
    """三处**都**要写出两个值域——只说一个，模型碰到另一种形状就没得选。"""
    text = _SURFACES[name].read_text(encoding="utf-8")
    for token in ("percent_fraction", "percent_points"):
        assert token in text, f"{name} 没提 {token}"


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_SURFACES))
def test_the_reason_is_stated_not_just_the_syntax(name: str) -> None:
    """★ 光给语法不够，要说清**为什么只有它知道**。

    模型照抄语法但判不准该传哪个值，等于把一次瞎猜换成另一次瞎猜。
    三处都得点出「除没除 100 只有你知道」这层意思。
    """
    text = _SURFACES[name].read_text(encoding="utf-8")
    assert "100" in text, f"{name} 没说清 ×100 这件事"
    assert "只有你知道" in text, f"{name} 没说清「只有你知道」——模型不知道为什么该由它来声明"


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_SURFACES))
def test_the_free_channel_is_mentioned(name: str) -> None:
    """★ 既有通道也要说：CSV 里写成 `15.37%` 文本本身就是一次声明。

    `_coerce_formatted_numeric_columns` 会把它还原成数并把 `unit` 设成 `%`，
    而 `units.py` 认「人填 % ⇒ 值已放大 ⇒ points」。这条一直存在、一行代码不用改，
    唯一缺的就是没人告诉模型。
    """
    text = _SURFACES[name].read_text(encoding="utf-8")
    assert "15.37%" in text, f"{name} 没提「写成带 % 的文本」这条等价做法"
