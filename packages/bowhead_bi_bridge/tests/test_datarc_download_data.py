"""`download_data` 把 datarc 的响应映射成 `DownloadDataResult` 的那一段（接线层）。

⚠ 为什么单独写这一份：core 侧的 bi_query 测试用 `MagicMock` 当 download 结果，
**永远跑不到这里的映射**——字段改名、少接一个键、形状判断写反，那边全绿。
这里走 respx 模拟真实 HTTP：建任务 → 轮询 → 下 ZIP → 解 CSV。

口径字段（`message` / `message_json`）与「数据不完整」信号（`errors` / 多 CSV）都在这条路上。
两实例实测见 docs/datarc-api-dump/async_task_download_data.md §2b：dev 有结构化版、demo 只有文本版，
所以缺字段必须是「少一层信息」而不是报错。
"""

from __future__ import annotations

import io
import zipfile

import pytest
import respx

from bowhead_bi_bridge import DatarcClient

BASE = "https://demo.datarc.cn"
ZIP_URL = "https://obs.example.com/openapi/task.zip"

_EXPLAIN = {
    "models": ["门店销售"],
    "measures": ["营业额"],
    "date_filters": ["「下单日期」的「2026年」（2026-01-01 至 2026-09-18）"],
}


@pytest.fixture
async def client():
    c = DatarcClient(BASE, auth_key="dev-key", timeout_sec=10)
    try:
        yield c
    finally:
        await c.aclose()


def _zip_bytes(*csvs: tuple[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in csvs:
            z.writestr(name, body)
    return buf.getvalue()


def _mock_task(result: dict, zip_payload: bytes) -> None:
    respx.post(f"{BASE}/openapi/v1/async-tasks/").respond(
        200,
        json={"errcode": 200, "errmsg": "success", "data": {"uid": "task_1", "taskType": 22, "status": 0, "result": {}}},
    )
    respx.get(f"{BASE}/openapi/v1/async-tasks/task_1/").respond(
        200,
        json={"errcode": 200, "errmsg": "success", "data": {"uid": "task_1", "taskType": 22, "status": 2, "result": result}},
    )
    respx.get(ZIP_URL).respond(200, content=zip_payload)


async def _run(client: DatarcClient):
    return await client.download_data(
        datarc_workspace_id="1",
        member_uid="m",
        text="2026年 各品牌 营业额",
        poll_interval_sec=0.01,
    )


@pytest.mark.unit
@respx.mock
async def test_explain_and_completeness_fields_are_mapped(client: DatarcClient) -> None:
    """新版实例：口径两种形态都带回来；多 CSV + 失败项要如实计数。"""
    _mock_task(
        {"url": ZIP_URL, "errors": [1], "message": "生效模型为「门店销售」…", "message_json": _EXPLAIN},
        _zip_bytes(("a.csv", "品牌,营业额\nA,1\n"), ("b.csv", "品牌,营业额\nB,2\n")),
    )
    r = await _run(client)

    assert r.explain_json == _EXPLAIN, "结构化口径要原样带回"
    assert r.explain_message.startswith("生效模型")
    assert r.download_errors == [1]
    assert r.csv_count == 2, "ZIP 里有几个 CSV 要如实计数——只读第一个是既有行为，但不能不说"
    assert r.columns == ["品牌", "营业额"] and r.rows == [{"品牌": "A", "营业额": 1}], "仍取第一个 CSV"


@pytest.mark.unit
@respx.mock
async def test_old_instance_without_structured_explain(client: DatarcClient) -> None:
    """116 连的实例实测就是这形态：只有 message、没有 message_json ⇒ 少一层信息，不报错。"""
    _mock_task(
        {"url": ZIP_URL, "errors": [], "message": "生效模型为「门店销售」；查询指标为「营业额」。"},
        _zip_bytes(("only.csv", "品牌,营业额\nA,1\n")),
    )
    r = await _run(client)

    assert r.explain_json == {}
    assert r.explain_message.startswith("生效模型")
    assert r.download_errors == []
    assert r.csv_count == 1


@pytest.mark.unit
@respx.mock
async def test_legacy_result_without_any_explain_fields(client: DatarcClient) -> None:
    """更老的响应连 message 都没有：三个新字段各自退回空值，取数本身照常。"""
    _mock_task({"url": ZIP_URL}, _zip_bytes(("only.csv", "品牌,营业额\nA,1\n")))
    r = await _run(client)

    assert (r.explain_json, r.explain_message, r.download_errors) == ({}, "", [])
    assert r.csv_count == 1
    assert r.total_rows == 1


@pytest.mark.unit
@respx.mock
async def test_bool_in_errors_is_not_counted_as_an_index(client: DatarcClient) -> None:
    """`True` 在 Python 里是 int 的子类——不排除就会被当成下标 1，凭空报「有一项失败」。"""
    _mock_task(
        {"url": ZIP_URL, "errors": [True, False], "message": "x"},
        _zip_bytes(("only.csv", "品牌,营业额\nA,1\n")),
    )
    r = await _run(client)

    assert r.download_errors == []
