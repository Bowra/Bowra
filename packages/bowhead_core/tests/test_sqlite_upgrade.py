"""Startup upgrades keep a recoverable archive and never create missing live targets."""

from __future__ import annotations

import fcntl
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from bowhead_core.sqlite_domains import DOMAIN_LAYOUT, ORIGINAL_TARGET_FILES
from bowhead_core.sqlite_upgrade import LAYOUT_FILE, SQLiteUpgradeError, database_head, migration_lock, run_legacy_upgrade
from cryptography.fernet import Fernet

OLD = "old"
NEW = "new"
NAMES = tuple(ORIGINAL_TARGET_FILES)


def _create(path: Path, head: str, secret: bytes = b"retained") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE alembic_version(version_num TEXT NOT NULL)")
        connection.execute("INSERT INTO alembic_version VALUES(?)", (head,))
        connection.execute("CREATE TABLE retained(value BLOB)")
        connection.execute("INSERT INTO retained VALUES(?)", (secret,))


def _upgrade(_target: str, path: Path) -> None:
    if not path.exists():
        _create(path, NEW)
        return
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE alembic_version SET version_num=?", (NEW,))


def _run(directory: Path, callback=_upgrade) -> tuple[str, ...]:
    return run_legacy_upgrade(directory, NAMES, NEW, frozenset({OLD, NEW}), callback)


def _existing(tmp_path: Path) -> Path:
    directory = tmp_path / "data" / "sqlite"
    for filename in ORIGINAL_TARGET_FILES.values():
        _create(directory / filename, OLD)
    return directory


def test_fresh_initialization_and_normal_restart_do_not_create_backups(tmp_path: Path) -> None:
    directory = tmp_path / "data" / "sqlite"
    assert _run(directory) == NAMES
    state = json.loads((directory / LAYOUT_FILE).read_text())
    assert state["status"] == "ready"
    assert state["backup"] is None

    def unexpected_upgrade(_target: str, _path: Path) -> None:
        pytest.fail("A fully upgraded ordinary restart must not replay migrations")

    assert _run(directory, unexpected_upgrade) == NAMES
    assert not (directory.parent / "backups").exists()


def test_pending_upgrade_snapshots_wal_objects_privacy_and_encryption_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = _existing(tmp_path)
    key = Fernet.generate_key()
    ciphertext = Fernet(key).encrypt(b"historical private payload")
    with sqlite3.connect(directory / "bowhead.db") as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("UPDATE retained SET value=?", (ciphertext,))
    artifact = directory.parent / "artifacts" / "original.bin"
    artifact.parent.mkdir()
    artifact.write_bytes(b"unmodified artifact")
    privacy = tmp_path / "privacy-ledger" / "deletions.db"
    _create(privacy, OLD)
    _create(privacy.with_name("account-deletions.db"), OLD)
    monkeypatch.setenv("BOWHEAD_PRIVACY_LEDGER_DB", str(privacy))
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", key.decode())

    assert _run(directory) == NAMES
    state = json.loads((directory / LAYOUT_FILE).read_text())
    backup = Path(state["backup"])
    roots = json.loads((backup / "instance-secrets.json").read_text())
    with sqlite3.connect(backup / "sqlite" / "bowhead.db") as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (OLD,)
        saved = connection.execute("SELECT value FROM retained").fetchone()[0]
    assert Fernet(roots["BOWHEAD_FERNET_KEY"].encode()).decrypt(saved) == b"historical private payload"
    assert (backup / "data" / "artifacts" / "original.bin").read_bytes() == b"unmodified artifact"
    assert database_head(backup / "privacy-ledger" / "account-deletions.db") == OLD
    assert (backup / "instance-secrets.json").stat().st_mode & 0o777 == 0o600
    assert backup.stat().st_mode & 0o777 == 0o700


def test_backup_restores_chroma_metadata_wal_indexes_and_flag_history_inside_sqlite_directory(tmp_path: Path) -> None:
    directory = _existing(tmp_path)
    chroma = directory / "kb_chromadb"
    index = chroma / "segment" / "index.bin"
    index.parent.mkdir(parents=True)
    index.write_bytes(b"retained vector index bytes")
    history = directory / "flag_transitions.jsonl"
    history.write_bytes(b'{"flag":"existing","enabled":true}\n')
    (directory / ".installation-prepare.lock").touch()
    locks = directory / ".account-artifact-locks"
    locks.mkdir()
    (locks / "existing.lock").touch()
    source = sqlite3.connect(chroma / "chroma.sqlite3")
    try:
        source.execute("PRAGMA journal_mode=WAL")
        source.execute("CREATE TABLE retained_chunks(content TEXT)")
        source.execute("INSERT INTO retained_chunks VALUES ('retained committed chunk')")
        source.commit()
        committed_wal = (chroma / "chroma.sqlite3-wal").read_bytes()
        _run(directory)
        backup = Path(json.loads((directory / LAYOUT_FILE).read_text())["backup"])
        restored = backup / "data/sqlite/kb_chromadb"
        assert (restored / "chroma.sqlite3-wal").read_bytes() == committed_wal
        assert (restored / "segment/index.bin").read_bytes() == index.read_bytes()
        assert (backup / "data/sqlite/flag_transitions.jsonl").read_bytes() == history.read_bytes()
        assert {path.name for path in (backup / "data/sqlite").iterdir()} == {"kb_chromadb", "flag_transitions.jsonl"}
        with sqlite3.connect(restored / "chroma.sqlite3") as connection:
            assert connection.execute("SELECT content FROM retained_chunks").fetchall() == [("retained committed chunk",)]
    finally:
        source.close()


def test_stopped_chroma_collection_can_query_vectors_after_whole_unit_restore(tmp_path: Path) -> None:
    directory = _existing(tmp_path)
    source = directory / "kb_chromadb"
    prefix = (
        "import chromadb,json,sys\n"
        "from chromadb.config import Settings\n"
        "client=chromadb.PersistentClient(path=sys.argv[1],settings=Settings(anonymized_telemetry=False))\n"
    )
    subprocess.run(
        [
            sys.executable,
            "-c",
            prefix
            + "collection=client.get_or_create_collection('backup-vector-test')\n"
            + "collection.add(ids=['retained'],embeddings=[[1.0,0.0]],documents=['retained searchable chunk'])\n",
            str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    _run(directory)
    backup = Path(json.loads((directory / LAYOUT_FILE).read_text())["backup"])
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            prefix
            + "collection=client.get_collection('backup-vector-test')\n"
            + "print(json.dumps(collection.query(query_embeddings=[[1.0,0.0]],n_results=1)['documents']))\n",
            str(backup / "data/sqlite/kb_chromadb"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout.splitlines()[-1]) == [["retained searchable chunk"]]


def test_restart_after_committed_target_resumes_without_replaying_or_duplicate_backup(tmp_path: Path) -> None:
    directory = _existing(tmp_path)
    calls: list[str] = []

    def interrupted(target: str, path: Path) -> None:
        calls.append(target)
        _upgrade(target, path)
        raise RuntimeError("simulated process interruption after commit")

    with pytest.raises(RuntimeError, match="interruption"):
        _run(directory, interrupted)
    first = json.loads((directory / LAYOUT_FILE).read_text())
    assert first["status"] == "in_progress"
    assert calls == ["core"]

    def resumed(target: str, path: Path) -> None:
        calls.append(target)
        _upgrade(target, path)

    _run(directory, resumed)
    final = json.loads((directory / LAYOUT_FILE).read_text())
    assert calls == list(NAMES)
    assert final["status"] == "ready"
    assert final["backup"] == first["backup"]
    assert len(list(Path(first["backup"]).parent.glob("*/backup-manifest.json"))) == 1


def test_missing_live_target_is_refused_without_replacement(tmp_path: Path) -> None:
    directory = tmp_path / "data" / "sqlite"
    _run(directory)
    missing = directory / ORIGINAL_TARGET_FILES["matrix"]
    missing.unlink()
    with pytest.raises(SQLiteUpgradeError, match="missing"):
        _run(directory)
    assert not missing.exists()


def test_wrong_layout_or_unknown_source_revision_is_refused_before_mutation(tmp_path: Path) -> None:
    directory = _existing(tmp_path)
    with sqlite3.connect(directory / "bowhead.db") as connection:
        connection.execute("UPDATE alembic_version SET version_num='unknown'")
    with pytest.raises(SQLiteUpgradeError, match="Unknown source"):
        _run(directory)
    assert database_head(directory / "bowhead.db") == "unknown"
    assert not (directory.parent / "backups").exists()
    (directory / LAYOUT_FILE).write_text(json.dumps({"format": 1, "layout": DOMAIN_LAYOUT}))
    with pytest.raises(SQLiteUpgradeError, match="layout record is incomplete"):
        _run(directory)


def test_competing_migration_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "data" / "sqlite"
    with migration_lock(directory), pytest.raises(SQLiteUpgradeError, match="already owns"):
        _run(directory)
    assert not (directory / "bowhead.db").exists()


def test_backup_failure_never_runs_schema_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = _existing(tmp_path)

    def fail(*_args, **_kwargs):
        raise OSError("simulated unavailable backup storage")

    monkeypatch.setattr("bowhead_core.sqlite_upgrade._snapshot", fail)
    with pytest.raises(OSError, match="unavailable backup"):
        _run(directory)
    assert all(database_head(directory / filename) == OLD for filename in ORIGINAL_TARGET_FILES.values())
    assert json.loads((directory / LAYOUT_FILE).read_text())["status"] == "in_progress"


@pytest.mark.parametrize("target", ("core", "privacy"))
def test_actual_process_exit_recovers_hot_journal_before_upgrade_or_privacy_backup(tmp_path: Path, monkeypatch, target) -> None:
    directory = _existing(tmp_path)
    if target == "privacy":
        path = tmp_path / "privacy-ledger" / "deletions.db"
        _create(path, OLD)
        monkeypatch.setenv("BOWHEAD_PRIVACY_LEDGER_DB", str(path))
    else:
        path = directory / "bowhead.db"
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import os,sqlite3,sys; db=sqlite3.connect(sys.argv[1]); "
            "db.execute('PRAGMA cache_size=1'); db.execute('BEGIN IMMEDIATE'); "
            'db.execute("UPDATE retained SET value=zeroblob(100000)"); os._exit(0)',
            str(path),
        ],
        check=True,
    )
    assert path.with_name(path.name + "-journal").is_file()
    with pytest.raises(SQLiteUpgradeError, match="Cannot read migration ledger"):
        database_head(path)
    if target == "privacy":
        backups = directory.parent / "backups"
        backups.mkdir()
        with (backups / ".account-deletion.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(SQLiteUpgradeError, match="Account backup cleanup is active"):
                _run(directory)
            assert path.with_name(path.name + "-journal").is_file()
            assert all(database_head(directory / filename) == OLD for filename in ORIGINAL_TARGET_FILES.values())
    _run(directory)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM retained").fetchone() == (b"retained",)
    assert database_head(path) == (OLD if target == "privacy" else NEW)
    if target == "privacy":
        backup = Path(json.loads((directory / LAYOUT_FILE).read_text())["backup"])
        with sqlite3.connect(backup / "privacy-ledger" / "deletions.db") as connection:
            assert connection.execute("SELECT value FROM retained").fetchone() == (b"retained",)


def test_nested_backup_symlink_is_rejected_before_running_migrations(tmp_path: Path) -> None:
    directory = _existing(tmp_path)
    external = tmp_path / "external-private.bin"
    external.write_bytes(b"unrelated contents")
    artifacts = directory.parent / "artifacts" / "nested"
    artifacts.mkdir(parents=True)
    (artifacts / "linked.bin").symlink_to(external)
    with pytest.raises(SQLiteUpgradeError, match="symlink or special file"):
        _run(directory, lambda *_: pytest.fail("An incomplete backup cannot admit migrations"))
    assert all(database_head(directory / filename) == OLD for filename in ORIGINAL_TARGET_FILES.values())
    assert not list((directory.parent / "backups").rglob("backup-manifest.json"))


def test_targeted_upgrade_does_not_publish_a_complete_layout(tmp_path: Path) -> None:
    directory = tmp_path / "data" / "sqlite"
    assert run_legacy_upgrade(directory, ("core",), NEW, frozenset({OLD, NEW}), _upgrade) == ("core",)
    assert json.loads((directory / LAYOUT_FILE).read_text())["status"] == "in_progress"
    assert not (directory / ORIGINAL_TARGET_FILES["matrix"]).exists()
    _run(directory)
    assert json.loads((directory / LAYOUT_FILE).read_text())["status"] == "ready"


def test_custom_database_directory_does_not_archive_unrelated_siblings(tmp_path: Path) -> None:
    directory = tmp_path / "operator-selected-directory"
    _create(directory / "bowhead.db", OLD)
    unrelated = tmp_path / "another-service" / "private.bin"
    unrelated.parent.mkdir()
    unrelated.write_bytes(b"unrelated data")
    _run(directory)
    state = json.loads((directory / LAYOUT_FILE).read_text())
    backup = Path(state["backup"])
    assert not (backup / "data").exists()
    assert database_head(backup / "sqlite" / "bowhead.db") == OLD
    assert unrelated.read_bytes() == b"unrelated data"


def test_resume_rejects_backup_missing_a_versioned_database(tmp_path: Path) -> None:
    directory = _existing(tmp_path)

    def interrupted(_target: str, _path: Path) -> None:
        raise RuntimeError("simulated interruption")

    with pytest.raises(RuntimeError, match="interruption"):
        _run(directory, interrupted)
    state = json.loads((directory / LAYOUT_FILE).read_text())
    manifest_path = Path(state["backup"]) / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["databases"].pop("core")
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(SQLiteUpgradeError, match="omits a versioned database"):
        _run(directory)
    assert all(database_head(directory / filename) == OLD for filename in ORIGINAL_TARGET_FILES.values())


def test_runtime_lease_excludes_migrations_without_inventing_ready_layout(tmp_path: Path) -> None:
    from bowhead_core.sqlite_upgrade import runtime_shared_lease

    directory = tmp_path / "sqlite"
    with runtime_shared_lease(directory):
        with (
            runtime_shared_lease(directory),
            pytest.raises(SQLiteUpgradeError, match="application or a database migration"),
            migration_lock(directory),
        ):
            pytest.fail("exclusive migration entered a live data directory")
        assert not (directory / LAYOUT_FILE).exists()
    with migration_lock(directory), pytest.raises(SQLiteUpgradeError, match="migration owns"), runtime_shared_lease(directory):
        pytest.fail("application entered an active migration")
    with migration_lock(directory):
        pass


def test_runtime_lease_requires_complete_matching_managed_layout(tmp_path: Path) -> None:
    from bowhead_core.sqlite_upgrade import runtime_shared_lease

    directory = _existing(tmp_path)
    _run(directory)
    with runtime_shared_lease(directory, expected_head=NEW):
        pass
    with pytest.raises(SQLiteUpgradeError, match="differs from this application"), runtime_shared_lease(directory, expected_head="future"):
        pytest.fail("stale managed layout admitted")
    state = json.loads((directory / LAYOUT_FILE).read_text())
    state["status"] = "in_progress"
    (directory / LAYOUT_FILE).write_text(json.dumps(state))
    with pytest.raises(SQLiteUpgradeError, match="incomplete"), runtime_shared_lease(directory, expected_head=NEW):
        pytest.fail("incomplete layout admitted")
    # Failed admission must release the shared lock too.
    with migration_lock(directory):
        pass


def test_runtime_lease_rejects_actual_ledger_mismatch_and_orphan_domain(tmp_path: Path) -> None:
    from bowhead_core.sqlite_upgrade import runtime_shared_lease

    directory = _existing(tmp_path)
    _run(directory)
    with sqlite3.connect(directory / ORIGINAL_TARGET_FILES["core"]) as connection:
        connection.execute("UPDATE alembic_version SET version_num=?", (OLD,))
    with pytest.raises(SQLiteUpgradeError, match="bowhead.db"), runtime_shared_lease(directory, expected_head=NEW):
        pytest.fail("mismatching actual ledger admitted")
    (directory / LAYOUT_FILE).unlink()
    (directory / "bowhead_execution.db").touch()
    with pytest.raises(SQLiteUpgradeError, match="no matching layout record"), runtime_shared_lease(directory):
        pytest.fail("orphan domain database admitted")
    assert not (directory / LAYOUT_FILE).exists()


def test_wait_status_distinguishes_exclusive_migration_and_live_shared_lease(tmp_path) -> None:
    from bowhead_core.sqlite_upgrade import migration_wait_status, runtime_shared_lease

    directory = tmp_path / "sqlite"
    assert migration_wait_status(directory) == "unmanaged"
    with migration_lock(directory):
        assert migration_wait_status(directory) == "active"
    assert migration_wait_status(directory) == "unmanaged"
    with runtime_shared_lease(directory):
        assert migration_wait_status(directory) == "unmanaged"
    (directory / LAYOUT_FILE).write_text(json.dumps({"format": 1, "layout": "sqlite-legacy-v1", "status": "in_progress"}))
    assert migration_wait_status(directory) == "incomplete"
    (directory / LAYOUT_FILE).write_text("broken")
    assert migration_wait_status(directory) == "unknown"
