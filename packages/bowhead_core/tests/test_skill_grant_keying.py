"""skill-team-governance P1a —— skill grant 键按 resolve origin 定（audit #11）.

team origin → ("team_skill", id) 键（对齐 content-sharing）；catalog/user → 名字键
不变；收窄版 owner 豁免（仅 not_in_allowlist 放行作者，deny 仍生效）。harness 沿
test_tool_registry_resource_grants.py（真 AccessControlStore + execute 穿闸）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.access_control import AccessContext, AccessControlStore
from bowhead_core.resource_access import resolve_skill_grant_target
from bowhead_core.skill_access import skill_visible_by_grants
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, ToolSpec
from bowhead_router.types import ToolSchema


async def _ok_handler(args, ctx):
    return {"ok": True}


class _FakeCatalog:
    def __init__(self, names=()):
        self._names = set(names)

    def get(self, name):
        return SimpleNamespace(name=name) if name in self._names else None


class _FakeUserSkillStore:
    def __init__(self, names=()):
        self._names = set(names)

    async def get(self, *, user_id, name):
        if name in self._names:
            return SimpleNamespace(name=name, source_type="user_upload", source_path="", enabled=True)
        return None


class _FakeTeamSkillStore:
    def __init__(self, recs=()):
        self._recs = {r.name: r for r in recs}

    async def get_by_name(self, team_id, name, only_enabled=True):
        return self._recs.get(name)


def _team_rec(name="team-audit-skill", *, rec_id="oskill_abc123", created_by="u_creator"):
    return SimpleNamespace(id=rec_id, name=name, created_by=created_by, visibility="team_wide")


def _access_ctx(user_id="u_alice") -> dict:
    return AccessContext(
        user_id=user_id,
        team_id="team_acme",
        workspace_id="ws_sales",
        workspace_role="member",
        team_role=None,
        group_ids=(),
        policy_epoch=1,
        is_team_admin=False,
    ).__dict__


def _skill_ctx(access: AccessControlStore, *, user_id="u_alice", catalog=None, user_store=None, team_store=None) -> ToolExecutionContext:
    ctx = ToolExecutionContext(
        workspace_id="ws_sales",
        member_uid=user_id,
        user_id=user_id,
        turn_id="t1",
        datarc=object(),
        access_context=_access_ctx(user_id),
        access_control_store=access,
    )
    ctx.skill_catalog = catalog or _FakeCatalog()
    ctx.skill_instance_store = user_store or _FakeUserSkillStore()
    ctx.team_skill_store = team_store or _FakeTeamSkillStore()
    ctx.team_id = "team_acme"
    return ctx


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            schema=ToolSchema(name="skill_invoke", description="Run skill", input_schema={"type": "object", "properties": {}}),
            handler=_ok_handler,
        ),
    )
    return registry


async def _mk_access(tmp_path) -> AccessControlStore:
    access = AccessControlStore(tmp_path / "access.db")
    await access.initialize()
    return access


# ── resolve_skill_grant_target origin determination ──────────────────────────


@pytest.mark.asyncio
async def test_origin_catalog_and_user_keep_name_key(tmp_path):
    access = await _mk_access(tmp_path)
    ctx = _skill_ctx(
        access,
        catalog=_FakeCatalog({"builtin-x"}),
        user_store=_FakeUserSkillStore({"my-skill"}),
        team_store=_FakeTeamSkillStore([_team_rec("my-skill")]),
    )
    target, created_by = await resolve_skill_grant_target(ctx, "builtin-x")
    assert target == ("skill", "builtin-x", "use") and created_by is None
    # Same-name shadow: a user owns the name → name key (never reverse-lookup the team by name).
    target, created_by = await resolve_skill_grant_target(ctx, "my-skill")
    assert target == ("skill", "my-skill", "use") and created_by is None


@pytest.mark.asyncio
async def test_origin_team_uses_id_key(tmp_path):
    access = await _mk_access(tmp_path)
    rec = _team_rec()
    ctx = _skill_ctx(access, team_store=_FakeTeamSkillStore([rec]))
    target, created_by = await resolve_skill_grant_target(ctx, rec.name)
    assert target == ("team_skill", "oskill_abc123", "use")
    assert created_by == "u_creator"


@pytest.mark.asyncio
async def test_origin_unknown_falls_back_to_name_key(tmp_path):
    access = await _mk_access(tmp_path)
    ctx = _skill_ctx(access)
    target, created_by = await resolve_skill_grant_target(ctx, "ghost")
    assert target == ("skill", "ghost", "use") and created_by is None


# ── Passing the execution gate (tool_registry.execute) ───────────────────────


@pytest.mark.asyncio
async def test_execute_team_id_allowlist_enforced(tmp_path, monkeypatch):
    """id 键 allowlist 在执行闸真实生效：不在 allowlist 的成员被拒（改前名字键查询恒 miss=默认放行）."""
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    access = await _mk_access(tmp_path)
    rec = _team_rec()
    await access.create_or_update_resource_grant(
        team_id="team_acme",
        resource_kind="team_skill",
        resource_id=rec.id,
        principal_type="user",
        principal_id="u_bob",
        permission="invoke",
        effect="allow",
        created_by="u_admin",
    )
    team_store = _FakeTeamSkillStore([rec])

    denied = await _registry().execute("skill_invoke", {"name": rec.name}, _skill_ctx(access, user_id="u_alice", team_store=team_store))
    assert denied.error is not None and "team_skill" in denied.error

    allowed = await _registry().execute("skill_invoke", {"name": rec.name}, _skill_ctx(access, user_id="u_bob", team_store=team_store))
    assert allowed.error is None


@pytest.mark.asyncio
async def test_execute_owner_exempt_from_allowlist_but_not_deny(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    access = await _mk_access(tmp_path)
    rec = _team_rec(created_by="u_creator")
    await access.create_or_update_resource_grant(
        team_id="team_acme",
        resource_kind="team_skill",
        resource_id=rec.id,
        principal_type="user",
        principal_id="u_bob",
        permission="invoke",
        effect="allow",
        created_by="u_admin",
    )
    team_store = _FakeTeamSkillStore([rec])

    # Author absent from allowlist: not_in_allowlist → exempt and allow.
    owner_run = await _registry().execute("skill_invoke", {"name": rec.name}, _skill_ctx(access, user_id="u_creator", team_store=team_store))
    assert owner_run.error is None

    # Explicit deny still applies to the author (the exemption accepts only not_in_allowlist).
    await access.create_or_update_resource_grant(
        team_id="team_acme",
        resource_kind="team_skill",
        resource_id=rec.id,
        principal_type="user",
        principal_id="u_creator",
        permission="invoke",
        effect="deny",
        created_by="u_admin",
    )
    owner_denied = await _registry().execute("skill_invoke", {"name": rec.name}, _skill_ctx(access, user_id="u_creator", team_store=team_store))
    assert owner_denied.error is not None


@pytest.mark.asyncio
async def test_execute_same_name_user_skill_unaffected_by_team_allowlist(tmp_path, monkeypatch):
    """同名 shadow：作者个人版执行按名字键，team allowlist 不误伤（origin 定键）."""
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    access = await _mk_access(tmp_path)
    rec = _team_rec("dup-name")
    await access.create_or_update_resource_grant(
        team_id="team_acme",
        resource_kind="team_skill",
        resource_id=rec.id,
        principal_type="user",
        principal_id="u_bob",
        permission="use",
        effect="allow",
        created_by="u_admin",
    )
    ctx = _skill_ctx(access, user_id="u_alice", user_store=_FakeUserSkillStore({"dup-name"}), team_store=_FakeTeamSkillStore([rec]))
    run = await _registry().execute("skill_invoke", {"name": "dup-name"}, ctx)
    assert run.error is None  # No allowlist for the name key → allow by default


# ── Visibility surface (skill_visible_by_grants) ─────────────────────────────


@pytest.mark.asyncio
async def test_visibility_team_id_key_and_owner_exempt(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    access = await _mk_access(tmp_path)
    rec = _team_rec()
    await access.create_or_update_resource_grant(
        team_id="team_acme",
        resource_kind="team_skill",
        resource_id=rec.id,
        principal_type="user",
        principal_id="u_bob",
        permission="use",
        effect="allow",
        created_by="u_admin",
    )
    non_member = _skill_ctx(access, user_id="u_alice")
    assert await skill_visible_by_grants(non_member, rec.name, team_skill_id=rec.id, team_skill_created_by=rec.created_by) is False
    listed = _skill_ctx(access, user_id="u_bob")
    assert await skill_visible_by_grants(listed, rec.name, team_skill_id=rec.id, team_skill_created_by=rec.created_by) is True
    owner = _skill_ctx(access, user_id="u_creator")
    assert await skill_visible_by_grants(owner, rec.name, team_skill_id=rec.id, team_skill_created_by=rec.created_by) is True


@pytest.mark.asyncio
async def test_visibility_name_key_unchanged_without_id(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    access = await _mk_access(tmp_path)
    ctx = _skill_ctx(access)
    assert await skill_visible_by_grants(ctx, "any-skill") is True  # Regression: allow by default without a grant
