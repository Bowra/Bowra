"""批2/c2 — ConnectorContextService（可信身份阶梯 + 可及集合缓存）契约."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import TYPE_CHECKING

from bowhead_gateway.connector_context_service import ConnectorContextService, DatarcWorkspaceRef

from bowhead_gateway import connector_context_service as tcs

if TYPE_CHECKING:
    import pytest


class _FakeUserStore:
    def __init__(self, email: str = "a@x.com", verified: bool = True):
        self._u = SimpleNamespace(email=email, email_verified=verified)

    async def get_user(self, user_id: str):
        return self._u


class _FakeUserMemory:
    def __init__(self, l3_uid: str = ""):
        self._l3 = l3_uid

    async def get_tool_context(self, user_id: str, tool: str, slot: str = "workspace"):
        if slot == "account" and self._l3:
            return {"id": self._l3}
        return None


class _FakeDatarc:
    def __init__(self, account_uid: str = "acc_1", projects=None, fail_list: bool = False):
        self._uid = account_uid
        self._projects = (
            projects
            if projects is not None
            else [
                {"id": 42, "name": "零售集团"},
                {"projectId": "7", "name": "华东大区"},
            ]
        )
        self._fail_list = fail_list
        self.fetch_calls = 0
        self.list_calls = 0

    async def fetch_account(self, email: str):
        self.fetch_calls += 1
        return {"uid": self._uid} if self._uid else None

    async def list_account_projects(self, account_uid: str, *, limit: int = 50):
        self.list_calls += 1
        if self._fail_list:
            raise RuntimeError("datarc down")
        return self._projects


def _svc(store=None, mem=None) -> ConnectorContextService:
    return ConnectorContextService(
        user_store=store or _FakeUserStore(),
        user_memory_store=mem or _FakeUserMemory(),
    )


async def test_l1_verified_email_resolves_and_lists():
    d = _FakeDatarc()
    refs = await _svc().accessible_workspaces(d, "u_1")
    assert refs == (
        DatarcWorkspaceRef(id="42", name="零售集团"),
        DatarcWorkspaceRef(id="7", name="华东大区"),
    )


async def test_unverified_email_is_untrusted_falls_to_l3():
    """PRD §3.2: 未验证自报 email 不可信——L1 跳过；L3 admin 映射兜底."""
    d = _FakeDatarc()
    svc = _svc(store=_FakeUserStore(verified=False), mem=_FakeUserMemory(l3_uid="acc_l3"))
    uid = await svc.resolve_trusted_account_uid(d, "u_1")
    assert uid == "acc_l3"
    assert d.fetch_calls == 0  # Never fetch_account for unverified email.


async def test_no_trusted_identity_empty_and_negative_cached():
    d = _FakeDatarc(account_uid="")  # fetch_account returns None.
    svc = _svc(store=_FakeUserStore(verified=False))
    assert await svc.accessible_workspaces(d, "u_1") == ()
    assert await svc.accessible_workspaces(d, "u_1") == ()
    assert d.list_calls == 0  # Negative cache: second call does not hit datarc.


async def test_positive_cache_hits_within_ttl():
    d = _FakeDatarc()
    svc = _svc()
    await svc.accessible_workspaces(d, "u_1")
    await svc.accessible_workspaces(d, "u_1")
    assert d.list_calls == 1


async def test_negative_ttl_shorter_than_positive(monkeypatch: pytest.MonkeyPatch):
    """负缓存 5min < 正缓存 15min：6min 后无账号用户重查、有账号用户仍命中."""
    d_empty = _FakeDatarc(account_uid="")
    svc = _svc(store=_FakeUserStore(verified=False))
    await svc.accessible_workspaces(d_empty, "u_neg")
    d_pos = _FakeDatarc()
    svc2 = _svc()
    await svc2.accessible_workspaces(d_pos, "u_pos")

    t0 = time.monotonic()
    monkeypatch.setattr(tcs.time, "monotonic", lambda: t0 + 6 * 60)
    d_empty2 = _FakeDatarc(account_uid="acc_now")  # User provisioned within 6min.
    svc._user_store = _FakeUserStore(verified=True)
    refs = await svc.accessible_workspaces(d_empty2, "u_neg")
    assert refs  # Negative cache expired → requery newly provisioned set.
    assert (await svc2.accessible_workspaces(d_pos, "u_pos")) and d_pos.list_calls == 1  # Positive cache still hits.


async def test_list_failure_not_cached_as_empty():
    """datarc 故障≠无工作区：不落负缓存，下次重试（review：矩阵格5 语义分辨）."""
    d = _FakeDatarc(fail_list=True)
    svc = _svc()
    assert await svc.accessible_workspaces(d, "u_1") == ()
    d._fail_list = False
    assert await svc.accessible_workspaces(d, "u_1")  # Query immediately after recovery, no 5min penalty.
    assert d.list_calls == 2


async def test_manual_invalidate_clears():
    d = _FakeDatarc()
    svc = _svc()
    await svc.accessible_workspaces(d, "u_1")
    svc.invalidate_user("u_1")
    await svc.accessible_workspaces(d, "u_1")
    assert d.list_calls == 2


# ─── review P2 regression: transient identity failures must not become 5min negative cache ──


async def test_transient_identity_failure_not_negative_cached():
    """review P2: fetch_account 抛错（datarc blip）→ 不写负缓存，下 turn 重试。
    对比 genuine not-found（返 None）→ 可缓存。"""
    import types

    from bowhead_gateway.connector_context_service import ConnectorContextService

    class _UStore:
        async def get_user(self, uid):
            return types.SimpleNamespace(email="a@x.com", email_verified=True)

    class _UMem:
        async def get_tool_context(self, *a, **k):
            return None

    # Transient failure: fetch_account raises.
    class _DarcFail:
        async def fetch_account(self, email):
            raise RuntimeError("connection reset")

        async def list_account_projects(self, *a, **k):
            return []

    svc = ConnectorContextService(user_store=_UStore(), user_memory_store=_UMem())
    refs = await svc.accessible_workspaces(_DarcFail(), "u_1")
    assert refs == ()
    # Key point: transient failure writes no cache → next call requeries (not a 5min negative-cache hit).
    assert "u_1" not in svc._cache

    # Genuine not-found: fetch_account returns None → negative result may be cached.
    class _DarcNone:
        async def fetch_account(self, email):
            return None

        async def list_account_projects(self, *a, **k):
            return []

    svc2 = ConnectorContextService(user_store=_UStore(), user_memory_store=_UMem())
    assert await svc2.accessible_workspaces(_DarcNone(), "u_2") == ()
    assert "u_2" in svc2._cache  # Truly no identity → normal negative cache.


async def test_identity_store_failures_are_transient_and_not_negative_cached():
    class _FailingUserStore:
        async def get_user(self, _user_id: str):
            raise RuntimeError("user store unavailable")

    class _FailingUserMemory:
        async def get_tool_context(self, *_args, **_kwargs):
            raise RuntimeError("memory store unavailable")

    svc = ConnectorContextService(
        user_store=_FailingUserStore(),
        user_memory_store=_FailingUserMemory(),
    )

    refs, reason = await svc.accessible_workspaces_ex(_FakeDatarc(), "u_1")

    assert refs == ()
    assert reason == "transient"
    assert "u_1" not in svc._cache


async def test_upper_identity_failure_keeps_l3_refs_non_authoritative():
    class _FailingUserStore:
        async def get_user(self, _user_id: str):
            raise RuntimeError("user store unavailable")

    svc = ConnectorContextService(
        user_store=_FailingUserStore(),
        user_memory_store=_FakeUserMemory(l3_uid="acc_l3"),
    )

    refs, reason = await svc.accessible_workspaces_ex(_FakeDatarc(), "u_1")

    assert refs
    assert reason == "transient"
    assert "u_1" not in svc._cache
