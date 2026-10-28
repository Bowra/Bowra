"""单位富化必须**穿过真 handler** 才算数.

`test_units.py` 直接测富化函数，证明不了它在出图路上真被调到——单元测试全绿而
线上那个字段一直是空的，正是这次 review 逮到的形态。所以这里走 `_add_chart`，
断言落进 canvas 的 spec 里真带上了单位/形状。
"""

from __future__ import annotations

from typing import Any

import pytest
from bowhead_core.canvas_tools import _add_chart
from test_add_chart_validation import (
    _CapturingCanvasStore,
    _FakeArtifact,
    _RichArtifactStore,
    _StubCtx,
)

_CSV = ("品牌,金额达成率,销售金额\nA,1.0013,347065487.47\nB,1.1226,305098815.99\nC,0.9819,242055454.20\n").encode()

# Real shape: achievement rate is fraction but **often >1** (overachievement), so value-range heuristics necessarily fail;
# sales amount has only a currency code, no unitName.
_META: dict[str, dict[str, str]] = {
    "金额达成率": {"currency_code": "", "value_format": "percentage", "unit_name": ""},
    "销售金额": {"currency_code": "CNY", "value_format": "", "unit_name": ""},
}


class _FakeSource:
    """任何数据源只要长这样就能接进来——这里不依赖任何真 client."""

    def __init__(self, table: dict[str, dict[str, str]]) -> None:
        self.table = table
        self.calls: list[tuple[str, str]] = []

    def measure_metadata(self, ws: str, name: str) -> dict[str, str] | None:
        self.calls.append((ws, name))
        return self.table.get(name)


def _ctx_with_source(source: Any | None) -> tuple[Any, _CapturingCanvasStore]:
    art = _FakeArtifact(id="art_333333333333", filename="brand.csv", path="brand.csv")
    art.source = "bi_fetch_rows"
    canvas = _CapturingCanvasStore()
    ctx = _StubCtx(
        artifact_store=_RichArtifactStore([art], {"brand.csv": _CSV}),
        canvas_store=canvas,
    )
    art.created_by_turn = ctx.turn_id
    if source is not None:
        ctx.datarc = source
        ctx.resolve_datarc_project = lambda: "1"  # type: ignore[method-assign]
    return ctx, canvas


async def _run(ctx: Any) -> dict[str, Any]:
    return await _add_chart(
        {"data_artifact_id": "art_333333333333", "hint": "rank", "title": "各品牌达成率"},
        ctx,
    )


@pytest.mark.asyncio
async def test_chart_columns_carry_units_from_source() -> None:
    """★出图时按列名回查元数据，percent_scale / 货币符号落进 spec."""
    source = _FakeSource(_META)
    ctx, canvas = _ctx_with_source(source)
    out = await _run(ctx)

    assert "error" not in out, out
    assert canvas.appended_payload is not None
    cols = {c["key"]: c for c in canvas.appended_payload["spec"]["columns"]}

    # Achievement rate: 0.98–1.12 would be guessed as points by magnitude, rendering as 1.0% (original production bug).
    # Metadata says percentage ⇒ fraction; authoritative data overrides guesses.
    assert cols["金额达成率"].get("percent_scale") == "fraction"

    # Currency uses **symbols**: frontend prefixes only ¥/$/€; CNY would render as 12,345CNY.
    assert cols["销售金额"].get("format") == "currency"
    assert cols["销售金额"].get("unit") == "¥"

    # Leave dimension columns unchanged.
    assert not cols["品牌"].get("unit")
    # Lookup actually used column names and included workspace.
    assert ("1", "金额达成率") in source.calls


@pytest.mark.asyncio
async def test_chart_without_source_is_unchanged() -> None:
    """★负对照：没有数据源时出图完全照旧（富化是增强项，不是新的失败点）。

    没有这一条，上面那条绿了也说明不了是富化起的作用。
    """
    ctx, canvas = _ctx_with_source(None)
    out = await _run(ctx)

    assert "error" not in out, out
    assert canvas.appended_payload is not None
    cols = {c["key"]: c for c in canvas.appended_payload["spec"]["columns"]}
    assert cols["金额达成率"].get("percent_scale") is None
    assert cols["销售金额"].get("unit") is None


@pytest.mark.asyncio
async def test_broken_source_does_not_break_charting() -> None:
    """数据源炸了也必须出图——富化绝不许有能力搞死被增强的东西."""

    class _Boom:
        def measure_metadata(self, ws: str, name: str) -> dict[str, str]:
            raise RuntimeError("source down")

    ctx, canvas = _ctx_with_source(_Boom())
    out = await _run(ctx)

    assert "error" not in out, out
    assert canvas.appended_payload is not None
    assert out["row_count"] == 3


# ─────────────────────────────────────────────────────────────
# Second producer: backend automatically adds a chart when the model forgets add_chart.
# ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_auto_built_chart_also_carries_units() -> None:
    """★出图有**两个**产出方，只接一个等于没接。

    `_try_auto_build_chart_section` 是「模型忘了调 add_chart」时后端自动补的那条，
    它不走 _add_chart，所以要单独喂 lookup。漏掉它的话，恰恰在模型偷懒的那些轮
    百分比形状照样错——就是原 1% bug 的形态。
    """
    from bowhead_core.canvas_composer import _try_auto_build_chart_section
    from bowhead_core.chart.units_enrich import lookup_from_source

    art = _FakeArtifact(id="art_444444444444", filename="b.csv", path="b.csv")
    store = _RichArtifactStore([art], {"b.csv": _CSV})
    art.source = "bi_fetch_rows"
    art.created_by_turn = "t_1"
    canvas = _CapturingCanvasStore()

    class _Src:
        def measure_metadata(self, ws: str, name: str) -> dict[str, str] | None:
            return _META.get(name)

    sid = await _try_auto_build_chart_section(
        canvas_id="cv_1",
        artifact_store=store,
        canvas_store=canvas,
        data_artifact_id=art.id,
        caption="各品牌达成率",
        user_id="u",
        turn_id="t_1",
        bus=None,
        units_lookup=lookup_from_source(_Src(), "1"),
    )

    assert sid is not None
    assert canvas.appended_payload is not None
    cols = {c["key"]: c for c in canvas.appended_payload["spec"]["columns"]}
    assert cols["金额达成率"].get("percent_scale") == "fraction"
    assert cols["销售金额"].get("unit") == "¥"
    # Source metadata declares the shape; do not mark it undeclared, or frontend renders it as non-percent.
    assert cols["金额达成率"].get("percent_shape_unknown") is None


@pytest.mark.asyncio
async def test_auto_built_chart_without_lookup_still_works() -> None:
    """负对照：不传 lookup 时自动出图照常，只是不带单位."""
    from bowhead_core.canvas_composer import _try_auto_build_chart_section

    art = _FakeArtifact(id="art_555555555555", filename="c.csv", path="c.csv")
    art.source = "bi_fetch_rows"
    canvas = _CapturingCanvasStore()
    sid = await _try_auto_build_chart_section(
        canvas_id="cv_1",
        artifact_store=_RichArtifactStore([art], {"c.csv": _CSV}),
        canvas_store=canvas,
        data_artifact_id=art.id,
        caption="各品牌达成率",
        user_id="u",
        turn_id="t_1",
        bus=None,
    )
    assert sid is not None
    assert canvas.appended_payload is not None
    cols = {c["key"]: c for c in canvas.appended_payload["spec"]["columns"]}
    assert cols["金额达成率"].get("percent_scale") is None


# ─────────────────────────────────────────────────────────────
# 116 dogfood 2026-09-30 s_a0093acfa9f3: composer auto-chart path declares no shape,
# so frontend guesses: 利润率_环比变化 -0.2479 rendered as -24.8% (actual -0.25 percentage points).
# ─────────────────────────────────────────────────────────────
#: Real column names and values: 利润率 / 退款率 are fraction (0.17 = 17%);
#: the two _环比变化 columns are **percentage-point changes**, sharing name stems but measuring different quantities.
_QOQ_CSV = (
    "月份,利润率,退款率,利润率_环比变化,退款率_环比变化\n"
    "2026-01,0.17443,0.15374,,\n"
    "2026-02,0.17195,0.18328,-0.24791,2.95357\n"
    "2026-03,0.17091,0.16133,-0.10353,-2.19484\n"
).encode()


@pytest.mark.asyncio
async def test_composer_auto_chart_marks_undeclared_percent_shape() -> None:
    """★★ composer 自动出图：没人声明形状的数值列必须打标，前端才不会猜。

    这条路没有模型在场声明 `column_types`，拿不到 lookup 时也没有源元数据
    ⇒ 两个产出方全缺席。此前它**一个标都不打**（116 实测该路 80 个数值列
    unit/format/percent_scale 全 0），于是前端落到「整列 max ≤ 1 判 fraction」，
    把 -0.2479 个百分点渲成 -24.8%。
    """
    from bowhead_core.canvas_composer import _try_auto_build_chart_section

    art = _FakeArtifact(id="art_666666666666", filename="qoq.csv", path="qoq.csv")
    art.source = "bi_fetch_rows"
    canvas = _CapturingCanvasStore()
    sid = await _try_auto_build_chart_section(
        canvas_id="cv_1",
        artifact_store=_RichArtifactStore([art], {"qoq.csv": _QOQ_CSV}),
        canvas_store=canvas,
        data_artifact_id=art.id,
        caption="四指标相关性矩阵计算结果",
        user_id="u",
        turn_id="t_1",
        bus=None,
    )

    assert sid is not None
    cols = {c["key"]: c for c in canvas.appended_payload["spec"]["columns"]}
    for key in ("利润率", "退款率", "利润率_环比变化", "退款率_环比变化"):
        assert cols[key].get("percent_shape_unknown") is True, f"{key} 没打标 ⇒ 前端会按名称族/值域猜，`利润率_环比变化` 就是这么被渲成 -24.8% 的"
    # Do not mark dimension columns; they have no ratio shape at all.
    assert cols["月份"].get("percent_shape_unknown") is None


@pytest.mark.asyncio
async def test_composer_auto_chart_keeps_declared_shape_unmarked() -> None:
    """负对照：源元数据说得出形状的列，打标逻辑必须让开。

    否则这道闸会把**有权威依据**的百分比列一起打成裸数——那是比现状更大的回归。
    """
    from bowhead_core.canvas_composer import _try_auto_build_chart_section
    from bowhead_core.chart.units_enrich import lookup_from_source

    art = _FakeArtifact(id="art_777777777777", filename="d.csv", path="d.csv")
    art.source = "bi_fetch_rows"
    art.created_by_turn = "t_1"
    canvas = _CapturingCanvasStore()

    class _Src:
        def measure_metadata(self, ws: str, name: str) -> dict[str, str] | None:
            return _META.get(name)

    sid = await _try_auto_build_chart_section(
        canvas_id="cv_1",
        artifact_store=_RichArtifactStore([art], {"d.csv": _CSV}),
        canvas_store=canvas,
        data_artifact_id=art.id,
        caption="各品牌达成率",
        user_id="u",
        turn_id="t_1",
        bus=None,
        units_lookup=lookup_from_source(_Src(), "1"),
    )

    assert sid is not None
    cols = {c["key"]: c for c in canvas.appended_payload["spec"]["columns"]}
    assert cols["金额达成率"].get("percent_scale") == "fraction"
    assert cols["金额达成率"].get("percent_shape_unknown") is None
    # The column lacking source shape information must still be marked; its shape is also undeclared.
    assert cols["销售金额"].get("percent_shape_unknown") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact_source", ["python_sandbox", "upload", "personal_mcp", "power_bi", ""])
async def test_foreign_or_transformed_columns_do_not_inherit_datarc_metadata(artifact_source: str) -> None:
    source = _FakeSource(_META)
    ctx, canvas = _ctx_with_source(source)
    artifact = await ctx.artifact_store.get("art_333333333333")
    artifact.source = artifact_source
    result = await _run(ctx)
    assert "error" not in result, result
    assert source.calls == []
    assert canvas.appended_payload is not None
    for column in canvas.appended_payload["spec"]["columns"]:
        assert column.get("unit") is None
        assert column.get("percent_scale") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact_turn,derived_from", [("t_previous", None), (None, None), ("t_test", "art_parent")])
async def test_unverified_bi_result_does_not_borrow_current_source(artifact_turn: str | None, derived_from: str | None) -> None:
    source = _FakeSource(_META)
    ctx, canvas = _ctx_with_source(source)
    artifact = await ctx.artifact_store.get("art_333333333333")
    artifact.created_by_turn = artifact_turn
    artifact.derived_from = derived_from
    result = await _run(ctx)
    assert "error" not in result, result
    assert source.calls == []
    assert canvas.appended_payload is not None
    assert all(not col.get("unit") for col in canvas.appended_payload["spec"]["columns"])


@pytest.mark.asyncio
async def test_auto_chart_from_previous_turn_does_not_borrow_current_source() -> None:
    from bowhead_core.canvas_composer import _try_auto_build_chart_section
    from bowhead_core.chart.units_enrich import lookup_from_source

    source = _FakeSource(_META)
    ctx, canvas = _ctx_with_source(source)
    sid = await _try_auto_build_chart_section(
        canvas_id="cv_1",
        artifact_store=ctx.artifact_store,
        canvas_store=canvas,
        data_artifact_id="art_333333333333",
        caption="Brand performance",
        user_id="u",
        turn_id="t_next",
        bus=None,
        units_lookup=lookup_from_source(source, "another-project"),
    )
    assert sid is not None
    assert source.calls == []
    assert canvas.appended_payload is not None
    assert all(not col.get("unit") for col in canvas.appended_payload["spec"]["columns"])
