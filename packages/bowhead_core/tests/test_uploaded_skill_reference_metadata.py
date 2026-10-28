"""Real uploaded packages and database records, cached without executable data."""

from __future__ import annotations

import asyncio
import dataclasses
import sqlite3
from types import SimpleNamespace

import anyio
import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.access_control import AccessControlStore
from bowhead_core.chat_reference_catalog import ReferenceCatalogRequest, read_reference_catalog
from bowhead_core.skill_instance_store import SkillInstanceStore
from bowhead_core.skill_reference_metadata import build_builtin_skill_metadata
from bowhead_core.skill_upload import bundle_sha256
from bowhead_core.space_governance import SpaceOperation
from bowhead_core.uploaded_skill_reference_metadata import UploadedSkillMetadataCache, build_uploaded_skill_metadata, uploaded_skill_metadata_rows
from bowhead_core.user_store import TeamSkill
from bowhead_gateway.access_context import build_access_context
from test_agent_execution_binding import _setup
from test_skill_reference_metadata import _skill

from bowhead_core import uploaded_skill_reference_metadata as metadata

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def uploaded_case(tmp_path, monkeypatch):
    monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    fixture = await _setup(tmp_path)
    instances = SkillInstanceStore(fixture.db_path)
    await instances.initialize()
    access = AccessControlStore(fixture.db_path)
    await access.initialize()
    personal_path = _skill(tmp_path / "personal")
    personal = await instances.upsert(
        user_id=fixture.member_id, name="dupont", source_type="user_upload", source_path=str(personal_path), version="1.2.3"
    )
    other_path = _skill(tmp_path / "other")
    other_path.joinpath("SKILL.md").write_text(
        other_path.joinpath("SKILL.md").read_text().replace("Profitability method", "OTHER_USER_PRIVATE_SUMMARY")
    )
    other = await instances.upsert(user_id=fixture.owner_id, name="dupont", source_type="user_generated", source_path=str(other_path))
    team_path = _skill(tmp_path / "team")
    team = await fixture.users.create_team_skill(
        TeamSkill(
            id="oskill_dupont",
            team_id=fixture.team_id,
            name="dupont",
            display_name="团队杜邦",
            description="团队盈利分析",
            source_path=str(team_path),
            visibility="team_wide",
            created_by=fixture.owner_id,
            approved_sha256=bundle_sha256(team_path),
        )
    )
    _skill(tmp_path / "builtin")
    builtin = build_builtin_skill_metadata(builtin_root=tmp_path / "builtin")

    async def access_factory():
        context = await build_access_context(
            user_store=fixture.users,
            access_store=access,
            user_id=fixture.member_id,
            workspace_id=fixture.workspace_id,
            expected_team_id=fixture.team_id,
            operation=SpaceOperation.READ_HISTORY,
        )
        return access, context

    return SimpleNamespace(
        fixture=fixture,
        instances=instances,
        access=access,
        personal=personal,
        other=other,
        team=team,
        personal_path=personal_path,
        team_path=team_path,
        builtin=builtin,
        access_factory=access_factory,
    )


async def read(case, index, **patch):
    return await read_reference_catalog(
        case.fixture.db_path,
        team_id=case.fixture.team_id,
        user_id=case.fixture.member_id,
        query=ReferenceCatalogRequest(workspace_id=case.fixture.workspace_id, trigger="/", **patch),
        builtin_skill_index=case.builtin,
        builtin_skill_access=case.access_factory,
        uploaded_skill_index=index,
    )


async def test_exact_metadata_and_same_named_sources_remain_separate(uploaded_case):
    case = uploaded_case
    index = await build_uploaded_skill_metadata(case.fixture.db_path)
    assert len(index.items) == 3
    personal = next(item for item in index.items if item.id == case.personal.id)
    assert personal.revision == "sha256:" + bundle_sha256(case.personal_path) and personal.revision != "1.2.3"
    changed = personal.parameter_schema()
    changed["properties"]["company"]["default"] = "changed"
    assert personal.parameter_schema()["properties"]["company"]["default"] == "Example"
    with pytest.raises(dataclasses.FrozenInstanceError):
        personal.revision = "changed"
    wire = repr(index)
    assert "PRIVATE_BODY_NOT_IN_METADATA" not in wire and str(case.personal_path) not in wire and "MUST_NOT_EXECUTE" not in wire
    response = await read(case, index)
    items = response.groups[0].items
    assert {(item.source_kind, item.id) for item in items} == {("builtin", "builtin:dupont"), ("user", case.personal.id), ("team", case.team.id)}
    assert "OTHER_USER_PRIVATE_SUMMARY" not in response.model_dump_json() and case.other.id not in response.model_dump_json()
    assert all(item.resource and item.read_only_parameter_schema and item.reason_code == "unsupported_entry" for item in items)
    assert all(not cell.supported for cell in response.entry_support)


async def test_catalog_uses_cached_bytes_and_current_database_without_opening_files(uploaded_case, monkeypatch):
    case = uploaded_case
    index = await build_uploaded_skill_metadata(case.fixture.db_path)

    def no_capture(*args, **kwargs):
        raise AssertionError("request-time uploaded package capture")

    with monkeypatch.context() as guard:
        guard.setattr(metadata, "_capture", no_capture)
        guard.setattr(metadata, "_build_metadata", no_capture)
        case.personal_path.joinpath("SKILL.md").unlink()
        result = await read(case, index)
        assert next(item for item in result.groups[0].items if item.id == case.personal.id).resource is not None
    # The next background capture detects missing bytes; it cannot retain the
    # old entry indefinitely or turn the cache into preparation authority.
    rebuilt = await build_uploaded_skill_metadata(case.fixture.db_path)
    assert len(rebuilt.items) == 2 and all(item.id != case.personal.id for item in rebuilt.items)
    assert next(item for item in (await read(case, rebuilt)).groups[0].items if item.id == case.personal.id).resource is None


@pytest.mark.parametrize("change", ["pending", "disabled", "path", "approved_hash", "approval", "team_preference"])
async def test_live_source_changes_invalidate_metadata_or_usage_without_refresh(uploaded_case, change):
    case = uploaded_case
    index = await build_uploaded_skill_metadata(case.fixture.db_path)
    first = await read(case, index)
    target = case.personal.id if change in {"pending", "disabled", "path"} else case.team.id
    if change == "pending":
        await case.instances.set_import_status(user_id=case.fixture.member_id, name="dupont", import_status="pending", import_report_json="{}")
    elif change == "team_preference":
        await case.instances.upsert(
            user_id=case.fixture.member_id, name="team:" + case.team.id, source_type="team_shared", source_path="", enabled=False
        )
    else:
        with sqlite3.connect(case.fixture.db_path) as db:
            statements = {
                "disabled": ("UPDATE skill_instances SET enabled=0 WHERE id=?", case.personal.id),
                "path": ("UPDATE skill_instances SET source_path='replaced-source' WHERE id=?", case.personal.id),
                "approved_hash": ("UPDATE team_skill SET approved_sha256='" + "0" * 64 + "' WHERE id=?", case.team.id),
                "approval": ("UPDATE team_skill SET approval='pending' WHERE id=?", case.team.id),
            }
            sql, value = statements[change]
            db.execute(sql, (value,))
    second = await read(case, index)
    item = next(item for item in second.groups[0].items if item.id == target)
    assert item.resource is None and item.read_only_parameter_schema is None
    assert second.catalog_generation != first.catalog_generation
    assert next(item for item in second.groups[0].items if item.id == "builtin:dupont").resource is not None


async def test_pending_update_slot_does_not_replace_approved_team_metadata(uploaded_case):
    case = uploaded_case
    index = await build_uploaded_skill_metadata(case.fixture.db_path)
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute(
            "UPDATE team_skill SET candidate_path='PRIVATE_CANDIDATE',candidate_sha256=?,candidate_state='pending',updated_at='later' WHERE id=?",
            ("0" * 64, case.team.id),
        )
    item = next(item for item in (await read(case, index)).groups[0].items if item.id == case.team.id)
    assert item.resource.version == "sha256:" + bundle_sha256(case.team_path)
    assert "PRIVATE_CANDIDATE" not in item.model_dump_json()


@pytest.mark.parametrize("kind", ["user", "team"])
async def test_current_grants_filter_before_returning_cached_metadata(uploaded_case, kind):
    case = uploaded_case
    index = await build_uploaded_skill_metadata(case.fixture.db_path)
    await case.access.create_or_update_resource_grant(
        team_id=case.fixture.team_id,
        resource_kind="team_skill" if kind == "team" else "skill",
        resource_id=case.team.id if kind == "team" else "dupont",
        principal_type="user",
        principal_id=case.fixture.member_id,
        permission="use",
        effect="deny",
    )
    response = await read(case, index)
    assert (case.team.id if kind == "team" else case.personal.id) not in response.model_dump_json()
    assert case.other.id not in response.model_dump_json()


async def test_team_visibility_filters_even_an_existing_cached_exact_revision(uploaded_case):
    case = uploaded_case
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("UPDATE team_skill SET visibility='private' WHERE id=?", (case.team.id,))
    index = await build_uploaded_skill_metadata(case.fixture.db_path)
    assert any(item.id == case.team.id for item in index.items)
    assert case.team.id not in (await read(case, index)).model_dump_json()


async def test_capture_rechecks_record_state_before_publishing_index(uploaded_case, monkeypatch):
    case = uploaded_case
    original = metadata._build_metadata

    def changed(kind, row):
        item = original(kind, row)
        if row["id"] == case.personal.id:
            with sqlite3.connect(case.fixture.db_path) as db:
                db.execute("UPDATE skill_instances SET enabled=0 WHERE id=?", (case.personal.id,))
        return item

    monkeypatch.setattr(metadata, "_build_metadata", changed)
    index = await build_uploaded_skill_metadata(case.fixture.db_path)
    assert all(item.id != case.personal.id for item in index.items)
    assert any(item.id == case.team.id for item in index.items)


async def test_cache_age_and_real_background_cancellation(uploaded_case, monkeypatch):
    case = uploaded_case
    now = [100.0]
    monkeypatch.setattr(metadata, "monotonic", lambda: now[0])
    cache = UploadedSkillMetadataCache(case.fixture.db_path)
    assert cache.current() is None
    completed = anyio.Event()
    original = cache.refresh

    async def refreshed():
        await original()
        completed.set()

    monkeypatch.setattr(cache, "refresh", refreshed)
    task = asyncio.create_task(cache.run())
    try:
        with anyio.fail_after(10):
            await completed.wait()
        assert len(cache.current().items) == 3
        now[0] = 130.0
        assert cache.current() is None
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_scoped_row_reader_preserves_transaction_and_excludes_other_person(uploaded_case):
    case = uploaded_case
    async with open_shared_ctx(case.fixture.db_path) as db:
        with pytest.raises(ValueError, match="transaction_required"):
            await uploaded_skill_metadata_rows(db)
        await db.execute("BEGIN")
        rows = await uploaded_skill_metadata_rows(db, user_id=case.fixture.member_id, team_id=case.fixture.team_id)
        assert {(kind, row["id"]) for kind, row in rows} == {("user", case.personal.id), ("team", case.team.id)}
        assert db.in_transaction
        await db.rollback()


async def test_changed_script_bytes_refresh_personal_hash_but_need_team_approval(uploaded_case):
    case = uploaded_case
    first = await build_uploaded_skill_metadata(case.fixture.db_path)
    for root in (case.personal_path, case.team_path):
        root.joinpath("scripts/unused.py").write_text("raise RuntimeError('NEW_SCRIPT_MUST_NOT_RUN')")
    second = await build_uploaded_skill_metadata(case.fixture.db_path)
    assert all(item.id != case.team.id for item in second.items)
    old = next(item for item in first.items if item.id == case.personal.id)
    current = next(item for item in second.items if item.id == case.personal.id)
    assert current.revision != old.revision and current.revision == "sha256:" + bundle_sha256(case.personal_path)
    response = await read(case, second)
    assert next(item for item in response.groups[0].items if item.id == case.team.id).resource is None


async def test_ordinary_sop_has_exact_metadata_without_inventing_parameters(uploaded_case):
    case = uploaded_case
    case.personal_path.joinpath("SKILL.md").write_text("---\nname: dupont\ndescription: Ordinary SOP\n---\nRead the ordinary instructions.\n")
    index = await build_uploaded_skill_metadata(case.fixture.db_path)
    item = next(item for item in (await read(case, index)).groups[0].items if item.id == case.personal.id)
    assert item.resource.version == "sha256:" + bundle_sha256(case.personal_path)
    assert item.read_only_parameter_schema == {"type": "object", "properties": {}, "additionalProperties": False}
    assert item.summary == "Ordinary SOP" and item.reason_code == "unsupported_entry"


async def test_team_creator_allowlist_exemption_does_not_bypass_explicit_deny(uploaded_case):
    case = uploaded_case
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("UPDATE team_skill SET created_by=? WHERE id=?", (case.fixture.member_id, case.team.id))
    await case.access.create_or_update_resource_grant(
        team_id=case.fixture.team_id,
        resource_kind="team_skill",
        resource_id=case.team.id,
        principal_type="user",
        principal_id=case.fixture.owner_id,
        permission="use",
        effect="allow",
    )
    index = await build_uploaded_skill_metadata(case.fixture.db_path)
    assert next(item for item in (await read(case, index)).groups[0].items if item.id == case.team.id).resource is not None
    await case.access.create_or_update_resource_grant(
        team_id=case.fixture.team_id,
        resource_kind="team_skill",
        resource_id=case.team.id,
        principal_type="user",
        principal_id=case.fixture.member_id,
        permission="use",
        effect="deny",
    )
    assert case.team.id not in (await read(case, index)).model_dump_json()


async def test_other_team_metadata_is_never_returned_even_when_cached(uploaded_case, tmp_path):
    case = uploaded_case
    team = await case.fixture.users.create_team(name="Other Team")
    owner = await case.fixture.users.create_user(team_id=team.id, email="foreign@example.com")
    path = _skill(tmp_path / "foreign")
    await case.fixture.users.create_team_skill(
        TeamSkill(
            id="oskill_foreign",
            team_id=team.id,
            name="dupont",
            description="FOREIGN_TEAM_PRIVATE_SUMMARY",
            source_path=str(path),
            visibility="team_wide",
            created_by=owner.id,
            approved_sha256=bundle_sha256(path),
        )
    )
    index = await build_uploaded_skill_metadata(case.fixture.db_path)
    assert any(item.id == "oskill_foreign" for item in index.items)
    response = await read(case, index)
    assert "oskill_foreign" not in response.model_dump_json() and "FOREIGN_TEAM_PRIVATE_SUMMARY" not in response.model_dump_json()
