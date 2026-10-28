"""`0124_file_category_codes`：存量中文文件分类改写成语言无关的代码值。"""

from __future__ import annotations

import sqlite3
from importlib import import_module

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.artifact_classifier import CATEGORIES, ZH_LABEL_TO_CATEGORY
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

MIGRATION = import_module("bowhead_core.migrations.versions.0124_file_category_codes")
pytestmark = pytest.mark.integration

# Compare against the frozen migration mapping entry by entry, without importing it; a missing migration value must fail here.
_EXPECTED = {
    "汇报材料": "report",
    "数据明细": "dataset",
    "参考资料": "reference",
    "会议纪要": "meeting_notes",
    "口径定义": "metric_definition",
    "截图": "screenshot",
    "其他": "other",
}


def _insert(db: sqlite3.Connection, artifact_id: str, category: str | None, source: str | None = "llm") -> None:
    db.execute(
        "INSERT INTO artifacts(id,filename,mime,size,source,path,created_at,category,category_source) VALUES (?,?,?,?,?,?,?,?,?)",
        (artifact_id, f"{artifact_id}.pdf", "application/pdf", 1, "upload", f"/f/{artifact_id}", "2026-09-01T00:00:00Z", category, source),
    )


def _categories(path) -> dict[str, tuple[str | None, str | None]]:
    with sqlite3.connect(path) as db:
        return {row[0]: (row[1], row[2]) for row in db.execute("SELECT id,category,category_source FROM artifacts")}


def test_expected_mapping_matches_application_enum() -> None:
    assert dict(ZH_LABEL_TO_CATEGORY) == _EXPECTED
    assert set(CATEGORIES) == set(_EXPECTED.values())


def test_upgrade_rewrites_legacy_labels_keeps_unknown_and_null_and_is_idempotent(tmp_path) -> None:
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        for index, label in enumerate(_EXPECTED):
            _insert(db, f"art_old_{index}", label, "user" if index % 2 else "llm")
        _insert(db, "art_code", "report", "user")
        _insert(db, "art_unknown", "季度复盘", "user")
        _insert(db, "art_padded", " 截图 ", "rule")
        _insert(db, "art_null", None, None)
        _insert(db, "art_empty", "", None)
    _upgrade(path, MIGRATION.revision)
    after = _categories(path)
    for index, (label, code) in enumerate(_EXPECTED.items()):
        assert after[f"art_old_{index}"] == (code, "user" if index % 2 else "llm"), label
    assert after["art_code"] == ("report", "user")
    assert after["art_unknown"] == ("季度复盘", "user")
    assert after["art_padded"] == (" 截图 ", "rule")
    assert after["art_null"] == (None, None)
    assert after["art_empty"] == ("", None)

    before_repeat = snapshot(path)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM alembic_version")
        db.execute("INSERT INTO alembic_version(version_num) VALUES (?)", (MIGRATION.down_revision,))
    _upgrade(path, MIGRATION.revision)
    assert snapshot(path) == before_repeat


async def test_migrated_codes_are_what_the_store_filter_and_writer_use(tmp_path) -> None:
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        _insert(db, "art_deck", "汇报材料")
        db.execute("UPDATE artifacts SET created_by='u1', kind='user_upload', status='ready' WHERE id='art_deck'")
    _upgrade(path, "head")
    store = ArtifactStore(backend=LocalBackend(root_dir=tmp_path / "files"), db_path=path)
    await store.initialize()
    items, total, _ = await store.list_for_user(user_id="u1", category="report", sources=None, limit=10, offset=0)
    assert total == 1 and items[0].id == "art_deck"
    await store.set_category("art_deck", category="数据明细", source="user")
    assert (await store.get("art_deck")).category == "dataset"


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_other_targets_are_no_ops_and_core_schema_is_unchanged(tmp_path, target) -> None:
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before, previous = snapshot(path), schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert snapshot(path) == before
    assert schema(path) == previous


def test_core_without_artifacts_table_is_a_no_op(tmp_path) -> None:
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='artifacts'").fetchall():
            db.execute(f'DROP TRIGGER "{name}"')
        db.execute("ALTER TABLE artifacts RENAME TO artifacts_parked")
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision)
    assert snapshot(path) == before


def test_failed_rewrite_rolls_back_every_value(tmp_path, monkeypatch) -> None:
    from sqlalchemy.engine import Connection

    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        for index, label in enumerate(_EXPECTED):
            _insert(db, f"art_old_{index}", label)
    before = snapshot(path)
    original = Connection.exec_driver_sql
    updates = 0

    def fail(self, statement, *args, **kwargs):
        nonlocal updates
        result = original(self, statement, *args, **kwargs)
        if statement.startswith("UPDATE artifacts"):
            updates += 1
            if updates == 3:
                raise RuntimeError("injected category rewrite failure")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", fail)
    with pytest.raises(RuntimeError, match="injected category rewrite failure"):
        _upgrade(path, MIGRATION.revision)
    assert updates == 3
    assert snapshot(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)


def _stamp_version(path) -> str:
    with sqlite3.connect(path) as db:
        return db.execute("SELECT version_num FROM alembic_version").fetchone()[0]


def _insert_dirty(db: sqlite3.Connection, table: str, sql: str, params: tuple) -> None:
    """造脏行：这些状态本来就被表上的 INSERT 守卫拦着（正因如此才叫脏行）。

    只在造夹具的这一条 INSERT 期间摘掉该表的触发器，随后原样装回，迁移跑的是完整的守卫。
    """
    triggers = db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)).fetchall()
    for name, _ in triggers:
        db.execute(f'DROP TRIGGER "{name}"')
    db.execute(sql, params)
    for _, ddl in triggers:
        db.execute(ddl)


def test_live_artifact_with_purge_claim_fails_readably_and_changes_nothing(tmp_path) -> None:
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        _insert(db, "art_dirty", "汇报材料")
        _insert(db, "art_ok", "数据明细")
        # Also has a purge claim, but its category is outside the 7 Chinese values; UPDATE never touches it, so it does not count.
        _insert(db, "art_unknown_claimed", "季度复盘")
        for aid in ("art_dirty", "art_unknown_claimed"):
            _insert_dirty(
                db,
                "artifact_purge_claims",
                "INSERT INTO artifact_purge_claims(artifact_id,path,claimed_at) VALUES (?,?,?)",
                (aid, f"/f/{aid}", "2026-09-01"),
            )
    before = snapshot(path)
    with pytest.raises(RuntimeError) as err:
        _upgrade(path, MIGRATION.revision)
    message = str(err.value)
    assert "artifact_purge_claims" in message and "art_dirty" in message
    assert "art_unknown_claimed" not in message and "art_ok" not in message
    assert "0123" in message
    assert snapshot(path) == before
    assert _stamp_version(path) == MIGRATION.down_revision


def test_live_artifact_with_claimed_document_copy_fails_readably_and_changes_nothing(tmp_path) -> None:
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        _insert(db, "art_copy", "参考资料")
        _insert_dirty(
            db,
            "document_input_copies",
            "INSERT INTO document_input_copies(artifact_id,job_id,input_index,source_artifact_id,source_path,path,filename,mime,"
            "size_bytes,sha256,role,cleanup_claimed_at,cleanup_after) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("art_copy", "job_1", 0, "art_src", "/f/src", "/f/copy", "copy.pdf", "application/pdf", 1, "0" * 64, "reference", "2026-09-01", 0.0),
        )
    before = snapshot(path)
    with pytest.raises(RuntimeError) as err:
        _upgrade(path, MIGRATION.revision)
    message = str(err.value)
    assert "document_input_copies" in message and "cleanup_claimed_at" in message and "art_copy" in message
    assert snapshot(path) == before
    assert _stamp_version(path) == MIGRATION.down_revision


def test_trashed_artifact_with_purge_claim_is_rewritten(tmp_path) -> None:
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        _insert(db, "art_trashed", "截图")
        db.execute("UPDATE artifacts SET deleted_at='2026-09-01' WHERE id='art_trashed'")
        _insert_dirty(
            db,
            "artifact_purge_claims",
            "INSERT INTO artifact_purge_claims(artifact_id,path,claimed_at) VALUES (?,?,?)",
            ("art_trashed", "/f/art_trashed", "2026-09-02"),
        )
    _upgrade(path, MIGRATION.revision)
    assert _categories(path)["art_trashed"] == ("screenshot", "llm")
    assert _stamp_version(path) == MIGRATION.revision
