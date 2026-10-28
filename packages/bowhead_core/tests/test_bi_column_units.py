"""Expose cached column currencies and units through single and batch queries.

Verify currency_code precedence over unit_name and preservation through the
batch response whitelist. Missing metadata must not invent a unit.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from bowhead_core.tool_registry import (
    ToolExecutionContext,
    _column_units,
    build_default_registry,
)

# Synthetic currency metadata includes both a currency code and a local unit.
_META = {
    "内销销售金额": {"currency_code": "CNY", "unit_name": "元", "value_format": "float"},
    "内销销售数量": {"currency_code": "", "unit_name": "盒", "value_format": "int"},
}


def _client(meta=None):
    c = MagicMock()
    c.measure_metadata = MagicMock(side_effect=lambda ws, name: (meta if meta is not None else _META).get(name))
    return c


_WS = "ws_000000000001"  # Preserve the required workspace ID shape in this synthetic fixture.


def _ctx(client):
    return SimpleNamespace(datarc=client, resolve_datarc_project=lambda: _WS)


def test_measure_columns_get_their_unit() -> None:
    units = _column_units(_ctx(_client()), ["渠道", "内销销售金额", "内销销售数量"])
    assert units == {"内销销售金额": "CNY", "内销销售数量": "盒"}
    assert "渠道" not in units, "维度列没有单位，不该硬塞一个"


def test_currency_code_wins_over_unit_name() -> None:
    """`CNY` 在任何语言的正文里都不会被误读；`元` 在英文答复里会被模型换成 $。"""
    assert _column_units(_ctx(_client()), ["内销销售金额"]) == {"内销销售金额": "CNY"}


def test_fail_open_everywhere() -> None:
    assert _column_units(SimpleNamespace(datarc=None), ["内销销售金额"]) == {}
    # Client lacks measure_metadata (pluggable source) ⇒ no error and no keys.
    assert _column_units(_ctx(SimpleNamespace()), ["内销销售金额"]) == {}
    # Metadata is unavailable.
    assert _column_units(_ctx(_client({})), ["内销销售金额"]) == {}
    # Metadata exists but both fields are empty ⇒ do not guess.
    blank = {"内销销售金额": {"currency_code": "", "unit_name": ""}}
    assert _column_units(_ctx(_client(blank)), ["内销销售金额"]) == {}
    # A measure_metadata exception must not break data retrieval.
    boom = MagicMock()
    boom.measure_metadata = MagicMock(side_effect=RuntimeError("cache exploded"))
    assert _column_units(_ctx(boom), ["内销销售金额"]) == {}


# ── Wiring: a correct pure function is insufficient; it must reach the model surface ──


def _wire(columns, rows):
    fake = MagicMock()
    fake.rows = rows
    fake.columns = columns
    fake.total_rows = len(rows)
    fake.csv_filename = "x.csv"
    fake.task_uid = "task_u"
    fake.download_ms = 10

    datarc = _client()
    datarc.download_data = AsyncMock(return_value=fake)

    class _ArtifactStore:
        async def put_bytes(self, **kwargs):
            return SimpleNamespace(id="art_u", filename=kwargs["filename"])

    return datarc, ToolExecutionContext(
        workspace_id=_WS,
        # ⚠ Set explicitly: `resolve_datarc_project()` recognizes only `active_datarc_project`;
        # the workspace_id fallback is retired (the ws_ branch merely skips warnings and still returns "").
        active_datarc_project=_WS,
        member_uid="m",
        user_id="u",
        turn_id="t_units",
        datarc=datarc,
        artifact_store=_ArtifactStore(),
        session_id="s_units",
    )


@pytest.mark.asyncio
async def test_units_reach_the_model_on_a_single_query() -> None:
    _, ctx = _wire(["渠道", "内销销售金额"], [{"渠道": "Hospital", "内销销售金额": 2.6e9}])
    res = await build_default_registry().get("bi_query").handler({"text": "2026年8月 内销销售金额 按渠道"}, ctx)
    assert (res or {}).get("_units") == {"内销销售金额": "CNY"}, f"单条路没带单位: {list((res or {}).keys())}"


@pytest.mark.asyncio
async def test_units_survive_the_batch_wrapper() -> None:
    """★ 批量 wrapper 是**字段白名单**：不显式转发的键会被静默丢掉。

    合批是常走的路（真机 12 轮里绝大多数查询都合批），所以这条比单条那条更要紧；
    而且它一旦漏，纯函数测试和单条接线测试**全都是绿的**。
    """
    _, ctx = _wire(["渠道", "内销销售金额"], [{"渠道": "Hospital", "内销销售金额": 2.6e9}])
    res = await build_default_registry().get("bi_query").handler({"queries": ["2026年8月 内销销售金额 按渠道", "2026年7月 内销销售金额 按渠道"]}, ctx)
    subs = (res or {}).get("results") or []
    assert subs, f"批量没返回 results: {list((res or {}).keys())}"
    assert all(s.get("_units") == {"内销销售金额": "CNY"} for s in subs), f"批量路把 _units 丢了（白名单漏键）: {[s.get('_units') for s in subs]}"


@pytest.mark.asyncio
async def test_no_units_key_when_nothing_is_known() -> None:
    """空 map 不出键 —— 给模型一个空壳只会让它去解读「为什么没有单位」。"""
    fake = MagicMock()
    fake.rows = [{"渠道": "Hospital"}]
    fake.columns = ["渠道"]
    fake.total_rows = 1
    fake.csv_filename = "x.csv"
    fake.task_uid = "task_n"
    fake.download_ms = 10
    datarc = _client({})
    datarc.download_data = AsyncMock(return_value=fake)

    class _ArtifactStore:
        async def put_bytes(self, **kwargs):
            return SimpleNamespace(id="art_n", filename=kwargs["filename"])

    ctx = ToolExecutionContext(
        workspace_id=_WS,
        active_datarc_project=_WS,
        member_uid="m",
        user_id="u",
        turn_id="t_nounits",
        datarc=datarc,
        artifact_store=_ArtifactStore(),
        session_id="s_nounits",
    )
    res = await build_default_registry().get("bi_query").handler({"text": "2026年8月 内销销售金额 按渠道"}, ctx)
    assert "_units" not in (res or {})
