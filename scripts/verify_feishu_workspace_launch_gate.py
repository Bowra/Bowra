"""Verify Feishu Phase 1 workspace launch-gate readiness from SQLite state.

Run from repo root:

    python scripts/verify_feishu_workspace_launch_gate.py --db .data/sqlite/bowhead.db

Typical use around the 101 backfill gate:

1. Before backfill apply, capture the unresolved legacy memberships:

       python scripts/verify_feishu_workspace_launch_gate.py \
         --db /opt/bowhead/.data/sqlite/bowhead.db \
         --team-id team_101

2. After backfill apply, require the launch gate to pass:

       python scripts/verify_feishu_workspace_launch_gate.py \
         --db /opt/bowhead/.data/sqlite/bowhead.db \
         --team-id team_101 \
         --require-ready

This script is read-only. It never mutates the database.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _fetch_canonical_workspaces(
    conn: sqlite3.Connection,
    *,
    team_id: str | None,
    workspace_ids: list[str],
) -> list[dict[str, Any]]:
    sql = (
        "SELECT id AS workspace_id, team_id, name, datarc_workspace_id, status "
        "FROM workspaces WHERE 1=1"
    )
    params: list[Any] = []
    if team_id:
        sql += " AND team_id = ?"
        params.append(team_id)
    if workspace_ids:
        placeholders = ", ".join("?" for _ in workspace_ids)
        sql += f" AND id IN ({placeholders})"
        params.extend(workspace_ids)
    sql += " ORDER BY team_id, id"
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def _fetch_unresolved_legacy_memberships(
    conn: sqlite3.Connection,
    *,
    team_id: str | None,
    legacy_workspace_ids: list[str],
) -> list[dict[str, Any]]:
    sql = (
        "SELECT u.team_id, wm.workspace_id AS legacy_workspace_id, "
        "       COUNT(*) AS active_member_rows, "
        "       COUNT(DISTINCT wm.user_id) AS active_users "
        "FROM workspace_members wm "
        "JOIN users u ON u.id = wm.user_id "
        "LEFT JOIN workspaces w_legacy ON w_legacy.id = wm.workspace_id "
        "LEFT JOIN workspaces w_map "
        "       ON w_map.team_id = u.team_id "
        "      AND w_map.datarc_workspace_id = wm.workspace_id "
        "      AND w_map.status = 'active' "
        "WHERE w_legacy.id IS NULL "
        "  AND w_map.id IS NULL "
        "  AND wm.status = 'active' "
        "  AND wm.workspace_id NOT LIKE 'ws%'"
    )
    params: list[Any] = []
    if team_id:
        sql += " AND u.team_id = ?"
        params.append(team_id)
    if legacy_workspace_ids:
        placeholders = ", ".join("?" for _ in legacy_workspace_ids)
        sql += f" AND wm.workspace_id IN ({placeholders})"
        params.extend(legacy_workspace_ids)
    sql += (
        " GROUP BY u.team_id, wm.workspace_id "
        " ORDER BY u.team_id, wm.workspace_id"
    )
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def _fetch_legacy_session_workspace_ids(
    conn: sqlite3.Connection,
    *,
    team_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, "sessions"):
        return []
    columns = _table_columns(conn, "sessions")
    if "workspace_id" not in columns:
        return []
    sql = (
        "SELECT s.workspace_id, COUNT(*) AS sessions "
        "FROM sessions s "
        "WHERE s.workspace_id NOT LIKE 'ws%'"
    )
    params: list[Any] = []
    if team_id and "user_id" in columns:
        sql = (
            "SELECT s.workspace_id, COUNT(*) AS sessions "
            "FROM sessions s "
            "JOIN users u ON u.id = s.user_id "
            "WHERE s.workspace_id NOT LIKE 'ws%' "
            "  AND u.team_id = ?"
        )
        params.append(team_id)
    sql += " GROUP BY s.workspace_id ORDER BY sessions DESC, s.workspace_id LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def _build_report(args: argparse.Namespace) -> dict[str, Any]:
    db_path = str(Path(args.db).resolve())
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        required = {"users", "workspace_members"}
        missing_tables = sorted(table for table in required if not _table_exists(conn, table))
        if missing_tables:
            raise SystemExit(
                f"missing required tables in {db_path}: {', '.join(missing_tables)}",
            )

        workspace_ids = [str(item).strip() for item in (args.workspace_id or []) if str(item).strip()]
        legacy_workspace_ids = [
            str(item).strip()
            for item in (args.legacy_workspace_id or [])
            if str(item).strip()
        ]
        canonical_workspaces = (
            _fetch_canonical_workspaces(
                conn,
                team_id=args.team_id,
                workspace_ids=workspace_ids,
            )
            if _table_exists(conn, "workspaces")
            else []
        )
        unresolved_legacy = _fetch_unresolved_legacy_memberships(
            conn,
            team_id=args.team_id,
            legacy_workspace_ids=legacy_workspace_ids,
        )
        check_missing_datarc_mapping = bool(canonical_workspaces) and (
            bool(args.team_id) or bool(workspace_ids) or not legacy_workspace_ids
        )
        scoped_missing_datarc_mapping = [
            {
                "workspace_id": row["workspace_id"],
                "team_id": row["team_id"],
                "name": row["name"],
                "status": row["status"],
            }
            for row in canonical_workspaces
            if check_missing_datarc_mapping
            and not str(row.get("datarc_workspace_id") or "").strip()
        ]
        ready = not unresolved_legacy and not scoped_missing_datarc_mapping
        return {
            "db_path": db_path,
            "team_id_filter": args.team_id,
            "workspace_ids_filter": workspace_ids,
            "legacy_workspace_ids_filter": legacy_workspace_ids,
            "canonical_workspaces_count": len(canonical_workspaces),
            "mapped_canonical_workspaces_count": sum(
                1 for row in canonical_workspaces if str(row.get("datarc_workspace_id") or "").strip()
            ),
            "canonical_workspaces": canonical_workspaces,
            "legacy_unmapped_active_memberships_count": len(unresolved_legacy),
            "legacy_unmapped_active_memberships": unresolved_legacy,
            "scoped_workspaces_missing_datarc_mapping_count": len(scoped_missing_datarc_mapping),
            "scoped_workspaces_missing_datarc_mapping": scoped_missing_datarc_mapping,
            "legacy_session_workspace_ids_top": _fetch_legacy_session_workspace_ids(
                conn,
                team_id=args.team_id,
                limit=max(1, int(args.top_sessions)),
            ),
            "ready_for_feishu_workspace_launch_gate": ready,
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="SQLite bowhead.db path")
    parser.add_argument("--team-id", default=None, help="Limit report to one team id")
    parser.add_argument(
        "--workspace-id",
        action="append",
        default=None,
        help=(
            "Canonical Bowhead workspace id in launch scope. Repeat for multiple ids. "
            "When provided, the script also checks for missing datarc_workspace_id on these rows."
        ),
    )
    parser.add_argument(
        "--legacy-workspace-id",
        action="append",
        default=None,
        help="Limit unresolved legacy-membership checks to these historical ids.",
    )
    parser.add_argument(
        "--top-sessions",
        type=int,
        default=10,
        help="How many historical non-ws session workspace ids to include (default: 10).",
    )
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="Exit non-zero when unresolved legacy memberships or scoped mapping gaps remain.",
    )
    args = parser.parse_args()
    report = _build_report(args)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if args.require_ready and not report["ready_for_feishu_workspace_launch_gate"]:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
