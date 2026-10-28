"""datarc `displayName` 的解析 —— 展示标签，与查询标识符 `names` 分开。"""

from __future__ import annotations

import httpx2
import pytest
import respx
from bowhead_bi_bridge.datarc_client import DatarcClient, _display_names

BASE = "http://bi-mock"


@pytest.mark.unit
def test_parses_locale_map() -> None:
    assert _display_names({"displayName": {"zh-CN": "营业额", "en-US": "GMV"}}) == {
        "zh-CN": "营业额",
        "en-US": "GMV",
    }


@pytest.mark.unit
def test_tolerates_upstream_junk() -> None:
    """上游可能给 None / 缺键 / 非字符串 —— 规整掉，别让每个下游各判一遍。"""
    assert _display_names({}) == {}
    assert _display_names({"displayName": None}) == {}
    assert _display_names({"displayName": []}) == {}
    assert _display_names({"displayName": {"zh-CN": "", "en-US": "  "}}) == {}
    assert _display_names({"displayName": {"zh-CN": "营业额", "en-US": 42}}) == {"zh-CN": "营业额"}


@pytest.mark.unit
@respx.mock
async def test_measures_and_dimensions_carry_display_names() -> None:
    """★两个 meta 都要带出来——只接一个的话另一半在模型面永远看不到。"""
    async with DatarcClient(BASE, auth_key="k") as c:
        respx.get(f"{BASE}/openapi/v1/workspaces/1/measures/").respond(
            200,
            json={
                "errcode": 200,
                "errmsg": "success",
                "data": [{"id": 1, "names": ["营业额", "sales"], "displayName": {"zh-CN": "营业额", "en-US": "GMV"}}],
            },
        )
        respx.get(f"{BASE}/openapi/v1/workspaces/1/models/").respond(200, json={"errcode": 200, "errmsg": "success", "data": []})
        respx.get(f"{BASE}/openapi/v1/workspaces/1/dimensions/").respond(
            200,
            json={
                "errcode": 200,
                "errmsg": "success",
                "data": [{"id": 48, "names": ["市", "city"], "displayName": {"zh-CN": "市", "en-US": "city"}}],
            },
        )

        m = (await c.list_measures("1"))[0]
        assert m.display_names == {"zh-CN": "营业额", "en-US": "GMV"}
        assert m.names == ["营业额", "sales"], "names 不许被展示名污染"

        d = (await c.list_dimensions("1"))[0]
        assert d.display_names == {"zh-CN": "市", "en-US": "city"}
        assert d.names == ["市", "city"]


@pytest.mark.unit
@respx.mock
async def test_missing_display_name_is_empty_not_fatal() -> None:
    """★负对照：没有 displayName 的老 workspace 照常解析（这字段是后加的）。"""
    async with DatarcClient(BASE, auth_key="k") as c:
        respx.get(f"{BASE}/openapi/v1/workspaces/1/measures/").respond(
            200, json={"errcode": 200, "errmsg": "success", "data": [{"id": 1, "names": ["营业额"]}]}
        )
        respx.get(f"{BASE}/openapi/v1/workspaces/1/models/").respond(200, json={"errcode": 200, "errmsg": "success", "data": []})
        m = (await c.list_measures("1"))[0]
        assert m.display_names == {}
        assert m.names == ["营业额"]


@pytest.mark.unit
@respx.mock
async def test_en_us_field_may_hold_chinese() -> None:
    """★实测边角：demo ws=1 的指标 4 是 `{"en-US": "销售量", "zh-CN": "销售量"}`。

    「配了 en-US」不等于「那格是英文」——解析层照实带出来，不许在这里做语言判断。
    """
    async with DatarcClient(BASE, auth_key="k") as c:
        respx.get(f"{BASE}/openapi/v1/workspaces/1/measures/").respond(
            200,
            json={
                "errcode": 200,
                "errmsg": "success",
                "data": [{"id": 4, "names": ["销售量"], "displayName": {"zh-CN": "销售量", "en-US": "销售量"}}],
            },
        )
        respx.get(f"{BASE}/openapi/v1/workspaces/1/models/").respond(200, json={"errcode": 200, "errmsg": "success", "data": []})
        m = (await c.list_measures("1"))[0]
        assert m.display_names["en-US"] == "销售量"


@pytest.mark.unit
@respx.mock
async def test_display_name_survives_models_failure() -> None:
    """/models/ 挂了不影响 displayName —— 它来自概念层，与模型层无关。"""
    async with DatarcClient(BASE, auth_key="k") as c:
        respx.get(f"{BASE}/openapi/v1/workspaces/1/measures/").respond(
            200,
            json={
                "errcode": 200,
                "errmsg": "success",
                "data": [{"id": 1, "names": ["营业额"], "displayName": {"en-US": "GMV"}}],
            },
        )
        respx.get(f"{BASE}/openapi/v1/workspaces/1/models/").mock(side_effect=httpx2.Response(500, json={"detail": "boom"}))
        m = (await c.list_measures("1"))[0]
        assert m.display_names == {"en-US": "GMV"}
        assert m.value_format == ""


@pytest.mark.unit
@respx.mock
async def test_dimension_metadata_accessor() -> None:
    """★维度也要能按名回查——图表里 `省` 这种列走的是这条路。

    正对照在下半段：冷缓存时返 None 且**不发请求**（与 measure 那份同规矩）。
    """
    async with DatarcClient(BASE, auth_key="k") as c:
        assert c.dimension_metadata("1", "省") is None  # Cold cache.
        respx.get(f"{BASE}/openapi/v1/workspaces/1/dimensions/").respond(
            200,
            json={
                "errcode": 200,
                "errmsg": "success",
                "data": [{"id": 50, "names": ["省", "province"], "displayName": {"zh-CN": "省", "en-US": "province"}}],
            },
        )
        await c.list_dimensions("1")
        meta = c.dimension_metadata("1", "省")
        assert meta is not None
        assert meta["display_names"]["en-US"] == "province"
        assert meta["primary_name"] == "省"
        # Dimensions have no unit/currency/format; all three must be empty to avoid contaminating unit enrichment.
        assert meta["currency_code"] == "" and meta["unit_name"] == "" and meta["value_format"] == ""
        # Aliases resolve too.
        assert c.dimension_metadata("1", "province") is not None
