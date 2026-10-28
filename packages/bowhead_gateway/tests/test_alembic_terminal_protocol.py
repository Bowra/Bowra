"""Terminal Protocol v1 S0 migration gates.

覆盖 current baseline→head、八目标账本、legacy NULL parity、v1 shape/immutability，
以及 actual migration schema 上 deferred NO ACTION effect bundle 的原子删除语义。
"""

from __future__ import annotations

import re
import sqlite3

import pytest
from bowhead_cli.migrate import TARGET_FILES, upgrade
from bowhead_core.migrations import chain_head
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.terminal_protocol import (
    TERMINAL_PROTOCOL_TABLES,
    load_terminal_protocol_enum_registry,
)

# Never hardcode revision numbers; see bowhead_core.migrations.chain_head.
HEAD_REVISION = chain_head()

REQUIRED_TABLES = {
    "turn_generations",
    "turn_terminal_candidates",
    "turn_terminal_candidate_variants",
    "turn_terminal_candidate_events",
    "turn_terminal_receipts",
    "turn_terminal_session_mutations",
    "turn_event_proposals",
    "turn_control_decisions",
    "turn_decision_effects",
    "turn_recovery_records",
    "turn_effect_attempts",
    "turn_effect_settlements",
    "turn_effect_evidence_links",
    "turn_effect_retention_tombstones",
    "turn_effect_retention_member_keys",
    "turn_terminal_deletion_ledgers",
    "session_lifecycle_operations",
    "session_lifecycle_targets",
    "terminal_auth_scope_epochs",
    "terminal_protocol_deployment_attestations",
    "turn_delivery_intents",
    "terminal_protocol_writer_lease",
}


def test_runtime_terminal_table_inventory_matches_migration_gate() -> None:
    assert TERMINAL_PROTOCOL_TABLES == REQUIRED_TABLES


def _create_1_0_fixture(db_path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
        connection.commit()


def _insert_workspace(connection: sqlite3.Connection, *, workspace_id: str = "ws_1") -> None:
    connection.execute(
        "INSERT OR IGNORE INTO teams(id,name,status,created_at) VALUES ('team_1','Team','active','2026-08-03T00:00:00Z')",
    )
    connection.execute(
        """
        INSERT INTO workspaces(id,team_id,name,status,created_at,updated_at)
        VALUES (?, 'team_1', 'Space', 'active', '2026-08-03T00:00:00Z', '2026-08-03T00:00:00Z')
        """,
        (workspace_id,),
    )


def _insert_session(connection: sqlite3.Connection, *, session_id: str, workspace_id: str = "ws_1") -> None:
    connection.execute(
        """
        INSERT INTO sessions(id,workspace_id,user_id,title,started_at,last_active_at,status,cost_usd,tokens_total,intents)
        VALUES (?, ?, 'u_1', 'Session', '2026-08-03T00:00:00Z', '2026-08-03T00:00:00Z', 'active', 0, 0, '[]')
        """,
        (session_id, workspace_id),
    )


def _insert_v1_turn_and_generation(
    connection: sqlite3.Connection,
    *,
    session_id: str = "s_v1",
    turn_id: str = "t_v1",
) -> None:
    connection.execute(
        """
        INSERT INTO turns(
          id,session_id,seq,user_text,status,started_at,
          terminal_protocol_version,terminal_ingress_surface,terminal_admission_cohort,
          protocol_admitted_at,current_generation,generation_transition_epoch,
          event_sequence_next,delivery_fence_epoch,presentation_locale,message_catalog_version
        ) VALUES (?, ?, 1, 'hello', 'running', '2026-08-03T00:00:00Z',
                  1, 'web', 'bucket-a/config-1', '2026-08-03T00:00:00Z', 1, 1, 1, 0, 'zh-CN', 1)
        """,
        (turn_id, session_id),
    )
    connection.execute(
        """
        INSERT INTO turn_generations(
          turn_id,generation,protocol_version,kind,execution_intent_id,
          execution_intent_digest,opened_at,state
        ) VALUES (?,1,1,'execution','intent_1','digest_1','2026-08-03T00:00:00Z','opened')
        """,
        (turn_id,),
    )


def test_terminal_protocol_revision_upgrades_all_eight_targets(tmp_path) -> None:
    core_path = tmp_path / TARGET_FILES["core"]
    _create_1_0_fixture(core_path)

    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)
    assert upgrade(f"sqlite+aiosqlite:///{core_path}") == tuple(TARGET_FILES)

    for target, filename in TARGET_FILES.items():
        with sqlite3.connect(tmp_path / filename) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD_REVISION,)
            if target != "core":
                tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                assert tables.isdisjoint(REQUIRED_TABLES)

    with sqlite3.connect(core_path) as connection:
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables >= REQUIRED_TABLES
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        turn_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(turns)")}
        session_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(sessions)")}
        event_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(turn_events)")}
        assert {
            "terminal_protocol_version",
            "current_generation",
            "generation_transition_epoch",
            "event_sequence_next",
            "delivery_fence_epoch",
        } <= turn_columns
        assert {"active_turn_protocol_version", "active_turn_generation", "active_turn_claim_epoch"} <= session_columns
        assert {"protocol_version", "generation", "event_seq", "actor_scope", "phase_scope"} <= event_columns


def test_expand_migration_and_old_binary_leave_protocol_columns_null(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _create_1_0_fixture(db_path)
    with sqlite3.connect(db_path) as connection:
        _insert_workspace(connection)
        _insert_session(connection, session_id="s_legacy_before")
        connection.execute(
            """
            INSERT INTO turns(id,session_id,seq,user_text,status,started_at)
            VALUES ('t_legacy_before','s_legacy_before',1,'before','running','2026-08-03T00:00:00Z')
            """,
        )
        connection.commit()

    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")

    with sqlite3.connect(db_path) as connection:
        before = connection.execute(
            """
            SELECT terminal_protocol_version,current_generation,generation_transition_epoch,
                   event_sequence_next,delivery_fence_epoch
            FROM turns WHERE id='t_legacy_before'
            """,
        ).fetchone()
        assert before == (None, None, None, None, None)

        # Simulate old binary during reader-first rollout: write only legacy columns + active_turn_id.
        _insert_session(connection, session_id="s_legacy_after")
        connection.execute(
            """
            INSERT INTO turns(id,session_id,seq,user_text,status,started_at)
            VALUES ('t_legacy_after','s_legacy_after',1,'after','running','2026-08-03T00:00:00Z')
            """,
        )
        connection.execute("UPDATE sessions SET active_turn_id='t_legacy_after' WHERE id='s_legacy_after'")
        after = connection.execute(
            """
            SELECT terminal_protocol_version,current_generation,generation_transition_epoch,
                   event_sequence_next,delivery_fence_epoch
            FROM turns WHERE id='t_legacy_after'
            """,
        ).fetchone()
        active = connection.execute(
            """
            SELECT active_turn_id,active_turn_protocol_version,active_turn_generation,active_turn_claim_epoch
            FROM sessions WHERE id='s_legacy_after'
            """,
        ).fetchone()
        assert after == (None, None, None, None, None)
        assert active == ("t_legacy_after", None, None, None)

        _insert_session(connection, session_id="s_v1")
        _insert_v1_turn_and_generation(connection)
        connection.execute(
            """
            UPDATE sessions
            SET active_turn_id='t_v1',active_turn_protocol_version=1,
                active_turn_generation=1,active_turn_claim_epoch=0
            WHERE id='s_v1'
            """,
        )
        assert connection.execute(
            """
            SELECT terminal_protocol_version,current_generation,generation_transition_epoch,
                   event_sequence_next,delivery_fence_epoch
            FROM turns WHERE id='t_v1'
            """,
        ).fetchone() == (1, 1, 1, 1, 0)


def test_terminal_protocol_shape_and_immutability_guards(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _create_1_0_fixture(db_path)
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_workspace(connection)
        _insert_session(connection, session_id="s_v1")
        _insert_v1_turn_and_generation(connection)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE turns SET presentation_locale='en-US' WHERE id='t_v1'")
        with pytest.raises(sqlite3.IntegrityError, match="malformed terminal protocol turn"):
            connection.execute(
                """
                INSERT INTO turns(
                  id,session_id,seq,user_text,status,started_at,terminal_protocol_version,
                  terminal_ingress_surface,terminal_admission_cohort,protocol_admitted_at,
                  presentation_locale,message_catalog_version
                ) VALUES ('t_bad','s_v1',2,'bad','running','t',1,'web','b','t','zh-CN',1)
                """,
            )
        with pytest.raises(sqlite3.IntegrityError, match="active turn shape|protocol reference"):
            connection.execute(
                "UPDATE sessions SET active_turn_id='t_v1',active_turn_protocol_version=1 WHERE id='s_v1'",
            )
        _insert_session(connection, session_id="s_other")
        with pytest.raises(sqlite3.IntegrityError, match="protocol reference"):
            connection.execute(
                """
                UPDATE sessions SET active_turn_id='t_v1',active_turn_protocol_version=1,
                  active_turn_generation=1,active_turn_claim_epoch=0 WHERE id='s_other'
                """,
            )
        connection.execute("UPDATE turns SET delivery_fence_epoch=1 WHERE id='t_v1'")
        with pytest.raises(sqlite3.IntegrityError, match="monotonic"):
            connection.execute("UPDATE turns SET delivery_fence_epoch=0 WHERE id='t_v1'")
        connection.execute(
            """
            UPDATE sessions SET active_turn_id='t_v1',active_turn_protocol_version=1,
              active_turn_generation=1,active_turn_claim_epoch=0,steer_buffer_revision=1,
              steer_freeze_turn_id='t_v1',steer_freeze_generation=1,steer_freeze_claim_epoch=0,
              steer_frozen_revision=1,steer_buffer_state='frozen' WHERE id='s_v1'
            """,
        )
        with pytest.raises(sqlite3.IntegrityError, match="steer freeze"):
            connection.execute("UPDATE sessions SET steer_buffer_revision=0 WHERE id='s_v1'")
        with pytest.raises(sqlite3.IntegrityError, match="malformed terminal protocol turn"):
            connection.execute(
                """
                INSERT INTO turns(
                  id,session_id,seq,user_text,status,started_at,terminal_protocol_version,
                  terminal_ingress_surface,terminal_admission_cohort,protocol_admitted_at,
                  current_generation,generation_transition_epoch,event_sequence_next,
                  delivery_fence_epoch,presentation_locale,message_catalog_version
                ) VALUES ('t_long','s_v1',2,'bad','running','t',1,?,'b','t',1,1,1,0,'zh-CN',1)
                """,
                ("x" * 65,),
            )
        connection.execute(
            """
            INSERT INTO turn_events(id,turn_id,seq,event_type,payload_json,occurred_at)
            VALUES ('evt_legacy','t_v1',1,'progress','{}','t')
            """,
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE turn_events SET protocol_version=1,generation=1,event_seq=1,
                  actor_scope='step',phase_scope='in_generation',payload_digest='digest',
                  source_proposal_id='proposal',accept_ordinal=1 WHERE id='evt_legacy'
                """,
            )
        connection.execute(
            """
            INSERT INTO turn_terminal_candidates(
              candidate_id,turn_id,generation,candidate_origin,reserved_receipt_id,reserved_terminal_event_id,
              finalization_epoch_at_prepare,primary_status,primary_message_key,closing_accepted_through,
              persisted_event_highwater,terminal_event_counts_by_variant_json,candidate_projection_digest,
              canonicalization_version,state,session_disposition_plan_json,session_disposition_plan_digest,
              projection_variant_digests_json,projection_extensions_json,created_at,prepared_at
            ) VALUES ('ttc_1','t_v1',1,'normal','trc_1','evt_done',1,'completed','terminal.completed',0,0,
                      '{}','dc_1',1,'prepared','{}','plan_1','{}','{}','t','t')
            """,
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                """
                INSERT INTO turn_terminal_candidate_variants(
                  candidate_id,variant_key,outcome_variant,session_variant,status,message_key,
                  final_answer_mode,required_manifest_template_digest,
                  session_mutation_template_digest,variant_digest
                ) VALUES ('ttc_1','bad','primary','none','completed','terminal.completed',
                          'none','manifest','session','variant')
                """,
            )
        connection.execute(
            """
            INSERT INTO turn_terminal_candidate_variants(
              candidate_id,variant_key,outcome_variant,session_variant,status,message_key,
              final_answer_mode,final_answer_event_id,final_answer_digest,
              required_manifest_template_digest,session_mutation_template_digest,variant_digest
            ) VALUES ('ttc_1','good','primary','none','completed','terminal.completed',
                      'precommitted','evt_answer','answer_digest','manifest','session','variant')
            """,
        )
        connection.execute("UPDATE turn_terminal_candidates SET state='committed',committed_at='t' WHERE candidate_id='ttc_1'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE turn_terminal_candidates SET primary_status='failed' WHERE candidate_id='ttc_1'")
        with pytest.raises(sqlite3.IntegrityError, match="state transition"):
            connection.execute("UPDATE turn_terminal_candidates SET state='prepared' WHERE candidate_id='ttc_1'")


def test_terminal_protocol_schema_checks_match_canonical_enum_registry(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _create_1_0_fixture(db_path)
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    registry = load_terminal_protocol_enum_registry()
    cases = (
        ("sessions", "steer_buffer_state", "steer_buffer_state"),
        ("sessions", "recovery_lock_reason", "session_recovery_lock_reason"),
        ("turn_generations", "kind", "generation_kind"),
        ("turn_generations", "state", "generation_state"),
        ("turn_generations", "execution_effect_class", "effect_class"),
        ("turn_generations", "recovery_resolution", "recovery_resolution"),
        ("turn_terminal_candidates", "candidate_origin", "candidate_origin"),
        ("turn_terminal_candidates", "primary_status", "terminal_status"),
        ("turn_terminal_candidates", "state", "candidate_state"),
        ("turn_terminal_candidate_variants", "outcome_variant", "candidate_outcome"),
        ("turn_terminal_candidate_variants", "session_variant", "session_variant"),
        ("turn_terminal_candidate_variants", "status", "terminal_status"),
        ("turn_terminal_candidate_variants", "final_answer_mode", "final_answer_mode"),
        ("turn_terminal_candidate_events", "actor_scope", "actor_scope"),
        ("turn_terminal_candidate_events", "phase_scope", "phase_scope"),
        ("turn_terminal_receipts", "status", "terminal_status"),
        ("turn_terminal_receipts", "final_answer_mode", "final_answer_mode"),
        ("turn_terminal_receipts", "selected_outcome_variant", "candidate_outcome"),
        ("turn_terminal_receipts", "selected_session_variant", "session_variant"),
        ("turn_terminal_session_mutations", "disposition", "session_mutation_disposition"),
        ("turn_event_proposals", "actor_scope", "actor_scope"),
        ("turn_event_proposals", "phase_scope", "phase_scope"),
        ("turn_event_proposals", "durability", "root_durability"),
        ("turn_event_proposals", "state", "event_proposal_state"),
        ("turn_control_decisions", "state", "decision_state"),
        ("turn_recovery_records", "kind", "recovery_kind"),
        ("turn_recovery_records", "scope", "recovery_scope"),
        ("turn_recovery_records", "state", "recovery_state"),
        ("turn_recovery_records", "resolution", "recovery_resolution"),
        ("turn_effect_evidence_links", "retention_state", "evidence_retention_state"),
        ("turn_effect_attempts", "effect_class", "effect_class"),
        ("turn_effect_attempts", "state", "effect_state"),
        ("turn_effect_settlements", "source", "effect_settlement_source"),
        ("turn_effect_settlements", "outcome", "effect_settlement_outcome"),
        ("turn_terminal_deletion_ledgers", "prior_generation_state", "generation_state"),
        ("session_lifecycle_operations", "action", "session_lifecycle_action"),
        ("session_lifecycle_operations", "state", "session_lifecycle_state"),
        ("session_lifecycle_targets", "state", "session_lifecycle_target_state"),
        ("session_lifecycle_targets", "discovery_source_kind", "lifecycle_discovery_source_kind"),
        ("terminal_auth_scope_epochs", "scope_kind", "auth_fence_scope_kind"),
        ("turn_delivery_intents", "status", "delivery_state"),
        ("terminal_protocol_writer_lease", "admission_state", "writer_admission_state"),
    )
    with sqlite3.connect(db_path) as connection:
        for table, column, enum_name in cases:
            sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()[0]
            match = re.search(rf"\b{column}\s+IN\s*\(([^)]*)\)", sql, flags=re.IGNORECASE)
            assert match is not None, f"{table}.{column} has no enum CHECK"
            assert tuple(re.findall(r"'([^']+)'", match.group(1))) == registry[enum_name]


def test_sqlite_effect_bundle_uses_deferred_no_action_fk(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _create_1_0_fixture(db_path)
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _insert_workspace(connection)
        _insert_session(connection, session_id="s_v1")
        _insert_v1_turn_and_generation(connection)
        connection.execute(
            """
            INSERT INTO turn_effect_evidence_links(
              evidence_link_id,team_id,audit_object_ref,evidence_digest,object_version_digest,
              retention_state,purge_after,created_at
            ) VALUES ('eel_1','team_1','audit/object/1','ed_1','ov_1','active','2030-01-01T00:00:00Z','t')
            """,
        )
        connection.execute(
            """
            INSERT INTO turn_effect_attempts(
              attempt_id,turn_id,generation,claim_epoch,effect_class,provider,action_ref,
              idempotency_or_fence_token,non_retriable,state,started_at,settled_at,
              result_digest,settlement_id
            ) VALUES ('tea_1','t_v1',1,1,'side_effect_possible','provider','action_1',
                      'token_1',0,'settled','t','t','result_1','tes_1')
            """,
        )
        connection.execute(
            """
            INSERT INTO turn_effect_settlements(
              settlement_id,attempt_id,source,outcome,settlement_code,actor_audit_ref,
              authz_snapshot_digest,evidence_link_id,evidence_digest,evidence_recorded_at,created_at
            ) VALUES ('tes_1','tea_1','provider','confirmed_applied','provider_receipt',
                      'actor_ref','authz_1','eel_1','ed_1','t','t')
            """,
        )
        connection.commit()

        for delete_sql, row_id in (
            ("DELETE FROM turn_effect_attempts WHERE attempt_id=?", "tea_1"),
            ("DELETE FROM turn_effect_settlements WHERE settlement_id=?", "tes_1"),
            ("DELETE FROM turn_effect_evidence_links WHERE evidence_link_id=?", "eel_1"),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("BEGIN")
                connection.execute(delete_sql, (row_id,))
                connection.commit()
            connection.rollback()

        connection.execute("BEGIN")
        connection.execute(
            """
            INSERT INTO turn_effect_retention_tombstones(
              tombstone_id,team_id,turn_id,generation,effect_bundle_digest,audit_key_version,
              attempt_count,provider_token_digests_digest,settlement_outcomes_digest,state,
              eligible_at,provider_dedup_horizon_at,expires_at,created_at,aggregated_at
            ) VALUES ('tert_1','team_1','t_v1',1,'bundle_1','key_1',1,'tokens_1','outcomes_1',
                      'aggregated','t','t','2030-01-01T00:00:00Z','t','t')
            """,
        )
        connection.execute(
            """
            INSERT INTO turn_effect_retention_member_keys(
              member_key_id,team_id,attempt_identity_digest,provider_token_digest,
              audit_key_version,tombstone_id,expires_at
            ) VALUES ('term_1','team_1','attempt_hmac_1','provider_hmac_1','key_1','tert_1',
                      '2030-01-01T00:00:00Z')
            """,
        )
        connection.execute("DELETE FROM turn_effect_settlements WHERE settlement_id='tes_1'")
        connection.execute("DELETE FROM turn_effect_attempts WHERE attempt_id='tea_1'")
        connection.execute("DELETE FROM turn_effect_evidence_links WHERE evidence_link_id='eel_1'")
        connection.commit()
        assert connection.execute("SELECT count(*) FROM turn_effect_attempts").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM turn_effect_settlements").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM turn_effect_evidence_links").fetchone() == (0,)
        assert connection.execute("SELECT state FROM turn_effect_retention_tombstones").fetchone() == ("aggregated",)
        assert connection.execute("SELECT count(*) FROM turn_effect_retention_member_keys").fetchone() == (1,)


def test_ac_b1b_026_uncertified_surfaces_remain_legacy(tmp_path) -> None:
    db_path = tmp_path / "bowhead.db"
    _create_1_0_fixture(db_path)
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    with sqlite3.connect(db_path) as connection:
        _insert_workspace(connection)
        for surface in ("feishu", "scheduler"):
            session_id = f"s_{surface}"
            turn_id = f"t_{surface}"
            _insert_session(connection, session_id=session_id)
            connection.execute(
                "INSERT INTO turns(id,session_id,seq,user_text,status,started_at) VALUES (?,?,1,?,'running','t')",
                (turn_id, session_id, surface),
            )
            assert connection.execute(
                "SELECT terminal_protocol_version FROM turns WHERE id=?",
                (turn_id,),
            ).fetchone() == (None,)


def test_terminal_control_outcome_shape_and_immutability_are_db_enforced(
    tmp_path,
) -> None:
    db_path = tmp_path / "bowhead.db"
    _create_1_0_fixture(db_path)
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    with sqlite3.connect(db_path) as connection:
        _insert_workspace(connection)
        _insert_session(connection, session_id="s_v1")
        _insert_v1_turn_and_generation(connection)

        def insert_decision(
            decision_id: str,
            *,
            source_kind: str,
            action: str,
            outcome: str | None,
        ) -> None:
            connection.execute(
                """
                INSERT INTO turn_control_decisions(
                  decision_id,source_kind,source_id,turn_id,
                  prior_generation,action,decision_digest,state,
                  target_generation,actor_ref,requester_ref,
                  effect_principal_ref,source_projection_json,
                  source_projection_digest,prior_receipt_id,
                  prior_generation_transition_epoch,
                  authz_snapshot_digest,created_at,recorded_at,
                  consumed_at,control_outcome_code
                ) VALUES (?,?,?,'t_v1',1,?,?,'decision_recorded',
                          NULL,'u_1','u_1','u_1','{}','projection',
                          'receipt_1',1,'auth_1','t','t',NULL,?)
                """,
                (
                    decision_id,
                    source_kind,
                    f"source_{decision_id}",
                    action,
                    f"digest_{decision_id}",
                    outcome,
                ),
            )

        with pytest.raises(sqlite3.IntegrityError, match="outcome"):
            insert_decision(
                "td_noncontrol",
                source_kind="clarification",
                action="dismiss",
                outcome="access_revoked",
            )
        with pytest.raises(sqlite3.IntegrityError, match="outcome"):
            insert_decision(
                "td_missing",
                source_kind="system_control",
                action="revoke",
                outcome=None,
            )
        with pytest.raises(sqlite3.IntegrityError, match="outcome"):
            insert_decision(
                "td_invalid",
                source_kind="system_control",
                action="revoke",
                outcome="unknown",
            )
        insert_decision(
            "td_valid",
            source_kind="system_control",
            action="revoke",
            outcome="access_revoked",
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE turn_control_decisions
                SET control_outcome_code='dependency_missing'
                WHERE decision_id='td_valid'
                """
            )
        connection.rollback()


def test_terminal_decision_effect_skipped_resolution_shape_is_db_enforced(
    tmp_path,
) -> None:
    db_path = tmp_path / "bowhead.db"
    _create_1_0_fixture(db_path)
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    with sqlite3.connect(db_path) as connection:
        _insert_workspace(connection)
        _insert_session(connection, session_id="s_v1")
        _insert_v1_turn_and_generation(connection)
        connection.execute(
            """
            INSERT INTO turn_control_decisions(
              decision_id,source_kind,source_id,turn_id,prior_generation,
              action,decision_digest,state,actor_ref,requester_ref,
              effect_principal_ref,source_projection_json,
              source_projection_digest,prior_receipt_id,
              prior_generation_transition_epoch,authz_snapshot_digest,
              created_at,recorded_at
            ) VALUES (
              'td_effect','clarification','clarify_1','t_v1',1,
              'dismiss','digest','decision_recorded','u_1','u_1','u_1',
              '{}','projection','receipt_1',1,'auth_1','t','t'
            )
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO turn_decision_effects(
                  effect_id,decision_id,effect_kind,target_ref,payload_json,
                  payload_digest,state,resolution_code,created_at,applied_at
                ) VALUES (
                  'effect_bad','td_effect','clarification_projection',
                  'clarify_1','{}','payload','skipped',NULL,'t','t'
                )
                """
            )
        connection.execute(
            """
            INSERT INTO turn_decision_effects(
              effect_id,decision_id,effect_kind,target_ref,payload_json,
              payload_digest,state,resolution_code,created_at,applied_at
            ) VALUES (
              'effect_skipped','td_effect','clarification_projection',
              'clarify_1','{}','payload','skipped','access_revoked','t','t'
            )
            """
        )
        assert connection.execute("SELECT state,resolution_code FROM turn_decision_effects").fetchone() == ("skipped", "access_revoked")
