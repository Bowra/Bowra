from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bowhead_cli.migrate import upgrade
from bowhead_core.agent_assignment_store import (
    AgentAssignmentStore,
    AgentAssignmentUnavailableError,
)
from bowhead_core.audit_actions import AuditAction
from bowhead_core.user_store import UserStore


def _chain_head() -> str:
    """alembic 链条当前 head。

    ⚠ **别写死 revision 号**：本文件原先把 head 抄成字面量，于是**每加一条新迁移都要
    改这几处**（0029 / 0030 各撞过一次）。它们想证的是「升到了链条顶端」，
    不是「顶端恰好是某个号」。同 `bowhead_core.migrations.chain_head` 的理由。
    """
    from bowhead_core.migrations import chain_head

    return chain_head()


def _tables(db_path) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'",
            )
        }


def _upgrade_to(db_path, revision: str) -> None:
    from bowhead_core import migrations

    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(migrations.__file__).resolve().parent),
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, revision)


def test_0026_creates_recoverable_marketplace_schema_without_activating_cutover(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"

    assert upgrade(f"sqlite+aiosqlite:///{db_path}", target="core") == ("core",)
    assert upgrade(f"sqlite+aiosqlite:///{db_path}", target="core") == ("core",)

    assert {
        "agent_assignment",
        "agent_default_policy",
        "agent_default_opt_out",
        "agent_catalog_projection",
        "agent_name_claim",
        "agent_name_conflict_group",
        "agent_marketplace_schema_meta",
        "agent_assignment_backfill_run",
        "agent_assignment_backfill_expected",
        "team_openapi_keys",
        "team_openapi_call_logs",
        "agent_listing_safety_report",
        "agent_listing_safety_finding",
    } <= _tables(db_path)
    with sqlite3.connect(db_path) as connection:
        head = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        marker = connection.execute(
            """
            SELECT schema_version,assignment_cutover_complete_at,
                   minimum_reader_version,minimum_writer_version
            FROM agent_marketplace_schema_meta WHERE singleton=1
            """,
        ).fetchone()
        indexes = {str(row[1]) for row in connection.execute("PRAGMA index_list(agent_assignment)")}
        run_indexes = {str(row[1]) for row in connection.execute("PRAGMA index_list(agent_assignment_backfill_run)")}
    assert head == (_chain_head(),)
    assert marker == (2, None, "0002", "0002")
    assert {"idx_agent_assignment_user_state", "idx_agent_assignment_agent_state"} <= indexes
    assert "uq_agent_assignment_backfill_single_running" in run_indexes


def test_0028_adds_listing_safety_after_0027_without_mutating_agent_rows(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "0027_team_openapi")
    timestamp = "2026-09-02T00:00:00+00:00"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO teams(id,name,status,created_at) VALUES ('team_listing','Team','active',?)",
            (timestamp,),
        )
        connection.execute(
            """
            INSERT INTO team_subagent(
              id,team_id,name,display_name,description,tool_refs_json,skills_json,
              visibility,status,created_at,updated_at
            ) VALUES ('osa_listing','team_listing','listing','Listing','Safe listing','[]','[]',
                      'team_wide','enabled',?,?)
            """,
            (timestamp, timestamp),
        )
        before = connection.execute(
            "SELECT id,name,display_name,description FROM team_subagent WHERE id='osa_listing'",
        ).fetchone()
        connection.commit()

    _upgrade_to(db_path, "head")
    _upgrade_to(db_path, "head")

    with sqlite3.connect(db_path) as connection:
        after = connection.execute(
            "SELECT id,name,display_name,description FROM team_subagent WHERE id='osa_listing'",
        ).fetchone()
        tables = _tables(db_path)
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        report_indexes = {str(row[1]) for row in connection.execute("PRAGMA index_list(agent_listing_safety_report)")}
        report_foreign_keys = {
            (str(row[2]), str(row[3]), str(row[4]), str(row[6])) for row in connection.execute("PRAGMA foreign_key_list(agent_listing_safety_report)")
        }
    assert after == before
    assert {"agent_listing_safety_report", "agent_listing_safety_finding"} <= tables
    assert {
        "idx_agent_listing_safety_agent_time",
        "idx_agent_listing_safety_effective",
    } <= report_indexes
    assert {
        ("team_subagent", "team_id", "team_id", "CASCADE"),
        ("team_subagent", "agent_id", "id", "CASCADE"),
    } <= report_foreign_keys
    assert revision == (_chain_head(),)

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO teams(id,name,status,created_at) VALUES ('team_other','Other','active',?)",
            (timestamp,),
        )
        connection.execute(
            """
            INSERT INTO team_subagent(
              id,team_id,name,display_name,description,tool_refs_json,skills_json,
              visibility,status,created_at,updated_at
            ) VALUES ('osa_other','team_other','other','Other','Safe','[]','[]',
                      'team_wide','enabled',?,?)
            """,
            (timestamp, timestamp),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO agent_listing_safety_report(
                  id,team_id,agent_id,listing_hash,scanner_version,deterministic_status,
                  semantic_status,effective_status,team_dictionary_revision,created_at,completed_at
                ) VALUES ('alsr_cross_team','team_listing','osa_other','sha256:cross','v1','pass',
                          'not_run','pass','none',?,?)
                """,
                (timestamp, timestamp),
            )
        connection.execute(
            """
            INSERT INTO agent_listing_safety_report(
              id,team_id,agent_id,listing_hash,scanner_version,deterministic_status,
              semantic_status,effective_status,team_dictionary_revision,created_at,completed_at
            ) VALUES ('alsr_valid','team_listing','osa_listing','sha256:valid','v1','blocked',
                      'not_run','blocked','none',?,?)
            """,
            (timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO agent_listing_safety_finding(
              report_id,ordinal,rule_family,field_path,severity,disposition
            ) VALUES ('alsr_valid',0,'pii_email','listing.description','critical','open')
            """,
        )
        connection.execute("DELETE FROM team_subagent WHERE id='osa_listing'")
        assert connection.execute(
            "SELECT COUNT(*) FROM agent_listing_safety_report WHERE id='alsr_valid'",
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM agent_listing_safety_finding WHERE report_id='alsr_valid'",
        ).fetchone() == (0,)


def test_0029_adds_inert_release_foundation_with_tenant_and_immutability_guards(
    tmp_path,
) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "0028_agent_listing_safety")
    timestamp = "2026-09-02T01:00:00+00:00"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO teams(id,name,status,created_at) VALUES ('team_m2','M2','active',?)",
            (timestamp,),
        )
        connection.execute(
            "INSERT INTO teams(id,name,status,created_at) VALUES ('team_m2_other','Other','active',?)",
            (timestamp,),
        )
        connection.execute(
            """
            INSERT INTO team_subagent(
              id,team_id,name,display_name,description,tool_refs_json,skills_json,
              visibility,status,created_at,updated_at
            ) VALUES ('osa_m2','team_m2','m2','M2 Agent','Legacy','[]','[]',
                      'team_wide','enabled',?,?)
            """,
            (timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO team_subagent(
              id,team_id,name,display_name,description,tool_refs_json,skills_json,
              visibility,status,created_at,updated_at
            ) VALUES ('osa_m2_other','team_m2_other','other','Other','Legacy','[]','[]',
                      'team_wide','enabled',?,?)
            """,
            (timestamp, timestamp),
        )
        before = connection.execute(
            "SELECT id,team_id,name,display_name,description,status FROM team_subagent WHERE id='osa_m2'",
        ).fetchone()
        connection.commit()

    _upgrade_to(db_path, "head")
    _upgrade_to(db_path, "head")

    expected_tables = {
        "team_subagent_revision",
        "team_subagent_working_copy",
        "team_subagent_release",
        "team_subagent_release_candidate",
        "agent_safety_report",
        "agent_command_receipt",
    }
    assert expected_tables <= _tables(db_path)

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (_chain_head(),)
        assert (
            connection.execute(
                "SELECT id,team_id,name,display_name,description,status FROM team_subagent WHERE id='osa_m2'",
            ).fetchone()
            == before
        )
        assert connection.execute(
            """
            SELECT live_revision_id,live_release_id,release_sequence,archived_at,
                   archived_by,last_release_event_id,definition_mode
            FROM team_subagent WHERE id='osa_m2'
            """,
        ).fetchone() == (None, None, 0, None, None, None, "legacy_mutable")

        connection.execute(
            """
            INSERT INTO team_subagent_revision(
              id,team_id,subagent_id,sequence,revision_kind,definition_json,listing_json,
              definition_hash,dependency_fingerprint,capability_fingerprint,
              risk_compiler_version,risk_json,created_by,created_at
            ) VALUES (
              'arv_m2_1','team_m2','osa_m2',1,'imported_baseline','{}','{}',
              'sha256:def','sha256:dep','sha256:cap','risk-v1','{}','user_owner',?
            )
            """,
            (timestamp,),
        )
        connection.execute(
            """
            INSERT INTO agent_safety_report(
              id,team_id,agent_id,subject_kind,subject_ref,content_hash,
              dependency_fingerprint,compiler_version,semantic_scanner_version,status,
              deterministic_json,semantic_json,safe_summary_json,created_by,created_at
            ) VALUES (
              'asr_m2_1','team_m2','osa_m2','revision','arv_m2_1','sha256:def',
              'sha256:dep','safety-v1','none','warning','{}','{}','{}','user_owner',?
            )
            """,
            (timestamp,),
        )
        connection.execute(
            """
            INSERT INTO team_subagent_release(
              id,team_id,subagent_id,release_sequence,revision_id,audience_snapshot_json,
              audience_hash,capability_fingerprint,dependency_fingerprint,risk_json,
              safety_report_id,activated_by,activated_at
            ) VALUES (
              'arl_m2_1','team_m2','osa_m2',1,'arv_m2_1','{}','sha256:audience',
              'sha256:cap','sha256:dep','{}','asr_m2_1','user_owner',?
            )
            """,
            (timestamp,),
        )
        connection.execute(
            """
            INSERT INTO team_subagent_working_copy(
              id,team_id,subagent_id,base_release_id,definition_json,listing_json,
              audience_json,validation_json,updated_by,created_at,updated_at
            ) VALUES (
              'awc_m2_1','team_m2','osa_m2','arl_m2_1','{}','{}','{}','{}',
              'user_owner',?,?
            )
            """,
            (timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO team_subagent_release_candidate(
              id,team_id,subagent_id,revision_id,base_live_release_id,
              target_audience_json,target_audience_hash,capability_fingerprint,
              dependency_fingerprint,risk_compiler_version,risk_json,safety_report_id,
              candidate_hash,state,submitted_by,submitted_at,updated_at
            ) VALUES (
              'arc_m2_1','team_m2','osa_m2','arv_m2_1','arl_m2_1','{}',
              'sha256:audience','sha256:cap','sha256:dep','risk-v1','{}','asr_m2_1',
              'sha256:candidate','pending','user_owner',?,?
            )
            """,
            (timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO agent_command_receipt(
              team_id,actor_user_id,command_scope,idempotency_hash,request_hash,
              result_json,created_at,expires_at
            ) VALUES (
              'team_m2','user_owner','agent.publish','sha256:key','sha256:request',
              '{}',?,?
            )
            """,
            (timestamp, "2026-09-03T01:00:00+00:00"),
        )

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE team_subagent_revision SET risk_json='changed' WHERE id='arv_m2_1'",
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM team_subagent_release WHERE id='arl_m2_1'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE agent_safety_report SET status='pass' WHERE id='asr_m2_1'",
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO team_subagent_release_candidate(
                  id,team_id,subagent_id,revision_id,target_audience_json,
                  target_audience_hash,capability_fingerprint,dependency_fingerprint,
                  risk_compiler_version,risk_json,safety_report_id,candidate_hash,state,
                  submitted_by,submitted_at,updated_at
                ) VALUES (
                  'arc_m2_2','team_m2','osa_m2','arv_m2_1','{}','sha256:audience',
                  'sha256:cap','sha256:dep','risk-v1','{}','asr_m2_1','sha256:other',
                  'pending','user_owner',?,?
                )
                """,
                (timestamp, timestamp),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO team_subagent_revision(
                  id,team_id,subagent_id,sequence,revision_kind,definition_json,listing_json,
                  definition_hash,dependency_fingerprint,capability_fingerprint,
                  risk_compiler_version,risk_json,created_by,created_at
                ) VALUES (
                  'arv_cross_team','team_m2','osa_m2_other',1,'authored','{}','{}',
                  'sha256:cross','sha256:dep','sha256:cap','risk-v1','{}','user_owner',?
                )
                """,
                (timestamp,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE team_subagent_release_candidate
                SET decided_at=?, decided_by=NULL, self_review=NULL
                WHERE id='arc_m2_1'
                """,
                (timestamp,),
            )
        assert list(connection.execute("PRAGMA foreign_key_check")) == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_0031_adds_inert_candidate_name_and_baseline_ledgers(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "0030_canvas_surface_target")
    timestamp = "2026-09-03T00:00:00Z"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO teams(id,name,status,created_at) VALUES ('team_0031','Team','active',?)",
            (timestamp,),
        )
        connection.execute(
            """
            INSERT INTO team_subagent(
              id,team_id,name,display_name,description,tool_refs_json,skills_json,
              visibility,status,created_at,updated_at
            ) VALUES ('osa_0031','team_0031','baseline','Baseline','Safe','[]','[]',
                      'team_wide','enabled',?,?)
            """,
            (timestamp, timestamp),
        )
        before = connection.execute(
            "SELECT live_revision_id,live_release_id,release_sequence,definition_mode,row_version FROM team_subagent",
        ).fetchone()
        connection.commit()

    _upgrade_to(db_path, "head")
    _upgrade_to(db_path, "head")
    with sqlite3.connect(db_path) as connection:
        tables = _tables(db_path)
        after = connection.execute(
            "SELECT live_revision_id,live_release_id,release_sequence,definition_mode,row_version FROM team_subagent",
        ).fetchone()
        run_indexes = {str(row[1]) for row in connection.execute("PRAGMA index_list(agent_release_baseline_run)")}
        candidate_fks = {
            (str(row[2]), str(row[3]), str(row[4]), str(row[6]))
            for row in connection.execute("PRAGMA foreign_key_list(agent_release_candidate_name_claim)")
        }
    assert {
        "agent_release_candidate_name_claim",
        "agent_release_baseline_run",
        "agent_release_baseline_expected",
    } <= tables
    assert after == before == (None, None, 0, "legacy_mutable", 1)
    assert "uq_agent_release_baseline_one_open" in run_indexes
    assert {
        ("team_subagent", "team_id", "team_id", "RESTRICT"),
        ("team_subagent", "agent_id", "id", "RESTRICT"),
    } <= candidate_fks
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO agent_release_baseline_run(
              id,state,compiler_version,snapshot_at,started_at,updated_at
            ) VALUES ('abr_constraint','staging','v1',?,?,?)
            """,
            (timestamp, timestamp, timestamp),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO agent_release_baseline_expected(
                  run_id,team_id,agent_id,source_row_version,source_updated_at,
                  stage_state,source_hash
                ) VALUES ('abr_constraint','team_0031','osa_0031',1,?,'pending','sha256:invalid')
                """,
                (timestamp,),
            )


def test_0031_rejects_partially_persisted_index_with_wrong_semantics(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "head")
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP INDEX uq_agent_release_baseline_one_open")
        connection.execute(
            """
            CREATE UNIQUE INDEX uq_agent_release_baseline_one_open
            ON agent_release_baseline_run(id) WHERE state='completed'
            """,
        )
        connection.execute("UPDATE alembic_version SET version_num='0030_canvas_surface_target'")
        connection.commit()

    with pytest.raises(RuntimeError, match="incompatible index semantics"):
        _upgrade_to(db_path, "head")


def test_0032_adds_atomic_release_event_and_outbox_boundary(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "head")
    _upgrade_to(db_path, "head")

    with sqlite3.connect(db_path) as connection:
        tables = _tables(db_path)
        event_indexes = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA index_list(team_subagent_release_event)",
            )
        }
        outbox_indexes = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA index_list(team_subagent_notification_outbox)",
            )
        }
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger'",
            )
        }
    assert {
        "team_subagent_release_event",
        "team_subagent_notification_outbox",
    } <= tables
    assert "idx_agent_release_event_history" in event_indexes
    assert "idx_agent_release_outbox_pending" in outbox_indexes
    assert {
        "trg_team_subagent_release_candidate_immutable_delete",
        "trg_team_subagent_release_candidate_payload_immutable",
        "trg_team_subagent_release_event_actor_team",
        "trg_team_subagent_release_event_immutable_update",
        "trg_team_subagent_release_event_immutable_delete",
        "trg_team_subagent_release_outbox_payload_immutable",
        "trg_team_subagent_release_outbox_recipient_team",
    } <= triggers


def test_0033_adds_inert_cutover_marker_and_guards_revisioned_writers(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "head")
    timestamp = "2026-09-03T08:00:00+00:00"
    with sqlite3.connect(db_path) as connection:
        marker = connection.execute(
            """
            SELECT release_semantics_activated_at FROM team_subagent_schema_meta
            WHERE singleton=1
            """,
        ).fetchone()
        assert marker == (None,)
        connection.execute(
            "INSERT INTO teams(id,name,status,created_at) VALUES ('team_0033','Team','active',?)",
            (timestamp,),
        )
        connection.execute(
            """
            INSERT INTO team_subagent(
              id,team_id,name,display_name,description,tool_refs_json,skills_json,
              visibility,status,created_at,updated_at,owner_user_id,audience_mode,
              lifecycle_state,definition_mode
            ) VALUES (
              'osa_0033','team_0033','guarded','Guarded','Before','[]','[]',
              'creator_only','enabled',?,?,'user_owner','private','active','revisioned'
            )
            """,
            (timestamp, timestamp),
        )
        connection.execute(
            """
            UPDATE team_subagent_schema_meta
            SET release_semantics_activated_at=? WHERE singleton=1
            """,
            (timestamp,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="revision_writer_required"):
            connection.execute(
                """
                UPDATE team_subagent SET description='Bypass',row_version=row_version+1
                WHERE id='osa_0033'
                """,
            )
        connection.execute(
            """
            UPDATE team_subagent
            SET status='disabled',lifecycle_state='archived',archived_at=?,archived_by=?,
                row_version=row_version+1
            WHERE id='osa_0033'
            """,
            (timestamp, "user_owner"),
        )
        assert connection.execute(
            "SELECT status,lifecycle_state,row_version FROM team_subagent WHERE id='osa_0033'",
        ).fetchone() == ("disabled", "archived", 2)


def test_0034_adds_exact_agent_binding_tables_and_guards(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "head")
    _upgrade_to(db_path, "head")

    with sqlite3.connect(db_path) as connection:
        tables = _tables(db_path)
        execution_indexes = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA index_list(turn_agent_execution)",
            )
        }
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger'",
            )
        }
    assert {
        "system_agent_effective_revision",
        "turn_agent_binding",
        "turn_agent_execution",
    } <= tables
    assert {
        "idx_turn_agent_execution_turn",
        "idx_turn_agent_execution_agent",
    } <= execution_indexes
    assert {
        "trg_system_agent_effective_revision_immutable_update",
        "trg_system_agent_effective_revision_immutable_delete",
        "trg_turn_agent_binding_scope",
        "trg_turn_agent_binding_assignment",
        "trg_turn_agent_binding_revision",
        "trg_turn_agent_binding_immutable_update",
        "trg_turn_agent_execution_scope",
        "trg_turn_agent_execution_revision",
        "trg_turn_agent_execution_identity_immutable",
        "trg_turn_agent_execution_status_transition",
    } <= triggers


def test_0035_adds_normalized_scheduled_agent_binding_and_guards(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "head")
    _upgrade_to(db_path, "head")

    with sqlite3.connect(db_path) as connection:
        override_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(team_builtin_subagent_override)",
            )
        }
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(scheduled_job_agent_binding)",
            )
        }
        indexes = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA index_list(scheduled_job_agent_binding)",
            )
        }
        triggers = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_schema
                WHERE type='trigger' AND tbl_name='scheduled_job_agent_binding'
                """,
            )
        }
    assert {
        "job_id",
        "assignment_id",
        "agent_id",
        "agent_source_kind",
        "revision_ref",
        "release_id",
        "capability_fingerprint",
        "dependency_fingerprint",
        "follow_mode",
        "state",
        "confirmed_by",
        "confirmed_at",
        "row_version",
    } == columns
    assert "primary_capability" in override_columns
    assert "idx_scheduled_agent_binding_state" in indexes
    assert {
        "trg_scheduled_agent_binding_insert",
        "trg_scheduled_agent_binding_update",
    } == triggers


def test_0036_adds_exact_capability_execution_ledger_and_guards(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "head")
    _upgrade_to(db_path, "head")

    with sqlite3.connect(db_path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(turn_capability_execution)",
            )
        }
        indexes = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA index_list(turn_capability_execution)",
            )
        }
        triggers = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_schema
                WHERE type='trigger' AND tbl_name='turn_capability_execution'
                """,
            )
        }
        parent_triggers = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_schema
                WHERE type='trigger' AND tbl_name='turn_agent_execution'
                """,
            )
        }
    assert {
        "id",
        "team_id",
        "turn_id",
        "agent_execution_id",
        "composition_node_id",
        "attempt_generation",
        "capability_id",
        "capability_version",
        "implementation_id",
        "implementation_version",
        "adapter_id",
        "prompt_module_fingerprint",
        "dependency_fingerprint",
        "input_artifact_refs_json",
        "output_contract",
        "status",
        "created_at",
        "started_at",
        "ended_at",
        "failure_code",
    } == columns
    assert {
        "idx_turn_capability_execution_turn",
        "idx_turn_capability_execution_implementation",
    } <= indexes
    assert {
        "trg_turn_capability_execution_scope",
        "trg_turn_capability_execution_identity_immutable",
        "trg_turn_capability_execution_parent_state",
        "trg_turn_capability_execution_status_transition",
    } == triggers
    assert "trg_turn_agent_execution_capability_fence" in parent_triggers


def test_0037_adds_versioned_agent_eval_store_and_guards(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "head")
    _upgrade_to(db_path, "head")

    with sqlite3.connect(db_path) as connection:
        tables = _tables(db_path)
        suite_revision_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(agent_eval_suite_revision)",
            )
        }
        policy_pk = {
            str(row[1]): int(row[5])
            for row in connection.execute(
                "PRAGMA table_info(agent_eval_trigger_policy)",
            )
            if int(row[5]) > 0
        }
        triggers = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_schema
                WHERE type='trigger' AND tbl_name LIKE 'agent_eval_%'
                """,
            )
        }
    assert {
        "agent_eval_suite",
        "agent_eval_suite_revision",
        "agent_eval_case_revision",
        "agent_eval_run",
        "agent_eval_case_result",
        "agent_eval_trigger_policy",
        "agent_eval_evidence_state",
    } <= tables
    assert {"case_count", "case_set_hash"} <= suite_revision_columns
    assert policy_pk == {"team_id": 1, "agent_source_kind": 2, "agent_id": 3}
    assert {
        "trg_agent_eval_suite_scope",
        "trg_agent_eval_suite_update",
        "trg_agent_eval_suite_revision_scope",
        "trg_agent_eval_suite_revision_immutable_update",
        "trg_agent_eval_suite_revision_immutable_delete",
        "trg_agent_eval_case_capacity",
        "trg_agent_eval_case_immutable_update",
        "trg_agent_eval_case_immutable_delete",
        "trg_agent_eval_run_scope",
        "trg_agent_eval_run_identity_immutable",
        "trg_agent_eval_run_status_transition",
        "trg_agent_eval_run_complete_results",
        "trg_agent_eval_run_immutable_delete",
        "trg_agent_eval_case_result_scope",
        "trg_agent_eval_case_result_immutable_update",
        "trg_agent_eval_case_result_immutable_delete",
        "trg_agent_eval_trigger_policy_scope",
        "trg_agent_eval_trigger_policy_update",
        "trg_agent_eval_trigger_policy_scope_update",
        "trg_agent_eval_evidence_scope_insert",
        "trg_agent_eval_evidence_update",
        "trg_agent_eval_evidence_state_insert",
        "trg_agent_eval_evidence_state_update",
    } <= triggers


def test_0038_adds_durable_agent_eval_execution_and_guards(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "head")
    _upgrade_to(db_path, "head")

    with sqlite3.connect(db_path) as connection:
        tables = _tables(db_path)
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='index' AND name LIKE 'idx_agent_eval_%'",
            )
        }
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger' AND name LIKE 'trg_agent_eval_%'",
            )
        }
    assert {
        "agent_eval_run_execution",
        "agent_eval_case_job",
        "agent_eval_scratch_turn",
    } <= tables
    assert {
        "idx_agent_eval_case_job_claim",
        "idx_agent_eval_scratch_run",
    } <= indexes
    assert {
        "trg_agent_eval_run_execution_scope",
        "trg_agent_eval_run_execution_update",
        "trg_agent_eval_run_execution_delete",
        "trg_agent_eval_evidence_monotonic_run",
        "trg_agent_eval_case_job_scope",
        "trg_agent_eval_case_job_identity",
        "trg_agent_eval_case_job_transition",
        "trg_agent_eval_case_job_terminal_result",
        "trg_agent_eval_case_job_delete",
        "trg_agent_eval_scratch_scope",
        "trg_agent_eval_scratch_update",
        "trg_agent_eval_scratch_delete",
    } <= triggers


def test_0040_adds_agent_eval_product_state_and_candidate_subjects(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "head")
    _upgrade_to(db_path, "head")

    with sqlite3.connect(db_path) as connection:
        run_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(agent_eval_run)")}
        suite_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(agent_eval_suite)")}
        tables = _tables(db_path)
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger' AND name LIKE 'trg_agent_eval_%'",
            )
        }
    assert "candidate_id" in run_columns
    assert "platform_managed" in suite_columns
    assert "agent_eval_regression_outbox" in tables
    assert "agent_eval_notification_outbox" in tables
    assert {
        "trg_agent_eval_live_release_change",
        "trg_agent_eval_team_model_change",
        "trg_agent_eval_platform_suite_immutable",
        "trg_agent_eval_regression_update",
        "trg_agent_eval_regression_delete",
        "trg_agent_eval_failure_notification",
        "trg_agent_eval_notification_update",
        "trg_agent_eval_notification_delete",
    } <= triggers


def test_0040_rejects_partially_persisted_candidate_column_with_wrong_semantics(
    tmp_path,
) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "0038_agent_eval_execution")
    with sqlite3.connect(db_path) as connection:
        connection.execute("ALTER TABLE agent_eval_run ADD COLUMN candidate_id INTEGER")
        connection.commit()

    with pytest.raises(RuntimeError, match="incompatible column semantics"):
        _upgrade_to(db_path, "head")


def test_0041_adds_catalog_localization_without_mutating_business_data(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "0040_agent_eval_product")
    timestamp = "2026-09-05T08:00:00+00:00"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO teams(id,name,status,created_at) VALUES ('team_locale_migration','Locale','active',?)",
            (timestamp,),
        )
        before = connection.execute(
            "SELECT id,name,status,created_at FROM teams WHERE id='team_locale_migration'",
        ).fetchone()
        connection.commit()

    _upgrade_to(db_path, "head")
    _upgrade_to(db_path, "head")

    with sqlite3.connect(db_path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(catalog_localized_listing)",
            )
        }
        indexes = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA index_list(catalog_localized_listing)",
            )
        }
        name_claim_indexes = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA index_list(agent_name_claim)",
            )
        }
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name='catalog_localized_listing'",
            )
        }
        after = connection.execute(
            "SELECT id,name,status,created_at FROM teams WHERE id='team_locale_migration'",
        ).fetchone()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert {
        "id",
        "team_id",
        "resource_kind",
        "resource_id",
        "resource_revision",
        "locale",
        "source_locale",
        "fields_json",
        "source_hash",
        "generation_source",
        "review_state",
        "row_version",
        "reviewed_by",
        "reviewed_at",
        "created_by",
        "created_at",
        "updated_at",
    } == columns
    assert "idx_catalog_localized_listing_lookup" in indexes
    assert "idx_agent_name_claim_effective" in name_claim_indexes
    assert {
        "trg_catalog_localized_listing_scope",
        "trg_catalog_localized_listing_update",
        "trg_catalog_localized_listing_delete",
    } == triggers
    assert after == before
    assert revision == (_chain_head(),)


def test_0041_rejects_preexisting_localization_table_with_wrong_semantics(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "0040_agent_eval_product")
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE catalog_localized_listing(id TEXT PRIMARY KEY)")
        connection.commit()

    with pytest.raises(RuntimeError, match="incompatible table semantics"):
        _upgrade_to(db_path, "head")


def test_0038_rejects_partially_persisted_eval_job_trigger_with_wrong_semantics(
    tmp_path,
) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "head")
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TRIGGER trg_agent_eval_case_job_transition")
        connection.execute(
            """
            CREATE TRIGGER trg_agent_eval_case_job_transition
            BEFORE UPDATE ON agent_eval_case_job
            BEGIN
              SELECT RAISE(ABORT, 'wrong trigger');
            END
            """,
        )
        connection.execute(
            "UPDATE alembic_version SET version_num='0037_agent_eval_store'",
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="incompatible trigger semantics"):
        _upgrade_to(db_path, "head")


def test_0037_rejects_partially_persisted_eval_trigger_with_wrong_semantics(
    tmp_path,
) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "0037_agent_eval_store")
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TRIGGER trg_agent_eval_run_status_transition")
        connection.execute(
            """
            CREATE TRIGGER trg_agent_eval_run_status_transition
            BEFORE UPDATE ON agent_eval_run
            BEGIN
              SELECT RAISE(ABORT, 'wrong trigger');
            END
            """,
        )
        connection.execute(
            "UPDATE alembic_version SET version_num='0036_agent_capability_execution'",
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="incompatible trigger semantics"):
        _upgrade_to(db_path, "head")


def test_0036_rejects_partially_persisted_trigger_with_wrong_semantics(
    tmp_path,
) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "head")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "DROP TRIGGER trg_turn_capability_execution_status_transition",
        )
        connection.execute(
            """
            CREATE TRIGGER trg_turn_capability_execution_status_transition
            BEFORE UPDATE ON turn_capability_execution
            BEGIN
              SELECT RAISE(ABORT, 'wrong trigger');
            END
            """,
        )
        connection.execute(
            "UPDATE alembic_version SET version_num='0035_scheduled_agent_binding'",
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="incompatible trigger semantics"):
        _upgrade_to(db_path, "head")


def test_0034_rejects_partially_persisted_trigger_with_wrong_semantics(
    tmp_path,
) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "0034_agent_turn_binding")
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TRIGGER trg_turn_agent_execution_revision")
        connection.execute(
            """
            CREATE TRIGGER trg_turn_agent_execution_revision
            BEFORE INSERT ON turn_agent_execution
            BEGIN
              SELECT RAISE(ABORT, 'wrong trigger');
            END
            """,
        )
        connection.execute(
            "UPDATE alembic_version SET version_num='0033_agent_release_cutover'",
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="incompatible trigger semantics"):
        _upgrade_to(db_path, "0034_agent_turn_binding")


def test_0032_rejects_partially_persisted_trigger_with_wrong_semantics(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "head")
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TRIGGER trg_team_subagent_release_event_immutable_update")
        connection.execute(
            """
            CREATE TRIGGER trg_team_subagent_release_event_immutable_update
            BEFORE INSERT ON team_subagent_release_event
            BEGIN
              SELECT RAISE(ABORT, 'wrong trigger');
            END
            """,
        )
        connection.execute(
            "UPDATE alembic_version SET version_num='0031_agent_release_baseline'",
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="incompatible trigger semantics"):
        _upgrade_to(db_path, "head")


def test_0029_rejects_partially_persisted_index_with_wrong_semantics(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "0029_agent_release_foundation")
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP INDEX uq_agent_one_pending_candidate")
        connection.execute(
            """
            CREATE UNIQUE INDEX uq_agent_one_pending_candidate
            ON team_subagent_release_candidate(id)
            WHERE state='approved'
            """,
        )
        connection.execute(
            "UPDATE alembic_version SET version_num='0028_agent_listing_safety'",
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="incompatible index semantics"):
        _upgrade_to(db_path, "0029_agent_release_foundation")


def test_0029_rejects_partially_persisted_trigger_with_wrong_semantics(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "0029_agent_release_foundation")
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TRIGGER trg_team_subagent_revision_immutable_update")
        connection.execute(
            """
            CREATE TRIGGER trg_team_subagent_revision_immutable_update
            BEFORE INSERT ON team_subagent_revision
            BEGIN
              SELECT RAISE(ABORT, 'wrong trigger');
            END
            """,
        )
        connection.execute(
            "UPDATE alembic_version SET version_num='0028_agent_listing_safety'",
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="incompatible trigger semantics"):
        _upgrade_to(db_path, "0029_agent_release_foundation")


def test_0029_rejects_partially_persisted_aggregate_column_without_check(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "0028_agent_listing_safety")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            ALTER TABLE team_subagent
            ADD COLUMN definition_mode TEXT NOT NULL DEFAULT 'legacy_mutable'
            """,
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="incompatible column constraint"):
        _upgrade_to(db_path, "head")


def test_0026_resumes_from_partially_persisted_ddl(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "0025_agent_marketplace_assignments")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "ALTER TABLE agent_marketplace_schema_meta ADD COLUMN minimum_reader_version TEXT NOT NULL DEFAULT '0002'",
        )
        connection.execute(
            "ALTER TABLE agent_assignment_backfill_run ADD COLUMN snapshot_at TEXT",
        )
        connection.execute(
            """
            CREATE TABLE agent_assignment_backfill_expected (
              run_id TEXT NOT NULL,
              team_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              agent_id TEXT NOT NULL,
              PRIMARY KEY(run_id, team_id, user_id, agent_id),
              FOREIGN KEY(run_id) REFERENCES agent_assignment_backfill_run(id) ON DELETE CASCADE
            ) WITHOUT ROWID
            """,
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX uq_agent_assignment_backfill_single_running
            ON agent_assignment_backfill_run((1)) WHERE status='running'
            """,
        )
        connection.commit()

    _upgrade_to(db_path, "head")
    _upgrade_to(db_path, "head")

    with sqlite3.connect(db_path) as connection:
        meta_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(agent_marketplace_schema_meta)")}
        run_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(agent_assignment_backfill_run)")}
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert {"minimum_reader_version", "minimum_writer_version"} <= meta_columns
    assert {
        "snapshot_at",
        "snapshot_completed_at",
        "stage_cursor_team_id",
        "stage_cursor_user_id",
        "cursor_agent_id",
        "expected_digest",
        "owner_token",
        "lease_expires_at",
    } <= run_columns
    assert revision == (_chain_head(),)


def test_0026_rejects_partially_persisted_index_with_wrong_semantics(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "0025_agent_marketplace_assignments")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE UNIQUE INDEX uq_agent_assignment_backfill_single_running
            ON agent_assignment_backfill_run(id) WHERE status='completed'
            """,
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="single-running-backfill index"):
        _upgrade_to(db_path, "head")


def test_0026_rejects_partially_persisted_column_with_wrong_semantics(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "0025_agent_marketplace_assignments")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "ALTER TABLE agent_assignment_backfill_run ADD COLUMN snapshot_at INTEGER",
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="column semantics"):
        _upgrade_to(db_path, "head")


def test_0026_rejects_partially_persisted_table_with_wrong_semantics(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "0025_agent_marketplace_assignments")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE agent_assignment_backfill_expected (
              run_id TEXT NOT NULL,
              team_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              agent_id TEXT NOT NULL,
              PRIMARY KEY(run_id, team_id, user_id, agent_id),
              FOREIGN KEY(run_id) REFERENCES agent_assignment_backfill_run(id) ON DELETE CASCADE
            )
            """,
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="WITHOUT ROWID"):
        _upgrade_to(db_path, "head")


def test_0026_requires_offline_backfill_when_legacy_agents_exist(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "0024_memory_item_metadata")
    with sqlite3.connect(db_path) as connection:
        timestamp = "2026-08-30T00:00:00+00:00"
        connection.execute(
            "INSERT INTO teams(id,name,status,created_at) VALUES ('team_1','Team','active',?)",
            (timestamp,),
        )
        connection.execute(
            """
            INSERT INTO team_subagent(
              id,team_id,name,display_name,description,tool_refs_json,skills_json,
              visibility,status,created_at,updated_at
            ) VALUES ('osa_1','team_1','agent-one','Agent One','Legacy','["bi_query"]','[]',
                      'team_wide','enabled',?,?)
            """,
            (timestamp, timestamp),
        )
        connection.commit()

    _upgrade_to(db_path, "head")
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT assignment_cutover_complete_at FROM agent_marketplace_schema_meta WHERE singleton=1",
        ).fetchone() == (None,)


@pytest.mark.asyncio
async def test_0026_candidate_admission_fails_closed_on_schema_drift(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "ALTER TABLE agent_marketplace_schema_meta DROP COLUMN minimum_reader_version",
        )
        connection.commit()

    with pytest.raises(AgentAssignmentUnavailableError, match="after the safety revision"):
        await AgentAssignmentStore(db_path).deployment_admission()


@pytest.mark.asyncio
async def test_0025_completed_cutover_is_reconciled_before_0026_marketplace_startup(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "0025_agent_marketplace_assignments")
    timestamp = "2026-08-30T00:00:00+00:00"
    v1_completed_at = "2026-08-30T01:00:00+00:00"
    with sqlite3.connect(db_path) as connection:
        connection.executemany(
            "INSERT INTO teams(id,name,status,created_by_user_id,created_at) VALUES (?,?,?,?,?)",
            (
                ("team_primary", "Primary", "active", None, timestamp),
                ("team_secondary", "Secondary", "active", None, timestamp),
                ("team_post_membership", "Post membership", "active", None, timestamp),
                ("team_restore", "Restored membership", "active", "user_restore_admin", timestamp),
                ("team_malformed_audit", "Malformed audit", "active", None, timestamp),
                ("team_custom", "Custom grants", "active", "user_custom_owner", timestamp),
                ("team_suspended", "Suspended", "suspended", None, timestamp),
            ),
        )
        connection.executemany(
            "INSERT INTO users(id,team_id,email,status,created_at) VALUES (?,?,?,?,?)",
            (
                ("user_1", "team_primary", "user@example.com", "active", timestamp),
                (
                    "user_restore_admin",
                    "team_restore",
                    "restore-admin@example.com",
                    "active",
                    timestamp,
                ),
                (
                    "user_custom_owner",
                    "team_custom",
                    "custom-owner@example.com",
                    "active",
                    timestamp,
                ),
                (
                    "user_custom_pre",
                    "team_custom",
                    "custom-pre@example.com",
                    "active",
                    timestamp,
                ),
                (
                    "user_custom_post",
                    "team_custom",
                    "custom-post@example.com",
                    "active",
                    timestamp,
                ),
            ),
        )
        connection.executemany(
            "INSERT INTO team_members(id,user_id,team_id,role,status,created_at) VALUES (?,?,?,?,?,?)",
            (
                ("tm_primary", "user_1", "team_primary", "member", "active", timestamp),
                ("tm_secondary", "user_1", "team_secondary", "member", "active", timestamp),
                ("tm_restore", "user_1", "team_restore", "member", "active", timestamp),
                (
                    "tm_malformed_audit",
                    "user_1",
                    "team_malformed_audit",
                    "member",
                    "active",
                    timestamp,
                ),
                (
                    "tm_restore_admin",
                    "user_restore_admin",
                    "team_restore",
                    "team_admin",
                    "active",
                    timestamp,
                ),
                (
                    "tm_post_membership",
                    "user_1",
                    "team_post_membership",
                    "member",
                    "active",
                    "2026-08-30T02:00:00+00:00",
                ),
                (
                    "tm_custom_owner",
                    "user_custom_owner",
                    "team_custom",
                    "team_admin",
                    "active",
                    timestamp,
                ),
                (
                    "tm_custom_pre",
                    "user_custom_pre",
                    "team_custom",
                    "member",
                    "active",
                    timestamp,
                ),
                (
                    "tm_custom_post",
                    "user_custom_post",
                    "team_custom",
                    "member",
                    "active",
                    timestamp,
                ),
            ),
        )
        connection.executemany(
            """
            INSERT INTO team_subagent(
              id,team_id,name,display_name,description,tool_refs_json,skills_json,
              visibility,created_by,status,created_at,updated_at
            ) VALUES (?,?,?,?,?,'[]','[]','team_wide','user_1','enabled',?,?)
            """,
            (
                (
                    "osa_removed",
                    "team_primary",
                    "removed-agent",
                    "Removed Agent",
                    "Removed preference",
                    timestamp,
                    timestamp,
                ),
                (
                    "osa_disabled",
                    "team_primary",
                    "disabled-agent",
                    "Disabled Agent",
                    "Disabled preference",
                    timestamp,
                    timestamp,
                ),
                (
                    "osa_secondary",
                    "team_secondary",
                    "secondary-agent",
                    "Secondary Agent",
                    "Visible through the secondary membership",
                    timestamp,
                    timestamp,
                ),
                (
                    "osa_post_agent",
                    "team_secondary",
                    "post-agent",
                    "Post Agent",
                    "Created after the sticky v1 cutover",
                    "2026-08-30T02:00:00+00:00",
                    "2026-08-30T02:00:00+00:00",
                ),
                (
                    "osa_post_membership",
                    "team_post_membership",
                    "membership-agent",
                    "Membership Agent",
                    "Membership was created after the sticky v1 cutover",
                    timestamp,
                    timestamp,
                ),
                (
                    "osa_restored_membership",
                    "team_restore",
                    "restored-membership-agent",
                    "Restored Membership Agent",
                    "Must not be hired after a post-cutover membership restore",
                    timestamp,
                    timestamp,
                ),
                (
                    "osa_malformed_membership_audit",
                    "team_malformed_audit",
                    "malformed-membership-audit-agent",
                    "Malformed Membership Audit Agent",
                    "Must fail closed when membership history cannot be ordered",
                    timestamp,
                    timestamp,
                ),
                (
                    "osa_direct_pre",
                    "team_custom",
                    "direct-pre",
                    "Direct pre-cutover",
                    "A direct grant that existed at cutover",
                    timestamp,
                    timestamp,
                ),
                (
                    "osa_group_pre",
                    "team_custom",
                    "group-pre",
                    "Group pre-cutover",
                    "A group grant and membership that existed at cutover",
                    timestamp,
                    timestamp,
                ),
                (
                    "osa_direct_post",
                    "team_custom",
                    "direct-post",
                    "Direct post-cutover",
                    "The direct grant was created after cutover",
                    timestamp,
                    timestamp,
                ),
                (
                    "osa_group_post",
                    "team_custom",
                    "group-post",
                    "Group post-cutover",
                    "The user joined the granted group after cutover",
                    timestamp,
                    timestamp,
                ),
                (
                    "osa_deny_revoked",
                    "team_custom",
                    "deny-revoked",
                    "Revoked deny",
                    "A deny active at cutover was revoked afterwards",
                    timestamp,
                    timestamp,
                ),
                (
                    "osa_teamwide_deny_revoked",
                    "team_custom",
                    "teamwide-deny-revoked",
                    "Teamwide revoked deny",
                    "A teamwide Agent denied at cutover",
                    timestamp,
                    timestamp,
                ),
                (
                    "osa_teamwide_group_deny_revoked",
                    "team_custom",
                    "teamwide-group-deny-revoked",
                    "Teamwide revoked group deny",
                    "A teamwide Agent denied to a group at cutover",
                    timestamp,
                    timestamp,
                ),
            ),
        )
        connection.execute(
            "UPDATE team_subagent SET created_by='user_restore_admin' WHERE id='osa_restored_membership'",
        )
        connection.execute(
            """
            UPDATE team_subagent SET visibility='private',created_by='user_custom_owner'
            WHERE team_id='team_custom'
            """,
        )
        connection.execute(
            """
            UPDATE team_subagent SET visibility='team_wide'
            WHERE id IN ('osa_teamwide_deny_revoked','osa_teamwide_group_deny_revoked')
            """,
        )
        connection.executemany(
            """
            INSERT INTO groups(
              id,team_id,workspace_id,name,description,status,created_by,created_at,updated_at
            ) VALUES (?,?,'',?,'','active','user_custom_owner',?,?)
            """,
            (
                ("grp_custom_pre", "team_custom", "Custom pre", timestamp, timestamp),
                ("grp_custom_post", "team_custom", "Custom post", timestamp, timestamp),
            ),
        )
        connection.executemany(
            """
            INSERT INTO group_memberships(
              team_id,group_id,principal_type,principal_id,role,status,created_by,
              created_at,removed_at
            ) VALUES (?,?,'user',?,'member','active','user_custom_owner',?,NULL)
            """,
            (
                ("team_custom", "grp_custom_pre", "user_custom_pre", timestamp),
                (
                    "team_custom",
                    "grp_custom_post",
                    "user_custom_post",
                    "2026-08-30T02:00:00+00:00",
                ),
            ),
        )
        connection.executemany(
            """
            INSERT INTO resource_grants(
              id,team_id,resource_kind,resource_id,principal_type,principal_id,
              permission,effect,created_by,created_at,updated_at,expires_at,revoked_at
            ) VALUES (?,'team_custom','team_subagent',?,?,?,'use',?,'user_custom_owner',?,?,NULL,?)
            """,
            (
                (
                    "grant_direct_pre",
                    "osa_direct_pre",
                    "user",
                    "user_custom_pre",
                    "allow",
                    timestamp,
                    timestamp,
                    None,
                ),
                (
                    "grant_group_pre",
                    "osa_group_pre",
                    "group",
                    "grp_custom_pre",
                    "allow",
                    timestamp,
                    timestamp,
                    None,
                ),
                (
                    "grant_direct_post",
                    "osa_direct_post",
                    "user",
                    "user_custom_post",
                    "allow",
                    "2026-08-30T02:00:00+00:00",
                    "2026-08-30T02:00:00+00:00",
                    None,
                ),
                (
                    "grant_group_post",
                    "osa_group_post",
                    "group",
                    "grp_custom_post",
                    "allow",
                    timestamp,
                    timestamp,
                    None,
                ),
                (
                    "grant_deny_allow",
                    "osa_deny_revoked",
                    "user",
                    "user_custom_pre",
                    "allow",
                    timestamp,
                    timestamp,
                    None,
                ),
                (
                    "grant_deny_revoked",
                    "osa_deny_revoked",
                    "user",
                    "user_custom_pre",
                    "deny",
                    timestamp,
                    "2026-08-30T02:00:00+00:00",
                    "2026-08-30T02:00:00+00:00",
                ),
                (
                    "grant_teamwide_deny_revoked",
                    "osa_teamwide_deny_revoked",
                    "user",
                    "user_custom_post",
                    "deny",
                    timestamp,
                    "2026-08-30T02:00:00+00:00",
                    "2026-08-30T02:00:00+00:00",
                ),
                (
                    "grant_teamwide_group_deny_revoked",
                    "osa_teamwide_group_deny_revoked",
                    "group",
                    "grp_custom_pre",
                    "deny",
                    timestamp,
                    "2026-08-30T02:00:00+00:00",
                    "2026-08-30T02:00:00+00:00",
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO admin_audit_log(
              id,actor_user_id,team_id,action,target_kind,target_id,target_team_id,
              scope,payload_json,ts
            ) VALUES (
              'aud_malformed_membership','user_restore_admin','team_malformed_audit',
              ?,'user','user_1','team_malformed_audit','team','{}','not-a-timestamp'
            )
            """,
            (AuditAction.TEAM_MEMBER_DEACTIVATED,),
        )
        connection.executemany(
            """
            INSERT INTO agent_assignment(
              id,team_id,user_id,agent_id,agent_source_kind,personal_state,access_state,
              source,row_version,hired_at,current_cycle_started_at,personal_state_changed_at,
              access_state_changed_at,removed_at,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,'granted','migration_existing_access',1,?,?,?,?,?,?,?)
            """,
            (
                (
                    "asa_removed",
                    "team_primary",
                    "user_1",
                    "osa_removed",
                    "team",
                    "removed",
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
                (
                    "asa_disabled",
                    "team_primary",
                    "user_1",
                    "osa_disabled",
                    "team",
                    "disabled",
                    timestamp,
                    timestamp,
                    timestamp,
                    timestamp,
                    None,
                    timestamp,
                    timestamp,
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO agent_assignment_backfill_run(
              id,status,cursor_team_id,cursor_user_id,expected_count,inserted_count,
              conflict_count,error_count,source_build,started_at,updated_at,finished_at
            ) VALUES ('abf_v1','completed','team_primary','user_1',2,2,0,0,
                      '0025',?,?,?)
            """,
            (timestamp, v1_completed_at, v1_completed_at),
        )
        connection.commit()

    user_store = UserStore(db_path)
    monkeypatch.setattr("bowhead_core.user_store._now", lambda: "2026-08-30T00:30:00+00:00")
    removed = await user_store.admin_soft_delete_member_detailed(
        target_user_id="user_1",
        team_id="team_restore",
        actor_user_id="user_restore_admin",
    )
    assert removed.changed is True
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE agent_marketplace_schema_meta
            SET assignment_cutover_complete_at=?,assignment_backfill_expected=2,
                assignment_backfill_inserted=2,assignment_backfill_digest='v1',updated_at=?
            WHERE singleton=1
            """,
            (v1_completed_at, v1_completed_at),
        )
        connection.commit()
    monkeypatch.setattr("bowhead_core.user_store._now", lambda: "2026-08-30T02:00:00+00:00")
    assert (
        await user_store.add_existing_user_to_local_team(
            user_id="user_1",
            team_id="team_restore",
        )
        == "membership_restored"
    )
    monkeypatch.setattr("bowhead_core.user_store._now", lambda: "2026-08-30T03:00:00+00:00")
    removed_again = await user_store.admin_soft_delete_member_detailed(
        target_user_id="user_1",
        team_id="team_restore",
        actor_user_id="user_restore_admin",
    )
    assert removed_again.changed is True
    monkeypatch.setattr("bowhead_core.user_store._now", lambda: "2026-08-30T04:00:00+00:00")
    assert (
        await user_store.add_existing_user_to_local_team(
            user_id="user_1",
            team_id="team_restore",
        )
        == "membership_restored"
    )
    with sqlite3.connect(db_path) as connection:
        restored_membership = connection.execute(
            "SELECT status,created_at,removed_at FROM team_members WHERE team_id='team_restore' AND user_id='user_1'",
        ).fetchone()
        removal_audits = connection.execute(
            "SELECT action,target_id,target_team_id,team_id,ts FROM admin_audit_log "
            "WHERE action=? AND target_id='user_1' AND team_id='team_restore' ORDER BY ts",
            (AuditAction.TEAM_MEMBER_DEACTIVATED,),
        ).fetchall()
    assert restored_membership == ("active", timestamp, None)
    assert removal_audits == [
        (
            AuditAction.TEAM_MEMBER_DEACTIVATED,
            "user_1",
            "team_restore",
            "team_restore",
            "2026-08-30T00:30:00+00:00",
        ),
        (
            AuditAction.TEAM_MEMBER_DEACTIVATED,
            "user_1",
            "team_restore",
            "team_restore",
            "2026-08-30T03:00:00+00:00",
        ),
    ]

    _upgrade_to(db_path, "0026_agent_marketplace_safety")
    store = AgentAssignmentStore(db_path)
    assert await store.marketplace_ready() is False
    with pytest.raises(AgentAssignmentUnavailableError, match="marker is incomplete"):
        await store.verify_marketplace_cutover()

    dry_run = await store.backfill_existing_access(apply=False, batch_size=1)
    assert dry_run.cutover_completed_at is None

    # Force both ensure callers past their stale outer receipt read. The second
    # caller enters the BEGIN IMMEDIATE section only after the first commits,
    # proving the lock-local v2 receipt check prevents a duplicate repair run.
    outer_calls = 0
    outer_barrier = asyncio.Event()
    start_calls = 0
    first_completed = asyncio.Event()
    original_receipt_exists = store._legacy_v1_cutover_receipt_exists
    original_start = store._start_or_resume_legacy_v1_repair
    original_complete = store._complete_legacy_v1_repair

    async def synchronized_receipt_exists() -> bool:
        nonlocal outer_calls
        outer_calls += 1
        if outer_calls == 2:
            outer_barrier.set()
        await outer_barrier.wait()
        return await original_receipt_exists()

    async def ordered_start(**kwargs):
        nonlocal start_calls
        start_calls += 1
        if start_calls == 2:
            await first_completed.wait()
        return await original_start(**kwargs)

    async def signaling_complete(**kwargs):
        result = await original_complete(**kwargs)
        first_completed.set()
        return result

    monkeypatch.setattr(
        store,
        "_legacy_v1_cutover_receipt_exists",
        synchronized_receipt_exists,
    )
    monkeypatch.setattr(store, "_start_or_resume_legacy_v1_repair", ordered_start)
    monkeypatch.setattr(store, "_complete_legacy_v1_repair", signaling_complete)
    repaired, concurrent = await asyncio.wait_for(
        asyncio.gather(
            store.ensure_marketplace_cutover(
                source_build="0026-startup-a",
                batch_size=1,
            ),
            store.ensure_marketplace_cutover(
                source_build="0026-startup-b",
                batch_size=1,
            ),
        ),
        timeout=10,
    )
    verified = await store.verify_marketplace_cutover()

    assert repaired == concurrent == verified
    assert repaired.cutover_completed_at == v1_completed_at
    assert await store.marketplace_ready() is True
    with sqlite3.connect(db_path) as connection:
        personal_states = connection.execute(
            """
            SELECT agent_id,personal_state FROM agent_assignment
            WHERE team_id='team_primary' AND user_id='user_1' ORDER BY agent_id
            """,
        ).fetchall()
        secondary = connection.execute(
            """
            SELECT personal_state,access_state,source FROM agent_assignment
            WHERE team_id='team_secondary' AND user_id='user_1' AND agent_id='osa_secondary'
            """,
        ).fetchone()
        post_cutover_assignments = connection.execute(
            """
            SELECT team_id,agent_id FROM agent_assignment
            WHERE agent_id IN (
              'osa_post_agent','osa_post_membership','osa_restored_membership',
              'osa_malformed_membership_audit'
            )
              AND user_id='user_1'
            ORDER BY team_id,agent_id
            """,
        ).fetchall()
        custom_assignments = connection.execute(
            """
            SELECT user_id,agent_id FROM agent_assignment
            WHERE team_id='team_custom' AND user_id IN ('user_custom_pre','user_custom_post')
            ORDER BY user_id,agent_id
            """,
        ).fetchall()
        receipts = connection.execute(
            """
            SELECT status,snapshot_at FROM agent_assignment_backfill_run
            ORDER BY snapshot_at IS NOT NULL,snapshot_at
            """,
        ).fetchall()
        suspended_system_claims = connection.execute(
            """
            SELECT COUNT(*) FROM agent_name_claim
            WHERE team_id='team_suspended' AND holder_kind='system'
            """,
        ).fetchone()[0]
        marker = connection.execute(
            """
            SELECT assignment_cutover_complete_at FROM agent_marketplace_schema_meta
            WHERE singleton=1
            """,
        ).fetchone()[0]
    assert personal_states == [
        ("osa_disabled", "disabled"),
        ("osa_removed", "removed"),
    ]
    assert secondary == ("active", "granted", "migration_existing_access")
    assert post_cutover_assignments == []
    assert custom_assignments == [
        ("user_custom_post", "osa_teamwide_group_deny_revoked"),
        ("user_custom_pre", "osa_direct_pre"),
        ("user_custom_pre", "osa_group_pre"),
        ("user_custom_pre", "osa_teamwide_deny_revoked"),
    ]
    assert receipts[0] == ("completed", None)
    assert receipts[1] == ("completed", v1_completed_at)
    assert len(receipts) == 2
    assert suspended_system_claims > 0
    assert marker == v1_completed_at


def test_0035_rejects_preexisting_primary_capability_with_wrong_semantics(
    tmp_path,
) -> None:
    db_path = tmp_path / "bowhead.db"
    _upgrade_to(db_path, "0034_agent_turn_binding")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "ALTER TABLE team_builtin_subagent_override ADD COLUMN primary_capability INTEGER",
        )
        connection.commit()

    with pytest.raises(
        RuntimeError,
        match="incompatible column semantics.*primary_capability",
    ):
        _upgrade_to(db_path, "head")


@pytest.mark.asyncio
async def test_initialize_rejects_drifted_single_running_index_semantics(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP INDEX uq_agent_assignment_backfill_single_running")
        connection.execute(
            """
            CREATE UNIQUE INDEX uq_agent_assignment_backfill_single_running
            ON agent_assignment_backfill_run(id) WHERE status='completed'
            """,
        )
        connection.commit()

    with pytest.raises(
        AgentAssignmentUnavailableError,
        match="invalid single-running-backfill constraint",
    ):
        await AgentAssignmentStore(db_path).initialize()
