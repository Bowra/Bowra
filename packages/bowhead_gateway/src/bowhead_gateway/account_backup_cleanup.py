"""Verify and expire indivisible registered SQLite recovery units."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

from bowhead_core.account_deletion import AccountDeletionError
from bowhead_core.account_erasure import _POLICY, _q
from bowhead_core.sqlite_domains import DOMAIN_LAYOUT, DOMAIN_TARGET_FILES, LEGACY_LAYOUT, ORIGINAL_TARGET_FILES
from bowhead_core.sqlite_upgrade import SQLiteUpgradeError, database_head

_UUID = re.compile(r"[a-f0-9]{32}")
_LABEL = re.compile(r"instance-pre-[A-Za-z0-9._-]{1,128}")
_EXPIRATION = re.compile(r"\.account-expire-([a-f0-9]{32})--([A-Za-z0-9][A-Za-z0-9._-]{0,180})")
_OWNER_COLUMNS = frozenset(
    {
        "user_id",
        "owner_user_id",
        "subject_id",
        "uploaded_by",
        "created_by",
        "requested_by",
        "actor_user_id",
        "author_user_id",
        "created_by_user_id",
        "scope_ref",
    }
)

_USER_STUB_VALUES = {
    "avatar_url": None,
    "oauth_providers_json": "[]",
    "preferences_json": "{}",
    "email_verified": 0,
    "credit_balance": 0,
    "credit_quota": 0,
    "verify_token_jti": None,
    "datarc_member_uid": None,
    "active_team_id": None,
    "last_active_at": None,
    "locale": "auto",
    "timezone": "UTC",
}


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AccountDeletionError("account_backup_format_unregistered") from error
    if not isinstance(value, dict):
        raise AccountDeletionError("account_backup_format_unregistered")
    return value


def _walk_backup_tree(root: Path) -> Iterator[Path]:
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            for path in directory.iterdir():
                yield path
                if path.is_dir() and not path.is_symlink():
                    pending.append(path)
        except OSError as error:
            raise AccountDeletionError("account_backup_unreadable") from error


def validate_backup_tree(root: Path) -> Iterator[Path]:
    """Reject symlinks/special files without retaining a full blob tree in memory."""
    if root.is_symlink() or not root.is_dir():
        raise AccountDeletionError("account_backup_path_invalid")
    for path in _walk_backup_tree(root):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise AccountDeletionError("account_backup_path_invalid")
    return _walk_backup_tree(root)


def verify_backup_bundle(bundle: Path) -> tuple[str, tuple[Path, ...]]:
    """Validate format-one migration/operational bundles without restoring them."""
    validate_backup_tree(bundle)
    state = _json(bundle / "backup-manifest.json")
    identity = state.get("migration_id")
    if (
        type(state.get("format")) is not int
        or state["format"] != 1
        or not isinstance(identity, str)
        or _UUID.fullmatch(identity) is None
        or not (_UUID.fullmatch(bundle.name) and bundle.name == identity or _LABEL.fullmatch(bundle.name))
        or set(state) - {"format", "migration_id", "heads", "databases", "privacy_ledgers"}
    ):
        raise AccountDeletionError("account_backup_format_unregistered")
    heads, databases = state.get("heads"), state.get("databases")
    if not isinstance(heads, dict) or set(heads) not in (set(ORIGINAL_TARGET_FILES), set(DOMAIN_TARGET_FILES)):
        raise AccountDeletionError("account_backup_format_unregistered")
    if not isinstance(databases, dict) or not databases or set(databases) - set(heads):
        raise AccountDeletionError("account_backup_format_unregistered")
    if any(value is not None and (not isinstance(value, str) or not value or name not in databases) for name, value in heads.items()):
        raise AccountDeletionError("account_backup_format_unregistered")
    for name, filename in databases.items():
        if filename != DOMAIN_TARGET_FILES.get(name):
            raise AccountDeletionError("account_backup_path_invalid")
    ledgers = state.get("privacy_ledgers", [])
    allowed_ledgers = {
        "deletions.db",
        "account-deletions.db",
        Path(os.environ.get("BOWHEAD_PRIVACY_LEDGER_DB", "/app/.privacy-ledger/deletions.db")).name,
    }
    if (
        not isinstance(ledgers, list)
        or any(not isinstance(name, str) or name not in allowed_ledgers for name in ledgers)
        or len(set(ledgers)) != len(ledgers)
    ):
        raise AccountDeletionError("account_backup_format_unregistered")
    children = {path.name for path in bundle.iterdir()}
    if children - {"backup-manifest.json", "sqlite", "data", "privacy-ledger", "instance-secrets.json", "sqlite-layout.json"}:
        raise AccountDeletionError("account_backup_format_unregistered")
    database_root = bundle / "sqlite"
    if not database_root.is_dir() or {path.name for path in database_root.iterdir()} != set(databases.values()):
        raise AccountDeletionError("account_backup_format_unregistered")
    privacy_root = bundle / "privacy-ledger"
    if (
        bool(ledgers) != privacy_root.exists()
        or ledgers
        and (not privacy_root.is_dir() or {path.name for path in privacy_root.iterdir()} != set(ledgers))
    ):
        raise AccountDeletionError("account_backup_format_unregistered")
    if (bundle / "data").exists() and not (bundle / "data").is_dir():
        raise AccountDeletionError("account_backup_format_unregistered")
    secrets = bundle / "instance-secrets.json"
    if secrets.exists():
        if not secrets.is_file() or secrets.stat().st_size > 65536:
            raise AccountDeletionError("account_backup_format_unregistered")
        values = _json(secrets)
        if (
            not values
            or set(values) - {"BOWHEAD_FERNET_KEY", "BOWHEAD_JWT_SECRET"}
            or any(not isinstance(value, str) or not value for value in values.values())
        ):
            raise AccountDeletionError("account_backup_format_unregistered")
    layout_path = bundle / "sqlite-layout.json"
    if layout_path.exists():
        layout = _json(layout_path)
        targets = layout.get("targets")
        if (
            layout.get("format") != 1
            or layout.get("layout") not in {LEGACY_LAYOUT, DOMAIN_LAYOUT}
            or layout.get("status") != "ready"
            or not isinstance(targets, dict)
            or set(targets) != set(heads)
        ):
            raise AccountDeletionError("account_backup_format_unregistered")
        if any(
            not isinstance(targets[name], dict) or targets[name].get("file") != DOMAIN_TARGET_FILES[name] or targets[name].get("head") != revision
            for name, revision in heads.items()
        ):
            raise AccountDeletionError("account_backup_format_unregistered")
    paths = tuple(database_root / filename for filename in databases.values())
    for path in (*paths, *(privacy_root / filename for filename in ledgers)):
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
                if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise AccountDeletionError("account_backup_unreadable")
        except sqlite3.DatabaseError as error:
            raise AccountDeletionError("account_backup_unreadable") from error
    for name, filename in databases.items():
        try:
            if database_head(database_root / filename) != heads[name]:
                raise AccountDeletionError("account_backup_unreadable")
        except SQLiteUpgradeError as error:
            raise AccountDeletionError("account_backup_unreadable") from error
    return identity, paths


def snapshot_contains_account(path: Path, user_id: str, *, thorough: bool = False) -> bool:
    """Recognize private owner rows while preserving completed minimal stubs."""
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            contains = False
            anonymous = False
            if "users" in tables:
                columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
                if not {"status", "email", "password_hash", "display_name"} <= columns:
                    if db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                        return True
                else:
                    user = db.execute("SELECT status,email,password_hash,display_name FROM users WHERE id=?", (user_id,)).fetchone()
                    if user:
                        completed = (
                            "account_deletions" in tables
                            and db.execute("SELECT 1 FROM account_deletions WHERE user_id=? AND state='completed'", (user_id,)).fetchone()
                        )
                        anonymous = bool(
                            completed
                            and user[0] == "deleted"
                            and isinstance(user[1], str)
                            and user[1].endswith("@deleted.invalid")
                            and not user[2]
                            and not user[3]
                        )
                        contains = not anonymous
                        if anonymous and thorough:
                            fields = [field for field in _USER_STUB_VALUES if field in columns]
                            if fields:
                                stored = db.execute(
                                    "SELECT " + ",".join(_q(field) for field in fields) + " FROM users WHERE id=?", (user_id,)
                                ).fetchone()
                                for field, value in zip(fields, stored, strict=True):
                                    expected = _USER_STUB_VALUES[field]
                                    if field in {"oauth_providers_json", "preferences_json"}:
                                        try:
                                            value, expected = json.loads(value), json.loads(expected)
                                        except (ValueError, TypeError):
                                            contains = True
                                            break
                                    if value != expected:
                                        contains = True
                                        break
                if not thorough:
                    return contains
            if contains:
                return True
            for table in tables:
                if (
                    table.startswith("sqlite_")
                    or table == "users"
                    or anonymous
                    and table in {"account_deletions", "account_protocol_erasure_receipts"}
                ):
                    continue
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({_q(table)})")}
                owners = (
                    (_OWNER_COLUMNS | set(_POLICY["tables"].get(table, {}).get("owner_columns", [])))
                    if thorough
                    else {"user_id", "owner_user_id", "subject_id", "uploaded_by"}
                )
                for column in columns & owners:
                    extra = " AND scope='personal'" if column == "uploaded_by" and "scope" in columns else ""
                    if db.execute(f"SELECT 1 FROM {_q(table)} WHERE {_q(column)}=?{extra} LIMIT 1", (user_id,)).fetchone():
                        return True
            return False
    except sqlite3.DatabaseError as error:
        raise AccountDeletionError("account_backup_unreadable") from error


def resume_backup_expirations(root: Path) -> None:
    """Complete durable whole-unit deletions before parsing remaining archives."""
    # Intents live next to units, never in their opaque captured data trees.
    markers = list(root.glob(".account-expire-*"))
    for container in root.iterdir():
        if container.is_dir() and not (_UUID.fullmatch(container.name) or _LABEL.fullmatch(container.name)):
            markers.extend(container.glob(".account-expire-*"))
    for marker in markers:
        match = _EXPIRATION.fullmatch(marker.name)
        if match is None or not marker.is_file() or marker.is_symlink() or marker.stat().st_size or marker.stat().st_mode & 0o077:
            raise AccountDeletionError("account_backup_format_unregistered")
        target = marker.parent / match[2]
        if not (_UUID.fullmatch(target.name) and target.name == match[1] or _LABEL.fullmatch(target.name)):
            raise AccountDeletionError("account_backup_format_unregistered")
        try:
            if target.exists():
                if not target.is_dir() or target.is_symlink():
                    raise AccountDeletionError("account_backup_path_invalid")
                shutil.rmtree(target)
                _sync_directory(marker.parent)
            marker.unlink()
            _sync_directory(marker.parent)
        except OSError as error:
            raise AccountDeletionError("account_backup_cleanup_pending") from error


def expire_backup_bundle(bundle: Path, identity: str) -> None:
    """Publish a durable expiration intent before the first destructive unlink."""
    marker = bundle.parent / f".account-expire-{identity}--{bundle.name}"
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _sync_directory(bundle.parent)
        resume_backup_expirations(bundle.parent)
    except OSError as error:
        raise AccountDeletionError("account_backup_cleanup_pending") from error
