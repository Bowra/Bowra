from __future__ import annotations

import sqlite3

from bowhead_core.scripts.space_admin_preflight import (
    normalize_space_name,
    open_read_only,
    run_preflight,
)

SCHEMA = """
CREATE TABLE teams (id TEXT PRIMARY KEY, status TEXT NOT NULL);
CREATE TABLE users (id TEXT PRIMARY KEY, status TEXT NOT NULL);
CREATE TABLE team_members (
    user_id TEXT NOT NULL,
    team_id TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE workspaces (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE workspace_members (
    workspace_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL
);
"""


def _database(tmp_path):
    path = tmp_path / "users.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return path, conn


def test_normalize_space_name_matches_tech_design() -> None:
    assert normalize_space_name("  ＳaLeS  ") == ("SaLeS", "sales")


def test_clean_database_passes_without_writes(tmp_path) -> None:
    path, write_conn = _database(tmp_path)
    write_conn.execute("INSERT INTO teams VALUES ('team_a', 'active')")
    write_conn.execute("INSERT INTO users VALUES ('usr_admin', 'active')")
    write_conn.execute("INSERT INTO team_members VALUES ('usr_admin', 'team_a', 'active')")
    write_conn.execute("INSERT INTO workspaces VALUES ('ws_sales', 'team_a', 'Sales', 'active')")
    write_conn.execute("INSERT INTO workspace_members VALUES ('ws_sales', 'usr_admin', 'admin', 'active')")
    write_conn.commit()
    write_conn.close()
    before = path.read_bytes()

    with open_read_only(path) as read_conn:
        report = run_preflight(
            read_conn,
            expected_team_ids=("team_a",),
        )

    assert report["passed"] is True
    assert report["blocking_findings"] == 0
    assert path.read_bytes() == before


def test_findings_cover_migration_blockers_without_names(tmp_path) -> None:
    path, conn = _database(tmp_path)
    conn.executemany(
        "INSERT INTO teams VALUES (?, 'active')",
        [("team_a",), ("team_empty",)],
    )
    conn.executemany(
        "INSERT INTO users VALUES (?, ?)",
        [("usr_admin", "active"), ("usr_removed", "deleted")],
    )
    conn.execute("INSERT INTO team_members VALUES ('usr_admin', 'team_a', 'active')")
    conn.executemany(
        "INSERT INTO workspaces VALUES (?, 'team_a', ?, ?)",
        [
            ("ws_sales", "Sales", "active"),
            ("ws_sales_old", "  ｓＡＬＥＳ  ", "archived"),
            ("ws_blank", "  ", "active"),
        ],
    )
    conn.executemany(
        "INSERT INTO workspace_members VALUES (?, ?, ?, 'active')",
        [
            ("ws_sales", "usr_admin", "admin"),
            ("ws_blank", "usr_removed", "admin"),
            ("ws_missing", "usr_admin", "member"),
        ],
    )
    conn.commit()
    conn.close()

    with open_read_only(path) as read_conn:
        report = run_preflight(
            read_conn,
            expected_team_ids=("team_a", "team_missing"),
        )

    assert report["passed"] is False
    assert report["name_conflicts"][0]["workspace_ids"] == [
        "ws_sales",
        "ws_sales_old",
    ]
    assert "name_key_sha256" in report["name_conflicts"][0]
    assert "Sales" not in str(report["name_conflicts"])
    assert "ｓＡＬＥＳ" not in str(report["name_conflicts"])
    assert report["invalid_space_names"][0]["workspace_id"] == "ws_blank"
    assert report["active_spaces_without_admin"] == [{"team_id": "team_a", "workspace_id": "ws_blank"}]
    assert {(row["workspace_id"], row["reason"]) for row in report["orphan_active_space_memberships"]} == {
        ("ws_blank", "user_inactive"),
        ("ws_missing", "workspace_missing"),
    }
    assert report["active_teams_without_active_spaces"] == [{"team_id": "team_empty"}]
    assert report["database_identity_errors"] == [{"kind": "expected_teams_missing", "team_ids": ["team_missing"]}]


def test_missing_schema_fails_closed(tmp_path) -> None:
    path = tmp_path / "wrong.db"
    sqlite3.connect(path).close()

    with open_read_only(path) as conn:
        report = run_preflight(conn)

    assert report["passed"] is False
    assert {row["table"] for row in report["schema_errors"]} == {
        "teams",
        "users",
        "team_members",
        "workspaces",
        "workspace_members",
    }
