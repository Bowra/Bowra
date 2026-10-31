"""Carry attributable legacy Datarc choices into connection-scoped preferences."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from alembic import context

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.engine import Connection

revision = "0210_datarc_preference_backfill"
down_revision = "0209_reference_question_origin"
branch_labels = None
depends_on = None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _scope(db: Connection, user_id: str, selected_at: str, session_id: str | None) -> tuple[str, str] | None:
    memberships = db.exec_driver_sql(
        "SELECT team_id,status,created_at,removed_at FROM team_members WHERE user_id=? "
        "AND (julianday(created_at) IS NULL OR julianday(created_at)<=julianday(?)) "
        "AND (removed_at IS NULL OR julianday(removed_at) IS NULL OR julianday(removed_at)>julianday(?))",
        (user_id, selected_at, selected_at),
    ).fetchall()
    if session_id:
        session = db.exec_driver_sql(
            "SELECT w.team_id FROM sessions s JOIN workspaces w ON w.id=s.workspace_id "
            "WHERE s.id=? AND s.user_id=? AND s.deleted_at IS NULL AND w.status='active' "
            "AND julianday(s.started_at)<=julianday(?)",
            (session_id, user_id, selected_at),
        ).fetchone()
        if session is None:
            return None
        memberships = [row for row in memberships if row[0] == session[0]]
    if len(memberships) != 1 or memberships[0][1] != "active" or _timestamp(memberships[0][2]) is None or memberships[0][3] is not None:
        return None
    team_id = memberships[0][0]
    if not isinstance(team_id, str) or not team_id or len(team_id) > 128 or "/" in team_id:
        return None
    if not db.exec_driver_sql("SELECT 1 FROM teams WHERE id=? AND status='active'", (team_id,)).fetchone():
        return None
    # Archived connections also count: a numeric workspace ID cannot identify
    # which of several connections existing at the choice time was intended.
    connections = db.exec_driver_sql(
        "SELECT id,status,created_at FROM datarc_connection WHERE team_id=? "
        "AND (julianday(created_at) IS NULL OR julianday(created_at)<=julianday(?))",
        (team_id, selected_at),
    ).fetchall()
    if len(connections) != 1 or connections[0][1] != "active" or _timestamp(connections[0][2]) is None:
        return None
    connection_id = connections[0][0]
    if not isinstance(connection_id, str) or not connection_id or len(connection_id) > 128 or "/" in connection_id:
        return None
    return team_id, connection_id


def _candidate(db: Connection, row: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    try:
        value = json.loads(row["value"])
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or "schema_version" in value or _timestamp(value.get("updated_at")) is None:
        return None
    identifier, source = value.get("id"), value.get("source")
    if not isinstance(identifier, str) or not identifier.strip() or source not in {"explicit", "auto", "session"}:
        return None
    name = value.get("name", "")
    if not isinstance(name, str):
        return None
    if row["key"] == "datarc/workspace@session" and source != "session":
        return None
    session_id = value.get("session_id") if source == "session" else None
    if source == "session" and (not isinstance(session_id, str) or not session_id):
        return None
    scope = _scope(db, row["subject_id"], value["updated_at"], session_id)
    if scope is None:
        return None
    team_id, connection_id = scope
    if value.get("team_id", team_id) != team_id or value.get("connection_id", connection_id) != connection_id:
        return None
    base = f"datarc/workspace@v2/{team_id}/{connection_id}"
    key = f"{base}/session/{hashlib.sha256(session_id.encode()).hexdigest()}" if session_id else f"{base}/default"
    return key, {
        "schema_version": 2,
        "team_id": team_id,
        "connection_id": connection_id,
        "id": identifier,
        "name": name,
        "source": source,
        "updated_at": value["updated_at"],
        **({"session_id": session_id} if session_id else {}),
    }


def _backfill(db: Connection) -> None:
    # Include deleted new rows: a reset or cleared overlay must not revive an
    # old choice. Capture this set before inserting the migrated default.
    existing = set(
        db.exec_driver_sql(
            "SELECT subject_id,key FROM memory_entries WHERE subject_type='user' AND kind='tool_context' AND key LIKE 'datarc/workspace@v2/%'"
        ).fetchall()
    )
    rows = (
        db.exec_driver_sql(
            "SELECT m.* FROM memory_entries m JOIN users u ON u.id=m.subject_id "
            "WHERE m.subject_type='user' AND m.kind='tool_context' "
            "AND m.key IN ('datarc/workspace','datarc/workspace@session') "
            "AND m.workspace_id IS NULL AND m.deleted_at IS NULL AND m.superseded_by IS NULL "
            "AND u.status='active' AND NOT EXISTS (SELECT 1 FROM memory_privacy_state p "
            "WHERE p.user_id=m.subject_id AND p.barrier_status<>'idle') "
            "AND NOT EXISTS (SELECT 1 FROM memory_resource_deletion_tombstones t "
            "WHERE t.user_id=m.subject_id AND t.resource_type='memory_entry' AND t.resource_id=m.id "
            "AND t.deleted_through_version>=m.version) ORDER BY m.key,m.id"
        )
        .mappings()
        .all()
    )
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        candidate = _candidate(db, row)
        if candidate is None:
            continue
        key, value = candidate
        user_id = row["subject_id"]
        default_key = f"datarc/workspace@v2/{value['team_id']}/{value['connection_id']}/default"
        if (user_id, key) in existing or (value["source"] == "session" and (user_id, default_key) in existing):
            continue
        copied = dict(row)
        copied.update(
            id="mem_" + hashlib.sha256((row["id"] + "/" + key).encode()).hexdigest()[:24],
            key=key,
            value=json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            version=1,
            memory_role="operational",
            value_schema="object_v1",
            memory_policy_version=1,
            previous_value=None,
            previous_value_at=None,
        )
        # The original session-overlay key outranks a legacy base-key session
        # choice, matching the original reader's precedence.
        candidates[user_id, key] = copied
    for copied in candidates.values():
        columns = tuple(copied)
        db.exec_driver_sql(
            f"INSERT INTO memory_entries ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            tuple(copied[column] for column in columns),
        )


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT datarc_preference_backfill")
    try:
        _backfill(db)
        db.exec_driver_sql("RELEASE SAVEPOINT datarc_preference_backfill")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT datarc_preference_backfill")
        db.exec_driver_sql("RELEASE SAVEPOINT datarc_preference_backfill")
        raise


def downgrade() -> None:
    raise NotImplementedError("Restore the paired database snapshot to roll back.")
