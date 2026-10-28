"""`format: "percentage"` 给了事实，就必须同时给用法。

116 真机（2026-08-28，1.1.78 en-US 轮）：表格里 `profit_margin` 显示 **1784.0%**。
链路：BI 返 0.1784 → agent 在沙箱里 `df['利润率'] *= 100` 存成 17.84 → 后端按源指标
元数据标 `percent_scale=fraction` → 前端再 ×100 → 双重放大。

`4fed445de` 已经让渲染层对「值被加工过」鲁棒（值域矛盾时不声明形状），但那是**兜底**：
一触发就退回按值猜，本来查得到的形状白丢。根子在 agent 不知道这个值不用自己转——
而它之所以动手，很可能正是因为 schema 从 `b9d4a7e47` 起才开始告诉它 `format: percentage`。

三个面各一条闸：工具描述 / schema payload / compute 自己的 prompt。
**工具描述说了不够**是上一轮（display 那条）已经付过的学费——真正决定行为的是 agent
自己的 system prompt，所以 compute.md 那条不能省。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from bowhead_bi_bridge.types import DimensionMeta, MeasureMeta
from bowhead_core.tool_registry import _PERCENTAGE_FORMAT_NOTE, build_default_registry


def _measure(**kw: Any) -> MeasureMeta:
    base = {"id": 1, "names": ["利润率"], "display_names": {}}
    base.update(kw)
    return MeasureMeta(**base)  # type: ignore[arg-type]


@pytest.mark.unit
def test_the_note_says_the_value_is_already_a_ratio() -> None:
    """说明必须点明两件事：值是 0-1 原值、别自己乘。少一半都挡不住那个 bug。"""
    assert "0-1" in _PERCENTAGE_FORMAT_NOTE
    assert "不要在沙箱里自己 ×100" in _PERCENTAGE_FORMAT_NOTE
    # Saying "do not multiply" is insufficient: agents still write percentages in prose, so explain which layer may convert them.
    assert "保持原值" in _PERCENTAGE_FORMAT_NOTE


@pytest.mark.unit
def test_list_measures_description_carries_the_note() -> None:
    """`bi_list_measures` 返的是裸列表，没处挂条件说明 ⇒ 只能写进描述。"""
    reg = build_default_registry()
    assert _PERCENTAGE_FORMAT_NOTE in reg.get("bi_list_measures").schema.description


class _FakeDatarc:
    def __init__(self, measures: list[MeasureMeta]) -> None:
        self._measures = measures

    async def list_measures(self, ws: str) -> list[MeasureMeta]:
        return self._measures

    async def list_dimensions(self, ws: str) -> list[Any]:
        return [DimensionMeta(id=1, names=["省"], display_names={}, used=True)]


class _FakeCtx:
    def __init__(self, measures: list[MeasureMeta]) -> None:
        self.datarc = _FakeDatarc(measures)
        self.member_uid = "u_test"
        self.language_context = None

    def resolve_datarc_project(self) -> str:
        return "1"


async def _run_schema(measures: list[MeasureMeta]) -> dict[str, Any]:
    """跑**真的** bi_schema handler —— 不复刻它的判据。

    ⚠ 本轮已经两次栽在「在测试/探针里重写被测判据」上（见模块 docstring 那条
    reconstructed-input 教训）。条件产出这种「什么时候出」的行为，只有调 handler
    本体才算验过；自己写一遍 `any(... == "percentage")` 验的是我抄得对不对。
    """
    reg = build_default_registry()
    return await reg.get("bi_schema").handler({}, _FakeCtx(measures))  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.anyio
async def test_schema_payload_carries_the_note_only_when_relevant() -> None:
    """★ `bi_schema` 走**条件产出**：有百分比指标才出，没有就不占 token。

    说明与事实同处一个 payload，是它最不容易被跳过的位置。
    """
    with_pct = await _run_schema([_measure(value_format="percentage")])
    assert with_pct["_value_format_note"] == _PERCENTAGE_FORMAT_NOTE

    without = await _run_schema([_measure(names=["营业额"], value_format="float")])
    assert "_value_format_note" not in without, "没有百分比指标却出了说明——白占 token"


@pytest.mark.unit
def test_compute_prompt_teaches_it_too() -> None:
    """The compute prompt must teach the same percentage rule as the tool contract."""
    md = Path(__file__).resolve().parents[1] / "src/bowhead_core/agents/prompts/system/compute.md"
    body = md.read_text(encoding="utf-8")
    assert "不要自己 ×100" in body, "compute.md 没教这条——它会继续把 0.1784 乘成 17.84"
    assert "1784" in body, "要留住那个真机数字，否则下一个人不知道代价有多大"
    # Do not prohibit the prose conversion too: agents still need to write 17.84% in text.
    assert "保持原值" in body


@pytest.mark.unit
def test_note_lives_in_exactly_one_place() -> None:
    """一处定义、三处引用。第二份措辞会立刻漂移（本仓反复栽的形状）。"""
    src = Path(__file__).resolve().parents[1] / "src/bowhead_core"
    hits = [p for p in src.rglob("*.py") if "不要在沙箱里自己 ×100" in p.read_text(encoding="utf-8")]
    assert [p.name for p in hits] == ["tool_registry.py"], hits
