"""BI Mock Server — 模拟 datarc OpenAPI.

v0.2（对齐 datarc 真实响应格式，2026-04-18）。

覆盖端点（按优先级；M1 逐步补齐）：
    - GET  /health
    - GET  /openapi/v1/workspaces/
    - GET  /openapi/v1/workspaces/{ws}/
    - GET  /openapi/v1/workspaces/{ws}/models/
    - GET  /openapi/v1/workspaces/{ws}/measures/
    - GET  /openapi/v1/workspaces/{ws}/dimensions/
    - POST /openapi/v1/workspaces/{ws}/data/question/
    - POST /openapi/v1/workspaces/{ws}/data/card/
    - POST /openapi/v1/workspaces/{ws}/data/cards/
    - POST /openapi/v1/workspaces/{ws}/data/attribution-dimensions/
    - GET  /openapi/v1/workspaces/{ws}/data/insight/
    - POST /openapi/v1/accounts/fetch/

关键约束：
    - 所有 URL 必须以 `/` 结尾
    - 响应用 errcode 而非 HTTP status 表达业务状态
    - memberUid 放 body（POST）或 query（GET insight）
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

app = FastAPI(
    title="BI Mock (datarc-compatible)",
    version="0.2.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    description="Bowhead 开发期 BI 替身，模拟 datarc OpenAPI 响应格式。",
)


# ─────────────────────────────────────────────────────────────
# Common response envelope
# ─────────────────────────────────────────────────────────────
def envelope(data: Any, errcode: int = 200, errmsg: str = "success") -> dict[str, Any]:
    return {"errcode": errcode, "errmsg": errmsg, "data": data}


def business_error(errcode: int, errmsg: str) -> dict[str, Any]:
    return {"errcode": errcode, "errmsg": errmsg}


def _auth_ok(authorization: str | None) -> bool:
    return bool(authorization and authorization.lower().startswith("bearer "))


def _check_auth(authorization: str | None) -> None:
    if not _auth_ok(authorization):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")


# ─────────────────────────────────────────────────────────────
# Fixture data (in-memory)
# ─────────────────────────────────────────────────────────────
_WORKSPACES: list[dict[str, Any]] = [
    {
        "id": 1,
        "name": "Demo Ecommerce",
        "desc": "示例电商 workspace",
        "adapter": 0,
        "hc1": False,
        "auto_add_member": False,
        "date_offset": -1,
        "dt": True,
        "ddra": True,
        "ddya": True,
    },
]

_MODELS: dict[str, list[dict[str, Any]]] = {
    "1": [
        {
            "uid": "m_sales",
            "name": "sales_model",
            "table": "sales_fact",
            "dimensions": [{"id": 1}, {"id": 2}, {"id": 3}],
            "measures": [
                {"id": 17, "field": "gmv", "type": "float", "format": "currency"},
                {"id": 18, "field": "net_gmv", "type": "float", "format": "currency"},
            ],
        }
    ],
}

_MEASURES: dict[str, list[dict[str, Any]]] = {
    "1": [
        {"id": 17, "names": ["营业额", "GMV", "销售额"], "description": "订单金额（含退款）", "sort": 1},
        {"id": 18, "names": ["净营业额", "Net GMV"], "description": "扣除已完成退款后的营业额", "sort": 1},
        {"id": 19, "names": ["DAU", "日活"], "description": "当日去重活跃用户数", "sort": 1},
    ],
}

_DIMENSIONS: dict[str, list[dict[str, Any]]] = {
    "1": [
        {"id": 1, "names": ["省", "省份"], "type": "str", "rating": 1, "isKeyword": True, "used": True},
        {"id": 2, "names": ["城市", "市"], "type": "str", "rating": 1, "isKeyword": True, "used": True},
        {"id": 3, "names": ["渠道", "channel"], "type": "str", "rating": 1, "isKeyword": True, "used": True},
    ],
}

_ACCOUNTS: dict[str, dict[str, Any]] = {
    "aaa95ad475254d8e83566a13135e58fa": {
        "uid": "aaa95ad475254d8e83566a13135e58fa",
        "status": 1,
        "username": "alice",
        "nickname": "Alice",
        "email": "alice@example.com",
        "phone": "",
        "desc": "",
        "role": 1,
        "tier": 1,
        "type": 1,
        "metadata": {},
    },
}


# ─────────────────────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────────────────────
class AskQuestionRequest(BaseModel):
    memberUid: str
    text: str = Field(min_length=1, max_length=500)


class CardRequest(BaseModel):
    memberUid: str
    text: str
    autoLogin: int = 0


class AttributeRequest(BaseModel):
    memberUid: str
    question: str
    comparisonType: str = Field(pattern="^(yoy|ring)$")


class AccountFetchRequest(BaseModel):
    username: str


# ─────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────
@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "bi-mock", "emulates": "datarc v25.9+"}


# ─────────────────────────────────────────────────────────────
# Workspaces
# ─────────────────────────────────────────────────────────────
@app.get("/openapi/v1/workspaces/")
async def list_workspaces(
    authorization: Annotated[str | None, Header()] = None,
    q: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    _check_auth(authorization)
    items = _WORKSPACES
    if q:
        items = [w for w in items if q in w["name"] or q in w.get("desc", "")]
    return envelope(items)


@app.get("/openapi/v1/workspaces/{workspace_id}/")
async def get_workspace(
    workspace_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_auth(authorization)
    for w in _WORKSPACES:
        if str(w["id"]) == workspace_id:
            return envelope(w)
    return business_error(404, f"workspace {workspace_id} not found")


# ─────────────────────────────────────────────────────────────
# Models / Measures / Dimensions
# ─────────────────────────────────────────────────────────────
@app.get("/openapi/v1/workspaces/{workspace_id}/models/")
async def list_models(
    workspace_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_auth(authorization)
    return envelope(_MODELS.get(workspace_id, []))


@app.get("/openapi/v1/workspaces/{workspace_id}/models/{model_uid}/")
async def get_model(
    workspace_id: str,
    model_uid: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_auth(authorization)
    for m in _MODELS.get(workspace_id, []):
        if m["uid"] == model_uid:
            return envelope(m)
    return business_error(404, f"model {model_uid} not found")


@app.get("/openapi/v1/workspaces/{workspace_id}/measures/")
async def list_measures(
    workspace_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_auth(authorization)
    return envelope(_MEASURES.get(workspace_id, []))


@app.get("/openapi/v1/workspaces/{workspace_id}/dimensions/")
async def list_dimensions(
    workspace_id: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_auth(authorization)
    return envelope(_DIMENSIONS.get(workspace_id, []))


# ─────────────────────────────────────────────────────────────
# Data Query
# ─────────────────────────────────────────────────────────────
@app.post("/openapi/v1/workspaces/{workspace_id}/data/question/")
async def ask_question(
    workspace_id: str,
    body: AskQuestionRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_auth(authorization)
    text = body.text.strip()

    # Simple rules: keywords select different mock response types to test branches on the Bowhead side.
    if "无效" in text or "乱七八糟" in text:
        return business_error(400, "用户问题无法搜索出有效结果")

    if "表格" in text:
        return business_error(400, "数据查询结果为表格")

    # Contains "趋势" / "走势" / "每月" / "近N", etc. → image type
    if any(k in text for k in ("趋势", "走势", "每月", "每日", "各月", "走势")):
        return envelope(
            {
                "url": f"https://datarc.cn/#/discover?ws={workspace_id}&q={text}",
                "type": "image",
                "data": {"image": "iVBORw0KGgoAAAANSUhEUgAAAZAAAAA_MOCK_BASE64_PNG..."},
                "tokens": [],
                "explanations": _stub_explanations(text),
            }
        )

    # Default: metric
    metric_name = "营业额"
    for m in _MEASURES.get(workspace_id, []):
        if any(n in text for n in m["names"]):
            metric_name = m["names"][0]
            break

    return envelope(
        {
            "url": f"https://datarc.cn/#/discover?ws={workspace_id}&q={text}",
            "type": "metric",
            "data": {"metrics": [{"name": metric_name, "content": "29.3亿", "value": "2930123456.7"}]},
            "tokens": [],
            "explanations": _stub_explanations(text),
        }
    )


def _stub_explanations(text: str) -> list[dict[str, Any]]:
    return [
        {
            "sql": f"-- mock SQL for: {text}\nSELECT SUM(gmv) FROM sales_fact WHERE ...",
            "params": {"year": 2024},
            "model": "sales_model",
            "dimensions": [],
            "measures": [{"word": "营业额", "prop": "m17"}],
            "filters": [],
            "dateFilters": [],
            "keywords": [],
            "order": [],
            "limit": None,
            "having": [],
            "proportion": [],
        }
    ]


@app.post("/openapi/v1/workspaces/{workspace_id}/data/card/")
async def get_card(
    workspace_id: str,
    body: CardRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_auth(authorization)
    return envelope(
        {
            "url": f"https://datarc.cn/#/singleQuery?ws={workspace_id}&q={body.text}&headless=1",
            "title": "Mock 卡片",
            "cardType": 7,
            "sentenceType": 3,
            "explanations": _stub_explanations(body.text),
        }
    )


@app.post("/openapi/v1/workspaces/{workspace_id}/data/cards/")
async def get_cards(
    workspace_id: str,
    body: CardRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_auth(authorization)
    return envelope(
        [
            {
                "url": f"https://datarc.cn/#/singleQuery?ws={workspace_id}&q={body.text}&i=1&headless=1",
                "title": "Mock 卡片 1",
                "cardType": 7,
                "sentenceType": 3,
                "explanations": [],
            },
            {
                "url": f"https://datarc.cn/#/singleQuery?ws={workspace_id}&q={body.text}&i=2&headless=1",
                "title": "Mock 卡片 2",
                "cardType": 4,
                "sentenceType": 3,
                "explanations": [],
            },
        ]
    )


@app.post("/openapi/v1/workspaces/{workspace_id}/data/attribution-dimensions/")
async def attribute(
    workspace_id: str,
    body: AttributeRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_auth(authorization)
    return envelope(
        {
            "title": f"{body.question}{'同比' if body.comparisonType == 'yoy' else '环比'}",
            "insight": f"我们从不同维度分析，{body.question}在北京、上海的增长贡献最大。",
            "description": "以下维度数据变化显著，是导致指标变化最有可能的原因",
            "subTitle": "2024-02-25 至 03-25",
            "measure": {
                "name": body.question,
                "value": 0,
                "format": "float",
                "currencyCode": "CNY",
                "subTitle": "",
                "cumulative": False,
            },
            "measureDelta": 134668908.55,
            "measureDimensionMaxDelta": 43538786.44,
            "rows": [
                {
                    "rank": 1,
                    "dimension": "省",
                    "attr": "北京",
                    "measurePrevious": 324629749.57,
                    "measureCurrent": 368168536.02,
                    "text": "北京",
                    "mask_key": "",
                    "units": [],
                },
                {
                    "rank": 2,
                    "dimension": "省",
                    "attr": "上海",
                    "measurePrevious": 336211627.75,
                    "measureCurrent": 371107752.87,
                    "text": "上海",
                    "mask_key": "",
                    "units": [],
                },
                {
                    "rank": 3,
                    "dimension": "省",
                    "attr": "江苏",
                    "measurePrevious": 156901546.78,
                    "measureCurrent": 178744640.14,
                    "text": "江苏",
                    "mask_key": "",
                    "units": [],
                },
            ],
        }
    )


@app.get("/openapi/v1/workspaces/{workspace_id}/data/insight/")
async def explain(
    workspace_id: str,
    memberUid: Annotated[str, Query()],
    text: Annotated[str, Query()],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_auth(authorization)
    return envelope(
        {
            "type": "text",
            "text": f"针对'{text}'：整体呈增长趋势，主要由北京、上海两地驱动。",
        }
    )


# ─────────────────────────────────────────────────────────────
# Account
# ─────────────────────────────────────────────────────────────
@app.post("/openapi/v1/accounts/fetch/")
async def fetch_account(
    body: AccountFetchRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_auth(authorization)
    # Simple matching by username / phone / email
    for acc in _ACCOUNTS.values():
        if body.username in (acc["username"], acc["email"], acc["phone"]):
            return envelope(acc)
    return envelope(None)


# ─────────────────────────────────────────────────────────────
# Async LLM tasks (minimal implementation: complete immediately)
# ─────────────────────────────────────────────────────────────
_TASKS: dict[str, dict[str, Any]] = {}


@app.post("/openapi/v1/async-tasks/")
async def create_task(
    body: dict[str, Any],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_auth(authorization)
    uid = f"task_{uuid4().hex[:12]}"
    _TASKS[uid] = {
        "uid": uid,
        "taskType": body.get("taskType", 20),
        "status": 2,  # Mock succeeds immediately
        "params": body.get("params", {}),
        "result": {
            "content": "[mock] LLM reply",
            "reasoning_content": "",
            "prompt": body.get("params", {}).get("prompt", ""),
        },
        "creator": "mock",
        "createdAt": "2026-04-18T00:00:00Z",
        "updatedAt": "2026-04-18T00:00:00Z",
    }
    return envelope(_TASKS[uid])


@app.get("/openapi/v1/async-tasks/{task_uid}/")
async def get_task(
    task_uid: str,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _check_auth(authorization)
    if task_uid not in _TASKS:
        return business_error(404, "task not found")
    return envelope(_TASKS[task_uid])
