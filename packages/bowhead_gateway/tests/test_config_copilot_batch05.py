"""批 0.5 gateway 侧契约测试（tech-design-batch05-chat.md §10 测 16-23）.

user_appearance / workspace_i18n 注册面（materializer 真校验 + read 形状 + tier-1 fields 契约）、
scheduled read 权限对齐、web 构造点通道标记 source 锁。
批 1-a 改造：settings 路由 origin 审计测试改为「回退钉死」（任何请求零审计行，
tech-design-batch1 §7/§9-12）；新增三个 lite_applier 的表单层语义测试（§6.3）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.config_copilot import FormContext, get_form, reset_registry_for_tests
from bowhead_gateway.access_context import AccessContextError
from bowhead_gateway.config_copilot_forms import register_gateway_forms
from fastapi import FastAPI
from fastapi.testclient import TestClient

_SRC = Path(__file__).resolve().parents[1] / "src" / "bowhead_gateway"


class _KVStore:
    """user_settings / workspace_settings / team_settings 三面通用 stub。"""

    def __init__(self, data: dict[str, dict[str, str]] | None = None) -> None:
        self.data: dict[str, dict[str, str]] = data or {}
        self.sets: list[tuple[str, str, str]] = []
        self.deletes: list[tuple[str, str]] = []

    async def get(self, owner: str, key: str) -> str | None:
        return self.data.get(owner, {}).get(key)

    async def set(self, owner: str, key: str, value: str) -> None:
        self.data.setdefault(owner, {})[key] = value
        self.sets.append((owner, key, value))

    async def delete(self, owner: str, key: str) -> None:
        self.data.get(owner, {}).pop(key, None)
        self.deletes.append((owner, key))

    async def update_value(self, owner: str, key: str, mutate):
        # Batch 1-a UserSettingsStore.update_value semantic stub (read→mutate→write/delete, return before/after).
        before = self.data.get(owner, {}).get(key)
        after = mutate(before)
        if after is None:
            self.data.get(owner, {}).pop(key, None)
            self.deletes.append((owner, key))
        else:
            self.data.setdefault(owner, {})[key] = after
            self.sets.append((owner, key, after))
        return before, after

    async def list_for_workspace(self, workspace_id: str) -> dict[str, str]:
        return dict(self.data.get(workspace_id, {}))

    async def compare_and_apply(self, owner, changes):
        before = {key: self.data.get(owner, {}).get(key) for key in changes}
        if any(before[key] not in (expected, desired) for key, (expected, desired) in changes.items()):
            return None
        for key, (_, desired) in changes.items():
            if before[key] == desired:
                continue
            if desired is None:
                await self.delete(owner, key)
            else:
                await self.set(owner, key, desired)
        return before

    async def list_for_user(self, user_id: str) -> dict[str, str]:
        return dict(self.data.get(user_id, {}))


class _UserStore:
    def __init__(self) -> None:
        self.audits: list[dict[str, Any]] = []
        self.admin_audits: list[dict[str, Any]] = []
        self.audit_raises = False
        self.email_by_id: dict[str, str] = {}

    async def get_user(self, user_id: str):
        # Actual UserStore.get_user returns User with .email or None; match shape (user_store.py:3756).
        email = self.email_by_id.get(user_id)
        return SimpleNamespace(id=user_id, email=email) if email else None

    async def list_workspaces_for_user(self, user_id: str, team_id: str | None = None):
        return [{"workspace_id": "ws_1", "name": "商超零售"}]

    async def log_audit(self, **kwargs: Any) -> None:
        if self.audit_raises:
            raise RuntimeError("audit down")
        self.audits.append(kwargs)

    async def write_admin_audit_log(self, **kwargs: Any) -> None:
        if self.audit_raises:
            raise RuntimeError("audit down")
        self.admin_audits.append(kwargs)

    # workspace_settings._check_workspace_access dependencies.
    async def get_workspace_team_id(self, workspace_id: str) -> str:
        return "team_1"

    async def get_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        # Since 3f7a4c47a, write access rejects archived spaces first (workspace_settings._check_workspace_access).
        return {"id": workspace_id, "team_id": "team_1", "status": "active"}

    async def get_member_role(self, workspace_id: str, user_id: str) -> str | None:
        # Space settings writes check space role (ws_admin), no longer team role.
        return "ws_admin"

    async def get_team_role(self, user_id: str, team_id: str) -> str | None:
        return "owner"

    async def is_team_admin(self, user_id: str, team_id: str | None = None) -> bool:
        return True


@pytest.fixture
def stores() -> dict[str, Any]:
    return {
        "scheduled_store": SimpleNamespace(),
        "user_store": _UserStore(),
        "user_settings_store": _KVStore(),
        "workspace_settings_store": _KVStore(),
        "team_settings_store": _KVStore(),
        "access_control_store": None,
    }


@pytest.fixture
def forms_app(stores: dict[str, Any]):
    reset_registry_for_tests()
    app = FastAPI()
    for name, store in stores.items():
        setattr(app.state, name, store)
    register_gateway_forms(app)  # Registration implicitly asserts policy↔schema (§10 test22).
    yield app
    reset_registry_for_tests()


def _fctx(workspace_id: str | None = "ws_1") -> FormContext:
    return FormContext(user_id="u_1", user_email="u@t.local", team_id="team_1", locale="zh-CN", current_values={}, workspace_id=workspace_id)


# ── 16/17: user_appearance ───────────────────────────────────


@pytest.mark.asyncio
async def test_appearance_materializer_roundtrip_all_fields(forms_app) -> None:
    reg = get_form("user_appearance")
    patch = {
        "mode": "dark",
        "colorProfile": "ocean",
        "typographyProfile": "readable",
        "typeScale": "large",
        "density": "compact",
        "radius": "sharp",
        "motion": "reduced",
        "chartPalette": "colorblind-safe",
    }
    result = await reg.materializer(_fctx(), dict(patch))
    assert result.normalized == patch
    assert result.issues == []


@pytest.mark.asyncio
async def test_appearance_materializer_team_lock_drops_field(forms_app, stores) -> None:
    policy = {"version": 1, "defaults": {"colorProfile": {"type": "preset", "id": "plum"}}, "locks": {"colorProfile": True}}
    stores["team_settings_store"].data["team_1"] = {"theme_policy": json.dumps(policy)}
    reg = get_form("user_appearance")
    result = await reg.materializer(_fctx(), {"colorProfile": "ocean", "density": "compact"})
    assert result.normalized == {"density": "compact"}
    assert [issue.code for issue in result.issues] == ["team_locked"]
    assert result.issues[0].field == "colorProfile"
    assert result.issues[0].repairable is False


@pytest.mark.asyncio
async def test_appearance_read_three_layers(forms_app, stores) -> None:
    """team default + user override + lock 三层（§10 测 17）；tier-1 fields 契约。"""
    policy = {"version": 1, "defaults": {"colorProfile": {"type": "preset", "id": "plum"}}, "locks": {"colorProfile": True}}
    stores["team_settings_store"].data["team_1"] = {"theme_policy": json.dumps(policy)}
    stores["user_settings_store"].data["u_1"] = {"appearance_theme_config": json.dumps({"version": 1, "mode": "dark"})}
    reg = get_form("user_appearance")
    data = await reg.read_accessor(_fctx())
    assert data["effective"]["mode"] == "dark"
    assert data["effective"]["colorProfile"] == "plum"  # Lock outranks user.
    assert data["source"]["mode"] == "user"
    assert data["source"]["colorProfile"] == "team_lock"
    assert data["locks"] == {"colorProfile": True}
    assert data["user_overrides"] == {"mode": "dark"}
    assert data["fields"]["mode"] == {"value": "dark", "unset": False}
    assert data["fields"]["density"] == {"value": "comfortable", "unset": True}


# ── 18: workspace_i18n ───────────────────────────────────────


@pytest.mark.asyncio
async def test_ws_i18n_read_defaults_and_name(forms_app, stores) -> None:
    stores["workspace_settings_store"].data["ws_1"] = {"week_start_day": "sunday"}
    reg = get_form("workspace_i18n")
    data = await reg.read_accessor(_fctx())
    assert data["workspace_id"] == "ws_1"
    assert data["workspace_name"] == "商超零售"
    assert data["fields"]["week_start_day"] == {"value": "sunday", "unset": False}
    assert data["fields"]["default_data_language"] == {"value": "zh-CN", "unset": True}
    assert data["default_data_language"]["is_default"] is True


@pytest.mark.asyncio
async def test_ws_i18n_materializer_notes_impact(forms_app) -> None:
    reg = get_form("workspace_i18n")
    result = await reg.materializer(_fctx(), {"default_data_language": "en-US", "week_start_day": None})
    assert result.normalized == {"default_data_language": "en-US"}
    assert "翻译方向" in result.notes["default_data_language"]
    assert result.issues == []


@pytest.mark.asyncio
async def test_ws_i18n_read_requires_workspace(forms_app) -> None:
    reg = get_form("workspace_i18n")
    with pytest.raises(ValueError, match="workspace_required"):
        await reg.read_accessor(_fctx(workspace_id=None))


def test_ws_i18n_registration_scopes(forms_app) -> None:
    reg = get_form("workspace_i18n")
    assert reg.read_scope == "ws_member"
    assert reg.write_scope == "ws_admin"
    assert reg.risk_tier == 1
    assert get_form("user_appearance").risk_tier == 1
    assert get_form("scheduled_job").risk_tier == 2


# ── user_language (batch 0.5 final incidental item) ───────────


@pytest.mark.asyncio
async def test_user_language_read_default_and_set(forms_app, stores) -> None:
    reg = get_form("user_language")
    data = await reg.read_accessor(_fctx())
    assert data["fields"]["output_language"] == {"value": "auto", "unset": True}
    stores["user_settings_store"].data["u_1"] = {"output_language": "en-US"}
    data = await reg.read_accessor(_fctx())
    assert data["fields"]["output_language"] == {"value": "en-US", "unset": False}
    assert reg.risk_tier == 1
    assert reg.read_scope == "user"
    assert reg.write_scope == "user"


# ── 19: scheduled-read authorization parity ─────────────────


@pytest.mark.asyncio
async def test_scheduled_read_workspace_parity(forms_app, stores, monkeypatch: pytest.MonkeyPatch) -> None:
    """镜像 routes/scheduled.py:383-392：403 空间的 job 被过滤，403 只影响该空间。"""
    stores["scheduled_store"] = SimpleNamespace()
    jobs = [
        SimpleNamespace(name="可见", cron="0 9 * * *", tz="Asia/Shanghai", status="paused", workspace_id="ws_ok"),
        SimpleNamespace(name="失权", cron="0 9 * * *", tz="Asia/Shanghai", status="paused", workspace_id="ws_denied"),
    ]

    async def list_for_user(user_id: str):
        return jobs

    forms_app.state.scheduled_store = SimpleNamespace(list_for_user=list_for_user)

    async def gate(**kwargs):
        if kwargs["workspace_id"] == "ws_denied":
            raise AccessContextError("workspace_denied", 403)
        return

    monkeypatch.setattr("bowhead_gateway.config_copilot_forms.build_access_context", gate)
    reg = get_form("scheduled_job")
    summary = await reg.read_accessor(_fctx())
    assert [item["name"] for item in summary["items"]] == ["可见"]


@pytest.mark.asyncio
async def test_scheduled_read_authz_unavailable_raises(forms_app, monkeypatch: pytest.MonkeyPatch) -> None:
    """503 族不得静默吞成「没有任务」（review P2-7）→ raise → config_read read_failed 降级。"""

    async def list_for_user(user_id: str):
        return [SimpleNamespace(name="j", cron="0 9 * * *", tz="Asia/Shanghai", status="paused", workspace_id="ws_1")]

    forms_app.state.scheduled_store = SimpleNamespace(list_for_user=list_for_user)

    async def gate(**kwargs):
        raise AccessContextError("authz_unavailable", 503)

    monkeypatch.setattr("bowhead_gateway.config_copilot_forms.build_access_context", gate)
    reg = get_form("scheduled_job")
    with pytest.raises(AccessContextError):
        await reg.read_accessor(_fctx())


# ── Batch 1-a §7/§9-12: settings-route rollback pinned (origin tuple removed) ──


@pytest.fixture
def settings_client(stores: dict[str, Any]):
    from bowhead_gateway.routes import user_settings as us_routes
    from bowhead_gateway.routes import workspace_settings as ws_routes
    from bowhead_gateway.routes.auth import _current_user
    from bowhead_gateway.routes.auth import _store as _user_store_dep

    app = FastAPI()
    app.state.user_settings_store = stores["user_settings_store"]
    app.state.workspace_settings_store = stores["workspace_settings_store"]
    app.include_router(us_routes.router)
    app.include_router(ws_routes.router)
    app.dependency_overrides[_current_user] = lambda: SimpleNamespace(id="u_1", email="u@t.local", team_id="team_1")
    app.dependency_overrides[_user_store_dep] = lambda: stores["user_store"]
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_settings_routes_never_audit_even_with_legacy_origin_fields(settings_client, stores) -> None:
    """回退钉死：批 0.5 的三件套字段被忽略（不报错）且任何请求零审计行——
    provenance 自报通道已死，chat 写与审计只存在于 /config-proposals 端点。"""
    stores["user_settings_store"].data["u_1"] = {"output_language": "zh-CN"}
    resp = settings_client.put(
        "/api/v1/user-settings/output_language",
        json={"value": "en-US", "origin": "chat", "proposal_id": "p1", "op": "apply"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"value": "en-US"}  # Response shape unchanged.
    resp = settings_client.delete(
        "/api/v1/user-settings/output_language",
        params={"origin": "chat", "proposal_id": "p9", "op": "undo"},
    )
    assert resp.status_code == 200
    resp = settings_client.put(
        "/api/v1/workspace-settings/ws_1/week_start_day",
        json={"value": "sunday", "origin": "chat", "proposal_id": "p2", "op": "apply"},
    )
    assert resp.status_code == 200
    resp = settings_client.delete(
        "/api/v1/workspace-settings/ws_1/week_start_day",
        params={"origin": "chat", "proposal_id": "p2", "op": "undo"},
    )
    assert resp.status_code == 200
    assert stores["user_store"].audits == []
    assert stores["user_store"].admin_audits == []


def test_settings_routes_plain_requests_unchanged(settings_client, stores) -> None:
    """页面路径行为与批 0 现状字节不变。"""
    resp = settings_client.put("/api/v1/user-settings/output_language", json={"value": "en-US"})
    assert resp.status_code == 200
    assert stores["user_settings_store"].data["u_1"]["output_language"] == "en-US"
    resp = settings_client.put("/api/v1/workspace-settings/ws_1/week_start_day", json={"value": "sunday"})
    assert resp.status_code == 200
    assert stores["user_store"].audits == []
    assert stores["user_store"].admin_audits == []


# ── Batch 1-b §2.7: __me__ resolution degradation chain (R2 review P2-5) ──


async def _no_jobs(user_id: str):
    return []


@pytest.mark.asyncio
async def test_scheduled_me_sentinel_falls_back_to_user_store(forms_app, stores) -> None:
    """chat 工具路径 ctx.user_email 为空 → 按 user_id 查库拿真实邮箱（P1-3）。"""
    stores["user_store"].email_by_id = {"u_1": "real@datarc.cn"}
    forms_app.state.scheduled_store = SimpleNamespace(list_for_user=_no_jobs)
    reg = get_form("scheduled_job")
    ctx = FormContext(user_id="u_1", user_email="", team_id="team_1", locale="zh-CN", current_values={}, workspace_id="ws_1")
    result = await reg.materializer(
        ctx,
        {"name": "周报", "cron": "0 9 * * 1", "query": "q", "tz": "Asia/Shanghai", "notify_channels": {"email": ["__me__"]}},
    )
    assert result.normalized["notify_channels"] == {"email": ["real@datarc.cn"]}
    assert result.issues == []


@pytest.mark.asyncio
async def test_scheduled_me_sentinel_unresolvable_degrades_not_crash(forms_app, stores) -> None:
    """查无此人 → 结构化 email_invalid issue（草稿照出，失败不挡手填），不抛异常。"""
    stores["user_store"].email_by_id = {}
    forms_app.state.scheduled_store = SimpleNamespace(list_for_user=_no_jobs)
    reg = get_form("scheduled_job")
    ctx = FormContext(user_id="u_ghost", user_email="", team_id="team_1", locale="zh-CN", current_values={}, workspace_id="ws_1")
    result = await reg.materializer(
        ctx,
        {"name": "周报", "cron": "0 9 * * 1", "query": "q", "tz": "Asia/Shanghai", "notify_channels": {"email": ["__me__"]}},
    )
    assert result.normalized["notify_channels"] == {}
    assert [i.code for i in result.issues] == ["email_invalid"]


@pytest.mark.asyncio
async def test_scheduled_me_sentinel_http_path_uses_ctx_email(forms_app, stores) -> None:
    """HTTP 生成端点路径（ctx.user_email 非空）零行为差——不查库。"""
    stores["user_store"].email_by_id = {"u_1": "should-not-be-used@x.com"}
    forms_app.state.scheduled_store = SimpleNamespace(list_for_user=_no_jobs)
    reg = get_form("scheduled_job")
    ctx = FormContext(user_id="u_1", user_email="http@datarc.cn", team_id="team_1", locale="zh-CN", current_values={}, workspace_id="ws_1")
    result = await reg.materializer(
        ctx,
        {"name": "周报", "cron": "0 9 * * 1", "query": "q", "tz": "Asia/Shanghai", "notify_channels": {"email": ["__me__"]}},
    )
    assert result.normalized["notify_channels"] == {"email": ["http@datarc.cn"]}


# ── Batch 1-a §6.3: form semantics of three lite_appliers ─────


def _rows(*items: tuple[str, Any, Any, bool]) -> list[dict[str, Any]]:
    return [{"field": f, "from": frm, "to": to, "from_unset": unset} for f, frm, to, unset in items]


@pytest.mark.asyncio
async def test_appearance_applier_apply_merges_and_wraps(forms_app, stores) -> None:
    stores["user_settings_store"].data["u_1"] = {"appearance_theme_config": json.dumps({"version": 1, "density": "compact"})}
    reg = get_form("user_appearance")
    changes = await reg.lite_applier(_fctx(), _rows(("mode", "system", "dark", True), ("colorProfile", "plum", "ocean", False)), "apply")
    stored = json.loads(stores["user_settings_store"].data["u_1"]["appearance_theme_config"])
    assert stored["mode"] == "dark"
    assert stored["colorProfile"] == {"type": "preset", "id": "ocean"}
    assert stored["density"] == "compact"  # Preserve fields outside rows.
    assert len(changes) == 1
    assert changes[0].scope == "user" and changes[0].operation == "put"
    assert json.loads(changes[0].before)["density"] == "compact"


@pytest.mark.asyncio
async def test_appearance_applier_undo_field_level_with_profile_wrap(forms_app, stores) -> None:
    """undo：from_unset 删键、from 值恢复且 profile 包裹（review P2-7-3）、第三字段保值。"""
    cfg = {"version": 1, "mode": "dark", "colorProfile": {"type": "preset", "id": "ocean"}, "density": "spacious"}
    stores["user_settings_store"].data["u_1"] = {"appearance_theme_config": json.dumps(cfg)}
    reg = get_form("user_appearance")
    await reg.lite_applier(_fctx(), _rows(("mode", "system", "dark", True), ("colorProfile", "plum", "ocean", False)), "undo")
    stored = json.loads(stores["user_settings_store"].data["u_1"]["appearance_theme_config"])
    assert "mode" not in stored
    assert stored["colorProfile"] == {"type": "preset", "id": "plum"}
    assert stored["density"] == "spacious"


@pytest.mark.asyncio
async def test_appearance_applier_rejects_bad_rows_without_write(forms_app, stores) -> None:
    from bowhead_core.config_copilot import LiteApplyError

    stores["user_settings_store"].data["u_1"] = {"appearance_theme_config": json.dumps({"version": 1, "mode": "dark"})}
    reg = get_form("user_appearance")
    with pytest.raises(LiteApplyError):
        await reg.lite_applier(_fctx(), _rows(("bogus_field", None, "x", True)), "apply")
    assert json.loads(stores["user_settings_store"].data["u_1"]["appearance_theme_config"]) == {"version": 1, "mode": "dark"}


@pytest.mark.asyncio
async def test_ws_applier_validate_all_then_write(forms_app, stores) -> None:
    """review P2-2b：第二 key 值非法 → 整体拒绝零写入；合法则逐 key 写并出回执。"""
    from bowhead_core.config_copilot import LiteApplyError

    reg = get_form("workspace_i18n")
    with pytest.raises(LiteApplyError):
        await reg.lite_applier(_fctx(), _rows(("week_start_day", None, "sunday", True), ("default_data_language", None, "xx", True)), "apply")
    assert stores["workspace_settings_store"].sets == []

    changes = await reg.lite_applier(_fctx(), _rows(("week_start_day", "monday", "sunday", True)), "apply")
    assert stores["workspace_settings_store"].data["ws_1"]["week_start_day"] == "sunday"
    assert changes[0].scope == "workspace" and changes[0].before is None and changes[0].after == "sunday"

    changes = await reg.lite_applier(_fctx(), _rows(("week_start_day", "monday", "sunday", True)), "undo")
    assert "week_start_day" not in stores["workspace_settings_store"].data.get("ws_1", {})
    assert changes[0].operation == "delete" and changes[0].after is None

    with pytest.raises(LiteApplyError) as excinfo:
        await reg.lite_applier(_fctx(workspace_id=None), _rows(("week_start_day", None, "sunday", True)), "apply")
    assert excinfo.value.code == "workspace_required"


@pytest.mark.asyncio
async def test_user_language_applier_apply_and_undo(forms_app, stores, monkeypatch) -> None:
    from bowhead_core.config_copilot import LiteApplyError

    monkeypatch.delenv("FEATURE_GERMAN_AI_OUTPUT", raising=False)
    reg = get_form("user_language")
    changes = await reg.lite_applier(_fctx(), _rows(("output_language", "auto", "en-US", True)), "apply")
    assert stores["user_settings_store"].data["u_1"]["output_language"] == "en-US"
    assert changes[0].operation == "put"
    changes = await reg.lite_applier(_fctx(), _rows(("output_language", "auto", "en-US", True)), "undo")
    assert "output_language" not in stores["user_settings_store"].data.get("u_1", {})
    assert changes[0].operation == "delete"
    with pytest.raises(LiteApplyError):
        await reg.lite_applier(_fctx(), _rows(("output_language", None, "ja-JP", True)), "apply")

    with pytest.raises(LiteApplyError, match="not enabled"):
        await reg.lite_applier(_fctx(), _rows(("output_language", None, "de-DE", True)), "apply")

    monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "1")
    changes = await reg.lite_applier(_fctx(), _rows(("output_language", None, "de-DE", True)), "apply")
    assert changes[0].after == "de-DE"


# ── 23: web-construction channel-source guard (review P1-1) ──


def test_web_turnrequest_constructions_carry_channel_flag() -> None:
    """chat.py 主路径 + clarifications.py 澄清 resume 都带 config_lite_supported；
    scheduled runner / turn_service（飞书）不带（缺省 False）。

    ★ 本闸只认「通道标记在不在」，不锁写法。历史上它锁过源码字面全串，结果别人
    正当地把澄清侧细化成带附加条件的形态就红了——字面锁把「不变量」和「那一行怎么写」
    绑死，任何合理精化都要赔一次排查。2026-08-18 门控拆除后取值变为 True /
    `_sched_job is None`（定时任务的澄清 resume 不弹交互确认条），闸继续只认存在性。
    """
    import re

    # Extra intervening conditions allowed, but flag must participate.
    pattern = re.compile(r"config_lite_supported=(True|_sched_job is None)")
    for name in ("chat.py", "clarifications.py"):
        src = (_SRC / "routes" / name).read_text(encoding="utf-8")
        assert pattern.search(src), f"{name}: 构造点丢了 config_lite_supported 通道标记"
    for other in ("_scheduled_runner.py", "turn_service.py"):
        assert "config_lite_supported" not in (_SRC / other).read_text(encoding="utf-8"), other
