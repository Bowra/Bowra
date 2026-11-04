"""Exclusive, durable startup upgrades for the existing SQLite target set."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import time
from contextlib import closing, contextmanager
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import structlog

from bowhead_core.sqlite_domains import DOMAIN_LAYOUT, DOMAIN_TARGET_FILES, LEGACY_LAYOUT, ORIGINAL_TARGET_FILES

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

log = structlog.get_logger(__name__)
LAYOUT_FILE = "sqlite-layout.json"


class SQLiteUpgradeError(RuntimeError):
    """An unsafe or incomplete startup upgrade must not start the application."""


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            temporary.chmod(0o600)
            json.dump(value, stream, ensure_ascii=True, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def migration_lock(directory: Path) -> Iterator[None]:
    """Refuse competing startup/CLI migrations; do not pretend to stop old writers."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".sqlite-migration.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SQLiteUpgradeError(
                "The application or a database migration already owns this data directory; stop the application and wait for migrations to finish."
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


@contextmanager
def runtime_shared_lease(directory: Path, *, expected_head: str | None = None) -> Iterator[None]:
    """Hold migration exclusion for the entire live application lifetime.

    A missing layout remains unmanaged legacy development state. It is never
    promoted to ready here; managed installations must present every verified
    target at the current image revision before any Store opens a writer.
    """
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".sqlite-migration.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SQLiteUpgradeError("A database migration owns this data directory; wait for it to finish before starting the application.") from exc
        try:
            state = _load_state(directory)
            if state is None:
                if any((directory / filename).exists() for name, filename in DOMAIN_TARGET_FILES.items() if name not in ORIGINAL_TARGET_FILES):
                    raise SQLiteUpgradeError(
                        "Domain databases have no matching layout record; recover the matching record before starting the application."
                    )
                log.info("sqlite.runtime_unmanaged_legacy", directory=str(directory))
            else:
                if state["status"] != "ready":
                    raise SQLiteUpgradeError(
                        "The database migration is incomplete; stop the application and run the complete startup migration before restarting."
                    )
                if expected_head is None:
                    from bowhead_core.migrations import chain_head

                    expected_head = chain_head()
                if state.get("target_head") != expected_head:
                    raise SQLiteUpgradeError(
                        "The database layout revision differs from this application image; run its startup migration before starting the application."
                    )
                for name, filename in (DOMAIN_TARGET_FILES if state["layout"] == DOMAIN_LAYOUT else ORIGINAL_TARGET_FILES).items():
                    recorded = state["targets"][name].get("head")
                    actual = database_head(directory / filename)
                    if recorded != expected_head or actual != expected_head:
                        raise SQLiteUpgradeError(
                            f"Database {filename} is not at the verified application revision; run the complete startup migration before restarting."
                        )
            if state is not None and state["layout"] == DOMAIN_LAYOUT:
                from bowhead_core.sqlite_domain_upgrade import _validate_ready

                _validate_ready(directory, state)
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def database_head(path: Path) -> str | None:
    """Read a ledger without creating a missing database."""
    if not path.exists():
        return None
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            if connection.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='alembic_version'").fetchone() is None:
                return None
            rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
            if not rows:
                return None
            if len(rows) != 1 or not isinstance(rows[0][0], str):
                raise SQLiteUpgradeError(f"Invalid migration ledger for {path.name}; inspect the database before restarting.")
            return rows[0][0]
    except sqlite3.DatabaseError as exc:
        raise SQLiteUpgradeError(f"Cannot read migration ledger for {path.name}; inspect the database and storage.") from exc


def _recover_rollback_journal(path: Path) -> None:
    """Let SQLite roll back an interrupted transaction under the exclusive upgrade lease."""
    if not path.is_file() or not path.with_name(path.name + "-journal").is_file():
        return
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)) as connection:
            # A schema read triggers SQLite's own hot-journal recovery. Read-only
            # ledger probes cannot recover a process killed before its commit.
            connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
    except sqlite3.DatabaseError as exc:
        raise SQLiteUpgradeError(f"Cannot recover the interrupted transaction for {path.name}; stop other writers and inspect storage.") from exc


def _load_state(directory: Path) -> dict[str, object] | None:
    path = directory / LAYOUT_FILE
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SQLiteUpgradeError("The SQLite layout record is unreadable; recover its matching record before restarting.") from exc
    if not isinstance(state, dict) or state.get("format") != 1 or state.get("layout") not in {LEGACY_LAYOUT, DOMAIN_LAYOUT}:
        raise SQLiteUpgradeError("This migration runner does not support the recorded database layout; use a matching application image.")
    if (
        state.get("status") not in {"in_progress", "ready"}
        or not isinstance(state.get("targets"), dict)
        or not isinstance(state.get("migration_id"), str)
        or len(state["migration_id"]) != 32
        or any(character not in "0123456789abcdef" for character in state["migration_id"])
        or (state.get("backup") is not None and not isinstance(state["backup"], str))
    ):
        raise SQLiteUpgradeError("The SQLite layout record is incomplete; inspect its migration state before restarting.")
    target_files = DOMAIN_TARGET_FILES if state["layout"] == DOMAIN_LAYOUT else ORIGINAL_TARGET_FILES
    if state["layout"] == DOMAIN_LAYOUT and state.get("phase") not in {"copy", "cleanup", "vacuum", "verify", "append", "complete"}:
        raise SQLiteUpgradeError("The domain migration phase is invalid; inspect its durable checkpoint.")
    if state["layout"] == DOMAIN_LAYOUT and ((state["status"] == "ready") != (state.get("phase") == "complete")):
        raise SQLiteUpgradeError("The domain migration completion state is inconsistent; inspect its durable checkpoint.")
    if state["layout"] == DOMAIN_LAYOUT:
        witnesses = state.get("billing_witnesses")
        if witnesses is not None and (
            not isinstance(witnesses, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in witnesses.items())
        ):
            raise SQLiteUpgradeError("The sealed billing migration checkpoint is invalid; inspect the matching migration record.")
        for domain in set(DOMAIN_TARGET_FILES).difference(ORIGINAL_TARGET_FILES):
            target = state["targets"].get(domain)
            if not isinstance(target, dict):
                raise SQLiteUpgradeError("The domain import target record is invalid; inspect the migration checkpoint.")
            if target.get("head") is None:
                if state["phase"] != "copy":
                    raise SQLiteUpgradeError("The complete-copy barrier is missing a committed domain; inspect its checkpoint.")
                continue
            evidence = target.get("evidence")
            from bowhead_core.sqlite_domain_upgrade import SPLIT_TABLES

            expected_tables = {table for table in SPLIT_TABLES[domain] if not table.startswith("canvas_fts_")}
            if not isinstance(evidence, dict) or set(evidence) != expected_tables:
                raise SQLiteUpgradeError(f"The complete import evidence for {domain} is missing; inspect its migration checkpoint.")
            for record in evidence.values():
                if (
                    not isinstance(record, dict)
                    or type(record.get("rows")) is not int
                    or record["rows"] < 0
                    or not isinstance(record.get("digest"), str)
                    or len(record["digest"]) != 64
                    or any(character not in "0123456789abcdef" for character in record["digest"])
                ):
                    raise SQLiteUpgradeError(f"The import evidence for {domain} is invalid; inspect its migration checkpoint.")
    if set(state["targets"]) != set(target_files):
        raise SQLiteUpgradeError("The SQLite layout target set differs from the application; use a matching image.")
    for name, filename in target_files.items():
        target = state["targets"][name]
        if not isinstance(target, dict) or target.get("file") != filename:
            raise SQLiteUpgradeError(f"The layout path for target {name} is invalid; recover the matching layout record.")
        if target.get("head") is not None and not isinstance(target["head"], str):
            raise SQLiteUpgradeError(f"The layout revision for {name} is invalid; inspect the matching record.")
        if target.get("head") is not None and not (directory / filename).is_file():
            raise SQLiteUpgradeError(f"Required database {filename} is missing; restore it instead of creating an empty replacement.")
    return state


def _assert_integrity(path: Path) -> None:
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise SQLiteUpgradeError(f"Integrity verification failed for {path.name}; repair storage or restore a verified backup.")
    except sqlite3.DatabaseError as exc:
        raise SQLiteUpgradeError(f"Integrity verification failed for {path.name}; repair storage or restore a verified backup.") from exc


def _snapshot(source: Path, target: Path) -> None:
    with (
        closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as reader,
        closing(sqlite3.connect(target)) as writer,
    ):
        writer.execute("PRAGMA synchronous=FULL")
        reader.backup(writer, pages=2048)
        writer.execute("PRAGMA journal_mode=DELETE")
    target.chmod(0o600)
    _assert_integrity(target)
    with target.open("rb") as stream:
        os.fsync(stream.fileno())


def _data_files(root: Path) -> list[Path]:
    """Snapshot standard local data without recursively copying earlier backups."""
    files: list[Path] = []
    for path in root.iterdir():
        if path.name not in {"sqlite", "backups"}:
            files.append(path)
    return files


def backup_before_upgrade(directory: Path, migration_id: str, heads: Mapping[str, str | None]) -> Path | None:
    target_files = DOMAIN_TARGET_FILES if set(heads) == set(DOMAIN_TARGET_FILES) else ORIGINAL_TARGET_FILES
    if set(heads) != set(target_files):
        raise SQLiteUpgradeError("The backup target set is not registered; inspect the migration plan.")
    sources = {name: directory / filename for name, filename in target_files.items() if (directory / filename).is_file()}
    if not sources:
        return None
    root = directory.parent
    backups = root / "backups" / directory.name
    backups.mkdir(parents=True, exist_ok=True)
    backups.chmod(0o700)
    with (backups.parent / ".account-deletion.lock").open("a") as privacy_lock:
        try:
            fcntl.flock(privacy_lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SQLiteUpgradeError("Account backup cleanup is active; wait for it to finish before restarting the migration.") from exc
        destination = backups / migration_id
        if destination.exists():
            _verify_backup(destination, migration_id)
            return destination
        temporary = backups / f".{migration_id}.tmp"
        if temporary.exists():
            shutil.rmtree(temporary)
        extras = {path: Path(path.name) for path in _data_files(root)} if directory.name == "sqlite" else {}
        for name in ("kb_chromadb", "flag_transitions.jsonl"):
            path = directory / name
            if path.exists() or path.is_symlink():
                extras[path] = Path(directory.name) / name
        privacy = Path(os.environ.get("BOWHEAD_PRIVACY_LEDGER_DB", "/app/.privacy-ledger/deletions.db"))
        privacy_files = [path for path in (privacy, privacy.with_name("account-deletions.db")) if path.is_file()]
        for path in privacy_files:
            _recover_rollback_journal(path)
        size = sum(
            path.stat().st_size + Path(str(path) + "-wal").stat().st_size if Path(str(path) + "-wal").exists() else path.stat().st_size
            for path in [*sources.values(), *privacy_files]
        )
        for path in extras:
            for item in chain((path,), path.rglob("*") if path.is_dir() and not path.is_symlink() else ()):
                if item.is_symlink() or not (item.is_file() or item.is_dir()):
                    raise SQLiteUpgradeError("Local data contains a symlink or special file; validate the backup scope before upgrading.")
                if item.is_file():
                    size += item.stat().st_size
        if shutil.disk_usage(backups).free < max(size * 2, 1024 * 1024):
            raise SQLiteUpgradeError("Insufficient disk space for the migration backup; free space before restarting.")
        temporary.mkdir(mode=0o700)
        manifest: dict[str, object] = {"format": 1, "migration_id": migration_id, "heads": dict(heads), "databases": {}}
        try:
            database_directory = temporary / "sqlite"
            database_directory.mkdir(mode=0o700)
            for name, path in sources.items():
                _snapshot(path, database_directory / path.name)
                manifest["databases"][name] = path.name
            layout = directory / LAYOUT_FILE
            if layout.exists():
                layout_state = _load_state(directory)
                if layout_state is not None and layout_state["status"] == "ready":
                    _write_json(temporary / LAYOUT_FILE, layout_state)
            if extras:
                data_directory = temporary / "data"
                data_directory.mkdir(mode=0o700)
                for path in extras:
                    target = data_directory / extras[path]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if path.is_symlink():
                        raise SQLiteUpgradeError("Local data contains a symlink; validate the backup scope before upgrading.")
                    if path.is_dir():
                        shutil.copytree(path, target, symlinks=True)
                    else:
                        shutil.copy2(path, target)
                chroma = data_directory / directory.name / "kb_chromadb" / "chroma.sqlite3"
                if chroma.is_file():
                    _assert_integrity(chroma)
            if privacy_files:
                privacy_directory = temporary / "privacy-ledger"
                privacy_directory.mkdir(mode=0o700)
                for path in privacy_files:
                    _snapshot(path, privacy_directory / path.name)
                manifest["privacy_ledgers"] = [path.name for path in privacy_files]
            secrets = {name: os.environ[name] for name in ("BOWHEAD_FERNET_KEY", "BOWHEAD_JWT_SECRET") if os.environ.get(name)}
            if secrets:
                _write_json(temporary / "instance-secrets.json", secrets)
            if extras:
                for path in (temporary / "data").rglob("*"):
                    if path.is_file() and not path.is_symlink():
                        with path.open("rb") as stream:
                            os.fsync(stream.fileno())
                directories = [path for path in (temporary / "data").rglob("*") if path.is_dir() and not path.is_symlink()]
                for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
                    _sync_directory(path)
                _sync_directory(temporary / "data")
            _sync_directory(database_directory)
            if privacy_files:
                _sync_directory(temporary / "privacy-ledger")
            _write_json(temporary / "backup-manifest.json", manifest)
            _sync_directory(temporary)
            temporary.replace(destination)
            _sync_directory(backups)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        log.info("sqlite.migration_backup_completed", migration_id=migration_id, targets=len(sources))
        return destination


def _verify_backup(path: Path, migration_id: str) -> None:
    try:
        manifest = json.loads((path / "backup-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SQLiteUpgradeError("The migration backup is incomplete; repair its checkpoint before restarting.") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != 1
        or manifest.get("migration_id") != migration_id
        or not isinstance(manifest.get("databases"), dict)
        or not manifest["databases"]
        or not isinstance(manifest.get("heads"), dict)
        or set(manifest["heads"]) not in (set(ORIGINAL_TARGET_FILES), set(DOMAIN_TARGET_FILES))
        or not isinstance(manifest.get("privacy_ledgers", []), list)
    ):
        raise SQLiteUpgradeError("The migration backup identity does not match the checkpoint; inspect it before restarting.")
    if any(revision is not None and name not in manifest["databases"] for name, revision in manifest["heads"].items()):
        raise SQLiteUpgradeError("The migration backup omits a versioned database; inspect the archive before restarting.")
    for name, filename in manifest["databases"].items():
        if DOMAIN_TARGET_FILES.get(name) != filename:
            raise SQLiteUpgradeError("The backup contains an unknown database target; inspect the archive before restarting.")
        _assert_integrity(path / "sqlite" / filename)
        if database_head(path / "sqlite" / filename) != manifest["heads"][name]:
            raise SQLiteUpgradeError(f"The backup revision for {name} differs from its manifest; inspect the archive before restarting.")
    for filename in manifest.get("privacy_ledgers", []):
        if not isinstance(filename, str) or filename not in {
            "deletions.db",
            "account-deletions.db",
            Path(os.environ.get("BOWHEAD_PRIVACY_LEDGER_DB", "/app/.privacy-ledger/deletions.db")).name,
        }:
            raise SQLiteUpgradeError("The backup contains an unknown privacy ledger; inspect the archive before restarting.")
        _assert_integrity(path / "privacy-ledger" / filename)


def _verify_checkpoint_backup(directory: Path, backup: str, migration_id: str) -> None:
    expected = directory.parent / "backups" / directory.name / migration_id
    if Path(backup).resolve() != expected.resolve():
        raise SQLiteUpgradeError("The migration backup path differs from its data directory; inspect the layout record.")
    _verify_backup(expected, migration_id)


def run_legacy_upgrade(
    directory: Path,
    names: tuple[str, ...],
    head: str,
    known_revisions: frozenset[str],
    upgrade_one: Callable[[str, Path], None],
) -> tuple[str, ...]:
    """Back up pending legacy upgrades and persist a resumable startup checkpoint."""
    with migration_lock(directory):
        for name, filename in DOMAIN_TARGET_FILES.items():
            if name not in ORIGINAL_TARGET_FILES and (directory / filename).exists():
                raise SQLiteUpgradeError("Domain databases are present; use the matching domain-layout migration runner.")
        for filename in ORIGINAL_TARGET_FILES.values():
            _recover_rollback_journal(directory / filename)
        state = _load_state(directory)
        heads = {name: database_head(directory / filename) for name, filename in ORIGINAL_TARGET_FILES.items()}
        unknown = {name for name, revision in heads.items() if revision is not None and revision not in known_revisions}
        if unknown:
            raise SQLiteUpgradeError(f"Unknown source revisions for {', '.join(sorted(unknown))}; use a compatible application image.")
        if state is not None and state["status"] == "ready":
            for name, revision in heads.items():
                if state["targets"][name].get("head") != revision:
                    raise SQLiteUpgradeError(f"The recorded revision for {name} differs from the database; inspect the layout before restarting.")
        pending = tuple(name for name in names if heads[name] != head)
        if not pending:
            if state is not None and state["status"] == "in_progress":
                backup = state.get("backup")
                if backup is not None:
                    _verify_checkpoint_backup(directory, backup, str(state["migration_id"]))
            if tuple(names) == tuple(ORIGINAL_TARGET_FILES):
                _write_json(directory / LAYOUT_FILE, _ready_state(heads, head, state))
            return names
        migration_id = str(state["migration_id"]) if state is not None and state["status"] == "in_progress" else uuid4().hex
        checkpoint: dict[str, object] = {
            "format": 1,
            "layout": LEGACY_LAYOUT,
            "status": "in_progress",
            "migration_id": migration_id,
            "target_head": head,
            "selected_targets": list(names),
            "backup": None,
            "targets": {name: {"file": filename, "head": heads[name]} for name, filename in ORIGINAL_TARGET_FILES.items()},
        }
        _write_json(directory / LAYOUT_FILE, checkpoint)
        backup = state.get("backup") if state is not None and state["status"] == "in_progress" else None
        if backup is not None:
            _verify_checkpoint_backup(directory, backup, migration_id)
        else:
            backup_path = backup_before_upgrade(directory, migration_id, heads)
            backup = str(backup_path) if backup_path is not None else None
        checkpoint["backup"] = backup
        _write_json(directory / LAYOUT_FILE, checkpoint)
        for name in pending:
            started = time.monotonic()
            log.info("sqlite.migration_target_started", target=name, migration_id=migration_id)
            upgrade_one(name, directory / ORIGINAL_TARGET_FILES[name])
            if database_head(directory / ORIGINAL_TARGET_FILES[name]) != head:
                raise SQLiteUpgradeError(f"Target {name} did not reach the expected revision; inspect its migration before restarting.")
            _assert_integrity(directory / ORIGINAL_TARGET_FILES[name])
            log.info(
                "sqlite.migration_target_completed", target=name, migration_id=migration_id, duration_ms=round((time.monotonic() - started) * 1000)
            )
        final_heads = {name: database_head(directory / filename) for name, filename in ORIGINAL_TARGET_FILES.items()}
        _write_json(directory / LAYOUT_FILE, _ready_state(final_heads, head, checkpoint))
        return names


def _ready_state(heads: Mapping[str, str | None], head: str, previous: dict[str, object] | None) -> dict[str, object]:
    return {
        "format": 1,
        "layout": LEGACY_LAYOUT,
        "status": "ready" if all(value == head for value in heads.values()) else "in_progress",
        "migration_id": previous.get("migration_id", uuid4().hex) if previous is not None else uuid4().hex,
        "target_head": head,
        "backup": previous.get("backup") if previous is not None else None,
        "targets": {name: {"file": filename, "head": heads[name]} for name, filename in ORIGINAL_TARGET_FILES.items()},
    }


def migration_wait_status(directory: Path) -> str:
    """Read startup progress without opening business databases or mutating files."""
    lock = directory / ".sqlite-migration.lock"
    if lock.exists():
        try:
            with lock.open("r") as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    return "active"
                fcntl.flock(stream, fcntl.LOCK_UN)
        except OSError:
            return "unknown"
    manifest = directory / LAYOUT_FILE
    if not manifest.exists():
        return "unmanaged"
    try:
        state = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unknown"
    if not isinstance(state, dict) or state.get("format") != 1 or state.get("layout") not in {LEGACY_LAYOUT, DOMAIN_LAYOUT}:
        return "unknown"
    return {"ready": "ready", "in_progress": "incomplete"}.get(state.get("status"), "unknown")


def startup_migration_wait_status() -> str:
    """Return only a bounded status token to deployment readiness probes."""
    value = os.environ.get("BOWHEAD_DB_URL", "sqlite+aiosqlite:////app/.data/sqlite/bowhead.db")
    if not value.startswith("sqlite") or "///" not in value:
        return "unknown"
    return migration_wait_status(Path(value.split("///", 1)[1]).parent)
