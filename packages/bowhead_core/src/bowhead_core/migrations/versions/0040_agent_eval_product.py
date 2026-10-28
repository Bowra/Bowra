"""Agent Eval product surface, candidate runs, and regression outbox.

M3-11 makes an immutable release candidate an executable Eval subject, adds a
durable change-event outbox, and marks old evidence stale in the same
transaction as a live release or model configuration change.  The outbox does
not run work by itself; the feature-gated Gateway worker is the only consumer.

Revision ID: 0040_agent_eval_product
Revises: 0039_resource_dependency_projection
Create Date: 2026-09-05
"""

from __future__ import annotations

from typing import Any

from alembic import context

revision = "0040_agent_eval_product"
down_revision = "0039_resource_dependency_projection"
branch_labels = None
depends_on = None


_OUTBOX_TABLE = """
CREATE TABLE agent_eval_regression_outbox (
  id                 TEXT PRIMARY KEY CHECK(id LIKE 'aeo_%'),
  team_id            TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
  agent_source_kind  TEXT NOT NULL CHECK(agent_source_kind IN ('team','system')),
  agent_id           TEXT NOT NULL,
  change_kind        TEXT NOT NULL CHECK(change_kind IN (
                       'definition_change','dependency_change','model_change'
                     )),
  change_ref         TEXT NOT NULL CHECK(length(change_ref) BETWEEN 1 AND 240),
  status             TEXT NOT NULL CHECK(status IN ('pending','leased','delivered','skipped')),
  lease_generation   INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation >= 0),
  lease_owner        TEXT,
  lease_token_hash   TEXT CHECK(lease_token_hash IS NULL OR lease_token_hash LIKE 'sha256:%'),
  lease_expires_at   TEXT,
  attempt_count      INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
  outcome_code       TEXT,
  available_at       TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  delivered_at       TEXT,
  UNIQUE(team_id, agent_source_kind, agent_id, change_kind, change_ref),
  CHECK(
    (status='pending' AND lease_owner IS NULL AND lease_token_hash IS NULL
      AND lease_expires_at IS NULL AND delivered_at IS NULL)
    OR (status='leased' AND lease_generation >= 1 AND lease_owner IS NOT NULL
      AND lease_token_hash IS NOT NULL AND lease_expires_at IS NOT NULL
      AND delivered_at IS NULL)
    OR (status IN ('delivered','skipped') AND delivered_at IS NOT NULL)
  )
)
"""

_NOTIFICATION_OUTBOX_TABLE = """
CREATE TABLE agent_eval_notification_outbox (
  id                 TEXT PRIMARY KEY CHECK(id LIKE 'aen_%'),
  team_id            TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
  agent_id           TEXT NOT NULL,
  run_id             TEXT NOT NULL REFERENCES agent_eval_run(id) ON DELETE RESTRICT,
  recipient_user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  status             TEXT NOT NULL CHECK(status IN ('pending','leased','delivered','failed')),
  lease_generation   INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation >= 0),
  lease_owner        TEXT,
  lease_token_hash   TEXT CHECK(lease_token_hash IS NULL OR lease_token_hash LIKE 'sha256:%'),
  lease_expires_at   TEXT,
  attempt_count      INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
  outcome_code       TEXT,
  available_at       TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  settled_at         TEXT,
  UNIQUE(run_id, recipient_user_id),
  CHECK(
    (status='pending' AND lease_owner IS NULL AND lease_token_hash IS NULL
      AND lease_expires_at IS NULL AND settled_at IS NULL)
    OR (status='leased' AND lease_generation >= 1 AND lease_owner IS NOT NULL
      AND lease_token_hash IS NOT NULL AND lease_expires_at IS NOT NULL
      AND settled_at IS NULL)
    OR (status IN ('delivered','failed') AND settled_at IS NOT NULL)
  )
)
"""

_INDEXES: tuple[tuple[str, str], ...] = (
    (
        "idx_agent_eval_regression_claim",
        """
        CREATE INDEX idx_agent_eval_regression_claim
        ON agent_eval_regression_outbox(status, available_at, lease_expires_at, created_at, id)
        """,
    ),
    (
        "idx_agent_eval_run_candidate",
        """
        CREATE INDEX idx_agent_eval_run_candidate
        ON agent_eval_run(team_id, candidate_id, suite_revision_id, created_at, id)
        """,
    ),
    (
        "idx_agent_eval_notification_claim",
        """
        CREATE INDEX idx_agent_eval_notification_claim
        ON agent_eval_notification_outbox(
          status, available_at, lease_expires_at, created_at, id
        )
        """,
    ),
    (
        "uq_agent_eval_platform_contract",
        """
        CREATE UNIQUE INDEX uq_agent_eval_platform_contract
        ON agent_eval_suite(team_id, agent_source_kind, agent_id)
        WHERE suite_kind='platform_contract' AND platform_managed=1
        """,
    ),
)

_TRIGGERS: tuple[tuple[str, str], ...] = (
    (
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
            NEW.platform_managed!=0 OR NEW.agent_source_kind!='team' OR NOT EXISTS (
              SELECT 1 FROM team_subagent agent
              WHERE agent.team_id=NEW.team_id AND agent.id=NEW.agent_id
                AND COALESCE(agent.owner_user_id,agent.created_by)=NEW.created_by
            )
          )
        ) OR (
          NEW.suite_kind IN ('platform_contract','category_benchmark') AND NOT (
            EXISTS (
              SELECT 1
              FROM users actor
              JOIN team_members member
                ON member.user_id=actor.id AND member.team_id=NEW.team_id
               AND member.status='active'
              JOIN teams team ON team.id=NEW.team_id
              WHERE actor.id=NEW.created_by AND actor.status='active'
                AND (member.role='team_admin' OR team.created_by_user_id=actor.id)
            ) OR (
              NEW.platform_managed=1 AND NEW.suite_kind='platform_contract'
              AND NEW.agent_source_kind='team' AND EXISTS (
                SELECT 1 FROM team_subagent agent
                WHERE agent.team_id=NEW.team_id AND agent.id=NEW.agent_id
                  AND COALESCE(agent.owner_user_id,agent.created_by)=NEW.created_by
              )
            )
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'Agent Eval suite scope mismatch');
        END
        """,
    ),
    (
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
              ) OR (
                suite.platform_managed=1 AND suite.suite_kind='platform_contract'
                AND suite.agent_source_kind='team'
                AND COALESCE(agent.owner_user_id,agent.created_by)=actor.id
              )
            )
        ) OR NEW.sequence != COALESCE(
          (SELECT MAX(sequence)+1 FROM agent_eval_suite_revision WHERE suite_id=NEW.suite_id),
          1
        ) OR EXISTS (
          SELECT 1 FROM agent_eval_suite suite
          WHERE suite.id=NEW.suite_id AND suite.platform_managed=1
            AND EXISTS (
              SELECT 1 FROM agent_eval_suite_revision existing
              WHERE existing.suite_id=suite.id
            )
        )
        BEGIN
          SELECT RAISE(ABORT, 'Agent Eval suite revision scope mismatch');
        END
        """,
    ),
    (
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
          NEW.agent_source_kind='team' AND NOT (
            (
              NEW.release_id IS NOT NULL AND NEW.candidate_id IS NULL AND EXISTS (
                SELECT 1 FROM team_subagent_release release
                WHERE release.team_id=NEW.team_id
                  AND release.subagent_id=NEW.agent_id
                  AND release.id=NEW.release_id
                  AND release.revision_id=NEW.revision_ref
                  AND release.capability_fingerprint=NEW.capability_fingerprint
                  AND release.dependency_fingerprint=NEW.dependency_fingerprint
              )
            ) OR (
              NEW.release_id IS NULL AND NEW.candidate_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM team_subagent_release_candidate candidate
                WHERE candidate.team_id=NEW.team_id
                  AND candidate.subagent_id=NEW.agent_id
                  AND candidate.id=NEW.candidate_id
                  AND candidate.state IN ('pending','approved')
                  AND candidate.revision_id=NEW.revision_ref
                  AND candidate.capability_fingerprint=NEW.capability_fingerprint
                  AND candidate.dependency_fingerprint=NEW.dependency_fingerprint
              )
            )
          )
        ) OR (
          NEW.agent_source_kind='system' AND (
            NEW.release_id IS NOT NULL OR NEW.candidate_id IS NOT NULL OR NOT EXISTS (
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
          OR NEW.candidate_id IS NOT OLD.candidate_id
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
        "trg_turn_agent_execution_revision",
        """
        CREATE TRIGGER trg_turn_agent_execution_revision
        BEFORE INSERT ON turn_agent_execution
        WHEN (
          NEW.agent_source_kind='team'
          AND NOT (
            (
              NEW.release_id IS NOT NULL AND EXISTS (
                SELECT 1
                FROM team_subagent_release release
                WHERE release.team_id=NEW.team_id
                  AND release.subagent_id=NEW.agent_id
                  AND release.id=NEW.release_id
                  AND release.revision_id=NEW.revision_ref
                  AND release.capability_fingerprint=NEW.capability_fingerprint
              )
            ) OR (
              NEW.release_id IS NULL AND EXISTS (
                SELECT 1
                FROM agent_eval_scratch_turn scratch
                JOIN agent_eval_run run ON run.id=scratch.run_id
                JOIN team_subagent_release_candidate candidate
                  ON candidate.id=run.candidate_id
                 AND candidate.team_id=run.team_id
                 AND candidate.subagent_id=run.agent_id
                WHERE scratch.turn_id=NEW.turn_id
                  AND run.status='running'
                  AND run.team_id=NEW.team_id
                  AND run.agent_id=NEW.agent_id
                  AND run.revision_ref=NEW.revision_ref
                  AND run.capability_fingerprint=NEW.capability_fingerprint
                  AND candidate.state IN ('pending','approved')
              )
            )
          )
        ) OR (
          NEW.agent_source_kind='system'
          AND (
            NEW.release_id IS NOT NULL
            OR NOT EXISTS (
              SELECT 1
              FROM system_agent_effective_revision revision
              WHERE revision.team_id=NEW.team_id
                AND revision.agent_id=NEW.agent_id
                AND revision.id=NEW.revision_ref
            )
          )
        )
        BEGIN
          SELECT RAISE(ABORT, 'turn Agent execution revision mismatch');
        END
        """,
    ),
    (
        "trg_agent_eval_regression_update",
        """
        CREATE TRIGGER trg_agent_eval_regression_update
        BEFORE UPDATE ON agent_eval_regression_outbox
        WHEN NEW.id IS NOT OLD.id
          OR NEW.team_id IS NOT OLD.team_id
          OR NEW.agent_source_kind IS NOT OLD.agent_source_kind
          OR NEW.agent_id IS NOT OLD.agent_id
          OR NEW.change_kind IS NOT OLD.change_kind
          OR NEW.change_ref IS NOT OLD.change_ref
          OR (
            NEW.available_at IS NOT OLD.available_at
            AND NOT (OLD.status='leased' AND NEW.status='pending')
          )
          OR NEW.created_at IS NOT OLD.created_at
          OR NEW.lease_generation < OLD.lease_generation
          OR NEW.attempt_count < OLD.attempt_count
          OR OLD.status IN ('delivered','skipped')
          OR NEW.status NOT IN ('leased','delivered','skipped','pending')
        BEGIN
          SELECT RAISE(ABORT, 'invalid Agent Eval regression outbox update');
        END
        """,
    ),
    (
        "trg_agent_eval_regression_delete",
        """
        CREATE TRIGGER trg_agent_eval_regression_delete
        BEFORE DELETE ON agent_eval_regression_outbox
        BEGIN
          SELECT RAISE(ABORT, 'agent_eval_regression_outbox is immutable');
        END
        """,
    ),
    (
        "trg_agent_eval_live_release_change",
        """
        CREATE TRIGGER trg_agent_eval_live_release_change
        AFTER UPDATE OF live_release_id ON team_subagent
        WHEN NEW.live_release_id IS NOT OLD.live_release_id AND NEW.live_release_id IS NOT NULL
        BEGIN
          UPDATE agent_eval_evidence_state
          SET state='stale',stale_reason=(
                CASE
                  WHEN EXISTS (
                    SELECT 1 FROM team_subagent_release previous_release
                    JOIN team_subagent_release next_release
                      ON next_release.id=NEW.live_release_id
                    WHERE previous_release.id=OLD.live_release_id
                      AND previous_release.dependency_fingerprint
                          IS NOT next_release.dependency_fingerprint
                  ) THEN 'dependency_change'
                  ELSE 'definition_change'
                END
              ),row_version=row_version+1,updated_at=NEW.updated_at
          WHERE team_id=NEW.team_id AND agent_source_kind='team' AND agent_id=NEW.id
            AND current_run_id IS NOT NULL
            AND NOT EXISTS (
              SELECT 1 FROM agent_eval_run run
              JOIN team_subagent_release next_release ON next_release.id=NEW.live_release_id
              WHERE run.id=agent_eval_evidence_state.current_run_id
                AND run.status IN ('passed','failed')
                AND json_extract(run.summary_json,'$.critical_failure')=0
                AND run.revision_ref=next_release.revision_id
                AND run.capability_fingerprint=next_release.capability_fingerprint
                AND run.dependency_fingerprint=next_release.dependency_fingerprint
                AND (run.release_id=next_release.id OR run.candidate_id=next_release.source_candidate_id)
            );

          INSERT OR IGNORE INTO agent_eval_regression_outbox(
            id,team_id,agent_source_kind,agent_id,change_kind,change_ref,status,
            lease_generation,attempt_count,available_at,created_at
          )
          SELECT
            'aeo_' || lower(hex(randomblob(16))),NEW.team_id,'team',NEW.id,
            CASE
              WHEN EXISTS (
                SELECT 1 FROM team_subagent_release previous_release
                JOIN team_subagent_release next_release ON next_release.id=NEW.live_release_id
                WHERE previous_release.id=OLD.live_release_id
                  AND previous_release.dependency_fingerprint
                      IS NOT next_release.dependency_fingerprint
              ) THEN 'dependency_change'
              ELSE 'definition_change'
            END,
            NEW.live_release_id,'pending',0,0,NEW.updated_at,NEW.updated_at
          FROM agent_eval_trigger_policy policy
          WHERE policy.team_id=NEW.team_id
            AND policy.agent_source_kind='team' AND policy.agent_id=NEW.id
            AND policy.enabled=1
            AND (
              (policy.on_dependency_change=1 AND EXISTS (
                SELECT 1 FROM team_subagent_release previous_release
                JOIN team_subagent_release next_release ON next_release.id=NEW.live_release_id
                WHERE previous_release.id=OLD.live_release_id
                  AND previous_release.dependency_fingerprint
                      IS NOT next_release.dependency_fingerprint
              ))
              OR (policy.on_definition_change=1 AND NOT EXISTS (
                SELECT 1 FROM team_subagent_release previous_release
                JOIN team_subagent_release next_release ON next_release.id=NEW.live_release_id
                WHERE previous_release.id=OLD.live_release_id
                  AND previous_release.dependency_fingerprint
                      IS NOT next_release.dependency_fingerprint
              ))
            )
            AND NOT EXISTS (
              SELECT 1 FROM agent_eval_evidence_state evidence
              JOIN agent_eval_run run ON run.id=evidence.current_run_id
              JOIN team_subagent_release next_release ON next_release.id=NEW.live_release_id
              WHERE evidence.team_id=NEW.team_id AND evidence.agent_source_kind='team'
                AND evidence.agent_id=NEW.id AND evidence.state IN ('valid','failed')
                AND run.status IN ('passed','failed')
                AND json_extract(run.summary_json,'$.critical_failure')=0
                AND run.revision_ref=next_release.revision_id
                AND run.capability_fingerprint=next_release.capability_fingerprint
                AND run.dependency_fingerprint=next_release.dependency_fingerprint
                AND (run.release_id=next_release.id OR run.candidate_id=next_release.source_candidate_id)
            );
        END
        """,
    ),
    (
        "trg_agent_eval_team_model_change",
        """
        CREATE TRIGGER trg_agent_eval_team_model_change
        AFTER UPDATE OF llm_config_version ON teams
        WHEN NEW.llm_config_version IS NOT OLD.llm_config_version
        BEGIN
          UPDATE agent_eval_evidence_state
          SET state='stale',stale_reason='model_change',row_version=row_version+1,
              updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
          WHERE team_id=NEW.id AND current_run_id IS NOT NULL;

          INSERT OR IGNORE INTO agent_eval_regression_outbox(
            id,team_id,agent_source_kind,agent_id,change_kind,change_ref,status,
            lease_generation,attempt_count,available_at,created_at
          )
          SELECT
            'aeo_' || lower(hex(randomblob(16))),policy.team_id,
            policy.agent_source_kind,policy.agent_id,'model_change',
            'model:' || NEW.llm_config_version,'pending',0,0,
            strftime('%Y-%m-%dT%H:%M:%fZ','now'),
            strftime('%Y-%m-%dT%H:%M:%fZ','now')
          FROM agent_eval_trigger_policy policy
          WHERE policy.team_id=NEW.id AND policy.enabled=1 AND policy.on_model_change=1;
        END
        """,
    ),
    (
        "trg_agent_eval_platform_suite_immutable",
        """
        CREATE TRIGGER trg_agent_eval_platform_suite_immutable
        BEFORE UPDATE ON agent_eval_suite
        WHEN OLD.platform_managed=1 AND (
          NEW.platform_managed IS NOT OLD.platform_managed
          OR NEW.name IS NOT OLD.name OR NEW.visibility IS NOT OLD.visibility
          OR NEW.state IS NOT OLD.state
        )
        BEGIN
          SELECT RAISE(ABORT, 'platform Agent Eval suite is immutable');
        END
        """,
    ),
    (
        "trg_agent_eval_failure_notification",
        """
        CREATE TRIGGER trg_agent_eval_failure_notification
        AFTER UPDATE OF status ON agent_eval_run
        WHEN NEW.status IS NOT OLD.status
          AND NEW.agent_source_kind='team'
          AND NEW.trigger_kind!='manual'
          AND NEW.status IN ('failed','unknown','incomplete','cancelled')
        BEGIN
          INSERT OR IGNORE INTO agent_eval_notification_outbox(
            id,team_id,agent_id,run_id,recipient_user_id,status,
            lease_generation,attempt_count,available_at,created_at
          )
          SELECT
            'aen_' || lower(hex(randomblob(16))),NEW.team_id,NEW.agent_id,NEW.id,
            COALESCE(agent.owner_user_id,agent.created_by),'pending',0,0,
            NEW.finished_at,NEW.finished_at
          FROM team_subagent agent
          JOIN users owner
            ON owner.id=COALESCE(agent.owner_user_id,agent.created_by)
           AND owner.status='active'
          JOIN team_members membership
            ON membership.team_id=NEW.team_id AND membership.user_id=owner.id
           AND membership.status='active'
          WHERE agent.team_id=NEW.team_id AND agent.id=NEW.agent_id;
        END
        """,
    ),
    (
        "trg_agent_eval_notification_update",
        """
        CREATE TRIGGER trg_agent_eval_notification_update
        BEFORE UPDATE ON agent_eval_notification_outbox
        WHEN NEW.id IS NOT OLD.id
          OR NEW.team_id IS NOT OLD.team_id
          OR NEW.agent_id IS NOT OLD.agent_id
          OR NEW.run_id IS NOT OLD.run_id
          OR NEW.recipient_user_id IS NOT OLD.recipient_user_id
          OR (
            NEW.available_at IS NOT OLD.available_at
            AND NOT (OLD.status='leased' AND NEW.status='pending')
          )
          OR NEW.created_at IS NOT OLD.created_at
          OR NEW.lease_generation < OLD.lease_generation
          OR NEW.attempt_count < OLD.attempt_count
          OR OLD.status IN ('delivered','failed')
          OR NEW.status NOT IN ('leased','delivered','failed','pending')
        BEGIN
          SELECT RAISE(ABORT, 'invalid Agent Eval notification outbox update');
        END
        """,
    ),
    (
        "trg_agent_eval_notification_delete",
        """
        CREATE TRIGGER trg_agent_eval_notification_delete
        BEFORE DELETE ON agent_eval_notification_outbox
        BEGIN
          SELECT RAISE(ABORT, 'agent_eval_notification_outbox is immutable');
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


def _ensure_object(connection: Any, *, object_type: str, name: str, ddl: str) -> None:
    existing = _object_sql(connection, object_type=object_type, name=name)
    if existing is None:
        connection.exec_driver_sql(ddl)
        existing = _object_sql(connection, object_type=object_type, name=name)
    if existing is None or _canonical_sql(existing) != _canonical_sql(ddl):
        raise RuntimeError(f"0039 found incompatible {object_type} semantics: {name}")


def _ensure_column(
    connection: Any,
    *,
    table: str,
    column: str,
    ddl: str,
    expected: tuple[str, int, str | None, int],
) -> None:
    columns = {
        str(row[1]): (str(row[2]).upper(), int(row[3]), row[4], int(row[5]))
        for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")
    }
    if column not in columns:
        connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        columns = {
            str(row[1]): (str(row[2]).upper(), int(row[3]), row[4], int(row[5]))
            for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")
        }
    if columns.get(column) != expected:
        raise RuntimeError(f"0039 found incompatible column semantics: {table}.{column}")


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    _ensure_column(
        connection,
        table="agent_eval_run",
        column="candidate_id",
        ddl=(
            "candidate_id TEXT REFERENCES team_subagent_release_candidate(id) "
            "ON DELETE RESTRICT"
        ),
        expected=("TEXT", 0, None, 0),
    )
    candidate_foreign_keys = tuple(
        connection.exec_driver_sql("PRAGMA foreign_key_list(agent_eval_run)")
    )
    if not any(
        str(row[2]) == "team_subagent_release_candidate"
        and str(row[3]) == "candidate_id"
        and str(row[4]) == "id"
        and str(row[6]).upper() == "RESTRICT"
        for row in candidate_foreign_keys
    ):
        raise RuntimeError("0039 found incompatible candidate_id foreign key semantics")
    _ensure_column(
        connection,
        table="agent_eval_suite",
        column="platform_managed",
        ddl="platform_managed INTEGER NOT NULL DEFAULT 0 CHECK(platform_managed IN (0,1))",
        expected=("INTEGER", 1, "0", 0),
    )
    _ensure_object(
        connection,
        object_type="table",
        name="agent_eval_regression_outbox",
        ddl=_OUTBOX_TABLE,
    )
    _ensure_object(
        connection,
        object_type="table",
        name="agent_eval_notification_outbox",
        ddl=_NOTIFICATION_OUTBOX_TABLE,
    )
    for name in (
        "trg_agent_eval_suite_scope",
        "trg_agent_eval_suite_revision_scope",
        "trg_agent_eval_run_scope",
        "trg_agent_eval_run_identity_immutable",
        "trg_turn_agent_execution_revision",
    ):
        connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}")
    for name, ddl in _INDEXES:
        _ensure_object(connection, object_type="index", name=name, ddl=ddl)
    for name, ddl in _TRIGGERS:
        _ensure_object(connection, object_type="trigger", name=name, ddl=ddl)


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
