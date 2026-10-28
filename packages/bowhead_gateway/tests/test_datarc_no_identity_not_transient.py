"""「用户在 datarc 没账号」不能被当成「临时故障」（2026-09-08，116 真机取证）。

现场（turn `t_e49a3a7ae0a9`）：dogfood 账号在 datarc 不存在，用户拿到的是

    「BI 数据源当前出现**临时上游故障** … **这不是权限问题**，稍后数据源恢复即可重试」

而它永远不会恢复 —— 真正该说的「请管理员给你开账号」就在 `tool_registry` 的
`no_identity` 分支里，只是走不到。

链路：datarc 对未注册用户回 **HTTP 200 + errcode=400「未找到相关用户」** ⇒
`_request` 抛 `BIValidationError` ⇒ `fetch_account` 没接（姊妹方法
`fetch_workspace_member` 接了）⇒ 调用方按注释「未找到返 None，抛的一律当瞬时」
把它标成 `transient` ⇒ 错误串换成「临时故障，别说没权限，稍后重试」。

★ 这里钉的是**用户面结果**，不是某个中间布尔：只断言 `fetch_account` 返 None
不够——真正会回归的是「absence_reason 变回 transient」。
"""

from __future__ import annotations

from typing import Any

import pytest
from bowhead_bi_bridge.errors import BIValidationError


class _FakeResp:
    def __init__(self, data: Any) -> None:
        self.data = data
        self.errmsg = ""
        self.request_path = "openapi/v1/accounts/fetch/"


def _client(monkeypatch, *, raise_exc: Exception | None = None, data: Any = None):
    from bowhead_bi_bridge.datarc_client import DatarcClient

    client = DatarcClient.__new__(DatarcClient)

    async def _request(*_a: Any, **_kw: Any):
        if raise_exc is not None:
            raise raise_exc
        return _FakeResp(data)

    monkeypatch.setattr(client, "_request", _request, raising=False)
    return client


@pytest.mark.asyncio
async def test_account_not_found_returns_none_not_raises(monkeypatch):
    """datarc 的「未找到相关用户」是**软失败** ⇒ None（与 fetch_workspace_member 对齐）。"""
    exc = BIValidationError("[u@x.com]未找到相关用户", errcode=400, errmsg="[u@x.com]未找到相关用户")
    client = _client(monkeypatch, raise_exc=exc)
    assert await client.fetch_account("u@x.com") is None


@pytest.mark.asyncio
async def test_other_validation_errors_still_raise(monkeypatch):
    """只软化「未找到」这一种——别把真错误也吞了。"""
    exc = BIValidationError("bad request", errcode=400, errmsg="参数不合法")
    client = _client(monkeypatch, raise_exc=exc)
    with pytest.raises(BIValidationError):
        await client.fetch_account("u@x.com")


@pytest.mark.asyncio
async def test_found_account_passes_through(monkeypatch):
    client = _client(monkeypatch, data={"uid": "acc_1"})
    assert (await client.fetch_account("u@x.com")) == {"uid": "acc_1"}


@pytest.mark.asyncio
async def test_english_not_found_also_soft_fails(monkeypatch):
    """datarc 换英文错误串也要认（姊妹方法同款判据）。"""
    exc = BIValidationError("account not found", errcode=400, errmsg="account not found")
    client = _client(monkeypatch, raise_exc=exc)
    assert await client.fetch_account("u@x.com") is None


# ── User-facing result: absence_reason must be no_identity rather than transient ──


class _NoUserStore:
    async def get_user(self, _uid: str) -> Any:
        return type("U", (), {"email": "u@x.com", "email_verified": True})()


class _EmptyMemory:
    async def get_tool_context(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
        return {}


@pytest.mark.asyncio
async def test_absence_reason_is_no_identity_not_transient(monkeypatch):
    """★ 真正会回归的那条：用户没账号 ⇒ `no_identity`（⇒ 「请管理员开账号」），
    **不是** `transient`（⇒ 「临时故障，稍后重试」，而它永不恢复）。"""
    from bowhead_gateway.connector_context_service import ConnectorContextService

    svc = ConnectorContextService.__new__(ConnectorContextService)
    svc._user_store = _NoUserStore()  # type: ignore[attr-defined]
    svc._user_memory = _EmptyMemory()  # type: ignore[attr-defined]
    svc._cache = {}  # type: ignore[attr-defined]

    # ★ Use the **real** DatarcClient (stub only HTTP), so the chain
    # datarc raises BIValidationError → fetch_account → _resolve_identity → absence_reason
    # runs entirely for real; a fake 'client returning None' would skip the very part under test.
    exc = BIValidationError("[u@x.com]未找到相关用户", errcode=400, errmsg="[u@x.com]未找到相关用户")
    real_client = _client(monkeypatch, raise_exc=exc)

    async def _no_projects(*_a: Any, **_kw: Any) -> list[Any]:
        return []

    monkeypatch.setattr(real_client, "list_account_projects", _no_projects, raising=False)

    refs, reason = await svc.accessible_workspaces_ex(real_client, "usr_x")  # type: ignore[attr-defined]
    assert refs == ()
    assert reason == "no_identity", f"没账号被标成 {reason!r} —— 用户会被告知「稍后重试」而它永不恢复"
