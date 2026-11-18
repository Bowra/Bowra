"""Request writes on fenced tables repeat when a running Turn holds the authority lease.

On 116 (1.7.49) such writes failed with 500 at once ("SQLite authority lease is busy"). Each
admitted store method now repeats its whole transaction for up to five seconds and then raises
``SQLiteStoreBusy`` (503 ``store_busy``) with nothing written.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from bowhead_core.agent_revision_store import AgentRevisionStore
from bowhead_core.personal_appearance_store import PersonalAppearanceStore
from bowhead_core.reference_retention_policy import set_retention_policy
from bowhead_core.session_lifecycle import SessionLifecycleCoordinator
from bowhead_core.sqlite_diagnostics import DiagnosticCursor
from bowhead_core.team_settings_store import TeamSettingsStore
from bowhead_core.team_subagent_store import TeamSubagentStore
from bowhead_core.terminal_protocol_store import TerminalProtocolStore
from bowhead_core.user_settings_store import UserSettingsStore
from bowhead_core.user_store import UserStore
from bowhead_core.workspace_settings_store import WorkspaceSettingsStore
from bowhead_gateway.team_llm_apply_service import apply_team_llm_v2

from bowhead_core import _db, sqlite_domain_runtime


def _lose_lease(monkeypatch: pytest.MonkeyPatch, table: str, times: int) -> list[int]:
    """Fail the next ``times`` writes to ``table`` with the runtime's marked lease conflict."""
    write = re.compile(rf"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b.*\b{table}\b", re.IGNORECASE | re.DOTALL)
    attempts: list[int] = []
    original = DiagnosticCursor.execute

    def execute(self: DiagnosticCursor, sql: str, parameters: Any = (), /) -> Any:
        if write.search(sql):
            attempts.append(len(attempts) + 1)
            if len(attempts) <= times:
                raise sqlite_domain_runtime._busy()
        return original(self, sql, parameters)

    monkeypatch.setattr(DiagnosticCursor, "execute", execute)
    return attempts


@pytest.mark.asyncio
async def test_personal_setting_save_repeats_and_writes_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = UserSettingsStore(tmp_path / "core.db")
    await store.init()
    attempts = _lose_lease(monkeypatch, "user_settings", 1)

    await store.set("u_1", "ui_language", "zh-CN")

    assert attempts == [1, 2]
    assert await store.get("u_1", "ui_language") == "zh-CN"


@pytest.mark.asyncio
async def test_team_setting_compare_and_set_rechecks_on_the_repeat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = TeamSettingsStore(tmp_path / "core.db")
    await store.init()
    await store.set("team_1", "language_policy", "old", "u_1")
    attempts = _lose_lease(monkeypatch, "team_settings", 1)

    assert await store.compare_and_set("team_1", "language_policy", expected_value="old", value="new", updated_by="u_1")
    assert attempts == [1, 2]
    assert await store.get("team_1", "language_policy") == "new"


@pytest.mark.asyncio
async def test_space_setting_batch_repeats_as_a_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = WorkspaceSettingsStore(tmp_path / "core.db")
    await store.init()
    attempts = _lose_lease(monkeypatch, "workspace_settings", 1)

    before = await store.compare_and_apply("ws_1", {"week_start_day": (None, "monday"), "default_data_language": (None, "zh-CN")})

    # The first attempt failed on its first insert and rolled back; the repeat wrote both.
    assert before == {"week_start_day": None, "default_data_language": None}
    assert len(attempts) == 3
    assert await store.list_for_workspace("ws_1") == {"week_start_day": "monday", "default_data_language": "zh-CN"}


@pytest.mark.asyncio
async def test_space_member_and_model_config_writes_repeat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    users = UserStore(tmp_path / "core.db")
    await users.initialize()
    team = await users.create_team(name="Lease Team", max_seats=5)
    owner = await users.create_user(team_id=team.id, email="owner@example.com", password="Passw0rd!")
    member = await users.create_user(team_id=team.id, email="member@example.com", password="Passw0rd!")
    workspace_id = await users.create_local_workspace(team_id=team.id, name="Retail", creator_user_id=owner.id)

    members = _lose_lease(monkeypatch, "workspace_members", 1)
    await users.add_member(workspace_id=workspace_id, user_id=member.id, role="member")
    assert members == [1, 2]
    assert await users.get_member_role(workspace_id, member.id) == "member"

    teams = _lose_lease(monkeypatch, "teams", 1)
    assert await users.update_team_llm_config_if_version(team.id, {"providers": []}, expected_version=0)
    # The failed attempt rolled back, so the version moved exactly once.
    assert teams == [1, 2]
    assert not await users.update_team_llm_config_if_version(team.id, {"providers": []}, expected_version=0)


@pytest.mark.asyncio
async def test_a_spent_wait_raises_store_busy_and_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = UserSettingsStore(tmp_path / "core.db")
    await store.init()
    monkeypatch.setattr(_db, "AUTHORITY_BUSY_WAIT_SECONDS", 0.05)
    attempts = _lose_lease(monkeypatch, "user_settings", 10_000)

    with pytest.raises(_db.SQLiteStoreBusy):
        await store.set("u_1", "ui_language", "zh-CN")

    assert len(attempts) > 1
    assert await store.get("u_1", "ui_language") is None


def _standard_wrapper_code() -> object:
    async def probe() -> None:
        return None

    return _db.repeat_when_authority_busy()(probe).__code__


@pytest.mark.parametrize(
    "method",
    [
        UserSettingsStore.set,
        UserSettingsStore.update_value,
        UserSettingsStore.delete,
        PersonalAppearanceStore.compare_and_write,
        TeamSettingsStore.set,
        TeamSettingsStore.compare_and_set,
        TeamSettingsStore.delete,
        set_retention_policy,
        WorkspaceSettingsStore.set,
        WorkspaceSettingsStore.delete,
        WorkspaceSettingsStore.compare_and_apply,
        UserStore.add_member,
        UserStore.remove_member,
        UserStore.leave_workspace,
        UserStore.update_member_role,
        UserStore.update_team_llm_config,
        UserStore.update_team_llm_config_if_version,
        UserStore.update_team_tool_source_auth,
        UserStore._write_governed_workspace_metadata,
        apply_team_llm_v2,
        AgentRevisionStore.save_working_copy,
        TeamSubagentStore.compare_and_update_team_subagent,
        SessionLifecycleCoordinator._legacy_transition_if_still_legacy,
        TerminalProtocolStore._ensure_core_auth_scope_epochs,
    ],
    ids=lambda method: method.__qualname__,
)
def test_admitted_request_writes_use_the_standard_repeat(method: Any) -> None:
    # Each method was checked against the admission rules in docs/design/sqlite-authority-contention.md.
    assert method.__code__ is _standard_wrapper_code()
