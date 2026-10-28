"""Versioned Agent Eval persistence and state-machine foundation.

M3-9 adds inert suite, immutable revision/case, exact run/result, trigger
policy, and evidence projection records.  It does not run an Agent, call a
Judge, dispatch a Tool, schedule work, or mutate release eligibility.

Revision ID: 0037_agent_eval_store
Revises: 0036_agent_capability_execution
Create Date: 2026-09-05
"""

from __future__ import annotations

from typing import Any

from alembic import context

revision = "0037_agent_eval_store"
down_revision = "0036_agent_capability_execution"
branch_labels = None
depends_on = None


_OBJECTS: tuple[tuple[str, str, str], ...] = (
    (
        "table",
        "agent_eval_suite",
        """
        CREATE TABLE agent_eval_suite (
          id                 TEXT PRIMARY KEY CHECK(id LIKE 'aes_%'),
          team_id            TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          agent_id           TEXT,
          agent_source_kind  TEXT CHECK(agent_source_kind IN ('team','system')),
          category_term_id   TEXT,
          suite_kind         TEXT NOT NULL CHECK(suite_kind IN (
                               'platform_contract','owner_quality','category_benchmark'
                             )),
          name               TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 120),
          visibility         TEXT NOT NULL CHECK(visibility IN (
                               'owner','reviewer','team_evidence'
                             )),
          state              TEXT NOT NULL CHECK(state IN ('draft','active','archived')),
          row_version        INTEGER NOT NULL DEFAULT 1 CHECK(row_version >= 1),
          created_by         TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          created_at         TEXT NOT NULL,
          updated_at         TEXT NOT NULL,
          CHECK(
            (
              agent_id IS NOT NULL AND agent_source_kind IS NOT NULL
              AND category_term_id IS NULL AND suite_kind != 'category_benchmark'
            ) OR (
              agent_id IS NULL AND agent_source_kind IS NULL
              AND category_term_id IS NOT NULL AND suite_kind = 'category_benchmark'
            )
          ),
          UNIQUE(team_id, id)
        )
        """,
    ),
    (
        "table",
        "agent_eval_suite_revision",
        """
        CREATE TABLE agent_eval_suite_revision (
          id                 TEXT PRIMARY KEY CHECK(id LIKE 'aev_%'),
          suite_id           TEXT NOT NULL REFERENCES agent_eval_suite(id) ON DELETE RESTRICT,
          sequence           INTEGER NOT NULL CHECK(sequence >= 1),
          definition_json    TEXT NOT NULL CHECK(json_valid(definition_json))
                              CHECK(json_type(definition_json)='object'),
          definition_hash    TEXT NOT NULL CHECK(definition_hash LIKE 'sha256:%'),
          scorer_bundle_ref  TEXT NOT NULL CHECK(length(scorer_bundle_ref) > 0),
          judge_policy_json  TEXT NOT NULL CHECK(json_valid(judge_policy_json))
                              CHECK(json_type(judge_policy_json)='object'),
          case_count         INTEGER NOT NULL CHECK(case_count BETWEEN 1 AND 200),
          case_set_hash      TEXT NOT NULL CHECK(case_set_hash LIKE 'sha256:%'),
          created_by         TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          created_at         TEXT NOT NULL,
          UNIQUE(suite_id, sequence),
          UNIQUE(suite_id, definition_hash),
          UNIQUE(suite_id, id)
        )
        """,
    ),
    (
        "table",
        "agent_eval_case_revision",
        """
        CREATE TABLE agent_eval_case_revision (
          id                     TEXT PRIMARY KEY CHECK(id LIKE 'aec_%'),
          suite_revision_id      TEXT NOT NULL
                                  REFERENCES agent_eval_suite_revision(id) ON DELETE RESTRICT,
          case_key               TEXT NOT NULL CHECK(length(case_key) BETWEEN 1 AND 120),
          case_hash              TEXT NOT NULL CHECK(case_hash LIKE 'sha256:%'),
          input_json             TEXT NOT NULL CHECK(json_valid(input_json))
                                  CHECK(json_type(input_json)='object'),
          fixture_ref_json       TEXT NOT NULL CHECK(json_valid(fixture_ref_json))
                                  CHECK(json_type(fixture_ref_json)='object'),
          expected_contract_json TEXT NOT NULL CHECK(json_valid(expected_contract_json))
                                  CHECK(json_type(expected_contract_json)='object'),
          scorer_config_json     TEXT NOT NULL CHECK(json_valid(scorer_config_json))
                                  CHECK(json_type(scorer_config_json)='object'),
          effect_policy_json     TEXT NOT NULL CHECK(json_valid(effect_policy_json))
                                  CHECK(json_type(effect_policy_json)='object'),
          valid_until            TEXT,
          created_at             TEXT NOT NULL,
          UNIQUE(suite_revision_id, case_key),
          UNIQUE(suite_revision_id, id)
        )
        """,
    ),
    (
        "table",
        "agent_eval_run",
        """
        CREATE TABLE agent_eval_run (
          id                       TEXT PRIMARY KEY CHECK(id LIKE 'aer_%'),
          team_id                  TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          agent_id                 TEXT NOT NULL,
          agent_source_kind        TEXT NOT NULL CHECK(agent_source_kind IN ('team','system')),
          revision_ref             TEXT NOT NULL CHECK(revision_ref LIKE 'arv_%'),
          release_id               TEXT,
          capability_fingerprint   TEXT NOT NULL CHECK(capability_fingerprint LIKE 'sha256:%'),
          dependency_fingerprint   TEXT NOT NULL CHECK(dependency_fingerprint LIKE 'sha256:%'),
          model_config_hash        TEXT NOT NULL CHECK(model_config_hash LIKE 'sha256:%'),
          subject_fingerprint      TEXT NOT NULL CHECK(subject_fingerprint LIKE 'sha256:%'),
          suite_revision_id        TEXT NOT NULL
                                    REFERENCES agent_eval_suite_revision(id) ON DELETE RESTRICT,
          case_set_hash            TEXT NOT NULL CHECK(case_set_hash LIKE 'sha256:%'),
          scorer_bundle_ref        TEXT NOT NULL CHECK(length(scorer_bundle_ref) > 0),
          judge_role_ref           TEXT,
          judge_model_fingerprint  TEXT CHECK(
                                     judge_model_fingerprint IS NULL
                                     OR judge_model_fingerprint LIKE 'sha256:%'
                                   ),
          trigger_kind             TEXT NOT NULL CHECK(trigger_kind IN (
                                     'manual','definition_change','dependency_change',
                                     'model_change','scheduled'
                                   )),
          status                   TEXT NOT NULL CHECK(status IN (
                                     'queued','running','passed','failed','unknown',
                                     'incomplete','cancelled'
                                   )),
          summary_json             TEXT CHECK(
                                     summary_json IS NULL OR (
                                       json_valid(summary_json) AND json_type(summary_json)='object'
                                     )
                                   ),
          idempotency_key          TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 200),
          created_by               TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          created_at               TEXT NOT NULL,
          started_at               TEXT,
          finished_at              TEXT,
          UNIQUE(team_id, idempotency_key),
          UNIQUE(team_id, id),
          CHECK(
            (judge_role_ref IS NULL AND judge_model_fingerprint IS NULL)
            OR (judge_role_ref IS NOT NULL AND judge_model_fingerprint IS NOT NULL)
          ),
          CHECK(status != 'queued' OR (
            started_at IS NULL AND finished_at IS NULL AND summary_json IS NULL
          )),
          CHECK(status != 'running' OR (
            started_at IS NOT NULL AND finished_at IS NULL AND summary_json IS NULL
          )),
          CHECK(status IN ('queued','running') OR (
            finished_at IS NOT NULL AND summary_json IS NOT NULL
          ))
        )
        """,
    ),
    (
        "table",
        "agent_eval_case_result",
        """
        CREATE TABLE agent_eval_case_result (
          run_id                 TEXT NOT NULL REFERENCES agent_eval_run(id) ON DELETE RESTRICT,
          case_revision_id       TEXT NOT NULL
                                 REFERENCES agent_eval_case_revision(id) ON DELETE RESTRICT,
          attempt                INTEGER NOT NULL CHECK(attempt >= 1),
          status                 TEXT NOT NULL CHECK(status IN (
                                   'passed','failed','judge_unknown','infra_failed','cancelled'
                                 )),
          deterministic_json     TEXT NOT NULL CHECK(json_valid(deterministic_json))
                                 CHECK(json_type(deterministic_json)='object'),
          judge_json             TEXT CHECK(
                                   judge_json IS NULL OR (
                                     json_valid(judge_json) AND json_type(judge_json)='object'
                                   )
                                 ),
          trace_evidence_ref     TEXT,
          duration_ms            INTEGER CHECK(duration_ms IS NULL OR duration_ms >= 0),
          cost_microusd          INTEGER CHECK(cost_microusd IS NULL OR cost_microusd >= 0),
          failure_code           TEXT,
          created_at             TEXT NOT NULL,
          PRIMARY KEY(run_id, case_revision_id, attempt)
        )
        """,
    ),
    (
        "table",
        "agent_eval_trigger_policy",
        """
        CREATE TABLE agent_eval_trigger_policy (
          team_id               TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          agent_source_kind     TEXT NOT NULL CHECK(agent_source_kind IN ('team','system')),
          agent_id              TEXT NOT NULL,
          on_definition_change  INTEGER NOT NULL DEFAULT 1 CHECK(on_definition_change IN (0,1)),
          on_dependency_change  INTEGER NOT NULL DEFAULT 1 CHECK(on_dependency_change IN (0,1)),
          on_model_change       INTEGER NOT NULL DEFAULT 1 CHECK(on_model_change IN (0,1)),
          cron_expr             TEXT,
          enabled               INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
          row_version           INTEGER NOT NULL DEFAULT 1 CHECK(row_version >= 1),
          updated_by            TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
          updated_at            TEXT NOT NULL,
          PRIMARY KEY(team_id, agent_source_kind, agent_id)
        )
        """,
    ),
    (
        "table",
        "agent_eval_evidence_state",
        """
        CREATE TABLE agent_eval_evidence_state (
          team_id              TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
          agent_source_kind    TEXT NOT NULL CHECK(agent_source_kind IN ('team','system')),
          agent_id             TEXT NOT NULL,
          suite_id             TEXT NOT NULL REFERENCES agent_eval_suite(id) ON DELETE RESTRICT,
          current_run_id       TEXT REFERENCES agent_eval_run(id) ON DELETE RESTRICT,
          subject_fingerprint  TEXT NOT NULL CHECK(subject_fingerprint LIKE 'sha256:%'),
          state                TEXT NOT NULL CHECK(state IN (
                                 'missing','running','valid','failed','unknown',
                                 'stale','blocked'
                               )),
          stale_reason         TEXT,
          row_version          INTEGER NOT NULL DEFAULT 1 CHECK(row_version >= 1),
          updated_at           TEXT NOT NULL,
          PRIMARY KEY(team_id, agent_source_kind, agent_id, suite_id),
          CHECK(state != 'missing' OR current_run_id IS NULL),
          CHECK(state != 'running' OR current_run_id IS NOT NULL),
          CHECK(state != 'valid' OR current_run_id IS NOT NULL),
          CHECK(state != 'failed' OR current_run_id IS NOT NULL),
          CHECK(state != 'unknown' OR current_run_id IS NOT NULL),
          CHECK(state != 'blocked' OR current_run_id IS NOT NULL),
          CHECK(
            (state='stale' AND stale_reason IS NOT NULL)
            OR (state!='stale' AND stale_reason IS NULL)
          )
        )
        """,
    ),
    (
        "index",
        "idx_agent_eval_suite_subject",
        """
        CREATE INDEX idx_agent_eval_suite_subject
        ON agent_eval_suite(team_id, agent_source_kind, agent_id, state, suite_kind)
        """,
    ),
    (
        "index",
        "idx_agent_eval_run_subject",
        """
        CREATE INDEX idx_agent_eval_run_subject
        ON agent_eval_run(team_id, agent_source_kind, agent_id, created_at, id)
        """,
    ),
    (
        "index",
        "idx_agent_eval_run_status",
        """
        CREATE INDEX idx_agent_eval_run_status
        ON agent_eval_run(status, created_at, id)
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_suite_scope",
        """
        CREATE TRIGGER trg_agent_eval_suite_scope
        BEFORE INSERT ON agent_eval_suite
        WHEN NOT EXISTS (
          SELECT 1
          FROM teams team
          JOIN users actor ON actor.id=NEW.created_by AND actor.status='active'
          JOIN team_members member
            ON member.team_id=team.id AND member.user_id=actor.id AND member.status='active'
          WHERE team.id=NEW.team_id AND team.status='active'
        ) OR (
          NEW.agent_source_kind='team' AND NOT EXISTS (
            SELECT 1 FROM team_subagent agent
            WHERE agent.team_id=NEW.team_id AND agent.id=NEW.agent_id
          )
        ) OR (
          NEW.agent_source_kind='system' AND NOT EXISTS (
            SELECT 1 FROM system_agent_effective_revision revision
            WHERE revision.team_id=NEW.team_id AND revision.agent_id=NEW.agent_id
          )
        ) OR (
          NEW.suite_kind='owner_quality' AND (
            NEW.agent_source_kind!='team' OR NOT EXISTS (
              SELECT 1 FROM team_subagent agent
              WHERE agent.team_id=NEW.team_id AND agent.id=NEW.agent_id
                AND COALESCE(agent.owner_user_id,agent.created_by)=NEW.created_by
            )
          )
        ) OR (
          NEW.suite_kind IN ('platform_contract','category_benchmark') AND NOT EXISTS (
            SELECT 1
            FROM users actor
            JOIN team_members member
              ON member.user_id=actor.id AND member.team_id=NEW.team_id
             AND member.status='active'
            JOIN teams team ON team.id=NEW.team_id
            WHERE actor.id=NEW.created_by AND actor.status='active'
              AND (member.role='team_admin' OR team.created_by_user_id=actor.id)
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'Agent Eval suite scope mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_suite_update",
        """
        CREATE TRIGGER trg_agent_eval_suite_update
        BEFORE UPDATE ON agent_eval_suite
        WHEN NEW.id IS NOT OLD.id
          OR NEW.team_id IS NOT OLD.team_id
          OR NEW.agent_id IS NOT OLD.agent_id
          OR NEW.agent_source_kind IS NOT OLD.agent_source_kind
          OR NEW.category_term_id IS NOT OLD.category_term_id
          OR NEW.suite_kind IS NOT OLD.suite_kind
          OR NEW.created_by IS NOT OLD.created_by
          OR NEW.created_at IS NOT OLD.created_at
          OR NEW.row_version != OLD.row_version + 1
          OR (OLD.state='draft' AND NEW.state NOT IN ('draft','active','archived'))
          OR (OLD.state='active' AND NEW.state NOT IN ('active','archived'))
          OR OLD.state='archived'
        BEGIN
          SELECT RAISE(ABORT, 'invalid Agent Eval suite update');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_suite_revision_scope",
        """
        CREATE TRIGGER trg_agent_eval_suite_revision_scope
        BEFORE INSERT ON agent_eval_suite_revision
        WHEN NOT EXISTS (
          SELECT 1
          FROM agent_eval_suite suite
          JOIN users actor ON actor.id=NEW.created_by AND actor.status='active'
          JOIN team_members member
            ON member.team_id=suite.team_id AND member.user_id=actor.id
           AND member.status='active'
          LEFT JOIN team_subagent agent
            ON agent.team_id=suite.team_id AND agent.id=suite.agent_id
          JOIN teams team ON team.id=suite.team_id AND team.status='active'
          WHERE suite.id=NEW.suite_id AND suite.state!='archived'
            AND (
              (
                suite.suite_kind='owner_quality'
                AND suite.agent_source_kind='team'
                AND COALESCE(agent.owner_user_id,agent.created_by)=actor.id
              ) OR (
                suite.suite_kind IN ('platform_contract','category_benchmark')
                AND (member.role='team_admin' OR team.created_by_user_id=actor.id)
              )
            )
        ) OR NEW.sequence != COALESCE(
          (SELECT MAX(sequence)+1 FROM agent_eval_suite_revision WHERE suite_id=NEW.suite_id),
          1
        )
        BEGIN
          SELECT RAISE(ABORT, 'Agent Eval suite revision scope mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_suite_revision_immutable_update",
        """
        CREATE TRIGGER trg_agent_eval_suite_revision_immutable_update
        BEFORE UPDATE ON agent_eval_suite_revision
        BEGIN
          SELECT RAISE(ABORT, 'agent_eval_suite_revision is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_suite_revision_immutable_delete",
        """
        CREATE TRIGGER trg_agent_eval_suite_revision_immutable_delete
        BEFORE DELETE ON agent_eval_suite_revision
        BEGIN
          SELECT RAISE(ABORT, 'agent_eval_suite_revision is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_case_capacity",
        """
        CREATE TRIGGER trg_agent_eval_case_capacity
        BEFORE INSERT ON agent_eval_case_revision
        WHEN NOT EXISTS (
          SELECT 1 FROM agent_eval_suite_revision revision
          WHERE revision.id=NEW.suite_revision_id
            AND (
              SELECT COUNT(*) FROM agent_eval_case_revision existing
              WHERE existing.suite_revision_id=revision.id
            ) < revision.case_count
        )
        BEGIN
          SELECT RAISE(ABORT, 'Agent Eval suite revision case capacity exceeded');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_case_immutable_update",
        """
        CREATE TRIGGER trg_agent_eval_case_immutable_update
        BEFORE UPDATE ON agent_eval_case_revision
        BEGIN
          SELECT RAISE(ABORT, 'agent_eval_case_revision is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_case_immutable_delete",
        """
        CREATE TRIGGER trg_agent_eval_case_immutable_delete
        BEFORE DELETE ON agent_eval_case_revision
        BEGIN
          SELECT RAISE(ABORT, 'agent_eval_case_revision is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_run_scope",
        """
        CREATE TRIGGER trg_agent_eval_run_scope
        BEFORE INSERT ON agent_eval_run
        WHEN NOT EXISTS (
          SELECT 1
          FROM agent_eval_suite_revision revision
          JOIN agent_eval_suite suite ON suite.id=revision.suite_id
          JOIN users actor ON actor.id=NEW.created_by AND actor.status='active'
          JOIN team_members member
            ON member.team_id=NEW.team_id AND member.user_id=actor.id
           AND member.status='active'
          JOIN teams team ON team.id=NEW.team_id AND team.status='active'
          LEFT JOIN team_subagent agent
            ON agent.team_id=NEW.team_id AND agent.id=NEW.agent_id
          WHERE revision.id=NEW.suite_revision_id
            AND suite.team_id=NEW.team_id
            AND suite.agent_id=NEW.agent_id
            AND suite.agent_source_kind=NEW.agent_source_kind
            AND suite.state='active'
            AND revision.scorer_bundle_ref=NEW.scorer_bundle_ref
            AND revision.case_set_hash=NEW.case_set_hash
            AND revision.case_count=(
              SELECT COUNT(*) FROM agent_eval_case_revision case_revision
              WHERE case_revision.suite_revision_id=revision.id
            )
            AND (
              (
                NEW.agent_source_kind='team'
                AND (
                  COALESCE(agent.owner_user_id,agent.created_by)=actor.id
                  OR member.role='team_admin' OR team.created_by_user_id=actor.id
                )
              ) OR (
                NEW.agent_source_kind='system'
                AND (member.role='team_admin' OR team.created_by_user_id=actor.id)
              )
            )
        ) OR (
          NEW.agent_source_kind='team' AND (
            NEW.release_id IS NULL OR NOT EXISTS (
              SELECT 1 FROM team_subagent_release release
              WHERE release.team_id=NEW.team_id
                AND release.subagent_id=NEW.agent_id
                AND release.id=NEW.release_id
                AND release.revision_id=NEW.revision_ref
                AND release.capability_fingerprint=NEW.capability_fingerprint
                AND release.dependency_fingerprint=NEW.dependency_fingerprint
            )
          )
        ) OR (
          NEW.agent_source_kind='system' AND (
            NEW.release_id IS NOT NULL OR NOT EXISTS (
              SELECT 1 FROM system_agent_effective_revision revision
              WHERE revision.team_id=NEW.team_id
                AND revision.agent_id=NEW.agent_id
                AND revision.id=NEW.revision_ref
            )
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'Agent Eval run scope mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_run_identity_immutable",
        """
        CREATE TRIGGER trg_agent_eval_run_identity_immutable
        BEFORE UPDATE ON agent_eval_run
        WHEN NEW.id IS NOT OLD.id
          OR NEW.team_id IS NOT OLD.team_id
          OR NEW.agent_id IS NOT OLD.agent_id
          OR NEW.agent_source_kind IS NOT OLD.agent_source_kind
          OR NEW.revision_ref IS NOT OLD.revision_ref
          OR NEW.release_id IS NOT OLD.release_id
          OR NEW.capability_fingerprint IS NOT OLD.capability_fingerprint
          OR NEW.dependency_fingerprint IS NOT OLD.dependency_fingerprint
          OR NEW.model_config_hash IS NOT OLD.model_config_hash
          OR NEW.subject_fingerprint IS NOT OLD.subject_fingerprint
          OR NEW.suite_revision_id IS NOT OLD.suite_revision_id
          OR NEW.case_set_hash IS NOT OLD.case_set_hash
          OR NEW.scorer_bundle_ref IS NOT OLD.scorer_bundle_ref
          OR NEW.judge_role_ref IS NOT OLD.judge_role_ref
          OR NEW.judge_model_fingerprint IS NOT OLD.judge_model_fingerprint
          OR NEW.trigger_kind IS NOT OLD.trigger_kind
          OR NEW.idempotency_key IS NOT OLD.idempotency_key
          OR NEW.created_by IS NOT OLD.created_by
          OR NEW.created_at IS NOT OLD.created_at
        BEGIN
          SELECT RAISE(ABORT, 'Agent Eval run identity is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_run_status_transition",
        """
        CREATE TRIGGER trg_agent_eval_run_status_transition
        BEFORE UPDATE ON agent_eval_run
        WHEN NOT (
          (
            NEW.status=OLD.status
            AND NEW.started_at IS OLD.started_at
            AND NEW.finished_at IS OLD.finished_at
            AND NEW.summary_json IS OLD.summary_json
          ) OR (
            OLD.status='queued' AND NEW.status='running'
            AND NEW.started_at IS NOT NULL
            AND NEW.finished_at IS NULL
            AND NEW.summary_json IS NULL
          ) OR (
            OLD.status='queued' AND NEW.status='cancelled'
            AND NEW.started_at IS OLD.started_at
            AND NEW.finished_at IS NOT NULL
            AND NEW.summary_json IS NOT NULL
          ) OR (
            OLD.status='running'
            AND NEW.status IN ('passed','failed','unknown','incomplete','cancelled')
            AND NEW.started_at IS OLD.started_at
            AND NEW.finished_at IS NOT NULL
            AND NEW.summary_json IS NOT NULL
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'invalid Agent Eval run transition');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_run_complete_results",
        """
        CREATE TRIGGER trg_agent_eval_run_complete_results
        BEFORE UPDATE ON agent_eval_run
        WHEN NEW.status IN ('passed','failed','unknown') AND (
          EXISTS (
            SELECT 1
            FROM agent_eval_case_revision case_revision
            WHERE case_revision.suite_revision_id=NEW.suite_revision_id
              AND NOT EXISTS (
                SELECT 1 FROM agent_eval_case_result result
                WHERE result.run_id=NEW.id
                  AND result.case_revision_id=case_revision.id
              )
          ) OR (
            NEW.status='passed' AND EXISTS (
              SELECT 1
              FROM agent_eval_case_revision case_revision
              WHERE case_revision.suite_revision_id=NEW.suite_revision_id
                AND NOT EXISTS (
                  SELECT 1 FROM agent_eval_case_result result
                  WHERE result.run_id=NEW.id
                    AND result.case_revision_id=case_revision.id
                    AND result.status='passed'
                )
            )
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'Agent Eval terminal run has missing case results');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_run_immutable_delete",
        """
        CREATE TRIGGER trg_agent_eval_run_immutable_delete
        BEFORE DELETE ON agent_eval_run
        BEGIN
          SELECT RAISE(ABORT, 'agent_eval_run is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_case_result_scope",
        """
        CREATE TRIGGER trg_agent_eval_case_result_scope
        BEFORE INSERT ON agent_eval_case_result
        WHEN NOT EXISTS (
          SELECT 1
          FROM agent_eval_run run
          JOIN agent_eval_case_revision case_revision
            ON case_revision.id=NEW.case_revision_id
          WHERE run.id=NEW.run_id
            AND run.status='running'
            AND case_revision.suite_revision_id=run.suite_revision_id
            AND (NEW.judge_json IS NULL OR run.judge_role_ref IS NOT NULL)
        )
        BEGIN
          SELECT RAISE(ABORT, 'Agent Eval case result scope mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_case_result_immutable_update",
        """
        CREATE TRIGGER trg_agent_eval_case_result_immutable_update
        BEFORE UPDATE ON agent_eval_case_result
        BEGIN
          SELECT RAISE(ABORT, 'agent_eval_case_result is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_case_result_immutable_delete",
        """
        CREATE TRIGGER trg_agent_eval_case_result_immutable_delete
        BEFORE DELETE ON agent_eval_case_result
        BEGIN
          SELECT RAISE(ABORT, 'agent_eval_case_result is immutable');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_trigger_policy_scope",
        """
        CREATE TRIGGER trg_agent_eval_trigger_policy_scope
        BEFORE INSERT ON agent_eval_trigger_policy
        WHEN NOT EXISTS (
          SELECT 1
          FROM users actor
          JOIN team_members member
            ON member.user_id=actor.id AND member.team_id=NEW.team_id
           AND member.status='active'
          JOIN teams team ON team.id=NEW.team_id AND team.status='active'
          LEFT JOIN team_subagent agent
            ON agent.team_id=NEW.team_id AND agent.id=NEW.agent_id
          WHERE actor.id=NEW.updated_by AND actor.status='active'
            AND (
              (
                NEW.agent_source_kind='team'
                AND agent.id IS NOT NULL
                AND (
                  COALESCE(agent.owner_user_id,agent.created_by)=actor.id
                  OR member.role='team_admin' OR team.created_by_user_id=actor.id
                )
              ) OR (
                NEW.agent_source_kind='system'
                AND (member.role='team_admin' OR team.created_by_user_id=actor.id)
                AND EXISTS (
                  SELECT 1 FROM system_agent_effective_revision revision
                  WHERE revision.team_id=NEW.team_id AND revision.agent_id=NEW.agent_id
                )
              )
            )
        )
        BEGIN
          SELECT RAISE(ABORT, 'Agent Eval trigger policy scope mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_trigger_policy_update",
        """
        CREATE TRIGGER trg_agent_eval_trigger_policy_update
        BEFORE UPDATE ON agent_eval_trigger_policy
        WHEN NEW.team_id IS NOT OLD.team_id
          OR NEW.agent_source_kind IS NOT OLD.agent_source_kind
          OR NEW.agent_id IS NOT OLD.agent_id
          OR NEW.row_version != OLD.row_version + 1
        BEGIN
          SELECT RAISE(ABORT, 'invalid Agent Eval trigger policy update');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_trigger_policy_scope_update",
        """
        CREATE TRIGGER trg_agent_eval_trigger_policy_scope_update
        BEFORE UPDATE ON agent_eval_trigger_policy
        WHEN NOT EXISTS (
          SELECT 1
          FROM users actor
          JOIN team_members member
            ON member.user_id=actor.id AND member.team_id=NEW.team_id
           AND member.status='active'
          JOIN teams team ON team.id=NEW.team_id AND team.status='active'
          LEFT JOIN team_subagent agent
            ON agent.team_id=NEW.team_id AND agent.id=NEW.agent_id
          WHERE actor.id=NEW.updated_by AND actor.status='active'
            AND (
              (
                NEW.agent_source_kind='team'
                AND agent.id IS NOT NULL
                AND (
                  COALESCE(agent.owner_user_id,agent.created_by)=actor.id
                  OR member.role='team_admin' OR team.created_by_user_id=actor.id
                )
              ) OR (
                NEW.agent_source_kind='system'
                AND (member.role='team_admin' OR team.created_by_user_id=actor.id)
                AND EXISTS (
                  SELECT 1 FROM system_agent_effective_revision revision
                  WHERE revision.team_id=NEW.team_id AND revision.agent_id=NEW.agent_id
                )
              )
            )
        )
        BEGIN
          SELECT RAISE(ABORT, 'Agent Eval trigger policy scope mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_evidence_scope_insert",
        """
        CREATE TRIGGER trg_agent_eval_evidence_scope_insert
        BEFORE INSERT ON agent_eval_evidence_state
        WHEN NOT EXISTS (
          SELECT 1 FROM agent_eval_suite suite
          WHERE suite.id=NEW.suite_id
            AND suite.team_id=NEW.team_id
            AND suite.agent_id=NEW.agent_id
            AND suite.agent_source_kind=NEW.agent_source_kind
        ) OR (
          NEW.current_run_id IS NOT NULL AND NOT EXISTS (
            SELECT 1
            FROM agent_eval_run run
            JOIN agent_eval_suite_revision revision
              ON revision.id=run.suite_revision_id
            WHERE run.id=NEW.current_run_id
              AND run.team_id=NEW.team_id
              AND run.agent_id=NEW.agent_id
              AND run.agent_source_kind=NEW.agent_source_kind
              AND revision.suite_id=NEW.suite_id
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'Agent Eval evidence scope mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_evidence_update",
        """
        CREATE TRIGGER trg_agent_eval_evidence_update
        BEFORE UPDATE ON agent_eval_evidence_state
        WHEN NEW.team_id IS NOT OLD.team_id
          OR NEW.agent_source_kind IS NOT OLD.agent_source_kind
          OR NEW.agent_id IS NOT OLD.agent_id
          OR NEW.suite_id IS NOT OLD.suite_id
          OR NEW.row_version != OLD.row_version + 1
          OR (
            NEW.current_run_id IS NOT NULL AND NOT EXISTS (
              SELECT 1
              FROM agent_eval_run run
              JOIN agent_eval_suite_revision revision
                ON revision.id=run.suite_revision_id
              WHERE run.id=NEW.current_run_id
                AND run.team_id=NEW.team_id
                AND run.agent_id=NEW.agent_id
                AND run.agent_source_kind=NEW.agent_source_kind
                AND revision.suite_id=NEW.suite_id
            )
          )
        BEGIN
          SELECT RAISE(ABORT, 'invalid Agent Eval evidence update');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_evidence_state_insert",
        """
        CREATE TRIGGER trg_agent_eval_evidence_state_insert
        BEFORE INSERT ON agent_eval_evidence_state
        WHEN (
          NEW.state='running' AND NOT EXISTS (
            SELECT 1 FROM agent_eval_run run
            WHERE run.id=NEW.current_run_id
              AND run.status IN ('queued','running')
              AND run.subject_fingerprint=NEW.subject_fingerprint
          )
        ) OR (
          NEW.state='valid' AND NOT EXISTS (
            SELECT 1 FROM agent_eval_run run
            WHERE run.id=NEW.current_run_id AND run.status='passed'
              AND run.subject_fingerprint=NEW.subject_fingerprint
          )
        ) OR (
          NEW.state='failed' AND NOT EXISTS (
            SELECT 1 FROM agent_eval_run run
            WHERE run.id=NEW.current_run_id AND run.status='failed'
              AND run.subject_fingerprint=NEW.subject_fingerprint
          )
        ) OR (
          NEW.state='unknown' AND NOT EXISTS (
            SELECT 1 FROM agent_eval_run run
            WHERE run.id=NEW.current_run_id
              AND run.status IN ('unknown','incomplete','cancelled')
              AND run.subject_fingerprint=NEW.subject_fingerprint
          )
        ) OR (
          NEW.state='blocked' AND NOT EXISTS (
            SELECT 1 FROM agent_eval_run run
            WHERE run.id=NEW.current_run_id AND run.status='failed'
              AND run.subject_fingerprint=NEW.subject_fingerprint
              AND json_extract(run.summary_json,'$.critical_failure')=1
          )
        ) OR (
          NEW.state='stale' AND NOT EXISTS (
            SELECT 1 FROM agent_eval_run run
            WHERE run.id=NEW.current_run_id
              AND run.status IN ('passed','failed','unknown','incomplete','cancelled')
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'Agent Eval evidence run state mismatch');
        END
        """,
    ),
    (
        "trigger",
        "trg_agent_eval_evidence_state_update",
        """
        CREATE TRIGGER trg_agent_eval_evidence_state_update
        BEFORE UPDATE ON agent_eval_evidence_state
        WHEN (
          NEW.state='running' AND NOT EXISTS (
            SELECT 1 FROM agent_eval_run run
            WHERE run.id=NEW.current_run_id
              AND run.status IN ('queued','running')
              AND run.subject_fingerprint=NEW.subject_fingerprint
          )
        ) OR (
          NEW.state='valid' AND NOT EXISTS (
            SELECT 1 FROM agent_eval_run run
            WHERE run.id=NEW.current_run_id AND run.status='passed'
              AND run.subject_fingerprint=NEW.subject_fingerprint
          )
        ) OR (
          NEW.state='failed' AND NOT EXISTS (
            SELECT 1 FROM agent_eval_run run
            WHERE run.id=NEW.current_run_id AND run.status='failed'
              AND run.subject_fingerprint=NEW.subject_fingerprint
          )
        ) OR (
          NEW.state='unknown' AND NOT EXISTS (
            SELECT 1 FROM agent_eval_run run
            WHERE run.id=NEW.current_run_id
              AND run.status IN ('unknown','incomplete','cancelled')
              AND run.subject_fingerprint=NEW.subject_fingerprint
          )
        ) OR (
          NEW.state='blocked' AND NOT EXISTS (
            SELECT 1 FROM agent_eval_run run
            WHERE run.id=NEW.current_run_id AND run.status='failed'
              AND run.subject_fingerprint=NEW.subject_fingerprint
              AND json_extract(run.summary_json,'$.critical_failure')=1
          )
        ) OR (
          NEW.state='stale' AND NOT EXISTS (
            SELECT 1 FROM agent_eval_run run
            WHERE run.id=NEW.current_run_id
              AND run.status IN ('passed','failed','unknown','incomplete','cancelled')
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'Agent Eval evidence run state mismatch');
        END
        """,
    ),
)


def _canonical_sql(value: str) -> str:
    return "".join(value.lower().replace("if not exists", "").split())


def _object_sql(connection: Any, *, object_type: str, name: str) -> str | None:
    row = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_schema WHERE type=? AND name=?",
        (object_type, name),
    ).fetchone()
    return str(row[0]) if row is not None and row[0] is not None else None


def _ensure_object(
    connection: Any,
    *,
    object_type: str,
    name: str,
    ddl: str,
) -> None:
    existing = _object_sql(connection, object_type=object_type, name=name)
    if existing is None:
        connection.exec_driver_sql(ddl)
        existing = _object_sql(connection, object_type=object_type, name=name)
    if existing is None or _canonical_sql(existing) != _canonical_sql(ddl):
        raise RuntimeError(f"0037 found incompatible {object_type} semantics: {name}")


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    for object_type, name, ddl in _OBJECTS:
        _ensure_object(
            connection,
            object_type=object_type,
            name=name,
            ddl=ddl,
        )


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
