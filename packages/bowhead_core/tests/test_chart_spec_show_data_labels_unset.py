"""服务端**不表态**数值标签开关——不然前端那套策略是死码。

`ChartSpec.show_data_labels` 的默认值曾经是 `False`。`spec_to_dict` 的
`_drop_empty` 只丢 `None` / `[]` / `{}`，`False` 会原样落库，于是每一张图都带着一个
「显式 false」进前端；而前端 `resolveDataLabels` 的第一行正是「显式布尔值胜出」——
它下面的「导出面恒开 → 移动端恒开 → 单系列 ≤N 类默认开」四级从上线起一次都没跑过。

116 生产实测：2781 张图的 payload，**2781 张**都是 `show_data_labels: false`；
全仓 Python 也零处赋值（这个字段只有声明，没有产出方）。

对应前端闸：`lib/chart/build-config.test.ts` 的
「★★ 服务端不表态时梯子才活得成」。两边都在，才拦得住这条链。
"""

from __future__ import annotations

import dataclasses

import pytest
from bowhead_core.chart.types import ChartSpec, spec_to_dict


def _spec(**over: object) -> ChartSpec:
    return ChartSpec(kind="bar", title="各省营业额", **over)  # type: ignore[arg-type]


@pytest.mark.unit
def test_default_is_unset_not_false() -> None:
    """默认必须是 None（未表态），不是 False（表态说「别标」）。"""
    assert _spec().show_data_labels is None


@pytest.mark.unit
def test_unset_does_not_reach_the_payload() -> None:
    """未表态 ⇒ 键根本不出现，前端才走得到自己的策略梯子。"""
    assert "show_data_labels" not in spec_to_dict(_spec())


@pytest.mark.unit
@pytest.mark.parametrize("value", [True, False])
def test_explicit_value_survives_serialization(value: bool) -> None:
    """★ 显式值仍要原样带过去——包括 `False`。

    图表配置面板的「显示数据标签」开关关掉时写的就是显式 `False`；
    要是为了修上面那条把 falsy 一起丢了，用户的「关」会静默失效。
    """
    assert spec_to_dict(_spec(show_data_labels=value))["show_data_labels"] is value


@pytest.mark.unit
def test_no_producer_in_tree_relies_on_the_old_default() -> None:
    """正对照：字段还在、类型确实放开了 None，上面几条才不是空转。"""
    field = next(f for f in dataclasses.fields(ChartSpec) if f.name == "show_data_labels")
    assert field.default is None
    assert "None" in str(field.type)
