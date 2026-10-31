"""tool-context-framework v1（docs/tool-context-framework.md）单测.

两块：
1. DefaultContextLadder —— 阶梯各档 + back-compat 语义锁（与批2 原 datarc 实现逐条保真，
   防重构漂移；datarc_context._resolve_per_user_datarc_workspace 已是薄壳委托本实现）。
2. chat._make_datarc_context_switcher 闭包 —— remember 两态（§5 唯一行为变化）：
   缺省 session 级不变；remember=True 写 explicit 永久默认 + 清本会话覆盖层。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.datarc_selection import DatarcConnectionBinding, DatarcPreferenceScope, DatarcSelection
from bowhead_gateway.connector_context_provider import (
    DefaultContextLadder,
    ResolvedConnectorContext,
)

# Reuse real snapshot type (reuse review: do not copy as_dict into tests, which would verify the copy when contract drifts).
from bowhead_gateway.connector_context_service import DatarcWorkspaceRef as _Ref
from fakes_tool_context import FakeUserMemoryToolContext as _FakeUmem

REFS3 = (_Ref("1", "商超零售场景"), _Ref("234", "北极九章DEMO"), _Ref("235", "北极九章TEST"))


def _ladder(umem: _FakeUmem) -> DefaultContextLadder:
    return DefaultContextLadder(tool="datarc", umem=umem)


# ─── Tier 1: session overlay ────────────────────────────────


async def test_session_overlay_wins_when_session_matches():
    umem = _FakeUmem()
    umem.base = {"id": "1", "name": "商超零售场景", "source": "explicit"}
    umem.session = {"id": "234", "name": "北极九章DEMO", "source": "session", "session_id": "s_a"}
    got = await _ladder(umem).resolve("u", session_id="s_a", accessible=REFS3)
    assert got.context_id == "234"  # overlay overrides explicit.


async def test_session_overlay_ignored_cross_session():
    umem = _FakeUmem()
    umem.base = {"id": "1", "name": "商超零售场景", "source": "explicit"}
    umem.session = {"id": "234", "name": "北极九章DEMO", "source": "session", "session_id": "s_a"}
    got = await _ladder(umem).resolve("u", session_id="s_other", accessible=REFS3)
    assert got.context_id == "1"  # Cross-session overlay invalid → permanent default.


async def test_session_overlay_pointing_inaccessible_ignored_not_cleared():
    umem = _FakeUmem()
    umem.base = {"id": "1", "name": "商超零售场景", "source": "explicit"}
    umem.session = {"id": "999", "name": "已撤权", "source": "session", "session_id": "s_a"}
    got = await _ladder(umem).resolve("u", session_id="s_a", accessible=REFS3)
    assert got.context_id == "1"
    assert umem.clear_calls == []  # Keep overlay; naturally invalid across sessions.


async def test_session_overlay_trusted_when_refs_empty():
    umem = _FakeUmem()
    umem.session = {"id": "234", "name": "北极九章DEMO", "source": "session", "session_id": "s_a"}
    got = await _ladder(umem).resolve("u", session_id="s_a", accessible=())
    assert got.context_id == "234"  # Empty set (upstream failure): trust existing choice.


# ─── Tier 2: permanent default ───────────────────────────────


async def test_explicit_default_valid_returned_with_fresh_name():
    umem = _FakeUmem()
    umem.base = {"id": "234", "name": "旧名", "source": "explicit"}
    got = await _ladder(umem).resolve("u", session_id=None, accessible=REFS3)
    assert got == ResolvedConnectorContext("234", "北极九章DEMO", tuple(r.as_dict() for r in REFS3))


async def test_legacy_base_row_with_session_source_treated_as_none():
    """BUG1 前存量：base key 带 source=session（换XX 覆盖过）→ 跨会话视同无选择."""
    umem = _FakeUmem()
    umem.base = {"id": "234", "name": "北极九章DEMO", "source": "session", "session_id": "s_old"}
    got = await _ladder(umem).resolve("u", session_id="s_new", accessible=REFS3)
    assert got.context_id is None  # Show selection card, never revive old choice.


async def test_revoked_default_cleared_and_falls_through():
    umem = _FakeUmem()
    umem.base = {"id": "999", "name": "已撤权", "source": "explicit"}
    got = await _ladder(umem).resolve("u", session_id=None, accessible=REFS3)
    assert got.context_id is None
    assert umem.clear_calls == [{"tool": "datarc", "session_scoped": False}]


async def test_read_only_revoked_default_resolves_without_clearing():
    umem = _FakeUmem()
    umem.base = {"id": "999", "name": "已撤权", "source": "explicit"}
    got = await _ladder(umem).resolve(
        "u",
        session_id=None,
        accessible=REFS3,
        read_only=True,
    )
    assert got.context_id is None
    assert umem.clear_calls == []


async def test_stored_default_trusted_when_refs_empty():
    umem = _FakeUmem()
    umem.base = {"id": "234", "name": "北极九章DEMO", "source": "explicit"}
    got = await _ladder(umem).resolve("u", session_id=None, accessible=())
    assert got.context_id == "234"  # datarc failure must not wrongly clear it (authorization truth in member resolution).


async def test_auto_default_demoted_when_set_grows():
    umem = _FakeUmem()
    umem.base = {"id": "1", "name": "商超零售场景", "source": "auto"}
    got = await _ladder(umem).resolve("u", session_id=None, accessible=REFS3)
    assert got.context_id is None  # Expanded set → degrade and rerun (show users new Datarc workspaces).
    assert umem.clear_calls == []  # Do not clear row; overwrite after user confirms.


# ─── Tiers 3/4: automatic selection / selection card ─────────


async def test_single_ref_autoselected_and_written_back():
    umem = _FakeUmem()
    only = (_Ref("7", "唯一工作区"),)
    got = await _ladder(umem).resolve("u", session_id=None, accessible=only)
    assert got.context_id == "7"
    assert umem.set_calls == [{"tool": "datarc", "id": "7", "name": "唯一工作区", "source": "auto", "session_id": None}]


async def test_read_only_single_ref_resolves_without_writing_back():
    umem = _FakeUmem()
    only = (_Ref("7", "唯一工作区"),)
    got = await _ladder(umem).resolve(
        "u",
        session_id=None,
        accessible=only,
        read_only=True,
    )
    assert got.context_id == "7"
    assert umem.set_calls == []


async def test_read_only_ladder_propagates_snapshot_read_failure():
    class _FailingUmem(_FakeUmem):
        async def get_tool_context(self, *_args, **_kwargs):
            raise RuntimeError("context store unavailable")

    with pytest.raises(RuntimeError, match="context store unavailable"):
        await _ladder(_FailingUmem()).resolve(
            "u",
            session_id=None,
            accessible=REFS3,
            read_only=True,
        )


async def test_per_user_datarc_read_only_keeps_provider_resolution_non_mutating():
    from bowhead_gateway.datarc_context import (
        _resolve_per_user_datarc_workspace,
    )

    class _Service:
        async def accessible_workspaces_ex(self, _client: object, _user_id: str):
            return (_Ref("7", "唯一工作区"),), ""

    umem = _FakeUmem()
    got = await _resolve_per_user_datarc_workspace(
        SimpleNamespace(
            tool_context_service=_Service(),
            user_memory_store=umem,
        ),
        object(),
        "u",
        read_only=True,
    )

    assert got[0] == "7"
    assert umem.set_calls == []
    assert umem.clear_calls == []


async def test_per_user_datarc_read_only_propagates_transient_snapshot_failure():
    from bowhead_gateway.datarc_context import (
        _resolve_per_user_datarc_workspace,
    )

    class _Service:
        async def accessible_workspaces_ex(self, _client: object, _user_id: str):
            return (), "transient"

    with pytest.raises(RuntimeError, match="snapshot is unavailable"):
        await _resolve_per_user_datarc_workspace(
            SimpleNamespace(
                tool_context_service=_Service(),
                user_memory_store=_FakeUmem(),
            ),
            object(),
            "u",
            read_only=True,
        )


async def test_read_only_datarc_turn_context_skips_unconsumed_runtime_dependencies(
    monkeypatch: pytest.MonkeyPatch,
):
    from bowhead_gateway import _turn_runtime, datarc_context

    class _Service:
        async def accessible_workspaces_ex(self, _client: object, _user_id: str):
            return (_Ref("7", "唯一工作区"),), ""

    state = SimpleNamespace(
        tool_context_service=_Service(),
        user_memory_store=_FakeUmem(),
    )
    app = SimpleNamespace(state=state)
    datarc_client = object()
    embed_client = object()
    calls: list[str] = []

    async def _datarc(*_args, **_kwargs):
        return DatarcConnectionBinding("team_1", "dc_test", "revision_one", datarc_client, lambda projects: _embed())

    async def _embed(*_args, **_kwargs):
        calls.append("embed")
        return embed_client

    async def _no_connection(*_args, **_kwargs):
        calls.append("no_connection")
        return True

    monkeypatch.setattr(datarc_context, "resolve_team_datarc_binding", _datarc)
    monkeypatch.setattr(datarc_context, "resolve_team_embed_client", _embed)
    monkeypatch.setattr(_turn_runtime, "_team_has_no_connection", _no_connection)

    read_only = await datarc_context.resolve_datarc_turn_context(
        app,
        team_id="team_1",
        workspace_id="ws_1",
        user_id="u",
        read_only=True,
    )
    assert read_only.active_datarc_project == "7"
    assert read_only.vector_client is None
    assert calls == []

    fresh = await datarc_context.resolve_datarc_turn_context(
        app,
        team_id="team_1",
        workspace_id="ws_1",
        user_id="u",
    )
    assert fresh.vector_client is embed_client
    assert calls == ["embed"]

    async def _no_datarc(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        datarc_context,
        "resolve_team_datarc_binding",
        _no_datarc,
    )
    calls.clear()
    await datarc_context.resolve_datarc_turn_context(
        app,
        team_id="team_1",
        workspace_id="ws_1",
        user_id="u",
        read_only=True,
    )
    assert calls == []


async def test_read_only_datarc_turn_context_propagates_configured_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    from bowhead_gateway import datarc_context

    async def _unavailable(*_args, **_kwargs):
        raise datarc_context.DatarcResolutionError("configured connection failed")

    monkeypatch.setattr(
        datarc_context,
        "resolve_team_datarc_binding",
        _unavailable,
    )

    with pytest.raises(datarc_context.DatarcResolutionError):
        await datarc_context.resolve_datarc_turn_context(
            SimpleNamespace(state=SimpleNamespace()),
            team_id="team_1",
            workspace_id="ws_1",
            user_id="u",
            read_only=True,
        )


async def test_nothing_resolves_to_none_with_accessible_passthrough():
    umem = _FakeUmem()
    got = await _ladder(umem).resolve("u", session_id=None, accessible=REFS3)
    assert got.context_id is None
    assert {d["id"] for d in got.accessible} == {"1", "234", "235"}


@pytest.mark.parametrize(
    ("user_id", "pinned_project", "expected_project"),
    [("u_actor", None, "7"), (None, None, None), ("u_actor", "snapshot", "snapshot")],
    ids=["actor-session", "missing-actor", "artifact-snapshot"],
)
async def test_turn_context_preserves_actor_scope_and_artifact_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    user_id: str | None,
    pinned_project: str | None,
    expected_project: str | None,
) -> None:
    from unittest.mock import AsyncMock

    from bowhead_gateway import datarc_context

    client = object()
    service = SimpleNamespace(
        accessible_workspaces_ex=AsyncMock(return_value=((_Ref("7", "Session data"), _Ref("234", "Default data")), "")),
    )
    memory = _FakeUmem(
        base={"id": "234", "name": "Default data", "source": "explicit"},
        session={"id": "7", "name": "Session data", "source": "session", "session_id": "s_actor"},
    )
    historical_space_memory = SimpleNamespace(
        get_datarc_last_used_project=AsyncMock(return_value="other_user_data"),
        get_datarc_default_project=AsyncMock(return_value="other_user_data"),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            tool_context_service=service,
            user_memory_store=memory,
            project_memory_store=historical_space_memory,
        ),
    )
    resolve_client = AsyncMock(return_value=DatarcConnectionBinding("team_actor", "dc_test", "revision_one", client))
    monkeypatch.setattr(datarc_context, "resolve_team_datarc_binding", resolve_client)

    context = await datarc_context.resolve_datarc_turn_context(
        app,
        team_id="team_actor",
        workspace_id="ws_shared",
        user_id=user_id,
        session_id="s_actor",
        pinned_project=pinned_project,
    )

    resolve_client.assert_awaited_once_with(app, "team_actor")
    assert context.active_datarc_project == expected_project
    assert context.datarc_project_for_calls == (expected_project or "")
    if user_id and not pinned_project:
        service.accessible_workspaces_ex.assert_awaited_once_with(client, user_id)
    else:
        service.accessible_workspaces_ex.assert_not_awaited()
    historical_space_memory.get_datarc_last_used_project.assert_not_awaited()
    historical_space_memory.get_datarc_default_project.assert_not_awaited()
    assert memory.set_calls == memory.clear_calls == []


@pytest.mark.parametrize("selected_project", ["7", None], ids=["user-selection", "no-selection"])
async def test_guide_questions_follow_the_users_saved_data_context(
    monkeypatch: pytest.MonkeyPatch,
    selected_project: str | None,
) -> None:
    from unittest.mock import AsyncMock

    from bowhead_gateway.routes import datarc

    client = SimpleNamespace(list_guide_questions=AsyncMock(return_value=["What changed this month?"]))
    memory = SimpleNamespace(get_datarc_workspace=AsyncMock(return_value={"id": selected_project} if selected_project else None))
    store = SimpleNamespace(list_workspaces_for_user=AsyncMock(return_value=[{"datarc_workspace_id": "another_users_data"}]))
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                user_memory_store=memory, tool_context_service=SimpleNamespace(accessible_workspaces=AsyncMock(return_value=[_Ref("7", "Data")]))
            )
        )
    )
    user = SimpleNamespace(id="u_actor", team_id="team_actor")
    from bowhead_gateway import access_context, datarc_context

    monkeypatch.setattr(access_context, "build_access_context", AsyncMock(return_value=SimpleNamespace(team_id="team_actor")))
    monkeypatch.setattr(
        datarc_context,
        "resolve_team_datarc_binding",
        AsyncMock(return_value=DatarcConnectionBinding("team_actor", "dc_test", "revision_one", client)),
    )

    response = await datarc.guide_questions(request, workspace_id="ws_shared", user=user, store=store)

    memory.get_datarc_workspace.assert_awaited_once_with("u_actor", DatarcPreferenceScope("team_actor", "dc_test"))
    assert response.workspace_id == "ws_shared"
    assert response.items == (["What changed this month?"] if selected_project else [])
    if selected_project:
        client.list_guide_questions.assert_awaited_once_with(selected_project)
    else:
        client.list_guide_questions.assert_not_awaited()
    store.list_workspaces_for_user.assert_not_awaited()


# ─── switcher closure's two remember states (§5 sole behavior change) ──


# After team-connection consolidation (9e186d5d), switcher member resolution: user_store finds team_id →
# team-saved connection produces DatarcClient → authenticated user's **verified** email through member_resolver.
# Deployment datarc_identity and dev_default_user fallbacks removed; fakes must supply this entire chain,
# otherwise resolution returns 403/503 rather than falling back.
_DATARC_CLIENT = object()


class _FakeUserStore:
    async def get_user(self, user_id: str) -> Any:
        return SimpleNamespace(id=user_id, team_id="team_1", email="u@a.com", email_verified=True)


class _FakeConnStore:
    async def list_connections(self, team_id: str) -> list[Any]:
        return [SimpleNamespace(id="conn_1", updated_at="revision_one", base_url="https://datarc.example")]

    async def resolve_active_binding(self, team_id):
        return (SimpleNamespace(id="conn_1", updated_at="revision_one", base_url="https://datarc.example"), "auth_1", "revision_one")

    async def get_connection_auth(self, conn_id: str, team_id: str) -> str:
        return "auth_1"


class _FakeDatarcFactory:
    def get_client(self, base_url: str, auth_key: str) -> Any:
        return _DATARC_CLIENT


class _FakeMemberResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    async def resolve(self, identity: str, datarc_workspace_id: str, *, datarc: Any = None, refresh: bool = False) -> str:
        self.calls.append((identity, datarc_workspace_id, datarc))
        return "m_resolved"


async def _accessible(*args, **kwargs):
    return REFS3, ""


def _fake_request(umem: _FakeUmem | None, *, member_resolver: Any = None) -> Any:
    state = SimpleNamespace(
        user_store=_FakeUserStore(),
        datarc_connection_store=_FakeConnStore(),
        datarc_client_factory=_FakeDatarcFactory(),
        member_resolver=member_resolver or _FakeMemberResolver(),
        user_memory_store=umem,
        tool_context_service=SimpleNamespace(accessible_workspaces_ex=_accessible),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


@pytest.fixture(autouse=True)
def _verified_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    """These service tests control access; HTTP tests exercise actual authorization."""
    from unittest.mock import AsyncMock

    from bowhead_gateway import datarc_switch

    monkeypatch.setattr(datarc_switch, "build_access_context", AsyncMock(return_value=SimpleNamespace(team_id="team_1")))


async def _invoke_switch(switch, identifier, name, *, remember=False):
    selection = DatarcSelection(switch.binding, "u1", "1", "Source A", "old_member", _DATARC_CLIENT)
    prepared = await switch.prepare_selection(identifier, name, current=selection)
    return prepared.member_uid, await switch.save_preference(prepared, remember=remember)


async def test_switcher_default_writes_session_pref():
    from bowhead_gateway.routes.chat import _make_datarc_context_switcher

    umem = _FakeUmem()
    resolver = _FakeMemberResolver()
    switch = _make_datarc_context_switcher(
        _fake_request(umem, member_resolver=resolver),
        "u1",
        "s_a",
        workspace_id="ws_test",
        binding=DatarcConnectionBinding("team_1", "conn_1", "revision_one", _DATARC_CLIENT),
        ws_enforce=False,
        log=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
    )
    member, persisted = await _invoke_switch(switch, "234", "北极九章DEMO")
    assert member == "m_resolved"
    # Must resolve through the team-connection client, never a deployment singleton.
    assert resolver.calls == [("u@a.com", "234", _DATARC_CLIENT)]
    assert persisted.status == "saved"
    assert umem.set_calls == [{"tool": "datarc", "id": "234", "name": "北极九章DEMO", "source": "session", "session_id": "s_a"}]
    assert umem.clear_calls == []


async def test_switcher_remember_writes_explicit_and_clears_overlay():
    from bowhead_gateway.routes.chat import _make_datarc_context_switcher

    umem = _FakeUmem()
    umem.session = {"id": "1", "name": "商超零售场景", "source": "session", "session_id": "s_a"}
    switch = _make_datarc_context_switcher(
        _fake_request(umem),
        "u1",
        "s_a",
        workspace_id="ws_test",
        binding=DatarcConnectionBinding("team_1", "conn_1", "revision_one", _DATARC_CLIENT),
        ws_enforce=False,
        log=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
    )
    _member, persisted = await _invoke_switch(switch, "234", "北极九章DEMO", remember=True)
    assert persisted.status == "saved"
    assert umem.set_calls == [{"tool": "datarc", "id": "234", "name": "北极九章DEMO", "source": "explicit", "session_id": None}]
    # BUG1 decision 1: remembering clears current session overlay, otherwise it overrides the newly remembered default.
    assert umem.clear_calls == [{"tool": "datarc", "session_scoped": True}]
    assert umem.session is None


async def test_switcher_no_store_reports_not_persisted():
    """review P1-c：user_memory_store 缺席 → persisted=False（上游据此不宣称已设默认）."""
    from bowhead_gateway.routes.chat import _make_datarc_context_switcher

    switch = _make_datarc_context_switcher(
        _fake_request(None),
        "u1",
        "s_a",
        workspace_id="ws_test",
        binding=DatarcConnectionBinding("team_1", "conn_1", "revision_one", _DATARC_CLIENT),
        ws_enforce=False,
        log=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
    )
    _member, persisted = await _invoke_switch(switch, "234", "北极九章DEMO", remember=True)
    assert persisted.status in {"skipped", "failed"}


async def test_switcher_store_error_reports_not_persisted_not_raises():
    """review P1-b：提交失败绝不 raise（否则「切换失败」谎报 + 半提交）——降级 persisted=False."""
    from bowhead_gateway.routes.chat import _make_datarc_context_switcher

    class _BoomUmem(_FakeUmem):
        async def commit_datarc_workspace(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("sqlite locked")

    switch = _make_datarc_context_switcher(
        _fake_request(_BoomUmem()),
        "u1",
        "s_a",
        workspace_id="ws_test",
        binding=DatarcConnectionBinding("team_1", "conn_1", "revision_one", _DATARC_CLIENT),
        ws_enforce=False,
        log=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
    )
    _member, persisted = await _invoke_switch(switch, "234", "北极九章DEMO", remember=True)
    assert persisted.status in {"skipped", "failed"}
