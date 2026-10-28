"""0073 keeps historical candidates intact and installs an inert Eval release ledger."""

from __future__ import annotations

import sqlite3
from importlib import import_module
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from bowhead_cli.migrate import TARGET_FILES

from bowhead_core import migrations

pytestmark = pytest.mark.integration
MIGRATION = import_module("bowhead_core.migrations.versions.0073_agent_owner_eval_foundation")
NOW = "2026-09-14T08:00:00+00:00"


def _upgrade(path: Path, revision: str, target: str = "core") -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = target
    command.upgrade(config, revision)


def _rows(path: Path) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(path) as db:
        names = [
            str(row[0])
            for row in db.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='table' AND name<>'alembic_version' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """,
            )
        ]
        return {name: db.execute(f'SELECT * FROM "{name}"').fetchall() for name in names}


def _schema(path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as db:
        return db.execute(
            """
            SELECT type,name,sql FROM sqlite_master
            WHERE name<>'alembic_version' AND name NOT LIKE 'sqlite_%'
            ORDER BY type,name
            """,
        ).fetchall()


def _seed_candidate_graph(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(
            "INSERT INTO teams(id,name,status,created_at) VALUES('team_eval','Eval Team','active',?)",
            (NOW,),
        )
        db.execute(
            """
            INSERT INTO users(id,team_id,email,status,created_at)
            VALUES('user_owner','team_eval','owner@example.com','active',?)
            """,
            (NOW,),
        )
        db.execute("UPDATE teams SET created_by_user_id='user_owner' WHERE id='team_eval'")
        db.execute(
            """
            INSERT INTO team_members(id,user_id,team_id,role,status,created_at)
            VALUES('tm_owner','user_owner','team_eval','member','active',?)
            """,
            (NOW,),
        )
        db.execute(
            """
            INSERT INTO workspaces(id,team_id,name,status,created_at,updated_at)
            VALUES('ws_eval','team_eval','Eval Space','active',?,?)
            """,
            (NOW, NOW),
        )
        db.execute(
            """
            INSERT INTO team_subagent(
              id,team_id,name,display_name,description,tool_refs_json,skills_json,
              visibility,created_by,owner_user_id,status,definition_mode,created_at,updated_at
            ) VALUES(
              'osa_eval','team_eval','eval-agent','Eval Agent','Agent','[]','[]',
              'private','user_owner','user_owner','enabled','revisioned',?,?
            )
            """,
            (NOW, NOW),
        )
        db.execute(
            """
            INSERT INTO team_subagent_revision(
              id,team_id,subagent_id,sequence,revision_kind,definition_json,listing_json,
              definition_hash,dependency_fingerprint,capability_fingerprint,
              risk_compiler_version,risk_json,created_by,created_at
            ) VALUES(
              'arv_eval_1','team_eval','osa_eval',1,'authored','{}','{}',
              'sha256:def','sha256:dep','sha256:cap','risk-v1','{}','user_owner',?
            )
            """,
            (NOW,),
        )
        db.execute(
            """
            INSERT INTO agent_safety_report(
              id,team_id,agent_id,subject_kind,subject_ref,content_hash,
              dependency_fingerprint,compiler_version,semantic_scanner_version,status,
              deterministic_json,semantic_json,safe_summary_json,created_by,created_at
            ) VALUES(
              'asr_eval_1','team_eval','osa_eval','revision','arv_eval_1','sha256:def',
              'sha256:dep','safety-v1','semantic-v1','pass','{}','{}','{}','user_owner',?
            )
            """,
            (NOW,),
        )
        db.execute(
            """
            INSERT INTO team_subagent_release(
              id,team_id,subagent_id,release_sequence,revision_id,audience_snapshot_json,
              audience_hash,capability_fingerprint,dependency_fingerprint,risk_json,
              safety_report_id,activated_by,activated_at
            ) VALUES(
              'arl_eval_1','team_eval','osa_eval',1,'arv_eval_1','{}','sha256:audience',
              'sha256:cap','sha256:dep','{}','asr_eval_1','user_owner',?
            )
            """,
            (NOW,),
        )
        db.execute(
            """
            INSERT INTO team_subagent_working_copy(
              id,team_id,subagent_id,base_release_id,definition_json,listing_json,
              audience_json,validation_json,updated_by,created_at,updated_at
            ) VALUES(
              'awc_eval_1','team_eval','osa_eval','arl_eval_1','{}','{}','{}','{}',
              'user_owner',?,?
            )
            """,
            (NOW, NOW),
        )
        for candidate_id, state in (
            ("arc_pending", "pending"),
            ("arc_withdrawn", "withdrawn"),
            ("arc_stale", "stale"),
            ("arc_activated", "activated"),
        ):
            decided = state != "pending"
            db.execute(
                """
                INSERT INTO team_subagent_release_candidate(
                  id,team_id,subagent_id,revision_id,base_live_release_id,
                  target_audience_json,target_audience_hash,capability_fingerprint,
                  dependency_fingerprint,risk_compiler_version,risk_json,safety_report_id,
                  candidate_hash,state,submitted_by,submitted_at,decided_by,
                  decision_reason_code,self_review,decided_at,activated_release_id,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    candidate_id,
                    "team_eval",
                    "osa_eval",
                    "arv_eval_1",
                    "arl_eval_1",
                    "{}",
                    "sha256:audience",
                    "sha256:cap",
                    "sha256:dep",
                    "risk-v1",
                    "{}",
                    "asr_eval_1",
                    f"sha256:{candidate_id}",
                    state,
                    "user_owner",
                    NOW,
                    "user_owner" if decided else None,
                    f"candidate_{state}" if decided else None,
                    1 if decided else None,
                    NOW if decided else None,
                    "arl_eval_1" if state == "activated" else None,
                    NOW,
                ),
            )
        db.execute(
            """
            INSERT INTO team_subagent_release(
              id,team_id,subagent_id,release_sequence,revision_id,predecessor_release_id,
              source_candidate_id,audience_snapshot_json,audience_hash,
              capability_fingerprint,dependency_fingerprint,risk_json,safety_report_id,
              activated_by,activated_at
            ) VALUES(
              'arl_eval_2','team_eval','osa_eval',2,'arv_eval_1','arl_eval_1',
              'arc_activated','{}','sha256:audience','sha256:cap','sha256:dep','{}',
              'asr_eval_1','user_owner',?
            )
            """,
            (NOW,),
        )
        db.execute(
            """
            INSERT INTO agent_release_candidate_name_claim(
              team_id,candidate_id,agent_id,locale,normalized_key,claim_kind,state,claimed_at
            ) VALUES(
              'team_eval','arc_pending','osa_eval','zh-CN','eval-agent','exclusive','reserved',?
            )
            """,
            (NOW,),
        )
        db.execute(
            """
            INSERT INTO team_subagent_release_event(
              id,team_id,subagent_id,candidate_id,event_type,actor_user_id,payload_json,created_at
            ) VALUES(
              'are_eval_pending','team_eval','osa_eval','arc_pending',
              'candidate.submitted','user_owner','{}',?
            )
            """,
            (NOW,),
        )
        db.execute(
            """
            INSERT INTO agent_eval_suite(
              id,team_id,agent_id,agent_source_kind,suite_kind,name,visibility,state,
              created_by,created_at,updated_at
            ) VALUES(
              'aes_eval','team_eval','osa_eval','team','owner_quality','Regression',
              'owner','active','user_owner',?,?
            )
            """,
            (NOW, NOW),
        )
        db.execute(
            """
            INSERT INTO agent_eval_suite_revision(
              id,suite_id,sequence,definition_json,definition_hash,scorer_bundle_ref,
              judge_policy_json,case_count,case_set_hash,created_by,created_at
            ) VALUES(
              'aev_eval_1','aes_eval',1,'{}','sha256:suite','scorer-v1','{}',1,
              'sha256:cases','user_owner',?
            )
            """,
            (NOW,),
        )
        db.execute(
            """
            INSERT INTO agent_eval_case_revision(
              id,suite_revision_id,case_key,case_hash,input_json,fixture_ref_json,
              expected_contract_json,scorer_config_json,effect_policy_json,created_at
            ) VALUES(
              'aec_eval_1','aev_eval_1','case-1','sha256:case','{}','{}','{}','{}','{}',?
            )
            """,
            (NOW,),
        )
        _insert_eval_run(db, run_id="aer_pending", candidate_id="arc_pending", idempotency_key="pending")
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def _insert_eval_run(
    db: sqlite3.Connection,
    *,
    run_id: str,
    candidate_id: str | None,
    idempotency_key: str,
    release_id: str | None = None,
) -> None:
    db.execute(
        """
        INSERT INTO agent_eval_run(
          id,team_id,agent_id,agent_source_kind,revision_ref,release_id,candidate_id,
          capability_fingerprint,dependency_fingerprint,model_config_hash,
          subject_fingerprint,suite_revision_id,case_set_hash,scorer_bundle_ref,
          trigger_kind,status,idempotency_key,created_by,created_at
        ) VALUES(
          ?,'team_eval','osa_eval','team','arv_eval_1',?, ?,
          'sha256:cap','sha256:dep','sha256:model','sha256:subject',
          'aev_eval_1','sha256:cases','scorer-v1','manual','queued',?,'user_owner',?
        )
        """,
        (run_id, release_id, candidate_id, idempotency_key, NOW),
    )


def _replace_pending_with_prepared(db: sqlite3.Connection) -> None:
    db.execute(
        """
        UPDATE team_subagent_release_candidate
        SET state='withdrawn',row_version=row_version+1,decided_by='user_owner',
            decision_reason_code='candidate_withdrawn',self_review=1,decided_at=?,updated_at=?
        WHERE id='arc_pending'
        """,
        (NOW, NOW),
    )
    db.execute(
        """
        INSERT INTO team_subagent_release_candidate(
          id,team_id,subagent_id,revision_id,base_live_release_id,
          target_audience_json,target_audience_hash,capability_fingerprint,
          dependency_fingerprint,risk_compiler_version,risk_json,safety_report_id,
          candidate_hash,state,submitted_by,submitted_at,updated_at
        ) VALUES(
          'arc_prepared','team_eval','osa_eval','arv_eval_1','arl_eval_1','{}',
          'sha256:audience','sha256:cap','sha256:dep','risk-v1','{}','asr_eval_1',
          'sha256:prepared','prepared','user_owner',?,?
        )
        """,
        (NOW, NOW),
    )


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_all_targets_upgrade_twice_without_rewriting_existing_rows(tmp_path: Path, target: str) -> None:
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    before_rows = _rows(path)
    before_schema = _schema(path)

    _upgrade(path, MIGRATION.revision, target)
    _upgrade(path, MIGRATION.revision, target)

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.revision,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        if target == "core":
            candidate_sql = str(
                db.execute(
                    "SELECT sql FROM sqlite_master WHERE name='team_subagent_release_candidate'",
                ).fetchone()[0],
            )
            assert "'prepared'" in candidate_sql
            assert db.execute("SELECT COUNT(*) FROM agent_eval_plan").fetchone() == (0,)
            assert db.execute("SELECT COUNT(*) FROM agent_eval_snapshot").fetchone() == (0,)
        else:
            assert _schema(path) == before_schema

    after_rows = _rows(path)
    assert {name: after_rows[name] for name in before_rows} == before_rows


def test_candidate_copy_preserves_data_and_all_incoming_foreign_keys(tmp_path: Path) -> None:
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    _seed_candidate_graph(path)
    with sqlite3.connect(path) as db:
        candidates_before = db.execute(
            "SELECT * FROM team_subagent_release_candidate ORDER BY id",
        ).fetchall()
        children_before = {
            "release": db.execute(
                "SELECT id,source_candidate_id FROM team_subagent_release WHERE source_candidate_id IS NOT NULL",
            ).fetchall(),
            "claim": db.execute(
                "SELECT candidate_id,locale,normalized_key FROM agent_release_candidate_name_claim",
            ).fetchall(),
            "event": db.execute(
                "SELECT id,candidate_id FROM team_subagent_release_event WHERE candidate_id IS NOT NULL",
            ).fetchall(),
            "run": db.execute(
                "SELECT id,candidate_id FROM agent_eval_run WHERE candidate_id IS NOT NULL",
            ).fetchall(),
        }

    _upgrade(path, MIGRATION.revision)

    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        assert db.execute("SELECT * FROM team_subagent_release_candidate ORDER BY id").fetchall() == candidates_before
        assert (
            db.execute(
                "SELECT id,source_candidate_id FROM team_subagent_release WHERE source_candidate_id IS NOT NULL",
            ).fetchall()
            == children_before["release"]
        )
        assert (
            db.execute(
                "SELECT candidate_id,locale,normalized_key FROM agent_release_candidate_name_claim",
            ).fetchall()
            == children_before["claim"]
        )
        assert (
            db.execute(
                "SELECT id,candidate_id FROM team_subagent_release_event WHERE candidate_id IS NOT NULL",
            ).fetchall()
            == children_before["event"]
        )
        assert (
            db.execute(
                "SELECT id,candidate_id FROM agent_eval_run WHERE candidate_id IS NOT NULL",
            ).fetchall()
            == children_before["run"]
        )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []

        _replace_pending_with_prepared(db)
        with pytest.raises(sqlite3.IntegrityError, match="candidate transition mismatch"):
            db.execute(
                """
                UPDATE team_subagent_release_candidate
                SET state='rejected',row_version=row_version+1,decided_by='user_owner',
                    decision_reason_code='invalid_transition',self_review=1,
                    decided_at=?,updated_at=?
                WHERE id='arc_prepared'
                """,
                (NOW, NOW),
            )
        _insert_eval_run(
            db,
            run_id="aer_prepared",
            candidate_id="arc_prepared",
            idempotency_key="prepared",
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """
                INSERT INTO team_subagent_release_candidate(
                  id,team_id,subagent_id,revision_id,target_audience_json,
                  target_audience_hash,capability_fingerprint,dependency_fingerprint,
                  risk_compiler_version,risk_json,safety_report_id,candidate_hash,state,
                  submitted_by,submitted_at,updated_at
                ) VALUES(
                  'arc_second_active','team_eval','osa_eval','arv_eval_1','{}',
                  'sha256:audience','sha256:cap','sha256:dep','risk-v1','{}','asr_eval_1',
                  'sha256:second','pending','user_owner',?,?
                )
                """,
                (NOW, NOW),
            )


def test_new_ledgers_enforce_owner_scope_immutability_and_one_active_execution(tmp_path: Path) -> None:
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    _seed_candidate_graph(path)
    _upgrade(path, MIGRATION.revision)

    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        _replace_pending_with_prepared(db)
        db.execute(
            """
            INSERT INTO agent_revision_compatibility(
              revision_id,team_id,agent_id,compatibility_major,verdict,
              compiler_version,contract_hash,reason_codes_json,created_at
            ) VALUES(
              'arv_eval_1','team_eval','osa_eval',1,'compatible','compat-v1',
              'sha256:contract','[]',?
            )
            """,
            (NOW,),
        )
        db.execute(
            """
            INSERT INTO agent_eval_policy_revision(
              id,team_id,agent_id,sequence,policy_json,policy_hash,created_by,created_at
            ) VALUES(
              'aepr_eval_1','team_eval','osa_eval',1,'{}','sha256:policy','user_owner',?
            )
            """,
            (NOW,),
        )
        db.execute(
            """
            INSERT INTO agent_eval_plan(
              id,team_id,agent_id,candidate_id,sequence,policy_revision_id,
              baseline_release_id,execution_workspace_id,execution_workspace_scope_hash,
              candidate_hash,plan_json,plan_hash,compiler_version,created_by,created_at
            ) VALUES(
              'aep_eval_1','team_eval','osa_eval','arc_prepared',1,'aepr_eval_1',
              'arl_eval_1','ws_eval','sha256:workspace','sha256:prepared','{}',
              'sha256:plan','plan-v1','user_owner',?
            )
            """,
            (NOW,),
        )
        db.execute(
            """
            INSERT INTO agent_eval_plan_suite(
              plan_id,suite_revision_id,suite_kind,execution_scope,required,tier,
              attempts,fixture_bundle_json,fixture_bundle_hash,scorer_bundle_ref,
              scorer_bundle_hash,judge_policy_hash,order_index
            ) VALUES(
              'aep_eval_1','aev_eval_1','owner_quality','paired',1,'standard',1,
              '{}','sha256:fixture','scorer-v1','sha256:scorer','sha256:judge',0
            )
            """,
        )
        db.execute(
            """
            INSERT INTO agent_eval_candidate_control(
              candidate_id,team_id,agent_id,source_working_copy_id,
              source_working_copy_version,source_working_copy_hash,active_plan_id,
              readiness,reason_codes_json,prepared_actor_kind,prepared_actor_ref,
              prepared_user_id,row_version,created_at,updated_at
            ) VALUES(
              'arc_prepared','team_eval','osa_eval','awc_eval_1',1,'sha256:working',
              'aep_eval_1','missing','[]','user','user_owner','user_owner',1,?,?
            )
            """,
            (NOW, NOW),
        )
        _insert_comparison(db, execution_id="aex_eval_1", idempotency="sha256:key-one")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_comparison(db, execution_id="aex_eval_2", idempotency="sha256:key-two")
        with pytest.raises(sqlite3.IntegrityError, match="comparison run scope mismatch"):
            db.execute(
                """
                INSERT INTO agent_eval_comparison_run(
                  execution_id,suite_revision_id,subject_side,run_id
                ) VALUES('aex_eval_1','aev_eval_1','baseline','aer_pending')
                """,
            )
        _insert_eval_run(
            db,
            run_id="aer_compare_candidate",
            candidate_id="arc_prepared",
            idempotency_key="compare-candidate",
        )
        _insert_eval_run(
            db,
            run_id="aer_compare_baseline",
            candidate_id=None,
            release_id="arl_eval_1",
            idempotency_key="compare-baseline",
        )
        db.execute(
            """
            INSERT INTO agent_eval_comparison_run(
              execution_id,suite_revision_id,subject_side,run_id
            ) VALUES('aex_eval_1','aev_eval_1','candidate','aer_compare_candidate')
            """,
        )
        db.execute(
            """
            INSERT INTO agent_eval_comparison_run(
              execution_id,suite_revision_id,subject_side,run_id
            ) VALUES('aex_eval_1','aev_eval_1','baseline','aer_compare_baseline')
            """,
        )
        with pytest.raises(sqlite3.IntegrityError, match="comparison transition mismatch"):
            db.execute(
                """
                UPDATE agent_eval_comparison_execution
                SET status='completed',row_version=row_version+1,finished_at=?
                WHERE id='aex_eval_1'
                """,
                (NOW,),
            )
        db.execute(
            """
            UPDATE agent_eval_comparison_execution
            SET status='running',row_version=row_version+1,started_at=?
            WHERE id='aex_eval_1'
            """,
            (NOW,),
        )
        db.execute(
            """
            UPDATE agent_eval_comparison_execution
            SET status='finalizing',row_version=row_version+1,finalizing_at=?
            WHERE id='aex_eval_1'
            """,
            (NOW,),
        )
        db.execute(
            """
            UPDATE agent_eval_comparison_execution
            SET status='completed',row_version=row_version+1,finished_at=?
            WHERE id='aex_eval_1'
            """,
            (NOW,),
        )
        db.execute(
            """
            INSERT INTO agent_eval_snapshot(
              id,team_id,agent_id,candidate_id,plan_id,execution_id,readiness,
              identity_json,identity_hash,summary_json,reviewer_projection_json,
              reviewer_projection_hash,snapshot_hash,created_at
            ) VALUES(
              'aevs_eval_1','team_eval','osa_eval','arc_prepared','aep_eval_1',
              'aex_eval_1','ready_with_risks','{}','sha256:identity','{}','{}',
              'sha256:projection','sha256:snapshot',?
            )
            """,
            (NOW,),
        )
        db.execute(
            """
            INSERT INTO agent_eval_override(
              id,team_id,agent_id,candidate_id,plan_id,snapshot_id,override_hash,
              reason_overrides_json,reason_overrides_hash,
              team_evidence_selection_json,team_evidence_selection_hash,
              team_evidence_json,team_evidence_hash,created_by,created_at
            ) VALUES(
              'aeov_eval_1','team_eval','osa_eval','arc_prepared','aep_eval_1',
              'aevs_eval_1','sha256:override','["risk"]','sha256:reasons','{}',
              'sha256:selection','{}','sha256:evidence','user_owner',?
            )
            """,
            (NOW,),
        )
        db.execute(
            """
            UPDATE team_subagent_release_candidate
            SET state='pending',row_version=row_version+1,updated_at=?
            WHERE id='arc_prepared'
            """,
            (NOW,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="projection scope mismatch"):
            _freeze_review_override(db, override_hash="sha256:wrong")
        _freeze_review_override(db, override_hash="sha256:override")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(
                "UPDATE agent_eval_policy_revision SET policy_json=? WHERE id='aepr_eval_1'",
                ('{"changed":true}',),
            )

        db.execute(
            "INSERT INTO teams(id,name,status,created_at) VALUES('team_other','Other','active',?)",
            (NOW,),
        )
        db.execute(
            """
            INSERT INTO workspaces(id,team_id,name,status,created_at,updated_at)
            VALUES('ws_other','team_other','Other Space','active',?,?)
            """,
            (NOW, NOW),
        )
        with pytest.raises(sqlite3.IntegrityError, match="workspace scope mismatch"):
            db.execute(
                """
                INSERT INTO agent_eval_plan(
                  id,team_id,agent_id,candidate_id,sequence,policy_revision_id,
                  baseline_release_id,execution_workspace_id,execution_workspace_scope_hash,
                  candidate_hash,plan_json,plan_hash,compiler_version,created_by,created_at
                ) VALUES(
                  'aep_eval_cross','team_eval','osa_eval','arc_prepared',2,'aepr_eval_1',
                  'arl_eval_1','ws_other','sha256:workspace','sha256:prepared','{}',
                  'sha256:plan-cross','plan-v1','user_owner',?
                )
                """,
                (NOW,),
            )

        db.execute(
            """
            UPDATE team_subagent_release_candidate
            SET state='approved',row_version=row_version+1,decided_by='user_owner',
                decision_reason_code='owner_approved',self_review=1,decided_at=?,updated_at=?
            WHERE id='arc_prepared'
            """,
            (NOW, NOW),
        )
        db.execute(
            """
            INSERT INTO team_subagent_release(
              id,team_id,subagent_id,release_sequence,revision_id,predecessor_release_id,
              source_candidate_id,audience_snapshot_json,audience_hash,
              capability_fingerprint,dependency_fingerprint,risk_json,safety_report_id,
              activated_by,activated_at
            ) VALUES(
              'arl_eval_3','team_eval','osa_eval',3,'arv_eval_1','arl_eval_1',
              'arc_prepared','{}','sha256:audience','sha256:cap','sha256:dep','{}',
              'asr_eval_1','user_owner',?
            )
            """,
            (NOW,),
        )
        db.execute(
            """
            UPDATE team_subagent_release_candidate
            SET state='activated',row_version=row_version+1,
                activated_release_id='arl_eval_3',updated_at=?
            WHERE id='arc_prepared'
            """,
            (NOW,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="release Eval evidence scope mismatch"):
            _insert_release_evidence(db, override_hash="sha256:wrong")
        _insert_release_evidence(db, override_hash="sha256:override")


def _insert_comparison(
    db: sqlite3.Connection,
    *,
    execution_id: str,
    idempotency: str,
) -> None:
    db.execute(
        """
        INSERT INTO agent_eval_comparison_execution(
          id,team_id,agent_id,candidate_id,plan_id,run_kind,selection_json,
          selection_hash,status,idempotency_hash,request_hash,actor_kind,actor_ref,
          requested_at
        ) VALUES(
          ?,'team_eval','osa_eval','arc_prepared','aep_eval_1','release','{}',
          'sha256:selection','queued',?,'sha256:request','user','user_owner',?
        )
        """,
        (execution_id, idempotency, NOW),
    )


def _freeze_review_override(db: sqlite3.Connection, *, override_hash: str) -> None:
    db.execute(
        """
        UPDATE agent_eval_candidate_control
        SET active_execution_id='aex_eval_1',active_snapshot_id='aevs_eval_1',
            active_override_id='aeov_eval_1',readiness='ready_with_risks',
            review_submitted_by='user_owner',review_submitted_at=?,
            review_plan_id='aep_eval_1',review_plan_hash='sha256:plan',
            review_snapshot_id='aevs_eval_1',review_snapshot_hash='sha256:snapshot',
            review_override_id='aeov_eval_1',review_override_hash=?,
            row_version=row_version+1,updated_at=?
        WHERE candidate_id='arc_prepared'
        """,
        (NOW, override_hash, NOW),
    )


def _insert_release_evidence(db: sqlite3.Connection, *, override_hash: str) -> None:
    db.execute(
        """
        INSERT INTO agent_release_eval_evidence(
          release_id,team_id,agent_id,candidate_id,plan_id,snapshot_id,override_id,
          candidate_hash,plan_hash,snapshot_hash,override_hash,readiness,created_at
        ) VALUES(
          'arl_eval_3','team_eval','osa_eval','arc_prepared','aep_eval_1','aevs_eval_1',
          'aeov_eval_1','sha256:prepared','sha256:plan','sha256:snapshot',?,
          'ready_with_risks',?
        )
        """,
        (override_hash, NOW),
    )


def test_schema_drift_is_rejected_before_copy(tmp_path: Path) -> None:
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    with sqlite3.connect(path) as db:
        db.execute("DROP INDEX uq_agent_one_pending_candidate")
        db.execute(
            """
            CREATE UNIQUE INDEX uq_agent_one_pending_candidate
            ON team_subagent_release_candidate(id)
            WHERE state='approved'
            """,
        )

    with pytest.raises(RuntimeError, match="candidate object semantics drift"):
        _upgrade(path, MIGRATION.revision)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='agent_eval_plan'",
            ).fetchone()
            is None
        )


def test_failure_rolls_back_candidate_copy_and_all_new_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision)
    _seed_candidate_graph(path)
    from sqlalchemy.engine import Connection

    original = Connection.exec_driver_sql

    def fail_plan(self: object, statement: str, *args: object, **kwargs: object):
        if statement.lstrip().startswith("CREATE TABLE agent_eval_plan ("):
            raise RuntimeError("injected foundation failure")
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "exec_driver_sql", fail_plan)
    with pytest.raises(RuntimeError, match="injected foundation failure"):
        _upgrade(path, MIGRATION.revision)

    with sqlite3.connect(path) as db:
        candidate_sql = str(
            db.execute(
                "SELECT sql FROM sqlite_master WHERE name='team_subagent_release_candidate'",
            ).fetchone()[0],
        )
        run_trigger = str(
            db.execute(
                "SELECT sql FROM sqlite_master WHERE name='trg_agent_eval_run_scope'",
            ).fetchone()[0],
        )
        assert "'prepared'" not in candidate_sql
        assert "'prepared'" not in run_trigger
        assert db.execute("SELECT COUNT(*) FROM team_subagent_release_candidate").fetchone() == (4,)
        assert db.execute("SELECT name FROM sqlite_master WHERE name='agent_eval_plan'").fetchone() is None
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (MIGRATION.down_revision,)
