"""Personal Skill authoring preserves packages and reconciles exact save effects."""

import sqlite3
from importlib import import_module
from pathlib import Path

import aiosqlite
import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.coach_skill_authoring import CoachSkillAuthoring
from bowhead_core.skill_instance_store import SkillInstanceStore
from bowhead_core.skill_upload import SkillUploadError, bundle_sha256
from test_agent_revision_store import _db, _seed
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

pytestmark = pytest.mark.unit
MIGRATION = import_module("bowhead_core.migrations.versions.0139_coach_skill_operations")
BODY = "---\nname: owned-method\ndescription: Summarize supplied evidence\nversion: 1.0.0\n---\nRead the supplied evidence.\n"


async def allow(db, name, write):
    assert db.in_transaction


async def setup(tmp_path):
    path = _db(tmp_path)
    _seed(path)
    store = SkillInstanceStore(path)
    await store.initialize()
    scope = ReferenceRequestScope(team_id="team_release", user_id="user_owner", workspace_id="ws_original", session_id="s_original")
    return CoachSkillAuthoring(str(path), tmp_path / "packages", scope, allow)


async def create(service):
    target = (await service.inspect(name="owned-method"))["target"]
    args = {"target": target, "changes": [{"path": "SKILL.md", "content": BODY}], "request_id": "create", "turn_id": "t_original"}
    saved = await service.save(**args)
    assert saved["ok"], saved
    return saved, args


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_migration_preserves_existing_data_and_advances_all_targets(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    after = snapshot(path)
    assert {name: after[name] for name in before} == before
    assert ("coach_skill_operation" in after) == (target == "core")
    structure = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == structure
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.revision


def test_migration_failure_restores_schema_and_version(tmp_path, monkeypatch):
    from types import SimpleNamespace

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision, "core")
    before = schema(path)
    monkeypatch.setattr(MIGRATION, "_DDL", (*MIGRATION._DDL[:2], "invalid sql"))
    with sqlite3.connect(path) as db:
        monkeypatch.setattr(
            MIGRATION,
            "context",
            SimpleNamespace(
                config=SimpleNamespace(attributes={"bowhead_target": "core"}), get_bind=lambda: SimpleNamespace(exec_driver_sql=db.execute)
            ),
        )
        with pytest.raises(sqlite3.OperationalError):
            MIGRATION.upgrade()
    assert schema(path) == before


async def test_create_replay_and_focused_patch_preserve_assets_and_preferences(tmp_path):
    service = await setup(tmp_path)
    saved, args = await create(service)
    bundle = service.root / "owned-method"
    (bundle / "assets").mkdir()
    (bundle / "assets" / "image.bin").write_bytes(b"\x00unchanged asset\xff")
    (bundle / "references").mkdir()
    (bundle / "references" / "original.md").write_text("Keep this")
    await service.instances.set_enabled(user_id=service.scope.user_id, name="owned-method", enabled=False)
    await service.instances.set_import_status(
        user_id=service.scope.user_id, name="owned-method", import_status="overridden", import_report_json='{"prior":true}'
    )
    await service.instances.set_verdict(user_id=service.scope.user_id, name="owned-method", verdict="pass")
    current = await service.inspect(name="owned-method")
    target = current["target"]
    assert current["enabled"] is False and len(current["files"]) == 3
    assert current["import_review"]["status"] == "overridden"
    patch = [{"path": "SKILL.md", "content": BODY.replace("Read the", "Carefully read the")}]
    preview = await service.validate(target=target, changes=patch)
    assert preview["saved"] is False
    report = preview["validation_report"]
    assert report["candidate_hash"] == preview["candidate_revision"]
    assert report["checks"]["import_inspection"]["status"] == "not_run"
    assert report["checks"]["business_trial"]["status"] == "not_run"
    assert report["trial_support"]["status"] == "no_standalone_executor"
    assert (bundle / "SKILL.md").read_text() == BODY
    changed = await service.save(target=target, changes=patch, request_id="edit", turn_id="t_original")
    assert changed["ok"] and changed["result"]["target"]["id"] == target["id"]
    assert (bundle / "assets" / "image.bin").read_bytes() == b"\x00unchanged asset\xff"
    assert (bundle / "references" / "original.md").read_text() == "Keep this"
    instance = await service.instances.get(user_id=service.scope.user_id, name="owned-method")
    assert instance.enabled is False and instance.verdict is None and instance.import_status == "pending"
    assert await service.save(**args) == saved, "A lost creation response must recover the old receipt after later edits"
    assert await service.get(operation_id=saved["operation_id"]) == saved
    with pytest.raises(ValueError, match="idempotency_conflict"):
        await service.save(**{**args, "changes": [{"path": "SKILL.md", "content": BODY + "Changed intent"}]})
    with pytest.raises(SkillUploadError, match="skill_write_conflict"):
        await service.read(target=target, file="SKILL.md")
    with sqlite3.connect(service.db_path) as db:
        assert db.execute("SELECT count(*) FROM skill_instances WHERE name='owned-method'").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE coach_skill_operation SET request_hash=?", ("sha256:" + "0" * 64,))
        with pytest.raises(sqlite3.IntegrityError, match="transition"):
            db.execute("UPDATE coach_skill_operation SET state='failed',result_json='{}' WHERE state='succeeded'")


class ProcessLost(BaseException):
    pass


@pytest.mark.parametrize("window", ["prepared", "backup", "published", "metadata", "committed"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_process_loss_recovers_without_republishing_or_losing_files(tmp_path, monkeypatch, window, cancel):
    service = await setup(tmp_path)
    _, _ = await create(service)
    target = (await service.inspect(name="owned-method"))["target"]
    bundle = service.root / "owned-method"
    original_rename = Path.rename
    original_upsert = SkillInstanceStore.upsert
    original_recover = CoachSkillAuthoring._recover
    original_commit = aiosqlite.Connection.commit
    moves = []

    def rename(path, destination):
        result = original_rename(path, destination)
        moves.append((path.name, Path(destination).name))
        if window == "backup" and Path(destination).name == "backup" or window == "published" and path.name == "package":
            raise ProcessLost()
        return result

    async def upsert(self, **kwargs):
        instance = await original_upsert(self, **kwargs)
        if window == "metadata":
            raise ProcessLost()
        return instance

    async def recover(self, operation_id):
        if window == "prepared":
            raise ProcessLost()
        return await original_recover(self, operation_id)

    async def commit(db):
        await original_commit(db)
        rows = await (await db.execute("SELECT state FROM coach_skill_operation WHERE turn_id='t_edit'")).fetchall()
        if window == "committed" and rows and rows[0][0] == "succeeded":
            raise ProcessLost()

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", rename)
        patch.setattr(SkillInstanceStore, "upsert", upsert)
        patch.setattr(CoachSkillAuthoring, "_recover", recover)
        patch.setattr(aiosqlite.Connection, "commit", commit)
        with pytest.raises(ProcessLost):
            await service.save(target=target, changes=[{"path": "SKILL.md", "content": BODY + "New behavior"}], request_id="edit", turn_id="t_edit")
    with sqlite3.connect(service.db_path) as db:
        operation_id = db.execute("SELECT id FROM coach_skill_operation WHERE turn_id='t_edit'").fetchone()[0]
        assert db.execute("SELECT import_status FROM skill_instances WHERE name='owned-method'").fetchone()[0] == "pending"
    restarted = CoachSkillAuthoring(service.db_path, service.root, service.scope, allow)
    if cancel:
        result = await restarted.cancel(operation_id=operation_id)
        if window in {"prepared", "backup"}:
            assert result["cancellation_status"] == "cancelled" and result["result"]["effect"] == "not_applied"
            assert (bundle / "SKILL.md").read_text() == BODY
            assert not (service.root / ".coach-operations" / operation_id).exists()
            assert await restarted.cancel(operation_id=operation_id) == result
            return
        assert result["cancellation_status"] == "already_applied"
    result = await restarted.get(operation_id=operation_id, recover=True)
    assert result["ok"], result
    assert (bundle / "SKILL.md").read_text() == BODY + "New behavior"
    assert result["result"]["target"]["revision"] == "sha256:" + bundle_sha256(bundle)
    assert await restarted.get(operation_id=operation_id, recover=True) == result
    if window in {"published", "metadata", "committed"}:
        assert len(moves) == 2, "Only the original worker published the prepared package"


async def prepare_interrupted(service, monkeypatch):
    target = (await service.inspect(name="owned-method"))["target"]

    async def crash(*args):
        raise ProcessLost()

    with monkeypatch.context() as patch:
        patch.setattr(CoachSkillAuthoring, "_recover", crash)
        with pytest.raises(ProcessLost):
            await service.save(target=target, changes=[{"path": "SKILL.md", "content": BODY + "Prepared"}], request_id="pending", turn_id="t_pending")
    with sqlite3.connect(service.db_path) as db:
        return db.execute("SELECT id FROM coach_skill_operation WHERE turn_id='t_pending'").fetchone()[0]


async def test_recovery_keeps_newer_manual_content_across_owned_chats(tmp_path, monkeypatch):
    service = await setup(tmp_path)
    await create(service)
    operation_id = await prepare_interrupted(service, monkeypatch)
    bundle = service.root / "owned-method"
    (bundle / "SKILL.md").write_text(BODY + "Newer manual content")
    result = await service.get(operation_id=operation_id, recover=True)
    assert result["outcome"] == "unknown" and result["error"] == "skill_recovery_conflict"
    assert (bundle / "SKILL.md").read_text().endswith("Newer manual content")
    assert (service.root / ".coach-operations" / operation_id / "package").is_dir()
    other = CoachSkillAuthoring(service.db_path, service.root, service.scope.model_copy(update={"session_id": "s_other"}), allow)
    recovered = await other.get(operation_id=operation_id)
    assert recovered["operation_id"] == operation_id and recovered["source_session_id"] == "s_original"
    assert (await other.cancel(operation_id=operation_id))["cancellation_status"] == "unknown"
    assert (service.root / ".coach-operations" / operation_id / "package").is_dir()
    for key in ("user_id", "team_id", "workspace_id"):
        foreign = CoachSkillAuthoring(service.db_path, service.root, service.scope.model_copy(update={key: "other"}), allow)
        with pytest.raises(ValueError, match="unavailable"):
            await foreign.get(operation_id=operation_id)


async def test_recovery_rechecks_write_authority_and_never_reuses_the_saved_permission(tmp_path, monkeypatch):
    service = await setup(tmp_path)
    await create(service)
    operation_id = await prepare_interrupted(service, monkeypatch)

    async def deny(db, name, write):
        assert write
        raise ValueError("resource_grant_denied")

    restricted = CoachSkillAuthoring(service.db_path, service.root, service.scope, deny)
    with pytest.raises(ValueError, match="resource_grant_denied"):
        await restricted.get(operation_id=operation_id, recover=True)
    with pytest.raises(ValueError, match="resource_grant_denied"):
        await restricted.cancel(operation_id=operation_id)
    assert (service.root / "owned-method" / "SKILL.md").read_text() == BODY


async def test_read_chunks_and_invalid_patch_cannot_escape_or_drop_assets(tmp_path):
    service = await setup(tmp_path)
    saved, _ = await create(service)
    target = saved["result"]["target"]
    first = await service.read(target=target, file="SKILL.md", limit=20)
    second = await service.read(target=target, file="SKILL.md", offset=first["next_offset"])
    assert first["content"] + second["content"] == BODY
    for path in ("../outside", "scripts/../../outside", "assets/new.txt"):
        with pytest.raises(ValueError):
            await service.validate(target=target, changes=[{"path": path, "content": "must not write"}])
    with pytest.raises(ValueError, match="manifest_required"):
        await service.validate(target=target, changes=[{"path": "SKILL.md", "content": None}])
    assert (service.root / "owned-method" / "SKILL.md").read_text() == BODY


async def test_reads_proceed_alongside_another_reader_but_not_a_writer(tmp_path):
    # Coach issues parallel file reads; a concurrent reader must not surface as skill_write_conflict.
    from bowhead_core.skill_bundle_lock import skill_bundle_lock

    service = await setup(tmp_path)
    saved, _ = await create(service)
    target = saved["result"]["target"]
    package = service.root / "owned-method"
    with skill_bundle_lock(package, shared=True):
        assert (await service.read(target=target, file="SKILL.md"))["content"] == BODY
        assert (await service.inspect(name="owned-method"))["target"] == target
    with skill_bundle_lock(package), pytest.raises(SkillUploadError, match="skill_write_conflict"):
        await service.read(target=target, file="SKILL.md")


async def test_new_package_publication_before_sql_commit_recovers_creation_once(tmp_path, monkeypatch):
    service = await setup(tmp_path)
    original = SkillInstanceStore.upsert

    async def interrupted(self, **kwargs):
        await original(self, **kwargs)
        raise ProcessLost()

    with monkeypatch.context() as patch:
        patch.setattr(SkillInstanceStore, "upsert", interrupted)
        with pytest.raises(ProcessLost):
            await create(service)
    with sqlite3.connect(service.db_path) as db:
        operation_id = db.execute("SELECT id FROM coach_skill_operation").fetchone()[0]
        assert db.execute("SELECT count(*) FROM skill_instances").fetchone()[0] == 0
    result = await service.get(operation_id=operation_id, recover=True)
    assert result["ok"] and result["result"]["target"]["id"].startswith("sk_")
    assert not (service.root / ".coach-operations" / operation_id).exists()
    assert await service.get(operation_id=operation_id, recover=True) == result


async def test_import_inspection_is_bound_to_saved_revision(tmp_path):
    from bowhead_core.skill_import.hook import apply_import_inventory
    from bowhead_core.skill_import.inventory import ResolutionSources

    service = await setup(tmp_path)
    saved, _ = await create(service)
    path = service.root / "owned-method"
    (path / "SKILL.md").write_text(BODY + "A newer change")
    result = await apply_import_inventory(
        service.instances,
        user_id=service.scope.user_id,
        name="owned-method",
        skill_dir=path,
        sources=ResolutionSources(),
        expected_content_hash=saved["result"]["target"]["revision"],
    )
    assert result is None
    assert (await service.instances.get(user_id=service.scope.user_id, name="owned-method")).import_status == "pending"


async def test_new_request_cannot_bypass_an_unresolved_save(tmp_path, monkeypatch):
    service = await setup(tmp_path)
    await create(service)
    operation_id = await prepare_interrupted(service, monkeypatch)
    target = (await service.inspect(name="owned-method"))["target"]
    with pytest.raises(ValueError, match="pending_save_requires_recovery"):
        await service.save(
            target=target, changes=[{"path": "SKILL.md", "content": BODY + "Another attempt"}], request_id="another", turn_id="t_later"
        )
    assert (await service.get(operation_id=operation_id, recover=True))["ok"]


async def test_cancel_restores_exact_unpublished_review_and_releases_pending_slot(tmp_path, monkeypatch):
    service = await setup(tmp_path)
    await create(service)
    await service.instances.set_import_status(
        user_id=service.scope.user_id, name="owned-method", import_status="overridden", import_report_json='{"prior":true}'
    )
    await service.instances.set_verdict(user_id=service.scope.user_id, name="owned-method", verdict="pass")
    operation_id = await prepare_interrupted(service, monkeypatch)
    original_commit = aiosqlite.Connection.commit
    checked = []

    async def commit_under_package_lock(db):
        state = await (await db.execute("SELECT state FROM coach_skill_operation WHERE id=?", (operation_id,))).fetchone()
        if db.in_transaction and state and state[0] == "failed":
            from bowhead_core.skill_bundle_lock import skill_bundle_lock

            with pytest.raises(SkillUploadError, match="skill_write_conflict"), skill_bundle_lock(service.root / "owned-method"):
                raise AssertionError("A competing writer acquired the package before cancellation committed")
            checked.append(True)
        await original_commit(db)

    with monkeypatch.context() as patch:
        patch.setattr(aiosqlite.Connection, "commit", commit_under_package_lock)
        result = await service.cancel(operation_id=operation_id)
    assert checked and result["cancellation_status"] == "cancelled"
    instance = await service.instances.get(user_id=service.scope.user_id, name="owned-method")
    assert instance.import_status == "overridden" and instance.import_report_json == '{"prior":true}' and instance.verdict == "pass"
    target = (await service.inspect(name="owned-method"))["target"]
    saved = await service.save(
        target=target, changes=[{"path": "SKILL.md", "content": BODY + "New deliberate change"}], request_id="after-cancel", turn_id="t_new"
    )
    assert saved["ok"] and saved["operation_id"] != operation_id
    assert (await service.get(operation_id=operation_id))["result"]["effect"] == "not_applied"


async def test_cancel_reserved_creation_does_not_create_package_or_metadata(tmp_path, monkeypatch):
    service = await setup(tmp_path)
    target = (await service.inspect(name="owned-method"))["target"]

    def interrupted(*args):
        raise ProcessLost()

    with monkeypatch.context() as patch:
        patch.setattr(CoachSkillAuthoring, "_stage", interrupted)
        with pytest.raises(ProcessLost):
            await service.save(target=target, changes=[{"path": "SKILL.md", "content": BODY}], request_id="reserved", turn_id="t_reserved")
    with sqlite3.connect(service.db_path) as db:
        operation_id = db.execute("SELECT id FROM coach_skill_operation WHERE turn_id='t_reserved'").fetchone()[0]
    assert (await service.cancel(operation_id=operation_id))["cancellation_status"] == "cancelled"
    assert not (service.root / "owned-method").exists()
    assert await service.instances.get(user_id=service.scope.user_id, name="owned-method") is None
