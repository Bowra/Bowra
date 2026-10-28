"""Read-only data gate for the Space administration migration.

Run this against a read-only copy of the UserStore database before the M0
expand/repair/contract migration:

    uv run python -m bowhead_core.scripts.space_admin_preflight \
        --db /path/to/users.db \
        --expect-team team_example

The command never initializes or mutates the database. It reports identifiers
and hashes of normalized Space names, not user email addresses or Space names.
Exit 0 means the data gate passed; exit 1 means findings block the migration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "space-admin-preflight/v1"
SPACE_ADMIN_ROLES = ("admin", "owner", "workspace_admin", "ws_admin")
REQUIRED_COLUMNS: dict[str, set[str]] = {
    "teams": {"id", "status"},
    "users": {"id", "status"},
    "team_members": {"user_id", "team_id", "status"},
    "workspaces": {"id", "team_id", "name", "status"},
    "workspace_members": {"workspace_id", "user_id", "role", "status"},
}


def normalize_space_name(value: str) -> tuple[str, str]:
    """Return the display form and uniqueness key defined by the Tech Design."""
    display_name = unicodedata.normalize("NFKC", value).strip()
    return display_name, display_name.casefold()


def open_read_only(db_path: Path) -> sqlite3.Connection:
    """Open an existing SQLite database without creating or writing it."""
    path = db_path.expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"database path is not a file: {path}")
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def run_preflight(
    conn: sqlite3.Connection,
    *,
    min_teams: int = 1,
    expected_team_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Run the migration blockers against an already-open read-only connection."""
    schema_errors = _schema_errors(conn)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "read_only": True,
        "schema_errors": schema_errors,
        "database_identity_errors": [],
        "name_conflicts": [],
        "invalid_space_names": [],
        "active_spaces_without_admin": [],
        "orphan_active_space_memberships": [],
        "active_teams_without_active_spaces": [],
        "counts": {},
    }
    if schema_errors:
        report["blocking_findings"] = len(schema_errors)
        report["passed"] = False
        return report

    team_ids = [str(row["id"]) for row in conn.execute("SELECT id FROM teams ORDER BY id").fetchall()]
    identity_errors: list[dict[str, Any]] = []
    if len(team_ids) < min_teams:
        identity_errors.append(
            {
                "kind": "min_teams_not_met",
                "expected_minimum": min_teams,
                "actual": len(team_ids),
            }
        )
    missing_team_ids = sorted(set(expected_team_ids) - set(team_ids))
    if missing_team_ids:
        identity_errors.append(
            {
                "kind": "expected_teams_missing",
                "team_ids": missing_team_ids,
            }
        )
    report["database_identity_errors"] = identity_errors

    workspaces = conn.execute("SELECT id, team_id, name, status FROM workspaces ORDER BY team_id, id").fetchall()
    grouped_names: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    invalid_names: list[dict[str, str]] = []
    for workspace in workspaces:
        display_name, name_key = normalize_space_name(str(workspace["name"]))
        if not display_name:
            invalid_names.append(
                {
                    "team_id": str(workspace["team_id"]),
                    "workspace_id": str(workspace["id"]),
                    "status": str(workspace["status"]),
                    "reason": "empty_after_nfkc_strip",
                }
            )
            continue
        grouped_names[(str(workspace["team_id"]), name_key)].append(workspace)

    name_conflicts: list[dict[str, Any]] = []
    for (team_id, name_key), rows in sorted(grouped_names.items()):
        if len(rows) < 2:
            continue
        name_conflicts.append(
            {
                "team_id": team_id,
                "name_key_sha256": hashlib.sha256(name_key.encode()).hexdigest(),
                "workspace_ids": sorted(str(row["id"]) for row in rows),
                "statuses": sorted({str(row["status"]) for row in rows}),
            }
        )
    report["name_conflicts"] = name_conflicts
    report["invalid_space_names"] = invalid_names

    admin_placeholders = ", ".join("?" for _ in SPACE_ADMIN_ROLES)
    no_admin_rows = conn.execute(
        "SELECT w.id AS workspace_id, w.team_id "
        "FROM workspaces w "
        "WHERE w.status = 'active' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM workspace_members wm "
        "  JOIN users u ON u.id = wm.user_id AND u.status = 'active' "
        "  JOIN team_members tm ON tm.user_id = wm.user_id "
        "    AND tm.team_id = w.team_id AND tm.status = 'active' "
        "  WHERE wm.workspace_id = w.id AND wm.status = 'active' "
        f"    AND wm.role IN ({admin_placeholders})"
        ") "
        "ORDER BY w.team_id, w.id",
        SPACE_ADMIN_ROLES,
    ).fetchall()
    report["active_spaces_without_admin"] = [
        {
            "team_id": str(row["team_id"]),
            "workspace_id": str(row["workspace_id"]),
        }
        for row in no_admin_rows
    ]

    membership_rows = conn.execute(
        "SELECT wm.workspace_id, wm.user_id, "
        "       w.team_id AS workspace_team_id, "
        "       u.id AS resolved_user_id, u.status AS user_status, "
        "       tm.user_id AS resolved_team_member_id "
        "FROM workspace_members wm "
        "LEFT JOIN workspaces w ON w.id = wm.workspace_id "
        "LEFT JOIN users u ON u.id = wm.user_id "
        "LEFT JOIN team_members tm ON tm.user_id = wm.user_id "
        "  AND tm.team_id = w.team_id AND tm.status = 'active' "
        "WHERE wm.status = 'active' "
        "ORDER BY wm.workspace_id, wm.user_id"
    ).fetchall()
    orphan_memberships: list[dict[str, str | None]] = []
    for row in membership_rows:
        reason: str | None = None
        if row["workspace_team_id"] is None:
            reason = "workspace_missing"
        elif row["resolved_user_id"] is None:
            reason = "user_missing"
        elif row["user_status"] != "active":
            reason = "user_inactive"
        elif row["resolved_team_member_id"] is None:
            reason = "active_team_membership_missing"
        if reason:
            orphan_memberships.append(
                {
                    "workspace_id": str(row["workspace_id"]),
                    "workspace_team_id": (str(row["workspace_team_id"]) if row["workspace_team_id"] is not None else None),
                    "user_id": str(row["user_id"]),
                    "reason": reason,
                }
            )
    report["orphan_active_space_memberships"] = orphan_memberships

    empty_team_rows = conn.execute(
        "SELECT t.id AS team_id "
        "FROM teams t "
        "WHERE t.status = 'active' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM workspaces w "
        "  WHERE w.team_id = t.id AND w.status = 'active'"
        ") "
        "ORDER BY t.id"
    ).fetchall()
    report["active_teams_without_active_spaces"] = [{"team_id": str(row["team_id"])} for row in empty_team_rows]

    report["counts"] = {
        "teams": len(team_ids),
        "workspaces": len(workspaces),
        "active_workspace_memberships": len(membership_rows),
    }
    finding_keys = (
        "schema_errors",
        "database_identity_errors",
        "name_conflicts",
        "invalid_space_names",
        "active_spaces_without_admin",
        "orphan_active_space_memberships",
        "active_teams_without_active_spaces",
    )
    report["blocking_findings"] = sum(len(report[key]) for key in finding_keys)
    report["passed"] = report["blocking_findings"] == 0
    return report


def _schema_errors(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for table, required in REQUIRED_COLUMNS.items():
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if exists is None:
            errors.append({"table": table, "reason": "table_missing"})
            continue
        actual = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        missing = sorted(required - actual)
        if missing:
            errors.append(
                {
                    "table": table,
                    "reason": "columns_missing",
                    "columns": missing,
                }
            )
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Space administration migration data gate.")
    parser.add_argument(
        "--db",
        required=True,
        type=Path,
        help="existing UserStore SQLite database or read-only copy",
    )
    parser.add_argument(
        "--min-teams",
        type=int,
        default=1,
        help="block if fewer teams are present (default: 1)",
    )
    parser.add_argument(
        "--expect-team",
        action="append",
        default=[],
        metavar="TEAM_ID",
        help="block unless this team exists; repeatable",
    )
    args = parser.parse_args()
    if args.min_teams < 0:
        parser.error("--min-teams must be >= 0")
    try:
        with open_read_only(args.db) as conn:
            report = run_preflight(
                conn,
                min_teams=args.min_teams,
                expected_team_ids=tuple(args.expect_team),
            )
    except (OSError, sqlite3.Error, ValueError) as exc:
        sys.stderr.write(f"PREFLIGHT: ERROR — {exc}\n")
        raise SystemExit(2) from exc
    sys.stdout.write(f"{json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)}\n")
    if report["passed"]:
        sys.stderr.write("PREFLIGHT: PASS — migration data gate is clear.\n")
        raise SystemExit(0)
    sys.stderr.write(f"PREFLIGHT: BLOCKED — {report['blocking_findings']} finding(s) require repair.\n")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
