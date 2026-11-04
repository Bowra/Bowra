"""Generated whole-unit snapshots participate in account erasure and crash recovery."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from bowhead_core.account_deletion import AccountDeletionError
from bowhead_core.sqlite_domains import ORIGINAL_TARGET_FILES
from bowhead_core.sqlite_upgrade import backup_before_upgrade
from bowhead_gateway.account_backup_cleanup import verify_backup_bundle
from bowhead_gateway.account_deletion_cleanup import AccountExternalCleanup


@pytest.fixture
def generated_backup(tmp_path, monkeypatch):
    directory = tmp_path / "data" / "sqlite"
    directory.mkdir(parents=True)
    core = directory / "bowhead.db"
    with sqlite3.connect(core) as connection:
        connection.executescript(
            "CREATE TABLE alembic_version(version_num TEXT); INSERT INTO alembic_version VALUES ('backup_test'); CREATE TABLE users(id TEXT,status TEXT,email TEXT,password_hash TEXT,display_name TEXT);"
        )
        connection.execute("INSERT INTO users VALUES ('user_one','active','private@example.test','hash','Private name')")
    blob_root = directory.parent / "blobs"
    blob_root.mkdir()
    (blob_root / "private.bin").write_bytes(b"private captured bytes")
    chroma = directory / "kb_chromadb"
    chroma.mkdir()
    with sqlite3.connect(chroma / "chroma.sqlite3") as connection:
        connection.execute("CREATE TABLE private_chunks(content TEXT)")
        connection.execute("INSERT INTO private_chunks VALUES ('private captured vector text')")
    (directory / "flag_transitions.jsonl").write_bytes(b'{"flag":"existing","enabled":true}\n')
    privacy_root = tmp_path / "private-ledgers"
    privacy_root.mkdir()
    for filename in ("deletions.db", "account-deletions.db"):
        with sqlite3.connect(privacy_root / filename) as connection:
            connection.execute("CREATE TABLE protected_receipts(subject TEXT)")
    monkeypatch.setenv("BOWHEAD_PRIVACY_LEDGER_DB", str(privacy_root / "deletions.db"))
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", "synthetic-encryption-root")
    monkeypatch.setenv("BOWHEAD_JWT_SECRET", "synthetic-jwt-root")
    heads = dict.fromkeys(ORIGINAL_TARGET_FILES)
    heads["core"] = "backup_test"
    bundle = backup_before_upgrade(directory, uuid4().hex, heads)
    assert bundle is not None
    cleanup = AccountExternalCleanup(SimpleNamespace(state=SimpleNamespace(user_store=SimpleNamespace(db_path=str(core)))))
    return cleanup, bundle, privacy_root, core


@pytest.mark.asyncio
async def test_custom_database_path_uses_the_automatic_snapshot_privacy_lock(generated_backup):
    import fcntl

    cleanup, bundle, _, core = generated_backup
    assert cleanup.backup_root == core.parent.parent / "backups"
    assert bundle.is_relative_to(cleanup.backup_root)
    with (cleanup.backup_root / ".account-deletion.lock").open("a") as backup_writer:
        fcntl.flock(backup_writer, fcntl.LOCK_SH | fcntl.LOCK_NB)
        with pytest.raises(AccountDeletionError, match="account_backup_in_progress"):
            async with cleanup.guard():
                pytest.fail("Account erasure must not race a custom-path backup")


def test_generated_whole_bundle_expires_without_changing_live_ledgers_or_secrets(generated_backup, monkeypatch):
    cleanup, bundle, privacy, core = generated_backup
    verify_backup_bundle(bundle)
    assert (bundle / "instance-secrets.json").exists()
    assert (bundle / "data" / "blobs" / "private.bin").exists()
    assert (bundle / "data/sqlite/kb_chromadb/chroma.sqlite3").is_file()
    cleanup._backups("user_one")
    assert not bundle.exists()
    assert os.environ["BOWHEAD_FERNET_KEY"] == "synthetic-encryption-root"
    assert os.environ["BOWHEAD_JWT_SECRET"] == "synthetic-jwt-root"
    assert core.exists()
    assert (privacy / "deletions.db").exists() and (privacy / "account-deletions.db").exists()
    assert (core.parent.parent / "blobs" / "private.bin").read_bytes() == b"private captured bytes"
    with sqlite3.connect(core.parent / "kb_chromadb/chroma.sqlite3") as connection:
        assert connection.execute("SELECT content FROM private_chunks").fetchall() == [("private captured vector text",)]
    assert not list(cleanup.backup_root.rglob(".account-expire-*"))


def test_operational_labeled_bundle_is_registered_and_other_account_is_retained(generated_backup):
    cleanup, bundle, _, _ = generated_backup
    labeled = cleanup.backup_root / "instance-pre-release_1.2.3"
    bundle.rename(labeled)
    verify_backup_bundle(labeled)
    cleanup._backups("another_user")
    assert labeled.exists()
    cleanup._backups("user_one")
    assert not labeled.exists()


def test_archived_citation_preimages_expire_with_complete_unit_without_touching_live_history(generated_backup):
    from bowhead_core import migrations

    cleanup, bundle, _, core = generated_backup
    metadata = json.loads((Path(migrations.__file__).parent.parent / "sqlite_source_0219_compatibility.json").read_text())
    archived_tables = set(metadata["archived_tables"])
    statements = [row[3] for row in metadata["retired_objects"] if row[0] == "table" and row[1] in archived_tables]
    for path in (core, bundle / "sqlite/bowhead.db"):
        with sqlite3.connect(path) as connection:
            for statement in statements:
                connection.execute(statement)
            connection.execute("INSERT INTO citation_migration_backup VALUES('section_original',3,'private original citation','t')")
            connection.execute("INSERT INTO citation_migration_backup_revisions VALUES('revision_original',NULL,'private original revision','t')")
            connection.execute("INSERT INTO citation_migration_backup_turns VALUES('turn_original','private original answer','t')")
            connection.execute("INSERT INTO citation_migration_meta VALUES('legacy_cutoff','original timestamp')")
    verify_backup_bundle(bundle)
    cleanup._backups("user_one")
    assert not bundle.exists()
    with sqlite3.connect(core) as connection:
        assert connection.execute("SELECT old_payload FROM citation_migration_backup").fetchone() == ("private original citation",)
        assert all(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone() == (1,) for table in archived_tables)


def test_partial_bundle_deletion_retries_without_manifest(generated_backup, monkeypatch):
    cleanup, bundle, _, _ = generated_backup
    original = shutil.rmtree

    def interrupted(path, *args, **kwargs):
        (Path(path) / "backup-manifest.json").unlink()
        raise OSError("interrupted removal")

    monkeypatch.setattr(shutil, "rmtree", interrupted)
    with pytest.raises(AccountDeletionError, match="account_backup_cleanup_pending"):
        cleanup._backups("user_one")
    assert bundle.exists() and not (bundle / "backup-manifest.json").exists()
    assert len(list(cleanup.backup_root.rglob(".account-expire-*"))) == 1
    monkeypatch.setattr(shutil, "rmtree", original)
    cleanup._backups("user_one")
    assert not bundle.exists()
    assert not list(cleanup.backup_root.rglob(".account-expire-*"))


@pytest.mark.parametrize("corruption", ["unknown_member", "unknown_format", "symlink", "unknown_marker"])
def test_unknown_or_unsafe_bundle_blocks_erasure(generated_backup, corruption):
    cleanup, bundle, _, core = generated_backup
    if corruption == "unknown_member":
        (bundle / "unknown.tar.gz").write_bytes(b"unregistered archive")
    elif corruption == "unknown_format":
        state = json.loads((bundle / "backup-manifest.json").read_text())
        state["format"] = 999
        (bundle / "backup-manifest.json").write_text(json.dumps(state))
    elif corruption == "symlink":
        (bundle / "data" / "link").symlink_to(core)
    else:
        (cleanup.backup_root / ".account-expire-unknown").write_text("unregistered")
    with pytest.raises(AccountDeletionError):
        cleanup._backups("user_one")
    assert bundle.exists() and core.exists()


def test_anonymized_user_stub_does_not_hide_owned_rows_in_same_archived_db(generated_backup):
    cleanup, bundle, _, _ = generated_backup
    archived = bundle / "sqlite" / "bowhead.db"
    with sqlite3.connect(archived) as connection:
        connection.execute("UPDATE users SET status='deleted',email='stub@deleted.invalid',password_hash=NULL,display_name=NULL")
        connection.execute("CREATE TABLE account_deletions(user_id TEXT,state TEXT)")
        connection.execute("INSERT INTO account_deletions VALUES ('user_one','completed')")
        connection.execute("CREATE TABLE artifacts(id TEXT,created_by TEXT,path TEXT)")
        connection.execute("INSERT INTO artifacts VALUES ('artifact','user_one','private/path')")
    cleanup._backups("user_one")
    assert not bundle.exists()


def test_anonymized_stub_with_private_profile_field_still_expires_unit(generated_backup):
    cleanup, bundle, _, _ = generated_backup
    with sqlite3.connect(bundle / "sqlite" / "bowhead.db") as connection:
        connection.execute("UPDATE users SET status='deleted',email='stub@deleted.invalid',password_hash=NULL,display_name=NULL")
        connection.execute("ALTER TABLE users ADD COLUMN avatar_url TEXT")
        connection.execute("UPDATE users SET avatar_url='https://private.example.test/avatar'")
        connection.execute("CREATE TABLE account_deletions(user_id TEXT,state TEXT)")
        connection.execute("INSERT INTO account_deletions VALUES ('user_one','completed')")
    cleanup._backups("user_one")
    assert not bundle.exists()


def test_complete_twelve_database_bundle_and_layout_expire_as_one_unit(generated_backup):
    from bowhead_core.sqlite_domains import DOMAIN_LAYOUT, DOMAIN_TARGET_FILES

    cleanup, bundle, _, _ = generated_backup
    for filename in DOMAIN_TARGET_FILES.values():
        path = bundle / "sqlite" / filename
        if not path.exists():
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE alembic_version(version_num TEXT)")
                connection.execute("INSERT INTO alembic_version VALUES ('backup_test')")
    state = json.loads((bundle / "backup-manifest.json").read_text())
    state["heads"] = dict.fromkeys(DOMAIN_TARGET_FILES, "backup_test")
    state["databases"] = DOMAIN_TARGET_FILES
    (bundle / "backup-manifest.json").write_text(json.dumps(state))
    (bundle / "sqlite-layout.json").write_text(
        json.dumps(
            {
                "format": 1,
                "layout": DOMAIN_LAYOUT,
                "status": "ready",
                "targets": {name: {"file": filename, "head": "backup_test"} for name, filename in DOMAIN_TARGET_FILES.items()},
            }
        )
    )
    verify_backup_bundle(bundle)
    cleanup._backups("user_one")
    assert not bundle.exists()


def test_captured_opaque_data_names_are_not_interpreted_as_operational_markers(generated_backup):
    cleanup, bundle, _, _ = generated_backup
    data = bundle / "data" / "blobs"
    (data / "backup-manifest.json").write_text('{"format":999}')
    (data / (".account-expire-" + "a" * 32 + "--" + "a" * 32)).write_text("private opaque bytes")
    cleanup._backups("another_user")
    assert bundle.exists()
    cleanup._backups("user_one")
    assert not bundle.exists()
