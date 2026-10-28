"""DatarcClient unit tests（respx 模拟 HTTP 交互）."""

from __future__ import annotations

import json

import httpx2
import pytest
import respx

from bowhead_bi_bridge import (
    BINotFoundError,
    BIPermissionError,
    BIQueryFailedError,
    BIValidationError,
    DatarcClient,
    DatarcError,
)

BASE = "https://demo.datarc.cn"


@pytest.fixture
async def client():
    c = DatarcClient(BASE, auth_key="dev-key", timeout_sec=10)
    try:
        yield c
    finally:
        await c.aclose()


# ─────────────────────────────────────────────────────────────
# Metadata
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
@respx.mock
async def test_get_scope_uses_bearer_and_scope_endpoint(client: DatarcClient) -> None:
    route = respx.get(f"{BASE}/openapi/v1/scopes/").respond(
        200,
        json={"errcode": 200, "errmsg": "success", "data": {"id": "key-1", "permissions": [], "workspaces": []}},
    )

    result = await client.get_scope()

    assert result["id"] == "key-1"
    assert route.calls[0].request.headers["Authorization"] == "Bearer dev-key"


@pytest.mark.unit
@respx.mock
async def test_list_workspaces_happy(client: DatarcClient) -> None:
    respx.get(f"{BASE}/openapi/v1/workspaces/").respond(
        200,
        json={
            "errcode": 200,
            "errmsg": "success",
            "data": [
                {"id": 1, "name": "Demo", "desc": "d", "adapter": 0, "date_offset": -1},
                {"id": 2, "name": "Other"},
            ],
        },
    )
    result = await client.list_workspaces()
    assert len(result) == 2
    # Convert id to string.
    assert result[0].id == "1"
    assert result[0].name == "Demo"


@pytest.mark.unit
@respx.mock
async def test_list_workspaces_trailing_slash_enforced(client: DatarcClient) -> None:
    """验证：即使调用方传的 path 没 `/`，client 也会补上."""
    # The respx mock must include `/`, because the client adds it.
    respx.get(f"{BASE}/openapi/v1/workspaces/").respond(200, json={"errcode": 200, "errmsg": "ok", "data": []})
    # Internal _request adds / automatically.
    await client._request("GET", "openapi/v1/workspaces")  # Deliberately omit /.
    assert respx.calls.call_count == 1


@pytest.mark.unit
@respx.mock
async def test_create_account_uses_password_disabled_payload(client: DatarcClient) -> None:
    route = respx.post(f"{BASE}/openapi/v1/accounts/").respond(
        200,
        json={
            "errcode": 200,
            "errmsg": "success",
            "data": {"uid": "7da1c2da-d81b-4c32-a7b5-f77624451d32"},
        },
    )
    result = await client.create_account(
        uid="7da1c2da-d81b-4c32-a7b5-f77624451d32",
        email="user@example.com",
        nickname="Ada",
        metadata={"groups": ["analysts"]},
    )

    assert result["uid"] == "7da1c2da-d81b-4c32-a7b5-f77624451d32"
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer dev-key"
    assert request.content == (
        b'{"uid":"7da1c2da-d81b-4c32-a7b5-f77624451d32","email":"user@example.com","nickname":"Ada","metadata":{"groups":["analysts"]}}'
    )


@pytest.mark.unit
@respx.mock
async def test_list_measures_with_names(client: DatarcClient) -> None:
    respx.get(f"{BASE}/openapi/v1/workspaces/1/measures/").respond(
        200,
        json={
            "errcode": 200,
            "errmsg": "success",
            "data": [
                {"id": 17, "names": ["营业额", "GMV", "销售额"], "description": "d", "sort": 1},
            ],
        },
    )
    respx.get(f"{BASE}/openapi/v1/workspaces/1/models/").respond(200, json={"errcode": 200, "errmsg": "success", "data": []})
    result = await client.list_measures("1")
    assert len(result) == 1
    assert result[0].primary_name == "营业额"
    assert "GMV" in result[0].names


# ─────────────────────────────────────────────────────────────
# Measure metadata enrichment (unitName / model-layer format)
# 2026-08-24: `/measures/` always included unitName, and the model layer included format; both were previously discarded.
# Fixture shape copied from a real response on demo.datarc.cn, ws=1.
# ─────────────────────────────────────────────────────────────
def _mock_measures_and_models(measures: list, models: list) -> None:
    respx.get(f"{BASE}/openapi/v1/workspaces/1/measures/").respond(200, json={"errcode": 200, "errmsg": "success", "data": measures})
    respx.get(f"{BASE}/openapi/v1/workspaces/1/models/").respond(200, json={"errcode": 200, "errmsg": "success", "data": models})


@pytest.mark.unit
@respx.mock
async def test_measure_carries_unit_and_percentage_format(client: DatarcClient) -> None:
    """★ 达成率这类 0-1 比率必须带出 format=percentage——下游靠它决定要不要 ×100，
    光看值分不出（达成率 1.02=102% vs 市场份额 2.45=2.45%）。"""
    _mock_measures_and_models(
        measures=[
            {"id": 1, "names": ["营业额"], "unitName": "元", "description": "赚了多少", "sort": -1},
            {"id": 71, "names": ["利润率3"], "sort": -1},
        ],
        models=[
            {
                "uid": "m1",
                "measures": [
                    {"id": 1, "type": "float", "field": "val_1", "format": "float", "calcType": "sum"},
                    {"id": 71, "sql": "sum(val_3)/sum(val_1)", "type": "float", "format": "percentage"},
                ],
            }
        ],
    )
    by_id = {m.id: m for m in await client.list_measures("1")}
    assert by_id[1].unit_name == "元"
    assert by_id[1].value_format == "float"
    assert by_id[71].value_format == "percentage"


@pytest.mark.unit
@respx.mock
async def test_formula_measure_format_comes_from_the_concept_layer(client: DatarcClient) -> None:
    """★★公式指标的 format 只在概念层——只读模型层等于对**比率类指标系统性丢失**这条事实。

    116 实测（2026-08-27）：`利润率`(id=68, formula=`[利润]/[营业额]`) 与 `退款率`(id=72)
    的 `models` 都是空数组，模型层 /models/ 里根本没有它们的 id，而概念层 item 自己就写着
    `"format": "percentage"`。原实现只 `formats.get(id)` → 恒空 → 表格把 0.1802 原样渲出来，
    同一轮的 KPI 和散文却都是 18.0%。比率类指标几乎全是公式指标，所以这不是个例。
    """
    _mock_measures_and_models(
        measures=[
            # Formula measure: the concept layer supplies format; models is empty.
            {"id": 68, "names": ["利润率", "profit_margin"], "format": "percentage", "formula": "[利润] / [营业额]", "models": []},
            # Physical measure: the concept layer lacks format; use the model layer.
            {"id": 1, "names": ["营业额"], "unitName": "元"},
        ],
        models=[{"uid": "m1", "measures": [{"id": 1, "format": "float"}]}],
    )
    by_id = {m.id: m for m in await client.list_measures("1")}
    assert by_id[68].value_format == "percentage"  # Read the concept-layer format.
    assert by_id[1].value_format == "float"  # Preserve the model-layer format.

    # Both downstream renaming and enrichment use the same measure_metadata.
    assert client.measure_metadata("1", "利润率")["value_format"] == "percentage"
    assert client.measure_metadata("1", "profit_margin")["value_format"] == "percentage"


@pytest.mark.unit
@respx.mock
async def test_concept_and_model_format_conflict_prefers_percentage(client: DatarcClient) -> None:
    """两层都说话时按同一套冲突解法：percentage 优先，其次 float 胜 int。"""
    _mock_measures_and_models(
        measures=[
            {"id": 6, "names": ["a"], "format": "int"},
            {"id": 12, "names": ["b"], "format": "percentage"},
        ],
        models=[{"uid": "m1", "measures": [{"id": 6, "format": "float"}, {"id": 12, "format": "float"}]}],
    )
    by_id = {m.id: m for m in await client.list_measures("1")}
    assert by_id[6].value_format == "float"
    assert by_id[12].value_format == "percentage"


@pytest.mark.unit
@respx.mock
async def test_measure_format_conflict_prefers_percentage_then_float(client: DatarcClient) -> None:
    """同一 measure 在不同模型里 format 不同（demo 实测 21 个里 2 个冲突）：
    percentage 优先，其次 float 胜 int。"""
    _mock_measures_and_models(
        measures=[{"id": 6, "names": ["a"]}, {"id": 12, "names": ["b"]}],
        models=[
            {"uid": "m1", "measures": [{"id": 6, "format": "int"}, {"id": 12, "format": "float"}]},
            {"uid": "m2", "measures": [{"id": 6, "format": "float"}, {"id": 12, "format": "percentage"}]},
        ],
    )
    by_id = {m.id: m for m in await client.list_measures("1")}
    assert by_id[6].value_format == "float"
    assert by_id[12].value_format == "percentage"


@pytest.mark.unit
@respx.mock
async def test_models_failure_is_fail_open(client: DatarcClient) -> None:
    """★ /models/ 挂了不许拖垮 schema——schema 是全链路最高频路径。
    正对照：measures 本身照常返回，只是没有 format。"""
    respx.get(f"{BASE}/openapi/v1/workspaces/1/measures/").respond(
        200,
        json={"errcode": 200, "errmsg": "success", "data": [{"id": 1, "names": ["营业额"], "unitName": "元"}]},
    )
    respx.get(f"{BASE}/openapi/v1/workspaces/1/models/").respond(500, json={"detail": "boom"})
    result = await client.list_measures("1")
    assert len(result) == 1
    assert result[0].unit_name == "元"  # Concept-layer fields are unaffected.
    assert result[0].value_format == ""  # Missing enrichment does not raise.


@pytest.mark.unit
@respx.mock
async def test_measure_metadata_exposes_raw_currency(client: DatarcClient) -> None:
    """★ 源只吐**原生字段**，不做解释——「这算不算钱」归 bowhead_core.units。

    真 currencyCode 必须原样传出去：没有它，USD workspace 的 revenue 会被
    core 的名称兜底猜成 CNY（citation_marks 里 USD→美元 一直都在，只是走不到）。
    """
    _mock_measures_and_models(
        measures=[{"id": 1, "names": ["营业额", "revenue"], "currencyCode": "USD"}],
        models=[],
    )
    await client.list_measures("1")  # Warm the schema cache (the spawn prefetch path does this in production).
    assert client.measure_metadata("1", "营业额") == {
        "currency_code": "USD",
        "value_format": "",
        "unit_name": "",
        "display_names": {},
        "primary_name": "营业额",
    }
    # Aliases resolve too.
    assert client.measure_metadata("1", "revenue")["currency_code"] == "USD"


@pytest.mark.unit
@respx.mock
async def test_measure_metadata_exposes_format_and_user_unit(client: DatarcClient) -> None:
    """模型层 format 与客户自填 unitName 都要原样带出来."""
    _mock_measures_and_models(
        measures=[
            {"id": 71, "names": ["利润率3"]},
            {"id": 2, "names": ["客流量"], "unitName": "人次"},
        ],
        models=[{"uid": "m1", "measures": [{"id": 71, "format": "percentage"}]}],
    )
    await client.list_measures("1")
    assert client.measure_metadata("1", "利润率3")["value_format"] == "percentage"
    assert client.measure_metadata("1", "客流量")["unit_name"] == "人次"


@pytest.mark.unit
async def test_measure_metadata_cold_cache_returns_none_without_fetching(client: DatarcClient) -> None:
    """★ 调用点在渲染路上：查不到就是 None，**绝不为查单位多打一跳**。

    正对照在上面两条——缓存热了就查得到，所以这条返 None 不是因为方法是死的。
    """
    assert client._measure_unit_cache == {}
    assert client.measure_metadata("1", "营业额") is None
    assert client.measure_metadata("1", "完成率") is None
    assert client._measure_unit_cache == {}  # A cache miss does not trigger a fetch.


@pytest.mark.unit
@respx.mock
async def test_models_fetched_once_per_workspace(client: DatarcClient) -> None:
    """缓存：list_measures 每次 spawn subagent 都会调，不缓存就是每轮多一跳。"""
    respx.get(f"{BASE}/openapi/v1/workspaces/1/measures/").respond(
        200, json={"errcode": 200, "errmsg": "success", "data": [{"id": 1, "names": ["a"]}]}
    )
    route = respx.get(f"{BASE}/openapi/v1/workspaces/1/models/").respond(200, json={"errcode": 200, "errmsg": "success", "data": []})
    await client.list_measures("1")
    await client.list_measures("1")
    await client.list_measures("1")
    assert route.call_count == 1


@pytest.mark.unit
@respx.mock
async def test_models_transient_failure_is_retried_not_cached() -> None:
    """A pooled client retries transient model-metadata failures instead of caching them.

    The second call must issue another request and retain the successful format.
    """
    async with DatarcClient(BASE, auth_key="k") as c:
        respx.get(f"{BASE}/openapi/v1/workspaces/1/measures/").respond(
            200, json={"errcode": 200, "errmsg": "success", "data": [{"id": 71, "names": ["达成率"]}]}
        )
        route = respx.get(f"{BASE}/openapi/v1/workspaces/1/models/")
        route.side_effect = [
            httpx2.Response(500, json={"detail": "boom"}),
            httpx2.Response(
                200,
                json={
                    "errcode": 200,
                    "errmsg": "success",
                    "data": [{"uid": "m1", "measures": [{"id": 71, "format": "percentage"}]}],
                },
            ),
        ]

        first = await c.list_measures("1")
        assert first[0].value_format == ""  # Fail open for this failed attempt.
        assert "1" not in c._measure_format_cache  # ← Crucially, do not cache the failure.

        second = await c.list_measures("1")
        assert route.call_count == 2  # A retry actually happened.
        assert second[0].value_format == "percentage"  # Retrieve the format after recovery.
        assert c.measure_metadata("1", "达成率")["value_format"] == "percentage"


# ─────────────────────────────────────────────────────────────
# ask_question
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
@respx.mock
async def test_ask_question_metric_type(client: DatarcClient) -> None:
    respx.post(f"{BASE}/openapi/v1/workspaces/1/data/question/").respond(
        200,
        json={
            "errcode": 200,
            "errmsg": "success",
            "data": {
                "url": "https://datarc.cn/#/discover?key=question&projectId=1",
                "type": "metric",
                "tokens": [],
                "data": {"metrics": [{"name": "营业额", "content": "29.3亿", "value": "2930123456.7"}]},
                "explanations": [
                    {
                        "sql": "SELECT SUM(amount) FROM sales WHERE ...",
                        "params": {"year": 2024},
                        "model": "sales_model",
                    }
                ],
            },
        },
    )
    result = await client.ask_question(datarc_workspace_id="1", member_uid="u_abc", text="今年 GMV")
    assert result.type == "metric"
    assert len(result.metrics) == 1
    assert result.metrics[0].name == "营业额"
    assert result.metrics[0].content == "29.3亿"
    # Parse explanations.
    assert len(result.explanations) == 1
    assert "SUM" in result.explanations[0].sql


@pytest.mark.unit
@respx.mock
async def test_ask_question_image_type(client: DatarcClient) -> None:
    respx.post(f"{BASE}/openapi/v1/workspaces/1/data/question/").respond(
        200,
        json={
            "errcode": 200,
            "errmsg": "success",
            "data": {
                "url": "https://...",
                "type": "image",
                "data": {"image": "iVBORw0KGgo..."},
                "tokens": [],
                "explanations": [],
            },
        },
    )
    result = await client.ask_question("1", "u", "今年各月营业额")
    assert result.type == "image"
    assert result.image_base64.startswith("iVBORw")
    assert result.metrics == []


@pytest.mark.unit
@respx.mock
async def test_ask_question_unsupported(client: DatarcClient) -> None:
    respx.post(f"{BASE}/openapi/v1/workspaces/1/data/question/").respond(
        200,
        json={
            "errcode": 200,
            "errmsg": "success",
            "data": {
                "url": "https://...",
                "type": "unsupported",
                "data": {},
                "explanations": [],
            },
        },
    )
    result = await client.ask_question("1", "u", "一个模糊的问题")
    assert result.type == "unsupported"
    assert result.metrics == []


@pytest.mark.unit
@respx.mock
async def test_ask_question_errcode_400_invalid_result(client: DatarcClient) -> None:
    respx.post(f"{BASE}/openapi/v1/workspaces/1/data/question/").respond(
        200,
        json={"errcode": 400, "errmsg": "用户问题无法搜索出有效结果"},
    )
    with pytest.raises(BIQueryFailedError) as exc:
        await client.ask_question("1", "u", "乱七八糟的问题")
    assert exc.value.errcode == 400
    assert "无法搜索" in str(exc.value)


# ─────────────────────────────────────────────────────────────
# attribute
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
@respx.mock
async def test_attribute_happy(client: DatarcClient) -> None:
    respx.post(f"{BASE}/openapi/v1/workspaces/1/data/attribution-dimensions/").respond(
        200,
        json={
            "errcode": 200,
            "errmsg": "success",
            "data": {
                "title": "净营业额同比",
                "insight": "...",
                "description": "...",
                "subTitle": "2024-02-25 至 03-25",
                "measure": {"name": "净营业额", "value": 0},
                "measureDelta": 1000.0,
                "measureDimensionMaxDelta": 500.0,
                "rows": [
                    {"rank": 1, "dimension": "省", "attr": "北京", "measurePrevious": 100.0, "measureCurrent": 200.0, "text": "北京"},
                    {"rank": 2, "dimension": "省", "attr": "上海", "measurePrevious": 80.0, "measureCurrent": 150.0, "text": "上海"},
                ],
            },
        },
    )
    result = await client.attribute(
        datarc_workspace_id="1",
        member_uid="u",
        question="今年净营业额",
        comparison_type="yoy",
    )
    assert result.measure_name == "净营业额"
    assert len(result.rows) == 2
    assert result.rows[0].attr == "北京"
    assert result.rows[0].measure_current == 200.0


@pytest.mark.unit
async def test_attribute_invalid_comparison_type(client: DatarcClient) -> None:
    with pytest.raises(BIValidationError):
        await client.attribute("1", "u", "q", comparison_type="invalid")


# ─────────────────────────────────────────────────────────────
# HTTP-layer errors
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
@respx.mock
async def test_401_raises_permission_error(client: DatarcClient) -> None:
    respx.get(f"{BASE}/openapi/v1/workspaces/").respond(401)
    with pytest.raises(BIPermissionError):
        await client.list_workspaces()


@pytest.mark.unit
@respx.mock
async def test_403_raises_permission_error(client: DatarcClient) -> None:
    respx.get(f"{BASE}/openapi/v1/workspaces/").respond(403)
    with pytest.raises(BIPermissionError):
        await client.list_workspaces()


@pytest.mark.unit
@respx.mock
async def test_404_raises_not_found_error(client: DatarcClient) -> None:
    respx.get(f"{BASE}/openapi/v1/workspaces/").respond(404)
    with pytest.raises(BINotFoundError) as exc:
        await client.list_workspaces()
    # Remind the user about URL slashes.
    assert "/" in str(exc.value)


# ─────────────────────────────────────────────────────────────
# R7.5 (ws-datarc-decouple): exceptions never echo the raw upstream body
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
@respx.mock
async def test_non_2xx_body_not_echoed_in_exception(client: DatarcClient) -> None:
    """A non-2xx upstream body can carry cross-tenant data — it must not land in str(e),
    which reaches the API/LLM. Message keeps only bounded shape (status + content-type + len)."""
    secret_body = "SENSITIVE_CROSS_TENANT_PAYLOAD_xyz789"
    respx.get(f"{BASE}/openapi/v1/workspaces/").respond(
        418,
        text=secret_body,
        headers={"content-type": "text/plain"},
    )
    with pytest.raises(DatarcError) as exc:
        await client.list_workspaces()
    msg = str(exc.value)
    assert secret_body not in msg
    assert "418" in msg  # status preserved
    assert "len=" in msg  # bounded shape preserved (debuggable without the body)


@pytest.mark.unit
@respx.mock
async def test_non_json_body_not_echoed_in_exception(client: DatarcClient) -> None:
    secret_html = "<html>internal-stack-trace-LEAK-abc</html>"
    respx.get(f"{BASE}/openapi/v1/workspaces/").respond(
        200,
        text=secret_html,
        headers={"content-type": "text/html"},
    )
    with pytest.raises(DatarcError) as exc:
        await client.list_workspaces()
    assert "internal-stack-trace-LEAK-abc" not in str(exc.value)
    assert "not JSON" in str(exc.value)


@pytest.mark.unit
@respx.mock
async def test_network_error_message_uses_exception_class_not_full_url(
    client: DatarcClient,
) -> None:
    """httpx2 error text can carry the resolved full URL (host included) — the message must
    surface only the exception class + relative path, not str(e)."""
    respx.get(f"{BASE}/openapi/v1/workspaces/").mock(
        side_effect=httpx2.ConnectError("connecting to https://demo.datarc.cn failed"),
    )
    with pytest.raises(DatarcError) as exc:
        await client.list_workspaces()
    msg = str(exc.value)
    assert "ConnectError" in msg  # class name surfaced
    assert "demo.datarc.cn" not in msg  # host from the raw httpx2 message not leaked
    assert "openapi/v1/workspaces/" in msg  # relative path kept for tracing


# ─────────────────────────────────────────────────────────────
# Asynchronous LLM tasks
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
@respx.mock
async def test_call_llm_async_submit(client: DatarcClient) -> None:
    respx.post(f"{BASE}/openapi/v1/async-tasks/").respond(
        200,
        json={
            "errcode": 200,
            "errmsg": "ok",
            "data": {
                "uid": "task_xxx",
                "taskType": 20,
                "status": 0,
                "params": {},
                "result": {},
            },
        },
    )
    task = await client.call_llm_async(member_uid="u", prompt="hello")
    assert task.uid == "task_xxx"
    assert task.status == 0
    assert not task.is_terminal


@pytest.mark.unit
@respx.mock
async def test_get_llm_result_terminal(client: DatarcClient) -> None:
    respx.get(f"{BASE}/openapi/v1/async-tasks/task_x/").respond(
        200,
        json={
            "errcode": 200,
            "errmsg": "ok",
            "data": {
                "uid": "task_x",
                "taskType": 20,
                "status": 2,
                "result": {"content": "done", "reasoning_content": "", "prompt": "p"},
            },
        },
    )
    task = await client.get_llm_result("task_x")
    assert task.status == 2
    assert task.is_terminal
    assert task.result["content"] == "done"


# ─────────────────────────────────────────────────────────────
# Dimension-value keyword recall /keywords/recall/
# ─────────────────────────────────────────────────────────────
RECALL_URL = f"{BASE}/openapi/v1/workspaces/1/keywords/recall/"


@pytest.mark.unit
@respx.mock
async def test_recall_keywords_happy(client: DatarcClient) -> None:
    route = respx.post(RECALL_URL).respond(
        200,
        json={
            "errcode": 200,
            "errmsg": "success",
            "data": [
                {
                    "source": "门店",
                    "results": [{"word": "北京门店", "distance": 0.12, "prop": "d1=北京门店", "field": "d1", "tuids": "model-1"}],
                },
                {"source": "商店", "results": []},
            ],
        },
    )
    result = await client.recall_keywords("1", ["门店", "商店"], n_results=10)

    assert json.loads(route.calls.last.request.content) == {"words": ["门店", "商店"], "n_results": 10}
    assert route.calls.last.request.headers["Authorization"] == "Bearer dev-key"
    assert [r.source for r in result] == ["门店", "商店"]
    (cand,) = result[0].candidates
    assert (cand.word, cand.distance, cand.field, cand.prop, cand.tuids) == ("北京门店", 0.12, "d1", "d1=北京门店", "model-1")
    assert result[1].candidates == []


@pytest.mark.unit
@respx.mock
async def test_recall_keywords_omits_n_results_by_default(client: DatarcClient) -> None:
    route = respx.post(RECALL_URL).respond(200, json={"errcode": 200, "errmsg": "success", "data": [{"source": "门店", "results": []}]})
    await client.recall_keywords("1", ["门店"])
    assert json.loads(route.calls.last.request.content) == {"words": ["门店"]}


@pytest.mark.unit
@respx.mock
async def test_recall_keywords_vector_service_unconfigured(client: DatarcClient) -> None:
    respx.post(RECALL_URL).respond(200, json={"errcode": 400, "errmsg": "当前工作区未配置向量服务，请先配置向量服务后重试。"})
    with pytest.raises(BIValidationError) as ei:
        await client.recall_keywords("1", ["门店"])
    assert "向量服务" in (ei.value.errmsg or "")


@pytest.mark.unit
@respx.mock
async def test_recall_keywords_http_404_is_not_found(client: DatarcClient) -> None:
    """旧版 datarc 没有这个接口：HTTP 404，errcode 为空（调用方据此区分「接口不存在」）."""
    respx.post(RECALL_URL).respond(404, text="<html>404</html>")
    with pytest.raises(BINotFoundError) as ei:
        await client.recall_keywords("1", ["门店"])
    assert ei.value.errcode is None


@pytest.mark.unit
@respx.mock
async def test_recall_keywords_result_count_mismatch_raises(client: DatarcClient) -> None:
    """条数对不上不许按位置硬配——错位会把 A 词的候选挂到 B 词头上."""
    respx.post(RECALL_URL).respond(200, json={"errcode": 200, "errmsg": "success", "data": [{"source": "门店", "results": []}]})
    with pytest.raises(DatarcError):
        await client.recall_keywords("1", ["门店", "商店"])


@pytest.mark.unit
@respx.mock
async def test_recall_keywords_empty_words_sends_nothing(client: DatarcClient) -> None:
    assert await client.recall_keywords("1", []) == []


PERMISSIONS_URL = f"{BASE}/openapi/v1/workspaces/1/tokens/permissions/"


@pytest.mark.unit
@respx.mock
async def test_check_token_permissions_sends_member_and_list_tuids(client: DatarcClient) -> None:
    route = respx.post(PERMISSIONS_URL).respond(200, json={"errcode": 200, "errmsg": "success", "data": ["Normal", "No permission"]})
    tokens = [{"word": "Revenue", "prop": "m1"}, {"word": "Beijing", "prop": "d50=5YyX5Lqs", "tuids": ["model-1"]}]

    assert await client.check_token_permissions("1", "member-1", tokens) == ["Normal", "No permission"]
    assert json.loads(route.calls.last.request.content) == {"memberUid": "member-1", "language": "en-US", "tokens": tokens}


@pytest.mark.unit
@respx.mock
async def test_check_token_permissions_rejects_misaligned_statuses(client: DatarcClient) -> None:
    respx.post(PERMISSIONS_URL).respond(200, json={"errcode": 200, "errmsg": "success", "data": ["Normal"]})
    with pytest.raises(DatarcError):
        await client.check_token_permissions("1", "member-1", [{"word": "a", "prop": "m1"}, {"word": "b", "prop": "m2"}])
