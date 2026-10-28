"""kb-matrix-sharing: 组织模板库端点验收（test_kb_matrix_authz 范式）。

覆盖：flag off 404 / 发布(creator) + 跨空间发现(同 team) / 发布 non-creator 403 /
kind 非 template 422 / 空模板 422 / 复制→目标空间 kind=template + 血缘 /
viewer 复制 403 / 跨组织复制 404 / self-copy 422 / 撞名 409 / already_copied 标记 /
**撤销 by team_admin 非源空间成员（P1 修复回归）** / 撤销 by 源空间 ws_admin /
撤销 plain member 403 / 源删级联撤销。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from bowhead_core.kb.doc_type_store import ExtractField, KbDocTypeStore
from bowhead_core.kb.team_template_store import KbTeamTemplateStore
from bowhead_core.user_store import User
from bowhead_gateway.routes import kb_doc_types, kb_team_templates
from bowhead_gateway.routes.auth import _current_user
from fastapi import FastAPI
from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

TEAM = "team_shared"
TEAM_OTHER = "team_other"
WS_A = "ws_alpha"
WS_B = "ws_beta"

U_MEMBER = "u_member"  # WS_A member (template creator/publisher).
U_MEMBER2 = "u_member2"  # WS_A member, not creator.
U_VIEWER = "u_viewer"  # WS_A viewer
U_WS_ADMIN = "u_ws_admin"  # WS_A ws_admin, not publisher.
U_OUTSIDER = "u_outsider"  # WS_B member only (same team).
U_TEAM_ADMIN = "u_team_admin"  # Team admin, member of no space.
U_OTHER_ORG = "u_other_team"  # Member of another team.


def _mk_user(uid: str, team: str = TEAM) -> User:
    return User(id=uid, email=f"{uid}@t.com", team_id=team, tier="ultra", email_verified=True)


class _MembershipStore:
    ROLES = {
        (WS_A, U_MEMBER): "member",
        (WS_A, U_MEMBER2): "member",
        (WS_A, U_VIEWER): "viewer",
        (WS_A, U_WS_ADMIN): "ws_admin",
        (WS_B, U_OUTSIDER): "member",
    }
    NAMES = {WS_A: "合规空间", WS_B: "财务空间"}

    def __init__(self) -> None:
        self.workspace_statuses = {
            WS_A: "active",
            WS_B: "active",
        }

    async def get_user_team_id(self, user_id: str) -> str:
        return TEAM_OTHER if user_id == U_OTHER_ORG else TEAM

    async def get_workspace_team_id(self, ws_id: str) -> str:
        return TEAM

    async def get_member_role(self, ws_id: str, user_id: str) -> str | None:
        return self.ROLES.get((ws_id, user_id))

    async def get_team_role(self, user_id: str, team_id: str) -> str | None:
        return "team_admin" if user_id == U_TEAM_ADMIN else None

    async def is_team_admin(self, user_id: str, team_id: str | None = None) -> bool:
        return user_id == U_TEAM_ADMIN

    async def get_workspace(self, ws_id: str) -> dict[str, Any] | None:
        status = self.workspace_statuses.get(ws_id)
        if status is None:
            return None
        return {
            "id": ws_id,
            "name": self.NAMES.get(ws_id, ws_id),
            "status": status,
        }

    async def get_user(self, user_id: str) -> User | None:
        return _mk_user(user_id)


@pytest.fixture
async def ctx(tmp_path: Path):
    dt_store = KbDocTypeStore(tmp_path / "dt.db")
    await dt_store.initialize()
    team_tpl_store = KbTeamTemplateStore(tmp_path / "otpl.db")
    await team_tpl_store.initialize()

    # Seed: one WS_A kind=template (creator U_MEMBER) + one manual + one empty template.
    tpl_id = await dt_store.create(
        workspace_id=WS_A,
        name="合同审阅",
        created_by=U_MEMBER,
        fields=[
            ExtractField(name="对方主体", type="string"),
            ExtractField(name="合同金额", type="currency"),
        ],
        cardinality="many",
        auto_extract_enabled=False,
        graduated_mode="template",
    )
    manual_id = await dt_store.create(
        workspace_id=WS_A,
        name="手工抽取表",
        created_by=U_MEMBER,
        fields=[ExtractField(name="字段", type="string")],
    )
    empty_id = await dt_store.create(
        workspace_id=WS_A,
        name="空模板",
        created_by=U_MEMBER,
        fields=[],
        cardinality="many",
        auto_extract_enabled=False,
        graduated_mode="template",
    )

    app = FastAPI()
    app.include_router(kb_team_templates.router)
    app.include_router(kb_doc_types.router)
    app.state.kb_doc_type_store = dt_store
    app.state.kb_team_template_store = team_tpl_store
    membership_store = _MembershipStore()
    app.state.user_store = membership_store
    client = TestClient(app, raise_server_exceptions=False)
    return {
        "client": client,
        "app": app,
        "dt_store": dt_store,
        "team_tpl_store": team_tpl_store,
        "tpl_id": tpl_id,
        "manual_id": manual_id,
        "empty_id": empty_id,
        "membership_store": membership_store,
    }


def _as(app: FastAPI, uid: str, team: str = TEAM) -> None:
    app.dependency_overrides[_current_user] = lambda: _mk_user(uid, team)


def _h(ws: str) -> dict[str, str]:
    return {"X-Space-Id": ws}


def _assert_code_only(response: Any, status_code: int, code: str) -> None:
    assert response.status_code == status_code, response.text
    assert response.json() == {"detail": {"error": {"code": code}}}


async def _publish(ctx: dict, uid: str = U_MEMBER, dt_id: str | None = None) -> Any:
    _as(ctx["app"], uid)
    return ctx["client"].post(
        "/api/v1/kb/team-templates",
        json={"workspace_id": WS_A, "doc_type_id": dt_id or ctx["tpl_id"]},
        headers=_h(WS_A),
    )


# ─── publish ────────────────────────────────────────────────


async def test_publish_and_discover_cross_space(ctx) -> None:
    r = await _publish(ctx)
    assert r.status_code == 200, r.text
    entry = r.json()
    assert entry["field_count"] == 2
    assert [f["name"] for f in entry["field_preview"]] == ["对方主体", "合同金额"]
    assert entry["source_doc_type_id"] == ctx["tpl_id"]  # design review P1: FE matching anchor.
    # Another space's member in the same team discovers it.
    _as(ctx["app"], U_OUTSIDER)
    r2 = ctx["client"].get("/api/v1/kb/team-templates")
    assert r2.status_code == 200, r2.text
    items = r2.json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "合同审阅"
    assert items[0]["source_workspace_name"] == "合规空间"  # Resolve space name.
    assert items[0]["published_by_name"] == "u_member@t.com"  # Resolve publisher name (_safe_user_name→email fallback).
    assert items[0]["source_doc_type_id"] == ctx["tpl_id"]  # Rename-proof matching anchor.
    # No data in payload (Q2): field_preview has only name/type.
    assert set(items[0]["field_preview"][0].keys()) == {"name", "type"}


async def test_publish_non_creator_non_admin_403(ctx) -> None:
    r = await _publish(ctx, uid=U_MEMBER2)
    _assert_code_only(r, 403, "workspace_admin_or_creator_required")


async def test_publish_kind_gate_422(ctx) -> None:
    r = await _publish(ctx, dt_id=ctx["manual_id"])
    _assert_code_only(r, 422, "not_publishable")


async def test_publish_empty_template_422(ctx) -> None:
    r = await _publish(ctx, dt_id=ctx["empty_id"])
    _assert_code_only(r, 422, "empty_template")


async def test_publish_idempotent(ctx) -> None:
    await _publish(ctx)
    await _publish(ctx)  # Republish same source.
    _as(ctx["app"], U_MEMBER)
    items = ctx["client"].get("/api/v1/kb/team-templates").json()["items"]
    assert len(items) == 1  # Idempotent, still one row.


async def test_publish_cross_space_doc_type_404(ctx) -> None:
    """confused-deputy：WS_A 成员传一个不属 WS_A 的 doc_type → 404。"""
    other = await ctx["dt_store"].create(
        workspace_id=WS_B,
        name="别空间模板",
        created_by=U_OUTSIDER,
        fields=[ExtractField(name="x", type="string")],
        cardinality="many",
        auto_extract_enabled=False,
        graduated_mode="template",
    )
    _as(ctx["app"], U_MEMBER)
    r = ctx["client"].post(
        "/api/v1/kb/team-templates",
        json={"workspace_id": WS_A, "doc_type_id": other},
        headers=_h(WS_A),
    )
    _assert_code_only(r, 404, "team_template_not_found")


async def test_archived_workspace_lists_history_but_rejects_publish_and_copy(ctx) -> None:
    entry_id = await _publish_get_entry(ctx)
    ctx["membership_store"].workspace_statuses[WS_A] = "archived"
    _as(ctx["app"], U_MEMBER)

    listed = ctx["client"].get(
        "/api/v1/kb/team-templates",
        params={"workspace_id": WS_A},
        headers=_h(WS_A),
    )
    publish = ctx["client"].post(
        "/api/v1/kb/team-templates",
        json={"workspace_id": WS_A, "doc_type_id": ctx["tpl_id"]},
        headers=_h(WS_A),
    )

    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["entry_id"] == entry_id
    assert publish.status_code == 409, publish.text
    assert publish.json()["detail"]["error"]["code"] == "workspace_archived"

    ctx["membership_store"].workspace_statuses[WS_B] = "archived"
    _as(ctx["app"], U_OUTSIDER)
    copied = ctx["client"].post(
        f"/api/v1/kb/team-templates/{entry_id}/copy",
        json={"workspace_id": WS_B},
        headers=_h(WS_B),
    )
    assert copied.status_code == 409, copied.text
    assert copied.json()["detail"]["error"]["code"] == "workspace_archived"


async def test_archived_source_ws_admin_can_revoke_team_governance_entry(ctx) -> None:
    entry_id = await _publish_get_entry(ctx)
    ctx["membership_store"].workspace_statuses[WS_A] = "archived"
    _as(ctx["app"], U_WS_ADMIN)

    response = ctx["client"].delete(f"/api/v1/kb/team-templates/{entry_id}")

    assert response.status_code == 200, response.text
    assert response.json() == {"revoked": True, "entry_id": entry_id}


async def test_publish_store_failure_is_code_only(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ctx["team_tpl_store"], "publish", AsyncMock(side_effect=RuntimeError("RAW sqlite detail")))
    r = await _publish(ctx)
    _assert_code_only(r, 503, "team_template_service_unavailable")
    assert "RAW sqlite detail" not in r.text


async def test_list_store_failure_is_code_only(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    _as(ctx["app"], U_MEMBER)
    monkeypatch.setattr(ctx["team_tpl_store"], "list_live", AsyncMock(side_effect=RuntimeError("RAW list detail")))
    r = ctx["client"].get("/api/v1/kb/team-templates")
    _assert_code_only(r, 503, "team_template_service_unavailable")
    assert "RAW list detail" not in r.text


# ─── copy ───────────────────────────────────────────────────


async def _publish_get_entry(ctx) -> str:
    r = await _publish(ctx)
    assert r.status_code == 200, r.text
    return r.json()["entry_id"]


async def test_copy_into_other_space(ctx) -> None:
    entry_id = await _publish_get_entry(ctx)
    _as(ctx["app"], U_OUTSIDER)
    r = ctx["client"].post(
        f"/api/v1/kb/team-templates/{entry_id}/copy",
        json={"workspace_id": WS_B},
        headers=_h(WS_B),
    )
    assert r.status_code == 200, r.text
    dt = r.json()
    assert dt["kind"] == "template"
    assert dt["workspace_id"] == WS_B
    assert dt["created_by"] == U_OUTSIDER
    assert dt["copied_from_team_template_id"] == entry_id
    assert [f["name"] for f in dt["fields"]] == ["对方主体", "合同金额"]  # Field round-trip.
    # already_copied marker (current space supplied).
    r2 = ctx["client"].get("/api/v1/kb/team-templates", params={"workspace_id": WS_B})
    item = r2.json()["items"][0]
    assert item["already_copied"] is True
    assert item["is_own_space"] is False


async def test_copy_viewer_403(ctx) -> None:
    entry_id = await _publish_get_entry(ctx)
    _as(ctx["app"], U_VIEWER)
    r = ctx["client"].post(
        f"/api/v1/kb/team-templates/{entry_id}/copy",
        json={"workspace_id": WS_A},
        headers=_h(WS_A),
    )
    _assert_code_only(r, 403, "workspace_member_role_required")


async def test_copy_cross_team_404(ctx) -> None:
    entry_id = await _publish_get_entry(ctx)
    _as(ctx["app"], U_OTHER_ORG, team=TEAM_OTHER)
    r = ctx["client"].post(
        f"/api/v1/kb/team-templates/{entry_id}/copy",
        json={"workspace_id": WS_B},
        headers=_h(WS_B),
    )
    _assert_code_only(r, 404, "team_template_not_found")  # get(entry, caller_team=team_other) → None


async def test_copy_into_source_space_422(ctx) -> None:
    entry_id = await _publish_get_entry(ctx)
    _as(ctx["app"], U_MEMBER)
    r = ctx["client"].post(
        f"/api/v1/kb/team-templates/{entry_id}/copy",
        json={"workspace_id": WS_A},
        headers=_h(WS_A),
    )
    _assert_code_only(r, 422, "copy_into_source_space")


async def test_copy_collision_409(ctx) -> None:
    entry_id = await _publish_get_entry(ctx)
    # WS_B already has a same-name doc_type.
    await ctx["dt_store"].create(
        workspace_id=WS_B,
        name="合同审阅",
        created_by=U_OUTSIDER,
        fields=[ExtractField(name="y", type="string")],
    )
    _as(ctx["app"], U_OUTSIDER)
    r = ctx["client"].post(
        f"/api/v1/kb/team-templates/{entry_id}/copy",
        json={"workspace_id": WS_B},
        headers=_h(WS_B),
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error"]["code"] == "doc_type_name_taken"
    assert r.json()["detail"]["error"]["suggestion"]


async def test_copy_invalid_schema_is_code_only(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    entry_id = await _publish_get_entry(ctx)

    def _invalid_schema(_schema_json: str) -> list:
        raise ValueError("RAW corrupt schema")

    monkeypatch.setattr(kb_team_templates, "_schema_json_to_fields", _invalid_schema)
    _as(ctx["app"], U_OUTSIDER)
    r = ctx["client"].post(
        f"/api/v1/kb/team-templates/{entry_id}/copy",
        json={"workspace_id": WS_B},
        headers=_h(WS_B),
    )
    _assert_code_only(r, 422, "invalid_template_schema")
    assert "RAW corrupt schema" not in r.text


async def test_copy_store_failure_is_code_only(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    entry_id = await _publish_get_entry(ctx)
    monkeypatch.setattr(ctx["dt_store"], "create", AsyncMock(side_effect=RuntimeError("RAW create detail")))
    _as(ctx["app"], U_OUTSIDER)
    r = ctx["client"].post(
        f"/api/v1/kb/team-templates/{entry_id}/copy",
        json={"workspace_id": WS_B},
        headers=_h(WS_B),
    )
    _assert_code_only(r, 503, "team_template_service_unavailable")
    assert "RAW create detail" not in r.text


async def test_copy_committed_reload_failure_requires_refresh(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    entry_id = await _publish_get_entry(ctx)
    original_get = ctx["dt_store"].get
    monkeypatch.setattr(ctx["dt_store"], "get", AsyncMock(side_effect=RuntimeError("RAW reload detail")))
    _as(ctx["app"], U_OUTSIDER)
    r = ctx["client"].post(
        f"/api/v1/kb/team-templates/{entry_id}/copy",
        json={"workspace_id": WS_B},
        headers=_h(WS_B),
    )
    _assert_code_only(r, 503, "team_template_copy_refresh_required")
    assert "RAW reload detail" not in r.text

    monkeypatch.setattr(ctx["dt_store"], "get", original_get)
    copied = await ctx["dt_store"].list_for_ws(WS_B)
    assert [item.name for item in copied] == ["合同审阅"]


# ─── revoke ─────────────────────────────────────────────────


async def test_revoke_by_publisher(ctx) -> None:
    entry_id = await _publish_get_entry(ctx)
    _as(ctx["app"], U_MEMBER)
    r = ctx["client"].delete(f"/api/v1/kb/team-templates/{entry_id}")
    assert r.status_code == 200, r.text
    assert ctx["client"].get("/api/v1/kb/team-templates").json()["items"] == []


async def test_revoke_by_team_admin_non_member(ctx) -> None:
    """P1 修复回归：不在源空间的 team_admin 也能撤销（原设计会 403 挡死）。"""
    entry_id = await _publish_get_entry(ctx)
    _as(ctx["app"], U_TEAM_ADMIN)
    r = ctx["client"].delete(f"/api/v1/kb/team-templates/{entry_id}")
    assert r.status_code == 200, r.text


async def test_revoke_by_source_ws_admin(ctx) -> None:
    """防御式源空间 ws_admin 腿：非发布者非 team_admin 的源空间 ws_admin 可撤销。"""
    entry_id = await _publish_get_entry(ctx)
    _as(ctx["app"], U_WS_ADMIN)
    r = ctx["client"].delete(f"/api/v1/kb/team-templates/{entry_id}")
    assert r.status_code == 200, r.text


async def test_revoke_plain_member_403(ctx) -> None:
    entry_id = await _publish_get_entry(ctx)
    _as(ctx["app"], U_MEMBER2)
    r = ctx["client"].delete(f"/api/v1/kb/team-templates/{entry_id}")
    _assert_code_only(r, 403, "revoke_not_allowed")


async def test_revoke_missing_is_code_only(ctx) -> None:
    _as(ctx["app"], U_MEMBER)
    r = ctx["client"].delete("/api/v1/kb/team-templates/ot_missing")
    _assert_code_only(r, 404, "team_template_not_found")


async def test_revoke_store_failure_is_code_only(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    entry_id = await _publish_get_entry(ctx)
    monkeypatch.setattr(ctx["team_tpl_store"], "revoke", AsyncMock(side_effect=RuntimeError("RAW revoke detail")))
    _as(ctx["app"], U_MEMBER)
    r = ctx["client"].delete(f"/api/v1/kb/team-templates/{entry_id}")
    _assert_code_only(r, 503, "team_template_service_unavailable")
    assert "RAW revoke detail" not in r.text


async def test_revoke_role_lookup_failure_is_code_only(ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    entry_id = await _publish_get_entry(ctx)
    monkeypatch.setattr(
        ctx["app"].state.user_store,
        "is_team_admin",
        AsyncMock(side_effect=RuntimeError("RAW authz detail")),
    )
    _as(ctx["app"], U_MEMBER2)
    r = ctx["client"].delete(f"/api/v1/kb/team-templates/{entry_id}")
    _assert_code_only(r, 503, "team_template_service_unavailable")
    assert "RAW authz detail" not in r.text


async def test_source_delete_cascade_revokes(ctx) -> None:
    entry_id = await _publish_get_entry(ctx)
    # Creator deletes source template → cascade revokes library entry.
    _as(ctx["app"], U_MEMBER)
    rd = ctx["client"].delete(f"/api/v1/kb/doc_types/{ctx['tpl_id']}")
    assert rd.status_code == 200, rd.text
    assert ctx["client"].get("/api/v1/kb/team-templates").json()["items"] == []
    # Soft-revoked entry (still returned by get, revoked).
    row = await ctx["team_tpl_store"].get(entry_id, team_id=TEAM)
    assert row is not None
    assert row.revoked_at is not None


# ─── round-trip / snapshot / edge (review MT-1..MT-5) ───────


async def test_copy_roundtrips_options_dimension(ctx) -> None:
    """MT-1：select/multi_select options + dimension + 全字段属性经 publish→copy 无损
    （守 M4 曾 ship-block 的 options 序列化类）。"""
    dt_id = await ctx["dt_store"].create(
        workspace_id=WS_A,
        name="富字段模板",
        created_by=U_MEMBER,
        fields=[
            ExtractField(name="情感", type="select", options=["正面", "中性", "负面"], extraction_hint="判断倾向", required=True),
            ExtractField(name="标签", type="multi_select", options=["A", "B", "C"]),
            ExtractField(name="金额", type="currency", cross_verify=True, dimension={"unit": "元"}),
        ],
        cardinality="many",
        auto_extract_enabled=False,
        graduated_mode="template",
    )
    r = await _publish(ctx, dt_id=dt_id)
    assert r.status_code == 200, r.text
    entry_id = r.json()["entry_id"]
    _as(ctx["app"], U_OUTSIDER)
    rc = ctx["client"].post(f"/api/v1/kb/team-templates/{entry_id}/copy", json={"workspace_id": WS_B}, headers=_h(WS_B))
    assert rc.status_code == 200, rc.text
    f = {x["name"]: x for x in rc.json()["fields"]}
    assert f["情感"]["type"] == "select"
    assert f["情感"]["options"] == ["正面", "中性", "负面"]
    assert f["情感"]["required"] is True
    assert f["情感"]["extraction_hint"] == "判断倾向"
    assert f["标签"]["type"] == "multi_select"
    assert f["标签"]["options"] == ["A", "B", "C"]
    assert f["金额"]["cross_verify"] is True
    assert f["金额"]["dimension"] == {"unit": "元"}


async def test_frozen_snapshot_ignores_source_edit(ctx) -> None:
    """MT-2：发布后改源字段但不重发布 → 复制仍得旧快照（snapshot 非 live pointer）。"""
    entry_id = await _publish_get_entry(ctx)  # Snapshot = [对方主体, 合同金额].
    await ctx["dt_store"].update(ctx["tpl_id"], fields=[ExtractField(name="新字段", type="string")])
    _as(ctx["app"], U_OUTSIDER)
    rc = ctx["client"].post(f"/api/v1/kb/team-templates/{entry_id}/copy", json={"workspace_id": WS_B}, headers=_h(WS_B))
    assert rc.status_code == 200, rc.text
    assert [x["name"] for x in rc.json()["fields"]] == ["对方主体", "合同金额"]  # Old snapshot, not ["新字段"].


async def test_publish_schema_too_large_422(ctx) -> None:
    """MT-3：O5 体积 cap——超大 dimension blob 使 schema_json > 100KB → 422。"""
    big = await ctx["dt_store"].create(
        workspace_id=WS_A,
        name="超大模板",
        created_by=U_MEMBER,
        fields=[ExtractField(name="f", type="string", dimension={"blob": "x" * 120_000})],
        cardinality="many",
        auto_extract_enabled=False,
        graduated_mode="template",
    )
    r = await _publish(ctx, dt_id=big)
    _assert_code_only(r, 422, "schema_too_large")


async def test_republish_refreshes_snapshot(ctx) -> None:
    """MT-4：重发布经路由刷新快照（field_count 更新）。"""
    await _publish(ctx)  # field_count 2
    await ctx["dt_store"].update(
        ctx["tpl_id"],
        fields=[
            ExtractField(name="对方主体", type="string"),
            ExtractField(name="合同金额", type="currency"),
            ExtractField(name="新增列", type="string"),
        ],
    )
    r2 = await _publish(ctx)  # Republish same source.
    assert r2.status_code == 200, r2.text
    _as(ctx["app"], U_OUTSIDER)
    items = ctx["client"].get("/api/v1/kb/team-templates").json()["items"]
    assert len(items) == 1
    assert items[0]["field_count"] == 3  # Refreshed.


async def test_revoke_non_source_member_swallowed_403(ctx) -> None:
    """MT-5：防御式 ws_admin 腿对非源空间成员——build_access_context raise 被吞，
    整请求 403（非 fail-open、非 500）。"""
    entry_id = await _publish_get_entry(ctx)
    _as(ctx["app"], U_OUTSIDER)  # WS_B member, not WS_A member, publisher or team_admin.
    r = ctx["client"].delete(f"/api/v1/kb/team-templates/{entry_id}")
    _assert_code_only(r, 403, "revoke_not_allowed")


async def test_copy_with_override_name_after_collision(ctx) -> None:
    """MT-6：撞名后带覆盖名复制成功（name 覆盖路 + 改名后 follow-through）。"""
    entry_id = await _publish_get_entry(ctx)
    await ctx["dt_store"].create(workspace_id=WS_B, name="合同审阅", created_by=U_OUTSIDER, fields=[ExtractField(name="y", type="string")])
    _as(ctx["app"], U_OUTSIDER)
    r = ctx["client"].post(
        f"/api/v1/kb/team-templates/{entry_id}/copy",
        json={"workspace_id": WS_B, "name": "合同审阅（复制）"},
        headers=_h(WS_B),
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "合同审阅（复制）"
    assert r.json()["kind"] == "template"


async def test_list_without_workspace_omits_flags(ctx) -> None:
    """MT-10：不带 workspace_id → is_own_space/already_copied 缺省（不误渲染标记）。"""
    await _publish(ctx)
    _as(ctx["app"], U_OUTSIDER)
    item = ctx["client"].get("/api/v1/kb/team-templates").json()["items"][0]
    assert "is_own_space" not in item
    assert "already_copied" not in item
