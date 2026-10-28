"""BI Mock（datarc-compatible）endpoint 测试。"""

from __future__ import annotations

import pytest
from bi_mock.main import app
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


HEADERS = {"Authorization": "Bearer dev-token"}
WS = "1"
MEM = "aaa95ad475254d8e83566a13135e58fa"


# ─────────────────────────────────────────────────────────────
# General
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200


@pytest.mark.unit
def test_auth_required(client: TestClient) -> None:
    resp = client.get("/openapi/v1/workspaces/")
    assert resp.status_code == 401


# ─────────────────────────────────────────────────────────────
# Workspaces
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_list_workspaces(client: TestClient) -> None:
    resp = client.get("/openapi/v1/workspaces/", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    # Contract: errcode must be present.
    assert body["errcode"] == 200
    assert body["errmsg"] == "success"
    assert isinstance(body["data"], list)
    assert len(body["data"]) >= 1


@pytest.mark.unit
def test_get_workspace_not_found_returns_200_with_errcode(client: TestClient) -> None:
    """datarc 契约：业务错误用 errcode 不是 HTTP status."""
    resp = client.get("/openapi/v1/workspaces/999/", headers=HEADERS)
    assert resp.status_code == 200  # HTTP remains 200
    body = resp.json()
    assert body["errcode"] == 404  # Business error in body
    assert "not found" in body["errmsg"]


# ─────────────────────────────────────────────────────────────
# Metadata (measures / dimensions)
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_list_measures_with_names(client: TestClient) -> None:
    resp = client.get(f"/openapi/v1/workspaces/{WS}/measures/", headers=HEADERS)
    body = resp.json()
    assert body["errcode"] == 200
    measures = body["data"]
    # At least a GMV metric; names[0] is the primary keyword.
    gmv = next(m for m in measures if "GMV" in m["names"])
    assert gmv["names"][0] in ("营业额", "GMV")


@pytest.mark.unit
def test_list_dimensions(client: TestClient) -> None:
    resp = client.get(f"/openapi/v1/workspaces/{WS}/dimensions/", headers=HEADERS)
    body = resp.json()
    assert body["errcode"] == 200
    dims = body["data"]
    assert any(d["isKeyword"] for d in dims)


# ─────────────────────────────────────────────────────────────
# Three ask_question branches
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_ask_question_metric_type(client: TestClient) -> None:
    resp = client.post(
        f"/openapi/v1/workspaces/{WS}/data/question/",
        headers=HEADERS,
        json={"memberUid": MEM, "text": "今年 GMV"},
    )
    body = resp.json()
    assert body["errcode"] == 200
    data = body["data"]
    assert data["type"] == "metric"
    assert len(data["data"]["metrics"]) == 1
    # explanations must include SQL.
    assert data["explanations"][0]["sql"]


@pytest.mark.unit
def test_ask_question_image_type(client: TestClient) -> None:
    resp = client.post(
        f"/openapi/v1/workspaces/{WS}/data/question/",
        headers=HEADERS,
        json={"memberUid": MEM, "text": "今年每月营业额走势"},
    )
    body = resp.json()
    data = body["data"]
    assert data["type"] == "image"
    assert "image" in data["data"]


@pytest.mark.unit
def test_ask_question_invalid_returns_errcode_400(client: TestClient) -> None:
    resp = client.post(
        f"/openapi/v1/workspaces/{WS}/data/question/",
        headers=HEADERS,
        json={"memberUid": MEM, "text": "乱七八糟的问题"},
    )
    # HTTP 200 + errcode=400 (datarc contract)
    assert resp.status_code == 200
    body = resp.json()
    assert body["errcode"] == 400
    assert "无法搜索" in body["errmsg"]


# ─────────────────────────────────────────────────────────────
# attribute
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_attribute_yoy(client: TestClient) -> None:
    resp = client.post(
        f"/openapi/v1/workspaces/{WS}/data/attribution-dimensions/",
        headers=HEADERS,
        json={"memberUid": MEM, "question": "今年净营业额", "comparisonType": "yoy"},
    )
    body = resp.json()
    assert body["errcode"] == 200
    data = body["data"]
    # Core contract: return ranked rows.
    assert len(data["rows"]) >= 3
    assert all("rank" in r and "dimension" in r for r in data["rows"])


@pytest.mark.unit
def test_attribute_invalid_comparison_type_422(client: TestClient) -> None:
    resp = client.post(
        f"/openapi/v1/workspaces/{WS}/data/attribution-dimensions/",
        headers=HEADERS,
        json={"memberUid": MEM, "question": "今年营业额", "comparisonType": "invalid"},
    )
    # FastAPI pydantic validation failure → 422
    assert resp.status_code == 422


# ─────────────────────────────────────────────────────────────
# card / cards / explain
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_get_card(client: TestClient) -> None:
    resp = client.post(
        f"/openapi/v1/workspaces/{WS}/data/card/",
        headers=HEADERS,
        json={"memberUid": MEM, "text": "今年营业额", "autoLogin": 0},
    )
    data = resp.json()["data"]
    assert data["url"].startswith("https://datarc.cn")
    assert "title" in data


@pytest.mark.unit
def test_get_cards_list(client: TestClient) -> None:
    resp = client.post(
        f"/openapi/v1/workspaces/{WS}/data/cards/",
        headers=HEADERS,
        json={"memberUid": MEM, "text": "今年 GMV 各渠道对比"},
    )
    data = resp.json()["data"]
    assert isinstance(data, list)
    assert len(data) >= 2


@pytest.mark.unit
def test_explain(client: TestClient) -> None:
    resp = client.get(
        f"/openapi/v1/workspaces/{WS}/data/insight/",
        headers=HEADERS,
        params={"memberUid": MEM, "text": "今年每月营业额"},
    )
    data = resp.json()["data"]
    assert data["type"] in ("text", "llm")
    assert data["text"]


# ─────────────────────────────────────────────────────────────
# Account
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_fetch_account_by_email(client: TestClient) -> None:
    resp = client.post(
        "/openapi/v1/accounts/fetch/",
        headers=HEADERS,
        json={"username": "alice@example.com"},
    )
    body = resp.json()
    assert body["errcode"] == 200
    assert body["data"]["uid"] == MEM


@pytest.mark.unit
def test_fetch_account_missing_returns_null(client: TestClient) -> None:
    resp = client.post(
        "/openapi/v1/accounts/fetch/",
        headers=HEADERS,
        json={"username": "nobody@example.com"},
    )
    body = resp.json()
    assert body["errcode"] == 200
    assert body["data"] is None


# ─────────────────────────────────────────────────────────────
# Async LLM tasks
# ─────────────────────────────────────────────────────────────
@pytest.mark.unit
def test_async_task_roundtrip(client: TestClient) -> None:
    resp = client.post(
        "/openapi/v1/async-tasks/",
        headers=HEADERS,
        json={"taskType": 20, "params": {"memberUid": MEM, "prompt": "hi"}},
    )
    task = resp.json()["data"]
    assert task["status"] == 2  # Mock succeeds immediately

    resp2 = client.get(
        f"/openapi/v1/async-tasks/{task['uid']}/",
        headers=HEADERS,
    )
    task2 = resp2.json()["data"]
    assert task2["result"]["content"]
