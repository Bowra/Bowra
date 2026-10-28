"""followup-context-deck #1 — bi_schema 只返 used=true 的可查维度.

根因 (prod s_ee09a8953acc): datarc 把 workspace **声明**的全部维度都返 (77 维), 但只有
used=true 的 10 个真有数据可查. used=false 维度 (如 中文省份/中文城市) 的 NL query 会被
datarc 静默按默认维度 (autoGroup, e.g. 中文品牌) 兜底 → 错维度数据, LLM 反复换写法 → 死循环.
修法: bi_schema 只把 used=true 维度交给 LLM, schema 不含某维 → 现有 prompt 让 LLM 直接
domain_mismatch 不盲试.
"""

from __future__ import annotations

import pytest
from bowhead_bi_bridge.types import DimensionMeta, MeasureMeta
from bowhead_core.tool_registry import build_default_registry


class _FakeDatarc:
    def __init__(self, dims: list[DimensionMeta], measures: list[MeasureMeta] | None = None) -> None:
        self._dims = dims
        self._measures = measures or []

    async def list_dimensions(self, _ws: str) -> list[DimensionMeta]:
        return self._dims

    async def list_measures(self, _ws: str) -> list[MeasureMeta]:
        return self._measures


class _FakeCtx:
    """Duck-typed ToolExecutionContext — _schema 只用 .datarc + resolve_datarc_project()."""

    def __init__(self, datarc: _FakeDatarc) -> None:
        self.datarc = datarc
        self.member_uid = "m_test"

    def resolve_datarc_project(self) -> str:
        return "336"


def _mixed_dims() -> list[DimensionMeta]:
    # Simulate prod ws336: few used=true dimensions (brand/product) + many used=false (region/staff).
    return [
        DimensionMeta(id=199, names=["中文品牌", "品牌", "药"], is_keyword=True, used=True, auto_group=True),
        DimensionMeta(id=185, names=["通用名", "molecule"], is_keyword=True, used=True),
        DimensionMeta(id=129, names=["中文省份", "省", "省份", "Province_cn"], is_keyword=True, used=False),
        DimensionMeta(id=131, names=["中文城市", "城市", "City_cn"], is_keyword=True, used=False),
        DimensionMeta(id=171, names=["代表岗位号", "REP_PID"], is_keyword=False, used=False),
    ]


@pytest.fixture
def schema_spec():
    return build_default_registry().get("bi_schema")


async def test_bi_schema_filters_used_false_dimensions(schema_spec) -> None:
    out = await schema_spec.handler({"kind": "dimensions"}, _FakeCtx(_FakeDatarc(_mixed_dims())))
    names = [d["names"][0] for d in out["dimensions"]]
    # Only the two used=true dimensions appear, preserving order. Province/city are filtered
    # → invisible to LLM → domain_mismatch, not blind attempts.
    assert names == ["中文品牌", "通用名"], names
    # Count declared-but-empty filtered dimensions so LLM can give precise domain_mismatch.
    assert out["dimensions_unused_count"] == 3


async def test_bi_schema_both_kind_also_filters(schema_spec) -> None:
    measures = [MeasureMeta(id=1, names=["销售金额", "Sales"])]
    out = await schema_spec.handler({"kind": "both"}, _FakeCtx(_FakeDatarc(_mixed_dims(), measures)))
    assert [d["names"][0] for d in out["dimensions"]] == ["中文品牌", "通用名"]
    assert out["dimensions_unused_count"] == 3
    # Measures are unaffected by used filtering (MeasureMeta lacks used).
    assert [m["names"][0] for m in out["measures"]] == ["销售金额"]


async def test_bi_schema_all_unused_falls_back_to_full(schema_spec) -> None:
    # Degenerate guard: all used=false in an abnormal/empty workspace still returns full schema, not empty (LLM needs a starting point).
    dims = [
        DimensionMeta(id=1, names=["a"], used=False),
        DimensionMeta(id=2, names=["b"], used=False),
    ]
    out = await schema_spec.handler({"kind": "dimensions"}, _FakeCtx(_FakeDatarc(dims)))
    assert [d["names"][0] for d in out["dimensions"]] == ["a", "b"]
    assert out["dimensions_unused_count"] == 0


# ── Symmetric metric-side handling (2026-09-17) ────────────────────────────
#
# Metrics previously had **no filtering**; the harm matches dimensions exactly. Actual demo.datarc.cn ws=1:
# `used=false` IPT/id_2 silently fell back in Datarc, returning **sales numbers**.
# ⚠ Only `used` is valid: profit margin/refund rate/special income in the same workspace have no model
# bindings but are queryable (derived metrics); filtering by `models` would reject them falsely.


def _mixed_measures() -> list[MeasureMeta]:
    return [
        MeasureMeta(id=1, names=["营业额", "GMV"], used=True),
        MeasureMeta(id=68, names=["利润率"], used=True),  # No model binding, but queryable.
        MeasureMeta(id=91, names=["IPT"], used=False),  # Query silently returns sales.
        MeasureMeta(id=92, names=["id_2"], used=False),
    ]


async def test_bi_schema_filters_used_false_measures(schema_spec) -> None:
    out = await schema_spec.handler({}, _FakeCtx(_FakeDatarc(_mixed_dims(), _mixed_measures())))

    names = [m["names"][0] for m in out["measures"]]
    assert names == ["营业额", "利润率"], names
    assert out["measures_unused_count"] == 2


async def test_bi_schema_keeps_all_measures_when_none_are_used(schema_spec) -> None:
    """退化保护：全 used=false（异常/空 workspace）仍返全量，别给 LLM 一张空 schema。"""
    none_used = [MeasureMeta(id=1, names=["营业额"], used=False), MeasureMeta(id=2, names=["利润"], used=False)]
    out = await schema_spec.handler({}, _FakeCtx(_FakeDatarc(_mixed_dims(), none_used)))

    assert [m["names"][0] for m in out["measures"]] == ["营业额", "利润"]
    assert out["measures_unused_count"] == 0


async def test_bi_schema_names_the_datarc_workspace_it_describes(schema_spec) -> None:
    """The Datarc workspace follows the user, not the Space; the schema says which one it is.

    No prompt pre-injection carries the name any more (2026-10-17), so the tool result must.
    """
    ctx = _FakeCtx(_FakeDatarc(_mixed_dims()))
    ctx.active_datarc_workspace_name = "商超零售"
    out = await schema_spec.handler({}, ctx)
    assert out["data_source"] == {"tool": "datarc", "context_id": "336", "context_name": "商超零售"}


async def test_bi_schema_fills_the_ui_label_cache_from_its_own_fetch(schema_spec) -> None:
    """English step cards relabel fields from this cache; bi_schema fills it without another Datarc call."""
    from bowhead_core.datarc_schema_cache import DatarcSchemaCache

    measures = [MeasureMeta(id=1, names=["营业额", "Revenue"], description="", sort=1)]
    ctx = _FakeCtx(_FakeDatarc(_mixed_dims(), measures))
    ctx.schema_cache = DatarcSchemaCache(ttl_sec=300)
    await schema_spec.handler({}, ctx)
    cached = ctx.schema_cache.get("336", count_stats=False)
    assert cached is not None
    assert [m.id for m in cached.measures] == [1]
    assert len(cached.dimensions) == len(_mixed_dims())


async def test_bi_schema_exposes_names_not_datarc_ids(schema_spec) -> None:
    """No tool accepts a Datarc id; the model refers to fields only by name."""
    out = await schema_spec.handler({}, _FakeCtx(_FakeDatarc(_mixed_dims(), _mixed_measures())))
    assert all("id" not in item for item in out["measures"] + out["dimensions"])
