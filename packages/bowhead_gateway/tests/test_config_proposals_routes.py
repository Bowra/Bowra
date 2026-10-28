"""批 1-a proposal 端点测试（tech-design-batch1-draft-store.md §9 测 6-11）.

真 sqlite（baseline+alembic 0007）+ 真 user/workspace settings store——apply 的
单事务合并、undo 字段级语义、审计版式都打真存储；user_store/权限面用 stub。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.config_copilot import reset_registry_for_tests
from bowhead_core.config_draft_store import ConfigDraftStore
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.user_settings_store import UserSettingsStore
from bowhead_core.workspace_settings_store import WorkspaceSettingsStore
from bowhead_gateway.config_copilot_forms import register_gateway_forms
from bowhead_gateway.routes import config_drafts, config_proposals
from bowhead_gateway.routes.auth import _current_user
from bowhead_gateway.routes.auth import _store as _user_store_dep
from fastapi import FastAPI
from fastapi.testclient import TestClient

_APPEARANCE_KEY = "appearance_theme_config"


class _FakeUserStore:
    """审计记录 + _check_workspace_access 权限面（角色可变，模拟 propose 后降权）。"""

    def __init__(self) -> None:
        self.audits: list[dict[str, Any]] = []
        self.admin_audits: list[dict[str, Any]] = []
        self.team_role = "member"
        self.member_role: str | None = "ws_admin"

    async def log_audit(self, **kwargs: Any) -> None:
        self.audits.append(kwargs)

    async def write_admin_audit_log(self, **kwargs: Any) -> None:
        self.admin_audits.append(kwargs)

    async def get_workspace_team_id(self, workspace_id: str) -> str:
        return "team_1"

    async def get_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        # Since 3f7a4c47a, write access first rejects archived spaces (workspace_settings._check_workspace_access).
        return {"id": workspace_id, "team_id": "team_1", "status": "active"}

    async def get_team_role(self, user_id: str, team_id: str) -> str | None:
        return self.team_role

    async def get_member_role(self, workspace_id: str, user_id: str) -> str | None:
        return self.member_role

    async def list_workspaces_for_user(self, user_id: str, team_id: str | None = None):
        return [{"workspace_id": "ws_1", "name": "商超零售"}]


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = tmp_path / "bowhead.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{path}", target="core")
    return str(path)


@pytest.fixture
def user_store() -> _FakeUserStore:
    return _FakeUserStore()


@pytest.fixture
def user_box() -> dict[str, Any]:
    return {"id": "u_1"}


@pytest.fixture
def app(db_path: str, user_store: _FakeUserStore, user_box: dict[str, Any]) -> FastAPI:
    reset_registry_for_tests()
    app = FastAPI()
    app.state.user_settings_store = UserSettingsStore(db_path)
    app.state.workspace_settings_store = WorkspaceSettingsStore(db_path)
    asyncio.run(app.state.user_settings_store.init())
    asyncio.run(app.state.workspace_settings_store.init())
    app.state.team_settings_store = SimpleNamespace(get=_none_getter)
    app.state.user_store = user_store
    app.state.scheduled_store = SimpleNamespace()
    app.state.access_control_store = None
    register_gateway_forms(app)
    app.state.config_draft_store = ConfigDraftStore(db_path)
    app.include_router(config_proposals.router)
    app.include_router(config_drafts.router)  # Batch 1-b: form_draft consumption uses the same router.
    app.dependency_overrides[_current_user] = lambda: SimpleNamespace(id=user_box["id"], email="u@t.local", team_id="team_1")
    app.dependency_overrides[_user_store_dep] = lambda: user_store
    yield app
    app.dependency_overrides.clear()
    reset_registry_for_tests()


async def _none_getter(*args: Any, **kwargs: Any) -> None:
    return None


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _seed(
    app: FastAPI,
    *,
    draft_id: str = "ccd_p1",
    form_id: str = "user_appearance",
    workspace_id: str | None = None,
    rows: list[dict[str, Any]],
    owner: str = "u_1",
    settings_target: dict[str, Any] | None = None,
) -> str:
    asyncio.run(
        app.state.config_draft_store.create_lite_proposal(
            draft_id=draft_id,
            team_id="team_1",
            owner_user_id=owner,
            form_id=form_id,
            workspace_id=workspace_id,
            turn_id="t_1",
            session_id="s_1",
            rows=rows,
            notes={},
            workspace_name="商超零售" if workspace_id else None,
            settings_target=settings_target,
        )
    )
    return draft_id


def _user_kv(app: FastAPI, key: str) -> str | None:
    return asyncio.run(app.state.user_settings_store.get("u_1", key))


def test_settings_review_is_zero_write_and_rechecks_original_admin_access(client, app, user_store, monkeypatch):
    rows = [{"field": "week_start_day", "from": "monday", "to": "sunday", "from_unset": True}]
    binding = {"form_id": "workspace_i18n", "user_id": "u_1", "team_id": "team_1", "workspace_id": "ws_1", "edit_generation": 3}
    pid = _seed(app, form_id="workspace_i18n", workspace_id="ws_1", rows=rows, settings_target=binding)
    before = asyncio.run(app.state.workspace_settings_store.list_for_workspace("ws_1"))
    review = client.get(f"/api/v1/config-proposals/{pid}/settings-review")
    assert review.status_code == 200
    assert review.json()["rows"] == rows
    assert review.json()["owner_user_id"] == "u_1"
    assert review.json()["team_id"] == "team_1"
    assert review.json()["settings_target"] == binding
    assert asyncio.run(app.state.workspace_settings_store.list_for_workspace("ws_1")) == before
    user_store.member_role = "member"
    assert client.get(f"/api/v1/config-proposals/{pid}/settings-review").status_code == 403


def test_save_receipt_identifies_the_actual_original_proposal_and_target(client, app):
    rows = [{"field": "week_start_day", "from": "monday", "to": "sunday", "from_unset": True}]
    pid = _seed(app, form_id="workspace_i18n", workspace_id="ws_1", rows=rows)
    receipt = client.post(f"/api/v1/config-proposals/{pid}/apply").json()
    assert receipt == {
        "proposal_id": pid,
        "form_id": "workspace_i18n",
        "status": "applied",
        "workspace_id": "ws_1",
        "team_id": "team_1",
        "owner_user_id": "u_1",
        "replayed": False,
    }


def _ws_kv(app: FastAPI, key: str) -> str | None:
    return asyncio.run(app.state.workspace_settings_store.get("ws_1", key))


_APPEARANCE_ROWS = [
    {"field": "mode", "from": "system", "to": "dark", "from_unset": True},
    {"field": "colorProfile", "from": "plum", "to": "ocean", "from_unset": False},
]


# ── 6/7: user_appearance apply/undo ──────────────────────────


def test_apply_happy_then_replay_no_double_audit(client: TestClient, app: FastAPI, user_store: _FakeUserStore) -> None:
    pid = _seed(app, rows=_APPEARANCE_ROWS)
    resp = client.post(f"/api/v1/config-proposals/{pid}/apply")
    assert resp.status_code == 200
    assert resp.json() == {
        "proposal_id": pid,
        "form_id": "user_appearance",
        "status": "applied",
        "workspace_id": None,
        "team_id": "team_1",
        "owner_user_id": "u_1",
        "replayed": False,
    }

    stored = json.loads(_user_kv(app, _APPEARANCE_KEY))
    assert stored["mode"] == "dark"
    assert stored["colorProfile"] == {"type": "preset", "id": "ocean"}  # Profile wrapper (§6.3).

    assert len(user_store.audits) == 1
    audit = user_store.audits[0]
    assert audit["event_type"] == "user_setting_changed"
    assert audit["payload"]["key"] == _APPEARANCE_KEY
    assert audit["payload"]["origin"] == "chat"
    assert audit["payload"]["proposal_id"] == pid  # Server-validated reference (P3-1 debt paid).
    assert audit["payload"]["op"] == "apply"
    assert audit["payload"]["before"] is None

    # Replay: idempotent success, no rewrite or additional audit.
    resp = client.post(f"/api/v1/config-proposals/{pid}/apply")
    assert resp.status_code == 200
    assert resp.json()["replayed"] is True
    assert len(user_store.audits) == 1

    assert client.get(f"/api/v1/config-proposals/{pid}").json()["status"] == "applied"


def test_undo_field_level_semantics(client: TestClient, app: FastAPI) -> None:
    """P1-2 服务端版：只回写 rows 字段；第三字段保值；from_unset 删键；profile 双向包裹（P2-7-3）。"""
    pid = _seed(app, rows=_APPEARANCE_ROWS)
    assert client.post(f"/api/v1/config-proposals/{pid}/apply").status_code == 200

    # Simulate the user changing a third field (density) in settings after apply.
    current = json.loads(_user_kv(app, _APPEARANCE_KEY))
    current["density"] = "spacious"
    asyncio.run(app.state.user_settings_store.set("u_1", _APPEARANCE_KEY, json.dumps(current)))

    resp = client.post(f"/api/v1/config-proposals/{pid}/undo")
    assert resp.status_code == 200
    assert resp.json()["status"] == "reverted"

    reverted = json.loads(_user_kv(app, _APPEARANCE_KEY))
    assert "mode" not in reverted  # from_unset: delete the key to follow the system again.
    assert reverted["colorProfile"] == {"type": "preset", "id": "plum"}  # undo also wraps preset.
    assert reverted["density"] == "spacious"  # Preserve the third field.

    # Cannot apply again after undo.
    resp = client.post(f"/api/v1/config-proposals/{pid}/apply")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"]["code"] == "proposal_state_conflict"
    assert resp.json()["detail"]["error"]["details"]["status"] == "reverted"


# ── 8: workspace_i18n permissions/audit/partial failure ──────


_WS_ROWS = [
    {"field": "week_start_day", "from": "monday", "to": "sunday", "from_unset": True},
    {"field": "default_data_language", "from": "zh-CN", "to": "en-US", "from_unset": True},
]


def test_ws_apply_permission_rechecked_at_click(client: TestClient, app: FastAPI, user_store: _FakeUserStore) -> None:
    pid = _seed(app, form_id="workspace_i18n", workspace_id="ws_1", rows=_WS_ROWS)
    user_store.member_role = "member"  # Demoted after propose.
    resp = client.post(f"/api/v1/config-proposals/{pid}/apply")
    assert resp.status_code == 403
    assert client.get(f"/api/v1/config-proposals/{pid}").json()["status"] == "proposed"
    assert user_store.admin_audits == []
    assert _ws_kv(app, "week_start_day") is None


def test_ws_apply_and_undo_with_audit(client: TestClient, app: FastAPI, user_store: _FakeUserStore) -> None:
    pid = _seed(app, form_id="workspace_i18n", workspace_id="ws_1", rows=_WS_ROWS)
    resp = client.post(f"/api/v1/config-proposals/{pid}/apply")
    assert resp.status_code == 200
    assert _ws_kv(app, "week_start_day") == "sunday"
    assert _ws_kv(app, "default_data_language") == "en-US"
    assert len(user_store.admin_audits) == 2
    put_audit = user_store.admin_audits[0]
    assert put_audit["action"] == "workspace_settings_changed"
    assert put_audit["payload"]["operation"] == "put"
    assert put_audit["payload"]["workspace_id"] == "ws_1"
    assert put_audit["payload"]["proposal_id"] == pid

    resp = client.post(f"/api/v1/config-proposals/{pid}/undo")
    assert resp.status_code == 200
    assert _ws_kv(app, "week_start_day") is None  # from_unset: delete to restore the default.
    delete_audits = [a for a in user_store.admin_audits[2:] if a["payload"]["operation"] == "delete"]
    assert len(delete_audits) == 2
    assert delete_audits[0]["payload"]["op"] == "undo"
    assert delete_audits[0]["payload"]["after"] is None


def test_configuration_agent_confirmation_reads_back_through_the_settings_api(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bowhead_core.agents._lead_context import reset_current_peer_name, set_current_peer_name
    from bowhead_core.config_copilot_chat_tools import set_proposal_writer_provider
    from bowhead_core.configuration_agent import AGENT_NAME, FORM_SKILLS, prepare_configuration_agent, register_configuration_agent_tools
    from bowhead_core.events import TurnEventBus
    from bowhead_core.skill_bridge import build_default_skill_stack, register_skill_tools
    from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
    from bowhead_gateway.routes import workspace_settings

    app.include_router(workspace_settings.router)
    set_proposal_writer_provider(lambda: app.state.config_draft_store)

    async def propose():
        lead = ToolExecutionContext(
            workspace_id="ws_1",
            team_id="team_1",
            user_id="u_1",
            member_uid="m_1",
            turn_id="t_1",
            session_id="s_1",
            datarc=None,
            role="lead",
            access_context={"workspace_role": "ws_admin", "team_role": "member"},
            config_lite_supported=True,
            parent_bus=TurnEventBus(),
        )
        child, _ = await prepare_configuration_agent(lead, form_id="workspace_i18n")
        assert child is not None
        child.role = "analyst"
        registry = ToolRegistry()

        async def access(ctx, form_id, write):
            return {"workspace_role": app.state.user_store.member_role}

        register_configuration_agent_tools(registry, access_resolver=access)
        # 63178fba0: the specialist must read the matching native domain Skill in this
        # execution before it may propose, the way the live agent is instructed to.
        catalog, runtime = build_default_skill_stack()
        register_skill_tools(registry, catalog, runtime)
        token = set_current_peer_name(AGENT_NAME)
        try:
            method = await registry.get("skill_read").handler({"name": FORM_SKILLS["workspace_i18n"]}, child)
            assert method.get("body") and not method.get("error"), method
            read = await registry.get("configuration_read").handler({"form_id": "workspace_i18n"}, child)
            assert read["ok"]
            receipt = await registry.get("configuration_propose").handler({"form_id": "workspace_i18n", "patch": {"week_start_day": "sunday"}}, child)
            assert receipt["ok"]
            return receipt
        finally:
            reset_current_peer_name(token)

    try:
        receipt = asyncio.run(propose())
    finally:
        set_proposal_writer_provider(None)
    assert receipt["ok"]
    assert _ws_kv(app, "week_start_day") is None
    proposal_id = receipt["proposal_id"]
    assert client.post(f"/api/v1/config-proposals/{proposal_id}/apply").status_code == 200
    assert client.get("/api/v1/workspace-settings/ws_1").json()["settings"]["week_start_day"] == "sunday"
    assert client.post(f"/api/v1/config-proposals/{proposal_id}/undo").status_code == 200
    assert "week_start_day" not in client.get("/api/v1/workspace-settings/ws_1").json()["settings"]


@pytest.mark.parametrize("operation", ["apply", "undo"])
def test_ws_confirmation_preserves_newer_manual_input(client: TestClient, app: FastAPI, operation: str) -> None:
    proposal_id = _seed(app, form_id="workspace_i18n", workspace_id="ws_1", rows=_WS_ROWS)
    if operation == "undo":
        assert client.post(f"/api/v1/config-proposals/{proposal_id}/apply").status_code == 200
    from bowhead_gateway.routes import workspace_settings

    app.include_router(workspace_settings.router)
    assert client.put("/api/v1/workspace-settings/ws_1/default_data_language", json={"value": "zh-CN"}).status_code == 200
    before = asyncio.run(app.state.workspace_settings_store.list_for_workspace("ws_1"))
    response = client.post(f"/api/v1/config-proposals/{proposal_id}/{operation}")
    assert response.status_code == 409
    assert response.json()["detail"]["error"]["code"] == "configuration_baseline_changed"
    assert asyncio.run(app.state.workspace_settings_store.list_for_workspace("ws_1")) == before
    assert client.get(f"/api/v1/config-proposals/{proposal_id}").json()["status"] == ("applied" if operation == "undo" else "proposed")


def test_ws_apply_failure_then_retry_preserves_state_and_audit(
    client: TestClient, app: FastAPI, user_store: _FakeUserStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed atomic field batch leaves the proposal retryable and unchanged."""
    pid = _seed(app, form_id="workspace_i18n", workspace_id="ws_1", rows=_WS_ROWS)
    store = app.state.workspace_settings_store
    original_apply = store.compare_and_apply

    async def unavailable_apply(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "compare_and_apply", unavailable_apply)
    resp = client.post(f"/api/v1/config-proposals/{pid}/apply")
    assert resp.status_code == 503
    assert client.get(f"/api/v1/config-proposals/{pid}").json()["status"] == "proposed"
    assert user_store.admin_audits == []
    assert _ws_kv(app, "week_start_day") is None
    assert _ws_kv(app, "default_data_language") is None

    monkeypatch.setattr(store, "compare_and_apply", original_apply)
    resp = client.post(f"/api/v1/config-proposals/{pid}/apply")
    assert resp.status_code == 200
    audited_keys = {a["payload"]["key"] for a in user_store.admin_audits}
    assert audited_keys == {"week_start_day", "default_data_language"}


# ── 9/10: State conflict matrix and CAS loser ───────────────


def test_state_conflict_matrix(client: TestClient, app: FastAPI, db_path: str) -> None:
    pid = _seed(app, rows=_APPEARANCE_ROWS)
    resp = client.post(f"/api/v1/config-proposals/{pid}/undo")  # undo against proposed.
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"]["code"] == "proposal_state_conflict"

    assert client.post(f"/api/v1/config-proposals/{pid}/ignore").status_code == 200
    assert client.post(f"/api/v1/config-proposals/{pid}/ignore").json()["replayed"] is True
    resp = client.post(f"/api/v1/config-proposals/{pid}/apply")  # apply against ignored.
    assert resp.status_code == 409

    # Defend against kind≠lite_proposal (batch 1-b form_draft misrouted here).
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO config_copilot_draft(
              id, team_id, owner_user_id, kind, form_id, status, row_version, created_at, updated_at
            ) VALUES ('ccd_fd','team_1','u_1','form_draft','scheduled_job','draft',1,'t','t')
            """
        )
        connection.commit()
    resp = client.post("/api/v1/config-proposals/ccd_fd/apply")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"]["code"] == "proposal_kind_mismatch"

    # Unregistered form: orphaned row after deployment rollback.
    ghost = _seed(app, draft_id="ccd_ghost", form_id="ghost_form", rows=_APPEARANCE_ROWS)
    resp = client.post(f"/api/v1/config-proposals/{ghost}/apply")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"]["code"] == "form_unavailable"


def test_apply_loses_cas_race_returns_replay_without_extra_state(client: TestClient, app: FastAPI, user_store: _FakeUserStore) -> None:
    """review P2-7-2：applier 完成与 CAS 之间被并发者抢先 applied → 按 replay 返回。"""
    pid = _seed(app, rows=_APPEARANCE_ROWS)
    store: ConfigDraftStore = app.state.config_draft_store
    original_transition = store.transition
    raced = {"done": False}

    async def racing_transition(*args: Any, **kwargs: Any):
        if not raced["done"]:
            raced["done"] = True
            await original_transition(pid, team_id="team_1", owner_user_id="u_1", to_status="applied", from_statuses=("proposed",))
        return await original_transition(*args, **kwargs)

    store.transition = racing_transition  # type: ignore[method-assign]
    try:
        resp = client.post(f"/api/v1/config-proposals/{pid}/apply")
    finally:
        del store.transition
    assert resp.status_code == 200
    assert resp.json() == {
        "proposal_id": pid,
        "form_id": "user_appearance",
        "status": "applied",
        "workspace_id": None,
        "team_id": "team_1",
        "owner_user_id": "u_1",
        "replayed": True,
    }
    assert len(user_store.audits) == 1  # Audit this attempt's writes normally, without duplicates.


# ── 11: Isolation and availability ─────────────────────────


def test_owner_isolation_and_store_unavailable(client: TestClient, app: FastAPI, user_box: dict[str, Any]) -> None:
    pid = _seed(app, rows=_APPEARANCE_ROWS)
    user_box["id"] = "u_2"
    assert client.get(f"/api/v1/config-proposals/{pid}").status_code == 404
    assert client.post(f"/api/v1/config-proposals/{pid}/apply").status_code == 404
    user_box["id"] = "u_1"

    del app.state.config_draft_store
    resp = client.get(f"/api/v1/config-proposals/{pid}")
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"]["code"] == "config_draft_store_unavailable"


def test_content_missing_refused(client: TestClient, app: FastAPI, db_path: str) -> None:
    pid = _seed(app, rows=_APPEARANCE_ROWS)
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE config_copilot_draft SET payload_json=NULL WHERE id=?", (pid,))
        connection.commit()
    resp = client.post(f"/api/v1/config-proposals/{pid}/apply")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"]["code"] == "proposal_content_missing"


def test_proposal_route_mounts_unconditionally(monkeypatch: pytest.MonkeyPatch) -> None:
    """轻确认条 proposal 端点无条件挂载（门控已拆，2026-08-18）。

    与 `/config-drafts` 同门——原先它俩共用 `FEATURE_CONFIG_COPILOT`，拆闸后两者
    都必须常在。这条与 test_config_drafts_routes 的同名闸成对：漏改一个就会出现
    「一个端点在、另一个不在」的半拆状态。
    """
    from bowhead_gateway.main import create_app

    monkeypatch.setenv("FEATURE_CONFIG_COPILOT", "0")  # The old OFF value should no longer affect behavior either.
    assert any(path.startswith("/api/v1/config-proposals") for path in create_app().openapi()["paths"])
    monkeypatch.delenv("FEATURE_CONFIG_COPILOT", raising=False)
    assert any(path.startswith("/api/v1/config-proposals") for path in create_app().openapi()["paths"])


# ── Batch 1-b: form_draft consumption (tech-design-batch1b §5 tests 8-9) ──


def _seed_form(app: FastAPI, *, draft_id: str = "ccd_f1", owner: str = "u_1", session_id: str = "s_1") -> str:
    asyncio.run(
        app.state.config_draft_store.create_form_draft(
            draft_id=draft_id,
            team_id="team_1",
            owner_user_id=owner,
            form_id="scheduled_job",
            workspace_id="ws_1",
            turn_id="t_1",
            session_id=session_id,
            fields={"name": "周报", "cron": "0 9 * * 1"},
            field_meta={"name": {"origin": "generated", "needs_confirm": False, "note": None}},
            notes={},
        )
    )
    return draft_id


def test_form_draft_get_consume_discard(client: TestClient, app: FastAPI) -> None:
    did = _seed_form(app)
    body = client.get(f"/api/v1/config-drafts/{did}").json()
    assert body["status"] == "draft"
    assert body["fields"] == {"name": "周报", "cron": "0 9 * * 1"}
    assert body["workspace_id"] == "ws_1" and body["result"] is None and body["row_version"] == 1

    resp = client.post(
        f"/api/v1/config-drafts/{did}/consume", json={"result": {"job_id": "sj_1", "name": "周报", "next_run_at": "2026-08-03T09:00:00+08:00"}}
    )
    assert resp.status_code == 200
    dto = resp.json()
    assert dto["status"] == "consumed" and dto["replayed"] is False
    assert dto["result"]["job_id"] == "sj_1" and dto["result"]["next_run_at"].startswith("2026-08-03")
    assert dto["fields"]["name"] == "周报"  # Receipt does not overwrite content.

    # Idempotent replay; discard conflicts after a terminal state.
    assert client.post(f"/api/v1/config-drafts/{did}/consume", json={"result": {"job_id": "sj_1"}}).json()["replayed"] is True
    resp = client.post(f"/api/v1/config-drafts/{did}/discard")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"]["code"] == "draft_state_conflict"
    assert resp.json()["detail"]["error"]["details"]["status"] == "consumed"

    other = _seed_form(app, draft_id="ccd_f2")
    assert client.post(f"/api/v1/config-drafts/{other}/discard").json()["status"] == "discarded"
    assert client.post(f"/api/v1/config-drafts/{other}/discard").json()["replayed"] is True
    assert client.post(f"/api/v1/config-drafts/{other}/consume", json={"result": {"job_id": "x"}}).status_code == 409


def test_form_draft_dto_projects_target_only_when_present(client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """批 3（3-0c，§4.5）：`_form_draft_dto` 是白名单投影——不加这两键，刷新/重连后抽屉
    就不知道在改谁（r1-P1-5）。

    成对断言：①单例面（不带这两键）的 DTO 键集**逐字节不变**——负例，防「顺手给所有草稿
    加两个 null 键」；②集合型面带上就必须出网——正对照，证明①不是「DTO 根本不认这两键」。
    """
    plain = client.get(f"/api/v1/config-drafts/{_seed_form(app)}").json()
    assert set(plain) == {"draft_id", "form_id", "status", "workspace_id", "fields", "field_meta", "notes", "result", "row_version"}

    # Collection surface `team_subagent` registers only with FEATURE_CONFIG_EDIT=1; otherwise its drafts are hidden
    # with 404 (9bee5570d). This test pins DTO projection, so explicitly enable the flag and register the form again.
    monkeypatch.setenv("FEATURE_CONFIG_EDIT", "1")
    register_gateway_forms(app)
    asyncio.run(
        app.state.config_draft_store.create_form_draft(
            draft_id="ccd_tgt",
            team_id="team_1",
            owner_user_id="u_1",
            form_id="team_subagent",
            workspace_id=None,
            turn_id="t_1",
            session_id="s_2",
            fields={"description": "新描述"},
            field_meta={},
            notes={},
            target={"id": "osa_a", "label": "分析助手"},
            changed_fields_cumulative=["description"],
        )
    )
    body = client.get("/api/v1/config-drafts/ccd_tgt").json()
    assert body["target"] == {"id": "osa_a", "label": "分析助手"}
    assert body["changed_fields_cumulative"] == ["description"]


def test_form_draft_isolation_and_kind_guard(client: TestClient, app: FastAPI, user_box: dict[str, Any]) -> None:
    did = _seed_form(app)
    lite = _seed(app, draft_id="ccd_lite", rows=_APPEARANCE_ROWS)
    # lite row hits form_draft endpoint → 409 (and vice versa, already tested in batch 1-a).
    resp = client.get(f"/api/v1/config-drafts/{lite}")
    assert resp.status_code == 409 and resp.json()["detail"]["error"]["code"] == "draft_kind_mismatch"

    user_box["id"] = "u_2"
    assert client.get(f"/api/v1/config-drafts/{did}").status_code == 404
    assert client.post(f"/api/v1/config-drafts/{did}/consume", json={"result": {"job_id": "x"}}).status_code == 404
    user_box["id"] = "u_1"

    del app.state.config_draft_store
    assert client.get(f"/api/v1/config-drafts/{did}").status_code == 503


def test_form_draft_maintenance_expires_only(app: FastAPI, db_path: str) -> None:
    """§5 测 9：form_draft 只走 expire 段（scrub/purge 零触碰——TTL 双 NULL）。"""
    did = _seed_form(app)
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE config_copilot_draft SET expires_at='2020-01-01T00:00:00+00:00' WHERE id=?", (did,))
        connection.commit()
    result = asyncio.run(app.state.config_draft_store.run_maintenance())
    assert (result.expired, result.scrubbed, result.purged) == (1, 0, 0)
    dto = asyncio.run(app.state.config_draft_store.get_draft(did, team_id="team_1", owner_user_id="u_1"))
    assert dto.status == "expired"
    assert dto.payload is not None  # Retain content so the chip can still identify the draft.


def test_lifespan_wires_store_and_maintenance_source_lock() -> None:
    """§9-14 的 source 锁面：lifespan 建店 + maintenance task + shutdown cancel 三件都在。"""
    source = (Path(__file__).resolve().parents[1] / "src" / "bowhead_gateway" / "main.py").read_text(encoding="utf-8")
    assert "app.state.config_draft_store = config_draft_store" in source
    assert 'name="config-draft-maintenance"' in source
    assert source.count("config_draft_maintenance_task.cancel()") == 1
    assert "proposal_writer_provider=lambda: getattr(app.state" in source


def _boot_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, db_name: str) -> Path:
    monkeypatch.chdir(tmp_path)
    db = tmp_path / db_name
    monkeypatch.setenv("FEATURE_CONFIG_COPILOT", "1")
    monkeypatch.setenv("BOWHEAD_AUTH_MODE", "dev")
    monkeypatch.setenv("BOWHEAD_DEV_USER_FALLBACK", "0")  # deployment_mode=local forbids fallback=1.
    monkeypatch.setenv("BOWHEAD_DB_URL", f"sqlite+aiosqlite:///{db}")
    monkeypatch.setenv("BOWHEAD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BOWHEAD_PRIVACY_LEDGER_DB", str(tmp_path / "privacy-ledger.db"))
    # lifespan derives the artifact-fetch key from BOWHEAD_JWT_SECRET and rejects startup without it; no host env/.env dependency.
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "test-config-proposals-jwt-secret-32bytes")
    from cryptography.fernet import Fernet

    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    return db


def _prepare_community_boot(app: FastAPI, db: Path) -> None:
    """Use real first-owner setup for an explicitly fresh synthetic Community DB."""
    if app.state.edition != "community":
        return
    from bowhead_core.installation import community_setup_token, initialize_installation, setup_community_owner

    async def prepare() -> None:
        identity = await initialize_installation(str(db), "community", admitted_fresh=True)
        await setup_community_owner(
            str(db),
            setup_token=community_setup_token(identity),
            team_name="Synthetic Configuration Team",
            email="setup-owner@example.test",
            password="SyntheticSetupOwner7!",
        )

    asyncio.run(prepare())


def test_lifespan_boot_flag_on_initializes_store_and_maintenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§9-14 真 boot（R2 review P2-1）：flag on + 已迁移库 → lifespan 全通、店与维护任务就位。"""
    from bowhead_gateway.main import create_app

    db = _boot_env(monkeypatch, tmp_path, "bowhead.db")
    with sqlite3.connect(db) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{db}")
    app = create_app()
    _prepare_community_boot(app, db)
    with TestClient(app):
        assert isinstance(app.state.config_draft_store, ConfigDraftStore)
        task = app.state.config_draft_maintenance_task
        assert task is not None and not task.done()


def test_lifespan_boot_schema_absent_tolerant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """flag on + config_copilot_draft 表缺席 → 容错跳过店不 brick boot（§10-3）。

    裸 1.0.0 库当前 gateway 本就起不来（0003+ 的列被其他子系统需要），用「迁到 head 后
    DROP 本批表」模拟 0007 未跑的形态。"""
    from bowhead_gateway.main import create_app

    db = _boot_env(monkeypatch, tmp_path, "bowhead.db")
    with sqlite3.connect(db) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()
    upgrade(f"sqlite+aiosqlite:///{db}")
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TABLE config_copilot_draft")
        connection.commit()
    app = create_app()
    _prepare_community_boot(app, db)
    with TestClient(app):
        assert getattr(app.state, "config_draft_store", None) is None
        assert getattr(app.state, "config_draft_maintenance_task", None) is None


# ── Batch 1-d completion: write-path differential gate (safety prerequisite for reinterpreting PRD R2) ──
#
# Batch 1-a reinterpreted R2's rule, 'write=existing API, no new write path', as 'reuse existing write executors and
# validation/permission/audit primitives; server-side orchestration is allowed'. Its **prerequisite**: copilot apply
# and settings PUT must persist identical results. Otherwise, upgrading settings validation without updating copilot
# causes a **silent fork**: no error or crash, but copilot writes bypass the new validation.
#
# This gate turns 'someone must remember' into 'CI catches it'.


def test_two_write_paths_produce_identical_state(client: TestClient, app: FastAPI) -> None:
    """同一个改动：设置页 PUT 与副驾 apply 落库结果必须逐字节一致。"""
    from bowhead_gateway.routes import user_settings

    app.include_router(user_settings.router)
    baseline = json.dumps({"version": 1, "mode": "system", "colorProfile": {"type": "preset", "id": "plum"}}, ensure_ascii=False)

    # ① Copilot path: proposal apply.
    asyncio.run(app.state.user_settings_store.set("u_1", _APPEARANCE_KEY, baseline))
    draft_id = _seed(app, rows=_APPEARANCE_ROWS)
    assert client.post(f"/api/v1/config-proposals/{draft_id}/apply").status_code == 200
    via_copilot = _user_kv(app, _APPEARANCE_KEY)

    # ② Settings path: PUT the equivalent complete configuration.
    asyncio.run(app.state.user_settings_store.set("u_1", _APPEARANCE_KEY, baseline))
    equivalent = json.dumps(
        {"version": 1, "mode": "dark", "colorProfile": {"type": "preset", "id": "ocean"}},
        ensure_ascii=False,
    )
    response = client.put(f"/api/v1/user-settings/{_APPEARANCE_KEY}", json={"value": equivalent})
    assert response.status_code == 200, response.text
    via_settings_page = _user_kv(app, _APPEARANCE_KEY)

    assert via_copilot is not None
    assert json.loads(via_copilot) == json.loads(via_settings_page or "{}"), "两条写路径落库结果分叉——副驾绕过了设置页路由上的某条校验/归一化"


def test_copilot_apply_rejects_what_settings_page_rejects(client: TestClient, app: FastAPI) -> None:
    """设置页拒绝的值，副驾这条路也必须拒——否则副驾成了绕过校验的第二通道。"""
    from bowhead_gateway.routes import user_settings

    app.include_router(user_settings.router)
    # Settings returns 400 for invalid colors (normalize_appearance_theme_config raises ThemeConfigError).
    bad = json.dumps({"version": 1, "colorProfile": {"type": "preset", "id": "不存在的配色"}}, ensure_ascii=False)
    assert client.put(f"/api/v1/user-settings/{_APPEARANCE_KEY}", json={"value": bad}).status_code == 400

    # Copilot uses the same normalizer: likewise rejects, with transaction rollback preventing partial writes.
    before = _user_kv(app, _APPEARANCE_KEY)
    draft_id = _seed(
        app,
        draft_id="ccd_bad",
        rows=[{"field": "colorProfile", "from": None, "to": "不存在的配色", "from_unset": True}],
    )
    response = client.post(f"/api/v1/config-proposals/{draft_id}/apply")
    assert response.status_code == 409
    assert _user_kv(app, _APPEARANCE_KEY) == before  # No partial write.
