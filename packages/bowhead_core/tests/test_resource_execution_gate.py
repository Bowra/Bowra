from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.openapi_tool_source import openapi_entry, parse_openapi_ops
from bowhead_core.resource_execution_gate import check_skill_execution
from bowhead_core.skill_bridge import _wrap_skill_execution_gate, _wrap_skill_read_with_cache
from bowhead_core.team_tool_registry_view import build_team_specs


@dataclass
class _SkillPreference:
    enabled: bool
    source_type: str = "builtin"
    source_path: str = ""


class _SkillStore:
    def __init__(self, preference: _SkillPreference) -> None:
        self.preference = preference

    async def get(self, *, user_id: str, name: str) -> _SkillPreference:
        return self.preference


class _Catalog:
    def get(self, name: str) -> object | None:
        return object() if name == "docx" else None


class _PreferenceMap:
    def __init__(self, values: dict[str, _SkillPreference]) -> None:
        self.values = values

    async def get(self, *, user_id: str, name: str) -> _SkillPreference | None:
        return self.values.get(name)


class _TeamSkillStore:
    async def get_by_name(self, team_id: str, name: str, only_enabled: bool = False) -> object:
        return SimpleNamespace(
            id="oskill_team",
            name=name,
            status="enabled",
            approval="approved",
            visibility="team_wide",
            created_by="usr_owner",
        )


@pytest.mark.asyncio
async def test_skill_gate_sits_outside_read_cache() -> None:
    calls = 0

    async def read(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"ok": True, "body": "trusted instructions"}

    store = _SkillStore(_SkillPreference(enabled=True))
    ctx = SimpleNamespace(user_id="usr_1", team_id="team_1", skill_instance_store=store)
    handler = _wrap_skill_execution_gate(
        "skill_read",
        _wrap_skill_read_with_cache(read),
        _Catalog(),  # type: ignore[arg-type]
    )
    assert (await handler({"name": "docx"}, ctx))["ok"] is True
    store.preference = _SkillPreference(enabled=False)
    denied = await handler({"name": "docx"}, ctx)
    assert denied["error_kind"] == "resource_revoked"
    assert calls == 1


@pytest.mark.asyncio
async def test_skill_gate_passes_the_actual_operation_to_the_last_moment_check(monkeypatch) -> None:
    checked: list[tuple[str, str]] = []

    async def check(ctx: Any, *, catalog: Any, skill_name: str, permission: str) -> None:
        checked.append((skill_name, permission))

    async def handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return {"ok": True}

    monkeypatch.setattr("bowhead_core.resource_execution_gate.check_skill_execution", check)
    ctx = SimpleNamespace(user_id="usr_1", team_id="team_1")
    catalog = SimpleNamespace(get=lambda _name: object())

    await _wrap_skill_execution_gate("skill_read", handler, catalog)({"name": "method"}, ctx)
    await _wrap_skill_execution_gate("skill_invoke", handler, catalog)({"name": "method"}, ctx)

    assert checked == [("method", "read"), ("method", "invoke")]


@pytest.mark.asyncio
async def test_team_skill_gate_reads_stable_id_preference() -> None:
    ctx = SimpleNamespace(
        user_id="usr_1",
        team_id="team_1",
        skill_instance_store=_PreferenceMap({"team:oskill_team": _SkillPreference(enabled=False, source_type="team_shared")}),
        team_skill_store=_TeamSkillStore(),
    )
    catalog = SimpleNamespace(get=lambda _name: None)

    denied = await check_skill_execution(
        ctx,
        catalog=catalog,
        skill_name="shared-skill",
        permission="use",
    )

    assert denied is not None and denied["error_kind"] == "resource_revoked"


def _tool_source_db(path: Path, *, status: str, selected: list[str]) -> None:
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE team_tool_source("
            "id TEXT,team_id TEXT,kind TEXT,status TEXT,selected_op_ids_json TEXT,"
            "tools_schema_json TEXT,openapi_spec_json TEXT,auth_type TEXT,auth_ref TEXT)"
        )
        db.execute(
            "INSERT INTO team_tool_source VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "ots_1",
                "team_1",
                "openapi",
                status,
                __import__("json").dumps(selected),
                "[]",
                "{}",
                "none",
                None,
            ),
        )


def _entry():
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/x": {
                "get": {
                    "operationId": "read_x",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    return openapi_entry("ots_1", parse_openapi_ops(spec)[0])


class _Client:
    def __init__(self) -> None:
        self.calls = 0

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> object:
        self.calls += 1
        return SimpleNamespace(is_error=False, structured_content={"ok": True}, content=[])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "selected", "error_kind"),
    [
        ("disabled", ["read_x"], "resource_revoked"),
        ("enabled", ["another_operation"], "operation_unavailable"),
    ],
)
async def test_team_tool_gate_blocks_stale_turn_view_before_remote_call(
    tmp_path: Path,
    status: str,
    selected: list[str],
    error_kind: str,
) -> None:
    path = tmp_path / "db.sqlite"
    _tool_source_db(path, status=status, selected=selected)
    skill_store = SimpleNamespace(_db_path=str(path))
    ctx = SimpleNamespace(team_id="team_1", user_id="usr_1", skill_instance_store=skill_store)
    client = _Client()
    handler = build_team_specs("team_1", [_entry()], client)[0].handler

    result = await handler({}, ctx)

    assert result["error_kind"] == error_kind
    assert client.calls == 0


class _GrantStore:
    def __init__(self, denied_kinds: set[str] | None = None, *, fail: bool = False) -> None:
        self.denied_kinds = denied_kinds or set()
        self.fail = fail
        self.asked: list[tuple[str, str, str]] = []

    async def explain_decision(self, access_ctx: Any, kind: str, resource_id: str, permission: str) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("grant store unavailable")
        self.asked.append((kind, resource_id, permission))
        if kind in self.denied_kinds:
            return {"allowed": False, "reason": "not_in_allowlist"}
        return {"allowed": True, "reason": "allowed_by_grant"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("store", "error_kind", "remote_calls"),
    [
        (_GrantStore(), None, 1),
        (_GrantStore({"team_tool_op"}), "resource_revoked", 0),
        (_GrantStore({"team_tool_source"}), "resource_revoked", 0),
        (_GrantStore(fail=True), "resource_state_unavailable", 0),
    ],
    ids=["allowed", "operation-not-granted", "source-not-granted", "grant-store-unreadable"],
)
async def test_team_tool_gate_rechecks_grants_before_the_remote_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: _GrantStore, error_kind: str | None, remote_calls: int
) -> None:
    """强制拦截下，最后一道闸在调远端前按调用者重新判一次连接与操作的授权——轮次开始时组装的视图可能已过期。"""
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    path = tmp_path / "db.sqlite"
    _tool_source_db(path, status="enabled", selected=["read_x"])
    entry = _entry()
    ctx = SimpleNamespace(
        team_id="team_1",
        user_id="usr_1",
        skill_instance_store=SimpleNamespace(_db_path=str(path)),
        access_control_store=store,
        access_context={"user_id": "usr_1", "team_id": "team_1"},
    )
    client = _Client()
    handler = build_team_specs("team_1", [entry], client)[0].handler

    result = await handler({}, ctx)

    assert client.calls == remote_calls
    if error_kind is None:
        assert result.get("error_kind") is None
        assert store.asked == [("team_tool_source", "ots_1", "use"), ("team_tool_op", entry.advertised_name, "use")]
    else:
        assert result["error_kind"] == error_kind
