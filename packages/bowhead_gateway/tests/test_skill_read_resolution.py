"""Exact origin preparation with real stores, scopes, sharing and material IO."""

from __future__ import annotations

import dataclasses
import sqlite3
import threading
from types import SimpleNamespace

import anyio
import pytest
import yaml
from bowhead_core.access_control import AccessControlStore
from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.chat_references import SkillResourceRef
from bowhead_core.share_service import reset_content_sharing_cache
from bowhead_core.skill_instance_store import SkillInstanceStore
from bowhead_core.skill_read_material import SkillReadMaterialError
from bowhead_core.skill_read_resolution import SkillReadResolutionError, SkillReadSourceResolver
from bowhead_core.skill_reference_metadata import build_builtin_skill_metadata
from bowhead_core.skill_upload import bundle_sha256
from bowhead_core.space_governance import SpaceOperation
from bowhead_core.user_store import TeamSkill
from bowhead_gateway.access_context import build_access_context
from test_team_chart_styles_routes import app_ctx as _app_ctx

from bowhead_core import skill_read_resolution as resolution

app_ctx = _app_ctx
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def package(root, name="dupont", text="SELECTED_METHOD"):
    root.mkdir(parents=True)
    declaration = {
        "schema_version": 1,
        "entry_file": "method.md",
        "required_files": [],
        "parameter_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "required_skill_refs": [],
        "requires_script": False,
        "requires_new_tools": False,
        "requires_new_connections": False,
    }
    (root / "SKILL.md").write_text(
        "---\n" + yaml.safe_dump({"name": name, "description": "Method", "read_only_entry": declaration}) + "---\nUNSELECTED_MANIFEST\n"
    )
    (root / "method.md").write_text(text)
    return root


def ref(kind, identity, path):
    return SkillResourceRef(kind="skill", source_kind=kind, id=identity, version="sha256:" + bundle_sha256(path))


@pytest.fixture
async def case(app_ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    monkeypatch.setenv("BOWHEAD_CONTENT_SHARING", "1")
    reset_content_sharing_cache()
    db_path = app_ctx.users.db_path
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA journal_mode=WAL")
    access = AccessControlStore(db_path)
    await access.initialize()
    instances = SkillInstanceStore(db_path)
    await instances.initialize()
    workspace = await app_ctx.users.create_local_workspace(team_id=app_ctx.team.id, name="Prepare", creator_user_id=app_ctx.member.id)
    scope = ReferenceRequestScope(team_id=app_ctx.team.id, user_id=app_ctx.member.id, workspace_id=workspace)
    builtin_root = tmp_path / "installed"
    builtin = package(builtin_root / "nested" / "package")
    index = build_builtin_skill_metadata(builtin_root=builtin_root)
    holder = SimpleNamespace(index=index)

    async def context():
        return await build_access_context(
            user_store=app_ctx.users,
            access_store=access,
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            expected_team_id=scope.team_id,
            operation=SpaceOperation.EXECUTE,
        )

    resolver = SkillReadSourceResolver(db_path, access_factory=context, builtin_index=lambda: holder.index, builtin_root=builtin_root)
    personal_root = package(tmp_path / "personal", text="PERSONAL_METHOD")
    personal = await instances.upsert(user_id=scope.user_id, name="dupont", source_type="user_upload", source_path=str(personal_root))
    team_root = package(tmp_path / "team", text="TEAM_METHOD")
    team = TeamSkill(
        id="oskill_exact",
        team_id=scope.team_id,
        name="dupont",
        source_path=str(team_root),
        created_by=app_ctx.owner.id,
        approval="approved",
        approved_sha256=bundle_sha256(team_root),
        visibility="team_wide",
    )
    await app_ctx.users.create_team_skill(team)
    yield SimpleNamespace(
        resolver=resolver,
        scope=scope,
        access=access,
        instances=instances,
        builtin=builtin,
        builtin_root=builtin_root,
        holder=holder,
        personal=personal,
        personal_root=personal_root,
        team=team,
        team_root=team_root,
        db_path=db_path,
        app=app_ctx,
    )
    reset_content_sharing_cache()


async def prepare(case, kind, *, scope=None, resource=None):
    identity, path = {
        "builtin": ("builtin:dupont", case.builtin),
        "user": (case.personal.id, case.personal_root),
        "team": (case.team.id, case.team_root),
    }[kind]
    return await case.resolver.prepare(resource or ref(kind, identity, path), scope=scope or case.scope)


@pytest.mark.parametrize("kind", ["builtin", "user", "team"])
async def test_retained_origin_reauthorizes_across_epochs_but_never_ignores_current_denial(case, kind, monkeypatch):
    from bowhead_core._db import open_shared_ctx

    prepared = await prepare(case, kind)
    proof = prepared.proof
    await case.access.bump_policy_epoch(case.scope.team_id)
    async with open_shared_ctx(case.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        with pytest.raises(SkillReadResolutionError):
            await case.resolver.revalidate_in_connection(db, proof)
        await case.resolver.revalidate_retained_in_connection(db, proof)
    # Legacy shadow mode is not an authorization for retained extensions.
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "0")
    await grant(case, "team_skill" if kind == "team" else "skill", case.team.id if kind == "team" else "dupont", effect="deny")
    async with open_shared_ctx(case.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        with pytest.raises(SkillReadResolutionError):
            await case.resolver.revalidate_retained_in_connection(db, proof)
    assert prepared.proof == proof


async def grant(case, kind, identity, *, actor=None, effect="allow", principal_type="user"):
    return await case.access.create_or_update_resource_grant(
        team_id=case.scope.team_id,
        resource_kind=kind,
        resource_id=identity,
        principal_type=principal_type,
        principal_id=actor or case.scope.user_id,
        permission="use",
        effect=effect,
    )


@pytest.mark.parametrize(
    "kind,body,trust",
    [
        ("builtin", "SELECTED_METHOD", "installed_builtin"),
        ("user", "PERSONAL_METHOD", "personal_owner_current_bytes"),
        ("team", "TEAM_METHOD", "team_approved_sha256"),
    ],
)
async def test_each_exact_origin_prepares_its_own_bytes_despite_same_names(case, kind, body, trust):
    result = await prepare(case, kind)
    assert result.material.document()["files"][0]["text"] == body
    assert result.proof.scope == case.scope
    assert result.proof.resource.source_kind == kind and result.proof.trust_basis == trust
    assert result.proof.policy_epoch == await case.access.get_policy_epoch(case.scope.team_id)
    assert str(case.builtin_root) not in repr(result)
    assert body not in repr(result)
    assert not hasattr(result.proof, "path")


async def test_personal_identity_never_reads_agent_authors_or_another_users_resource(case, monkeypatch, tmp_path):
    path = package(tmp_path / "authors", text="AGENT_AUTHOR_PRIVATE")
    other = await case.instances.upsert(user_id=case.app.owner.id, name="dupont", source_type="user_upload", source_path=str(path))
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("must not read a foreign package")

    monkeypatch.setattr(resolution, "prepare_skill_read", forbidden)
    with pytest.raises(SkillReadResolutionError):
        await prepare(case, "user", resource=ref("user", other.id, path))
    assert not calls


@pytest.mark.parametrize(
    "change", ["pending", "disabled", "team_revoked", "team_private", "team_version", "workspace_archived", "source_relocated", "index_removed"]
)
async def test_preparation_discards_material_when_current_state_changes(case, monkeypatch, tmp_path, change):
    kind = "team" if change.startswith("team_") else "builtin" if change == "index_removed" else "user"
    original = resolution.prepare_skill_read
    main_thread = threading.get_ident()
    calls = []

    def capture_then_change(*args, **kwargs):
        calls.append(threading.get_ident())
        material = original(*args, **kwargs)
        with sqlite3.connect(case.db_path) as db:
            if change in {"pending", "disabled"}:
                column, value = ("import_status", "pending") if change == "pending" else ("enabled", 0)
                db.execute(f"UPDATE skill_instances SET {column}=? WHERE id=?", (value, case.personal.id))
            elif change.startswith("team_"):
                column, value = {
                    "team_revoked": ("status", "disabled"),
                    "team_private": ("visibility", "creator_only"),
                    "team_version": ("approved_sha256", "0" * 64),
                }[change]
                db.execute(f"UPDATE team_skill SET {column}=? WHERE id=?", (value, case.team.id))
            elif change == "workspace_archived":
                db.execute("UPDATE workspaces SET status='archived' WHERE id=?", (case.scope.workspace_id,))
            elif change == "source_relocated":
                new_path = package(tmp_path / "replacement", text="PERSONAL_METHOD")
                db.execute("UPDATE skill_instances SET source_path=? WHERE id=?", (str(new_path), case.personal.id))
            else:
                case.holder.index = None
        return material

    monkeypatch.setattr(resolution, "prepare_skill_read", capture_then_change)
    with pytest.raises(SkillReadResolutionError):
        await prepare(case, kind)
    assert len(calls) == 1 and calls[0] != main_thread


async def test_pending_candidate_does_not_replace_or_invalidate_approved_live_slot(case, monkeypatch):
    original = resolution.prepare_skill_read

    def capture_then_submit(*args, **kwargs):
        material = original(*args, **kwargs)
        with sqlite3.connect(case.db_path) as db:
            db.execute(
                "UPDATE team_skill SET candidate_state='pending',candidate_path='/candidate/private',candidate_sha256=?,updated_at='later' WHERE id=?",
                ("0" * 64, case.team.id),
            )
        return material

    monkeypatch.setattr(resolution, "prepare_skill_read", capture_then_submit)
    assert (await prepare(case, "team")).material.document()["files"][0]["text"] == "TEAM_METHOD"


async def test_scoped_group_sharing_uses_current_membership(case):
    with sqlite3.connect(case.db_path) as db:
        db.execute("UPDATE team_skill SET visibility='creator_only',approved_sha256=NULL WHERE id=?", (case.team.id,))
    group = await case.access.create_group(team_id=case.scope.team_id, workspace_id=case.scope.workspace_id, name="Analysts")
    await grant(case, "team_skill", case.team.id, actor=group.id, principal_type="group")
    with pytest.raises(SkillReadResolutionError):
        await prepare(case, "team")
    await case.access.add_group_member(team_id=case.scope.team_id, group_id=group.id, user_id=case.scope.user_id)
    await prepare(case, "team")
    await case.access.remove_group_member(case.scope.team_id, group.id, case.scope.user_id)
    with pytest.raises(SkillReadResolutionError):
        await prepare(case, "team")


@pytest.mark.parametrize("kind", ["builtin", "user", "team"])
async def test_current_use_deny_holds_even_in_shadow_grant_mode(case, monkeypatch, kind):
    """显式引用的精确来源必须有当前 use 权限；4b00f98c8 起旧的 shadow 开关
    （``BOWHEAD_RESOURCE_GRANTS_ENFORCE=0``）不再能放行这条执行路径。"""
    target, identity = ("team_skill", case.team.id) if kind == "team" else ("skill", "dupont")
    await prepare(case, kind)  # Positive control: readable without deny.
    await grant(case, target, identity, effect="deny")
    with pytest.raises(SkillReadResolutionError, match="reference_skill_source_unavailable"):
        await prepare(case, kind)
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "0")
    with pytest.raises(SkillReadResolutionError, match="reference_skill_source_unavailable"):
        await prepare(case, kind)


@pytest.mark.parametrize("kind", ["builtin", "user", "team"])
async def test_mapped_capability_deny_cannot_be_avoided_by_exact_origin(case, kind):
    path = {"builtin": case.builtin, "user": case.personal_root, "team": case.team_root}[kind]
    manifest = path / "SKILL.md"
    manifest.write_text(manifest.read_text().replace("name: dupont", "name: python_sandbox"))
    with sqlite3.connect(case.db_path) as db:
        db.execute("UPDATE skill_instances SET name='python_sandbox' WHERE id=?", (case.personal.id,))
        db.execute("UPDATE team_skill SET name='python_sandbox',approved_sha256=? WHERE id=?", (bundle_sha256(case.team_root), case.team.id))
    case.holder.index = build_builtin_skill_metadata(builtin_root=case.builtin_root)
    identity = {"builtin": "builtin:python_sandbox", "user": case.personal.id, "team": case.team.id}[kind]
    resource = ref(kind, identity, path)
    await prepare(case, kind, resource=resource)
    capabilities = resolution.builtin_capability_ids_for_skill("python_sandbox")
    assert capabilities
    await grant(case, "builtin_capability", capabilities[0], effect="deny")
    with pytest.raises(SkillReadResolutionError):
        await prepare(case, kind, resource=resource)


async def test_policy_change_during_authorization_rejects_before_opening_material(case, monkeypatch):
    original = case.resolver.access.explain_decision

    async def decision_then_change(*args, **kwargs):
        result = await original(*args, **kwargs)
        await grant(case, "skill", "other_method")
        return result

    calls = []

    def capture(*args, **kwargs):
        calls.append(args)
        raise AssertionError("policy changed before material capture")

    monkeypatch.setattr(case.resolver.access, "explain_decision", decision_then_change)
    monkeypatch.setattr(resolution, "prepare_skill_read", capture)
    with pytest.raises(SkillReadResolutionError, match="^reference_skill_source_changed$"):
        await prepare(case, "builtin")
    assert not calls


@pytest.mark.parametrize("field", ["user_id", "team_id", "workspace_id", "policy_epoch"])
async def test_context_cannot_supply_a_different_scope_or_stale_policy(case, monkeypatch, field):
    original = case.resolver.access_factory

    async def invalid_context():
        ctx = await original()
        return dataclasses.replace(ctx, **{field: ctx.policy_epoch + 1 if field == "policy_epoch" else "other_scope"})

    monkeypatch.setattr(case.resolver, "access_factory", invalid_context)
    with pytest.raises(SkillReadResolutionError, match="^reference_scope_unavailable$"):
        await prepare(case, "builtin")


@pytest.mark.parametrize("boundary", ["context", "grant", "personal", "team", "index"])
async def test_source_failures_are_finite_and_do_not_return_partial_material(case, monkeypatch, boundary):
    async def broken(*args, **kwargs):
        raise RuntimeError("/private/root ACCOUNT_NAME PRIVATE_METHOD")

    def broken_index():
        raise RuntimeError("/private/root ACCOUNT_NAME PRIVATE_METHOD")

    target, attr = {
        "context": (case.resolver, "access_factory"),
        "grant": (case.resolver.access, "explain_decision"),
        "personal": (case.resolver.instances, "get_by_id"),
        "team": (case.resolver.users, "get_team_skill"),
        "index": (case.resolver, "builtin_index"),
    }[boundary]
    monkeypatch.setattr(target, attr, broken_index if boundary == "index" else broken)
    kind = {"personal": "user", "team": "team"}.get(boundary, "builtin")
    with pytest.raises(SkillReadResolutionError, match="^reference_skill_source_unavailable$"):
        await prepare(case, kind)


async def test_selected_version_is_never_rebound_to_current_personal_bytes(case):
    selected = ref("user", case.personal.id, case.personal_root)
    (case.personal_root / "method.md").write_text("REPLACEMENT_METHOD")
    with pytest.raises(SkillReadMaterialError, match="^reference_skill_version_changed$"):
        await prepare(case, "user", resource=selected)


@pytest.mark.parametrize("effect", ["deny", "allow"])
async def test_grant_changes_during_capture_require_fresh_context_even_if_still_allowed(case, monkeypatch, effect):
    original = resolution.prepare_skill_read

    async def change_grant():
        await grant(case, "skill", "dupont", effect=effect)

    def capture_then_change(*args, **kwargs):
        material = original(*args, **kwargs)
        anyio.from_thread.run(change_grant)
        return material

    monkeypatch.setattr(resolution, "prepare_skill_read", capture_then_change)
    with pytest.raises(SkillReadResolutionError):
        await prepare(case, "user")


@pytest.mark.parametrize("change", ["missing", "ambiguous", "version", "escape"])
async def test_unresolved_builtin_metadata_does_not_open_a_package(case, monkeypatch, change):
    index = case.holder.index
    if change == "missing":
        case.holder.index = None
    elif change == "ambiguous":
        case.holder.index = dataclasses.replace(index, items=index.items * 2)
    else:
        item = dataclasses.replace(
            index.items[0], **({"revision": "sha256:" + "0" * 64} if change == "version" else {"relative_path": "../personal"})
        )
        case.holder.index = dataclasses.replace(index, items=(item,))
    calls = []

    def capture(*args, **kwargs):
        calls.append(args)
        raise AssertionError("unresolved metadata cannot choose package IO")

    monkeypatch.setattr(resolution, "prepare_skill_read", capture)
    with pytest.raises(SkillReadResolutionError):
        await prepare(case, "builtin")
    assert not calls


@pytest.mark.parametrize("state", ["disabled", "pending", "builtin_row", "deleted"])
async def test_personal_lifecycle_is_not_replaced_by_same_named_builtin(case, state):
    with sqlite3.connect(case.db_path) as db:
        if state == "deleted":
            db.execute("DELETE FROM skill_instances WHERE id=?", (case.personal.id,))
        else:
            column, value = {"disabled": ("enabled", 0), "pending": ("import_status", "pending"), "builtin_row": ("source_type", "builtin")}[state]
            db.execute(f"UPDATE skill_instances SET {column}=? WHERE id=?", (value, case.personal.id))
    with pytest.raises(SkillReadResolutionError):
        await prepare(case, "user")


async def test_builtin_uses_index_path_and_ignores_personal_shadow_preferences(case):
    with sqlite3.connect(case.db_path) as db:
        db.execute("UPDATE skill_instances SET enabled=0,import_status='pending' WHERE id=?", (case.personal.id,))
    assert (await prepare(case, "builtin")).material.document()["files"][0]["text"] == "SELECTED_METHOD"
    await case.instances.upsert(user_id=case.scope.user_id, name="dupont", source_type="builtin", source_path="", enabled=False)
    with pytest.raises(SkillReadResolutionError):
        await prepare(case, "builtin")


@pytest.mark.parametrize("state", ["disabled", "pending", "rejected", "missing_hash", "wrong_hash", "wrong_team", "deleted"])
async def test_team_live_approval_and_identity_are_required(case, state):
    with sqlite3.connect(case.db_path) as db:
        if state == "deleted":
            db.execute("DELETE FROM team_skill WHERE id=?", (case.team.id,))
        else:
            column, value = {
                "disabled": ("status", "disabled"),
                "pending": ("approval", "pending"),
                "rejected": ("approval", "rejected"),
                "missing_hash": ("approved_sha256", None),
                "wrong_hash": ("approved_sha256", "0" * 64),
                "wrong_team": ("team_id", case.app.other_owner.team_id),
            }[state]
            db.execute(f"UPDATE team_skill SET {column}=? WHERE id=?", (value, case.team.id))
    with pytest.raises((SkillReadResolutionError, SkillReadMaterialError)):
        await prepare(case, "team")


async def test_creator_scoped_sharing_needs_real_allow_grant_not_default_allow(case, monkeypatch):
    with sqlite3.connect(case.db_path) as db:
        db.execute("UPDATE team_skill SET visibility='creator_only',approved_sha256=NULL WHERE id=?", (case.team.id,))
    with pytest.raises(SkillReadResolutionError):
        await prepare(case, "team")
    await grant(case, "team_skill", case.team.id)
    assert (await prepare(case, "team")).proof.trust_basis == "creator_scoped_current_bytes"
    monkeypatch.setenv("BOWHEAD_CONTENT_SHARING", "0")
    reset_content_sharing_cache()
    with pytest.raises(SkillReadResolutionError):
        await prepare(case, "team")


async def test_creator_allowlist_exemption_is_only_for_own_skill_not_explicit_deny(case):
    with sqlite3.connect(case.db_path) as db:
        db.execute("UPDATE team_skill SET created_by=?,visibility='creator_only',approved_sha256=NULL WHERE id=?", (case.scope.user_id, case.team.id))
    await grant(case, "team_skill", case.team.id, actor=case.app.owner.id)
    assert (await prepare(case, "team")).material.document()["files"][0]["text"] == "TEAM_METHOD"
    await grant(case, "team_skill", case.team.id, effect="deny")
    with pytest.raises(SkillReadResolutionError):
        await prepare(case, "team")


async def test_team_preference_uses_stable_id_then_legacy_team_shared_fallback(case):
    await case.instances.upsert(
        user_id=case.scope.user_id, name="dupont", source_type="user_upload", source_path=str(case.personal_root), enabled=False
    )
    await prepare(case, "team")
    await case.instances.upsert(user_id=case.scope.user_id, name="dupont", source_type="team_shared", source_path="", enabled=False)
    with pytest.raises(SkillReadResolutionError):
        await prepare(case, "team")
    await case.instances.upsert(user_id=case.scope.user_id, name="team:" + case.team.id, source_type="team_shared", source_path="", enabled=True)
    await prepare(case, "team")
    await case.instances.upsert(user_id=case.scope.user_id, name="team:" + case.team.id, source_type="team_shared", source_path="", enabled=False)
    with pytest.raises(SkillReadResolutionError):
        await prepare(case, "team")


@pytest.mark.parametrize("axis", ["workspace", "session", "team", "user_disabled", "membership_removed"])
async def test_scope_is_rechecked_before_material_io(case, monkeypatch, axis):
    scope = case.scope
    if axis in {"workspace", "session", "team"}:
        field = {"workspace": "workspace_id", "session": "session_id", "team": "team_id"}[axis]
        scope = scope.model_copy(update={field: "missing_scope"})
    else:
        with sqlite3.connect(case.db_path) as db:
            if axis == "user_disabled":
                db.execute("UPDATE users SET status='disabled' WHERE id=?", (scope.user_id,))
            else:
                db.execute("UPDATE workspace_members SET status='removed' WHERE user_id=? AND workspace_id=?", (scope.user_id, scope.workspace_id))
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("material must not be opened for an invalid scope")

    monkeypatch.setattr(resolution, "prepare_skill_read", forbidden)
    with pytest.raises(SkillReadResolutionError):
        await prepare(case, "builtin", scope=scope)
    assert not calls
