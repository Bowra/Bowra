"""bi-absent-turn R3.1 — 缺席原因在 gateway 侧的产出与透传.

覆盖 6 跳链路的前两跳（产地 + provider 注入）：
  service.accessible_workspaces_ex →(reason)→ DatarcContextProvider.resolve
  →(ResolvedConnectorContext.absence_reason)。
core 侧的消费（prompt 变体 / dispatch 分流）在
bowhead_core/tests/test_bi_availability.py；turn_runner 镜像双点有 grep-gate。
"""

from __future__ import annotations

from types import SimpleNamespace

from bowhead_gateway.connector_context_provider import DatarcContextProvider
from bowhead_gateway.connector_context_service import ConnectorContextService


class _FakeUserStore:
    def __init__(self, email: str = "a@x.com", verified: bool = True):
        self._u = SimpleNamespace(email=email, email_verified=verified)

    async def get_user(self, user_id: str):
        return self._u


class _FakeUserMemory:
    async def get_tool_context(self, user_id: str, tool: str, slot: str = "workspace", session_scoped: bool = False):
        return None

    async def set_tool_context(self, *a, **kw):
        return None

    async def clear_tool_context(self, *a, **kw):
        return None


class _FakeDatarc:
    """account_uid="" 模拟北极九章侧无账号；projects=[] 模拟有账号零授权。"""

    def __init__(self, account_uid: str = "acc_1", projects=None, fail_fetch: bool = False, fail_list: bool = False):
        self._uid = account_uid
        self._projects = projects if projects is not None else []
        self._fail_fetch = fail_fetch
        self._fail_list = fail_list

    async def fetch_account(self, email: str):
        if self._fail_fetch:
            raise RuntimeError("datarc blip")
        return {"uid": self._uid} if self._uid else None

    async def list_account_projects(self, account_uid: str, *, limit: int = 50):
        if self._fail_list:
            raise RuntimeError("datarc down")
        return self._projects


def _svc() -> ConnectorContextService:
    return ConnectorContextService(user_store=_FakeUserStore(), user_memory_store=_FakeUserMemory())


# ─── Hop 1: service produces the reason ─────────────────────


async def test_ex_no_identity() -> None:
    refs, reason = await _svc().accessible_workspaces_ex(_FakeDatarc(account_uid=""), "u_1")
    assert refs == () and reason == "no_identity"


async def test_ex_zero_workspaces() -> None:
    refs, reason = await _svc().accessible_workspaces_ex(_FakeDatarc(projects=[]), "u_1")
    assert refs == () and reason == "zero_workspaces"


async def test_ex_transient_on_identity_blip_not_cached() -> None:
    svc = _svc()
    refs, reason = await svc.accessible_workspaces_ex(_FakeDatarc(fail_fetch=True), "u_1")
    assert refs == () and reason == "transient"
    # Preserve the existing promise: transient failures are not negatively cached; query immediately after recovery (no 5min penalty).
    refs2, reason2 = await svc.accessible_workspaces_ex(_FakeDatarc(projects=[{"id": 1, "name": "w"}]), "u_1")
    assert len(refs2) == 1 and reason2 == ""


async def test_ex_transient_on_list_failure() -> None:
    refs, reason = await _svc().accessible_workspaces_ex(_FakeDatarc(fail_list=True), "u_1")
    assert refs == () and reason == "transient"


async def test_ex_resolved_reason_empty_and_cache_carries_reason() -> None:
    svc = _svc()
    refs, reason = await svc.accessible_workspaces_ex(_FakeDatarc(projects=[{"id": 1, "name": "w"}]), "u_1")
    assert len(refs) == 1 and reason == ""
    # Cache hits also carry the reason (reason and refs share an entry).
    refs2, reason2 = await svc.accessible_workspaces_ex(_FakeDatarc(projects=[{"id": 1, "name": "w"}]), "u_1")
    assert (len(refs2), reason2) == (1, "")


async def test_legacy_wrapper_unchanged_shape() -> None:
    """旧名 `accessible_workspaces` 仍返回 refs tuple（picker/存量测试零改动面）。"""
    refs = await _svc().accessible_workspaces(_FakeDatarc(account_uid=""), "u_1")
    assert refs == ()


# ─── Hop 2: provider injects ResolvedConnectorContext ───────


async def test_provider_injects_reason_on_empty_accessible() -> None:
    provider = DatarcContextProvider(service=_svc(), umem=_FakeUserMemory(), datarc_client=_FakeDatarc(account_uid=""))
    resolved = await provider.resolve("u_1", session_id=None)
    assert resolved.context_id is None
    assert resolved.accessible == ()
    assert resolved.absence_reason == "no_identity"


async def test_provider_no_reason_when_accessible_nonempty() -> None:
    provider = DatarcContextProvider(
        service=_svc(),
        umem=_FakeUserMemory(),
        datarc_client=_FakeDatarc(projects=[{"id": 1, "name": "w1"}, {"id": 2, "name": "w2"}]),
    )
    resolved = await provider.resolve("u_1", session_id=None)
    # >1 without a selection → ladder tier 4 returns None, but the nonempty set is not absent; reason is always "".
    assert resolved.context_id is None
    assert len(resolved.accessible) == 2
    assert resolved.absence_reason == ""


async def test_rejected_connection_is_actionable_and_not_cached() -> None:
    from bowhead_bi_bridge import BIPermissionError

    class RejectedDatarc(_FakeDatarc):
        async def fetch_account(self, email: str):
            raise BIPermissionError("access rejected", source="http")

    svc = _svc()
    refs, reason = await svc.accessible_workspaces_ex(RejectedDatarc(), "u_1")
    assert refs == () and reason == "connection_rejected"
    refs, reason = await svc.accessible_workspaces_ex(_FakeDatarc(projects=[{"id": 1, "name": "w"}]), "u_1")
    assert len(refs) == 1 and reason == ""


async def test_read_only_replay_rejects_unverifiable_connection_snapshot() -> None:
    import pytest

    from bowhead_bi_bridge import BIPermissionError

    class RejectedDatarc(_FakeDatarc):
        async def fetch_account(self, email: str):
            raise BIPermissionError("access rejected", source="http")

    provider = DatarcContextProvider(service=_svc(), umem=_FakeUserMemory(), datarc_client=RejectedDatarc())
    with pytest.raises(RuntimeError, match="snapshot is unavailable"):
        await provider.resolve("u_1", session_id=None, read_only=True)
