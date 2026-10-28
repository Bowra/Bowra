"""Stage 0A Subagent audit CLI contract tests."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from bowhead_cli.main import app
from bowhead_cli.subagent_audit import (
    audit_subagents,
    report_subagent_migration_impact,
    resolve_sqlite_path,
)
from bowhead_core.access_control import AccessControlStore
from bowhead_core.openapi_tool_source import op_tool_name
from bowhead_core.user_store import (
    TeamSkill,
    TeamSubagent,
    TeamToolSource,
    UserStore,
)
from typer.testing import CliRunner

runner = CliRunner()
_NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
_STAMP = _NOW.isoformat()


def _seed_current_schema(db: Path) -> tuple[str, str, str]:
    async def _inner() -> tuple[str, str, str]:
        store = UserStore(db)
        await store.initialize()
        await AccessControlStore(db).initialize()
        team = await store.create_team(name="Audit team", max_seats=10)
        active = await store.create_user(
            team_id=team.id,
            email="active-owner@secret.example",
            password="Passw0rd!",
        )
        inactive = await store.create_user(
            team_id=team.id,
            email="inactive-owner@secret.example",
            password="Passw0rd!",
        )
        await store.create_team_tool_source(
            TeamToolSource(
                id="ots_disabled",
                team_id=team.id,
                name="Disabled source",
                base_url="https://tools.invalid",
                kind="mcp",
                tools_schema=[{"name": "read_data"}],
                created_by=active.id,
                status="disabled",
                created_at=_STAMP,
                updated_at=_STAMP,
            ),
        )
        for skill in (
            TeamSkill(
                id="oskill_disabled",
                team_id=team.id,
                name="disabled-skill",
                visibility="team_wide",
                created_by=active.id,
                status="disabled",
                created_at=_STAMP,
                updated_at=_STAMP,
            ),
            TeamSkill(
                id="oskill_private",
                team_id=team.id,
                name="private-skill",
                visibility="creator_only",
                created_by=active.id,
                created_at=_STAMP,
                updated_at=_STAMP,
            ),
            TeamSkill(
                id="oskill_pending",
                team_id=team.id,
                name="pending-skill",
                visibility="team_wide",
                approval="pending",
                created_by=active.id,
                created_at=_STAMP,
                updated_at=_STAMP,
            ),
        ):
            await store.create_team_skill(skill)

        disabled_tool = op_tool_name("ots_disabled", "read_data")
        subagents = (
            TeamSubagent(
                id="osa_teamwide",
                team_id=team.id,
                name="teamwide-agent",
                display_name="TOP SECRET DISPLAY",
                system_prompt="TOP SECRET PROMPT",
                tool_refs=["python_sandbox"],
                visibility="team_wide",
                created_by=active.id,
                created_at=_STAMP,
                updated_at=_STAMP,
            ),
            TeamSubagent(
                id="osa_custom",
                team_id=team.id,
                name="custom-agent",
                tool_refs=[disabled_tool, "missing_native_tool"],
                skills=[
                    "disabled-skill",
                    "private-skill",
                    "pending-skill",
                    "missing-skill",
                ],
                visibility="creator_only",
                created_by=inactive.id,
                created_at=_STAMP,
                updated_at=_STAMP,
            ),
            TeamSubagent(
                id="osa_bad_json",
                team_id=team.id,
                name="Bad Name",
                visibility="creator_only",
                created_by=None,
                status="disabled",
                created_at=_STAMP,
                updated_at=_STAMP,
            ),
            TeamSubagent(
                id="osa_unknown_audience",
                team_id=team.id,
                name="unknown-audience",
                visibility="creator_only",
                created_by=active.id,
                created_at=_STAMP,
                updated_at=_STAMP,
            ),
            TeamSubagent(
                id="osa_duplicate_1",
                team_id=team.id,
                name="duplicate-agent",
                visibility="creator_only",
                created_by=active.id,
                status="disabled",
                created_at=_STAMP,
                updated_at=_STAMP,
            ),
        )
        for subagent in subagents:
            await store.create_team_subagent(subagent)
        return team.id, active.id, inactive.id

    team_id, active_id, inactive_id = asyncio.run(_inner())
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "UPDATE users SET status='deleted' WHERE id=?",
            (inactive_id,),
        )
        connection.execute(
            "UPDATE team_members SET status='removed' WHERE user_id=? AND team_id=?",
            (inactive_id, team_id),
        )
        connection.execute(
            "UPDATE team_subagent SET tool_refs_json='not-json', output_schema_json='[]' WHERE id='osa_bad_json'",
        )
        connection.execute("DROP INDEX uq_osa_team_name")
        connection.execute(
            "INSERT INTO team_subagent("
            "id,team_id,name,display_name,description,system_prompt,tool_refs_json,"
            "model,max_iterations,skills_json,output_schema_json,visibility,created_by,"
            "status,created_at,updated_at"
            ") SELECT ?,team_id,name,'','','','[]',NULL,10,'[]',NULL,"
            "visibility,created_by,status,created_at,updated_at "
            "FROM team_subagent WHERE id='osa_duplicate_1'",
            ("osa_duplicate_2",),
        )
        # ``UserStore.create_team_subagent`` now delegates to the canonical
        # aggregate writer, which discovers the shared ACL schema and creates
        # the team-wide grant atomically. Seed only the deliberately divergent
        # rows that the audit fixture still needs.
        grants = [
            _grant_row(
                "grant_unknown",
                team_id,
                "osa_unknown_audience",
                "team",
                team_id,
            ),
        ]
        for index in range(201):
            expiry = None
            if index == 0:
                expiry = "2030-01-01T00:00:00Z"
            elif index == 1:
                expiry = "malformed-expiry"
            grants.append(
                _grant_row(
                    f"grant_roster_{index}",
                    team_id,
                    "osa_custom",
                    "user",
                    f"usr_roster_{index}",
                    expiry,
                ),
            )
        connection.executemany(
            "INSERT INTO resource_grants("
            "id,team_id,resource_kind,resource_id,principal_type,principal_id,"
            "permission,effect,created_by,created_at,updated_at,expires_at,revoked_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            grants,
        )
        connection.commit()
    finally:
        connection.close()
    return team_id, active_id, inactive_id


def _grant_row(
    grant_id: str,
    team_id: str,
    subagent_id: str,
    principal_type: str,
    principal_id: str,
    expires_at: str | None = None,
) -> tuple[str | None, ...]:
    return (
        grant_id,
        team_id,
        "team_subagent",
        subagent_id,
        principal_type,
        principal_id,
        "use",
        "allow",
        None,
        _STAMP,
        _STAMP,
        expires_at,
        None,
    )


def _database_dump(db: Path) -> str:
    connection = sqlite3.connect(db)
    try:
        return "\n".join(connection.iterdump())
    finally:
        connection.close()


def test_current_schema_report_has_expected_migration_aggregates(tmp_path: Path) -> None:
    db = tmp_path / "current.db"
    team_id, _, _ = _seed_current_schema(db)

    report = audit_subagents(db, team_id=team_id, now=_NOW)

    assert report.schema_capabilities == {
        "owner_user_id": False,
        "audience_mode": False,
        "owner_semantics_active": False,
        "audience_semantics_active": False,
        "owner_membership": True,
        "audience_principals": True,
        "resource_grants": True,
        "team_tool_source": True,
        "team_skill": True,
        "validation": False,
    }
    assert report.totals == {
        "subagents": 6,
        "status": {"enabled": 3, "disabled": 3, "other": 0},
        "audience": {"private": 3, "custom": 1, "teamwide": 1, "unknown": 1},
        "owner_state": {"active": 4, "inactive": 1, "null": 1, "unknown": 0},
    }
    assert report.findings["enabled_shared_invalid_owner_subagents"] == 1
    assert report.findings["private_active_allow_subagents"] == 0
    assert report.findings["teamwide_hidden_allow_subagents"] == 1
    assert report.findings["visibility_grant_mismatch_subagents"] == 2
    assert report.findings["expiring_active_allow_subagents"] == 1
    assert report.findings["expiring_active_allow_grants"] == 2
    assert report.findings["malformed_allow_expiry_grants"] == 1
    assert report.findings["custom_roster_over_limit_subagents"] == 1
    assert report.findings["invalid_audience_principal_subagents"] == 1
    assert report.findings["invalid_audience_principal_grants"] == 201
    assert report.findings["invalid_name_subagents"] == 1
    assert report.findings["duplicate_name_subagents"] == 2
    assert report.findings["malformed_definition_json_subagents"] == 1
    assert report.findings["missing_or_disallowed_tool_ref_subagents"] == 1
    assert report.findings["disabled_tool_ref_subagents"] == 1
    assert report.findings["missing_skill_ref_subagents"] == 1
    assert report.findings["disabled_skill_ref_subagents"] == 1
    assert report.findings["unapproved_or_unattachable_skill_ref_subagents"] == 1
    assert report.findings["validation_pending_subagents"] is None


def test_current_schema_impact_has_mutually_exclusive_dispositions(
    tmp_path: Path,
) -> None:
    db = tmp_path / "current-impact.db"
    team_id, _, _ = _seed_current_schema(db)

    report = report_subagent_migration_impact(
        db,
        team_id=team_id,
        now=_NOW,
    )

    assert report.report_kind == "subagent_release_b_migration_impact"
    assert report.dry_run is True
    assert report.writes_performed == 0
    assert report.semantic_sources == {
        "owner": "created_by_compatibility",
        "audience": "legacy_visibility_grants_compatibility",
    }
    assert report.completeness["automatic_repairs_computable"] is True
    assert report.actions["owner"] == {
        "disable_invalid_owner_subagents": 1,
        "inactive_owner_subagents": 1,
        "null_owner_subagents": 0,
        "owner_transfers": 0,
    }
    audience = report.actions["audience"]
    assert audience["dispositions"] == {
        "unchanged_private_subagents": 3,
        "unchanged_custom_subagents": 0,
        "unchanged_teamwide_subagents": 0,
        "private_revoke_allow_subagents": 0,
        "teamwide_clear_hidden_allow_subagents": 1,
        "custom_prune_keep_custom_subagents": 0,
        "custom_prune_to_private_subagents": 1,
        "custom_roster_over_limit_needs_attention_subagents": 0,
        "fail_closed_to_private_subagents": 1,
        "projection_repair_only_subagents": 0,
    }
    assert sum(audience["dispositions"].values()) == report.totals["subagents"]
    assert audience["automatic_change_subagents"] == 3
    assert audience["audience_contraction_subagents"] == 2
    assert audience["to_private_subagents"] == 2
    assert audience["needs_attention_subagents"] == 0
    assert audience["revoke_allow_grants"] == 203
    assert audience["deny_grants_changed"] == 0
    assert report.totals["access_or_lifecycle_change_subagents"] == 3


def test_cli_json_is_read_only_and_does_not_leak_sensitive_rows(tmp_path: Path) -> None:
    db = tmp_path / "readonly.db"
    team_id, _, _ = _seed_current_schema(db)
    before = _database_dump(db)

    result = runner.invoke(
        app,
        [
            "admin",
            "audit-subagents",
            "--team-id",
            team_id,
            "--db",
            f"sqlite+aiosqlite:///{db}",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["team_id"] == team_id
    assert payload["totals"]["subagents"] == 6
    assert _database_dump(db) == before
    for sensitive in (
        "TOP SECRET",
        "active-owner@secret.example",
        "osa_teamwide",
        "usr_roster_0",
        "Bad Name",
        "duplicate-agent",
    ):
        assert sensitive not in result.output


def test_impact_cli_is_read_only_and_does_not_leak_sensitive_rows(
    tmp_path: Path,
) -> None:
    db = tmp_path / "readonly-impact.db"
    team_id, _, _ = _seed_current_schema(db)
    before = _database_dump(db)

    result = runner.invoke(
        app,
        [
            "admin",
            "report-subagent-migration-impact",
            "--team-id",
            team_id,
            "--db",
            str(db),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["writes_performed"] == 0
    assert payload["actions"]["audience"]["revoke_allow_grants"] == 203
    assert _database_dump(db) == before
    for sensitive in (
        "TOP SECRET",
        "active-owner@secret.example",
        "osa_teamwide",
        "usr_roster_0",
        "Bad Name",
        "duplicate-agent",
    ):
        assert sensitive not in result.output


def test_future_schema_uses_explicit_owner_audience_and_validation(tmp_path: Path) -> None:
    db = tmp_path / "future.db"
    team_id, active_id, _ = _seed_current_schema(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute("ALTER TABLE team_subagent ADD COLUMN owner_user_id TEXT")
        connection.execute("ALTER TABLE team_subagent ADD COLUMN audience_mode TEXT")
        connection.executescript(
            """
            CREATE TABLE team_subagent_schema_meta (
              singleton INTEGER PRIMARY KEY,
              schema_version INTEGER NOT NULL,
              minimum_reader_version TEXT NOT NULL,
              minimum_writer_version TEXT NOT NULL,
              owner_semantics_activated_at TEXT,
              lifecycle_semantics_activated_at TEXT,
              audience_semantics_activated_at TEXT,
              updated_at TEXT NOT NULL
            );
            INSERT INTO team_subagent_schema_meta(
              singleton,schema_version,minimum_reader_version,minimum_writer_version,
              owner_semantics_activated_at,lifecycle_semantics_activated_at,
              audience_semantics_activated_at,updated_at
            ) VALUES (
              1,2,'2','2','2026-07-27T00:00:00Z',NULL,
              '2026-07-27T00:00:00Z','2026-07-27T00:00:00Z'
            );
            """,
        )
        connection.execute(
            "UPDATE team_subagent SET owner_user_id=created_by, "
            "audience_mode=CASE "
            "WHEN id='osa_teamwide' THEN 'teamwide' "
            "WHEN id IN ('osa_custom','osa_unknown_audience') THEN 'custom' "
            "ELSE 'private' END",
        )
        connection.execute(
            "UPDATE team_subagent SET owner_user_id=? WHERE id='osa_bad_json'",
            (active_id,),
        )
        connection.execute(
            "INSERT INTO resource_grants("
            "id,team_id,resource_kind,resource_id,principal_type,principal_id,"
            "permission,effect,created_by,created_at,updated_at,expires_at,revoked_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            _grant_row(
                "grant_private",
                team_id,
                "osa_bad_json",
                "user",
                active_id,
            ),
        )
        connection.execute(
            "CREATE TABLE team_subagent_validation(team_id TEXT NOT NULL, subagent_id TEXT NOT NULL, status TEXT NOT NULL)",
        )
        connection.executemany(
            "INSERT INTO team_subagent_validation(team_id,subagent_id,status) VALUES (?,?,?)",
            [
                (team_id, subagent_id, "valid")
                for subagent_id in (
                    "osa_teamwide",
                    "osa_custom",
                    "osa_unknown_audience",
                    "osa_duplicate_1",
                    "osa_duplicate_2",
                )
            ],
        )
        connection.commit()
    finally:
        connection.close()

    report = audit_subagents(db, team_id=team_id, now=_NOW)

    assert report.schema_capabilities["owner_user_id"] is True
    assert report.schema_capabilities["audience_mode"] is True
    assert report.schema_capabilities["owner_semantics_active"] is True
    assert report.schema_capabilities["audience_semantics_active"] is True
    assert report.schema_capabilities["validation"] is True
    assert report.findings["private_active_allow_subagents"] == 1
    assert report.findings["private_active_allow_grants"] == 1
    assert report.findings["creator_owner_mismatch_subagents"] == 1
    assert report.findings["validation_pending_subagents"] == 1

    impact = report_subagent_migration_impact(
        db,
        team_id=team_id,
        now=_NOW,
    )
    assert impact.semantic_sources == {
        "owner": "owner_user_id",
        "audience": "audience_mode",
    }
    assert impact.actions["audience"]["dispositions"] == {
        "unchanged_private_subagents": 2,
        "unchanged_custom_subagents": 0,
        "unchanged_teamwide_subagents": 0,
        "private_revoke_allow_subagents": 1,
        "teamwide_clear_hidden_allow_subagents": 1,
        "custom_prune_keep_custom_subagents": 0,
        "custom_prune_to_private_subagents": 2,
        "custom_roster_over_limit_needs_attention_subagents": 0,
        "fail_closed_to_private_subagents": 0,
        "projection_repair_only_subagents": 0,
    }
    assert impact.actions["audience"]["revoke_allow_grants"] == 204
    assert impact.actions["audience"]["to_private_subagents"] == 2
    assert impact.follow_up["validation_pending_subagents"] == 1


def test_cli_rejects_missing_team_and_non_sqlite_urls(tmp_path: Path) -> None:
    db = tmp_path / "errors.db"
    _seed_current_schema(db)
    missing = runner.invoke(
        app,
        [
            "admin",
            "audit-subagents",
            "--team-id",
            "team_missing",
            "--db",
            str(db),
        ],
    )

    assert missing.exit_code == 1
    assert "team not found" in missing.output

    try:
        resolve_sqlite_path("postgresql://db.example/bowhead")
    except ValueError as exc:
        assert "only supports SQLite" in str(exc)
    else:
        raise AssertionError("non-SQLite audit source must be rejected")


def test_release_a_columns_do_not_activate_new_semantics_early(tmp_path: Path) -> None:
    db = tmp_path / "release-a.db"
    team_id, _, _ = _seed_current_schema(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute("ALTER TABLE team_subagent ADD COLUMN owner_user_id TEXT")
        connection.execute("ALTER TABLE team_subagent ADD COLUMN audience_mode TEXT DEFAULT 'private'")
        connection.execute("UPDATE team_subagent SET owner_user_id=NULL, audience_mode='private'")
        connection.commit()
    finally:
        connection.close()

    report = audit_subagents(db, team_id=team_id, now=_NOW)

    assert report.schema_capabilities["owner_user_id"] is True
    assert report.schema_capabilities["audience_mode"] is True
    assert report.schema_capabilities["owner_semantics_active"] is False
    assert report.schema_capabilities["audience_semantics_active"] is False
    assert report.totals["audience"] == {
        "private": 3,
        "custom": 1,
        "teamwide": 1,
        "unknown": 1,
    }
    assert report.totals["owner_state"] == {
        "active": 4,
        "inactive": 1,
        "null": 1,
        "unknown": 0,
    }
    assert report.findings["creator_owner_mismatch_subagents"] is None


def test_legacy_missing_optional_tables_reports_not_available(tmp_path: Path) -> None:
    db = tmp_path / "minimal-legacy.db"
    connection = sqlite3.connect(db)
    try:
        connection.executescript(
            """
            CREATE TABLE teams (id TEXT PRIMARY KEY);
            CREATE TABLE team_subagent (
              id TEXT PRIMARY KEY,
              team_id TEXT NOT NULL,
              name TEXT NOT NULL,
              tool_refs_json TEXT NOT NULL,
              skills_json TEXT NOT NULL,
              output_schema_json TEXT,
              visibility TEXT NOT NULL,
              created_by TEXT,
              status TEXT NOT NULL
            );
            INSERT INTO teams(id) VALUES ('team_legacy');
            INSERT INTO team_subagent(
              id,team_id,name,tool_refs_json,skills_json,output_schema_json,
              visibility,created_by,status
            ) VALUES (
              'osa_legacy','team_legacy','legacy-agent','[]','[]',NULL,
              'creator_only','usr_unknown','enabled'
            );
            """,
        )
        connection.commit()
    finally:
        connection.close()

    report = audit_subagents(db, team_id="team_legacy", now=_NOW)

    assert report.totals["audience"]["unknown"] == 1
    assert report.totals["owner_state"]["unknown"] == 1
    for finding in (
        "enabled_shared_invalid_owner_subagents",
        "private_active_allow_subagents",
        "visibility_grant_mismatch_subagents",
        "invalid_audience_principal_subagents",
        "missing_or_disallowed_tool_ref_subagents",
        "missing_skill_ref_subagents",
        "validation_pending_subagents",
    ):
        assert report.findings[finding] is None

    impact = report_subagent_migration_impact(
        db,
        team_id="team_legacy",
        now=_NOW,
    )
    assert impact.completeness["automatic_repairs_computable"] is False
    assert impact.totals["access_or_lifecycle_change_subagents"] is None
    assert impact.actions["owner"]["disable_invalid_owner_subagents"] is None
    assert all(value is None for value in impact.actions["audience"]["dispositions"].values())
    assert impact.actions["audience"]["revoke_allow_grants"] is None
    assert impact.actions["audience"]["deny_grants_changed"] == 0
    assert impact.completeness["unavailable_sections"] == [
        "owner_actions",
        "audience_actions",
        "tool_dependency_checks",
        "skill_dependency_checks",
        "validation_state",
    ]
    cli_result = runner.invoke(
        app,
        [
            "admin",
            "report-subagent-migration-impact",
            "--team-id",
            "team_legacy",
            "--db",
            str(db),
        ],
    )
    assert cli_result.exit_code == 0, cli_result.output
    assert "not_available" in cli_result.output


def test_custom_over_limit_preserves_valid_roster_and_flags_attention(
    tmp_path: Path,
) -> None:
    db = tmp_path / "over-limit.db"
    team_id, active_id, _ = _seed_current_schema(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "DELETE FROM resource_grants WHERE resource_id='osa_custom'",
        )
        connection.executemany(
            "INSERT INTO groups(id,team_id,workspace_id,name,description,status,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    f"group_valid_{index}",
                    team_id,
                    "",
                    f"valid-group-{index}",
                    "",
                    "active",
                    active_id,
                    _STAMP,
                    _STAMP,
                )
                for index in range(201)
            ],
        )
        connection.executemany(
            "INSERT INTO resource_grants("
            "id,team_id,resource_kind,resource_id,principal_type,principal_id,"
            "permission,effect,created_by,created_at,updated_at,expires_at,"
            "revoked_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                _grant_row(
                    f"grant_valid_group_{index}",
                    team_id,
                    "osa_custom",
                    "group",
                    f"group_valid_{index}",
                )
                for index in range(201)
            ],
        )
        connection.commit()
    finally:
        connection.close()

    impact = report_subagent_migration_impact(
        db,
        team_id=team_id,
        now=_NOW,
    )

    audience = impact.actions["audience"]
    assert audience["dispositions"]["custom_roster_over_limit_needs_attention_subagents"] == 1
    assert audience["needs_attention_subagents"] == 1
    assert audience["to_private_subagents"] == 1
    assert audience["revoke_allow_grants"] == 2


def test_custom_prunes_expiring_principal_but_keeps_valid_roster(
    tmp_path: Path,
) -> None:
    db = tmp_path / "custom-prune.db"
    team_id, active_id, _ = _seed_current_schema(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "DELETE FROM resource_grants WHERE resource_id='osa_custom'",
        )
        connection.executemany(
            "INSERT INTO groups(id,team_id,workspace_id,name,description,status,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    group_id,
                    team_id,
                    "",
                    group_id,
                    "",
                    "active",
                    active_id,
                    _STAMP,
                    _STAMP,
                )
                for group_id in ("group_retained", "group_expiring")
            ],
        )
        connection.executemany(
            "INSERT INTO resource_grants("
            "id,team_id,resource_kind,resource_id,principal_type,principal_id,"
            "permission,effect,created_by,created_at,updated_at,expires_at,"
            "revoked_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                _grant_row(
                    "grant_retained",
                    team_id,
                    "osa_custom",
                    "group",
                    "group_retained",
                ),
                _grant_row(
                    "grant_expiring",
                    team_id,
                    "osa_custom",
                    "group",
                    "group_expiring",
                    "2030-01-01T00:00:00Z",
                ),
            ],
        )
        connection.commit()
    finally:
        connection.close()

    impact = report_subagent_migration_impact(
        db,
        team_id=team_id,
        now=_NOW,
    )

    audience = impact.actions["audience"]
    assert audience["dispositions"]["custom_prune_keep_custom_subagents"] == 1
    assert audience["audience_contraction_subagents"] == 2
    assert audience["to_private_subagents"] == 1
    assert audience["revoke_allow_grants"] == 3


def test_enabled_null_owner_is_disabled_without_transfer(tmp_path: Path) -> None:
    db = tmp_path / "null-owner.db"
    team_id, _, _ = _seed_current_schema(db)
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            "UPDATE team_subagent SET status='enabled' WHERE id='osa_bad_json'",
        )
        connection.commit()
    finally:
        connection.close()

    impact = report_subagent_migration_impact(
        db,
        team_id=team_id,
        now=_NOW,
    )

    assert impact.actions["owner"] == {
        "disable_invalid_owner_subagents": 2,
        "inactive_owner_subagents": 1,
        "null_owner_subagents": 1,
        "owner_transfers": 0,
    }
