"""Agent Owner version-evaluation persistence foundation.

This dark migration expands the release-candidate lifecycle with ``prepared``
and adds the immutable policy/plan/comparison/snapshot ledgers required by the
version release gate.  It does not register routes, run evaluations, or change
the existing publication writer.

Revision ID: 0073_agent_owner_eval_foundation
Revises: 0072_skill_baseline_composition
Create Date: 2026-09-14
"""

from __future__ import annotations

from typing import Any

from alembic import context

revision = "0073_agent_owner_eval_foundation"
down_revision = "0072_skill_baseline_composition"
branch_labels = None
depends_on = None


_CANDIDATE_COLUMNS = (
    "id",
    "team_id",
    "subagent_id",
    "revision_id",
    "base_live_release_id",
    "target_audience_json",
    "target_audience_hash",
    "capability_fingerprint",
    "dependency_fingerprint",
    "risk_compiler_version",
    "risk_json",
    "safety_report_id",
    "eval_evidence_json",
    "release_note",
    "candidate_hash",
    "state",
    "row_version",
    "submitted_by",
    "submitted_at",
    "decided_by",
    "decision_reason_code",
    "self_review",
    "decided_at",
    "activated_release_id",
    "updated_at",
)

_CANDIDATE_DDL = """
CREATE TABLE team_subagent_release_candidate_owner_eval_new (
  id                       TEXT PRIMARY KEY CHECK(id LIKE 'arc_%'),
  team_id                  TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
  subagent_id              TEXT NOT NULL,
  revision_id              TEXT NOT NULL,
  base_live_release_id     TEXT,
  target_audience_json     TEXT NOT NULL CHECK(length(target_audience_json) > 0),
  target_audience_hash     TEXT NOT NULL CHECK(target_audience_hash LIKE 'sha256:%'),
  capability_fingerprint   TEXT NOT NULL CHECK(capability_fingerprint LIKE 'sha256:%'),
  dependency_fingerprint   TEXT NOT NULL CHECK(dependency_fingerprint LIKE 'sha256:%'),
  risk_compiler_version    TEXT NOT NULL,
  risk_json                TEXT NOT NULL CHECK(length(risk_json) > 0),
  safety_report_id         TEXT NOT NULL,
  eval_evidence_json       TEXT,
  release_note             TEXT,
  candidate_hash           TEXT NOT NULL CHECK(candidate_hash LIKE 'sha256:%'),
  state                    TEXT NOT NULL CHECK(state IN (
                             'prepared','pending','approved','rejected',
                             'withdrawn','activated','stale'
                           )),
  row_version              INTEGER NOT NULL DEFAULT 1 CHECK(row_version >= 1),
  submitted_by             TEXT NOT NULL,
  submitted_at             TEXT NOT NULL,
  decided_by               TEXT,
  decision_reason_code     TEXT,
  self_review              INTEGER CHECK(self_review IN (0,1)),
  decided_at               TEXT,
  activated_release_id     TEXT,
  updated_at               TEXT NOT NULL,
  UNIQUE(team_id, subagent_id, id),
  CHECK(state != 'activated' OR activated_release_id IS NOT NULL),
  CHECK(
    (decided_at IS NULL AND decided_by IS NULL AND self_review IS NULL)
    OR (decided_at IS NOT NULL AND decided_by IS NOT NULL AND self_review IS NOT NULL)
  ),
  FOREIGN KEY(team_id, subagent_id)
    REFERENCES team_subagent(team_id, id) ON DELETE RESTRICT,
  FOREIGN KEY(team_id, subagent_id, revision_id)
    REFERENCES team_subagent_revision(team_id, subagent_id, id) ON DELETE RESTRICT,
  FOREIGN KEY(team_id, subagent_id, base_live_release_id)
    REFERENCES team_subagent_release(team_id, subagent_id, id) ON DELETE RESTRICT,
  FOREIGN KEY(team_id, subagent_id, safety_report_id)
    REFERENCES agent_safety_report(team_id, agent_id, id) ON DELETE RESTRICT,
  FOREIGN KEY(team_id, subagent_id, activated_release_id)
    REFERENCES team_subagent_release(team_id, subagent_id, id) ON DELETE RESTRICT
)
"""

_CANDIDATE_INDEXES = (
    """
    CREATE UNIQUE INDEX uq_agent_one_pending_candidate
    ON team_subagent_release_candidate(team_id, subagent_id)
    WHERE state IN ('prepared','pending','approved')
    """,
    """
    CREATE INDEX idx_agent_candidate_inbox
    ON team_subagent_release_candidate(team_id, state, submitted_at DESC)
    """,
    """
    CREATE INDEX idx_agent_candidate_history
    ON team_subagent_release_candidate(team_id, subagent_id, submitted_at DESC)
    """,
)

_OLD_CANDIDATE_INDEXES = (
    """
    CREATE UNIQUE INDEX uq_agent_one_pending_candidate
    ON team_subagent_release_candidate(subagent_id)
    WHERE state='pending'
    """,
    _CANDIDATE_INDEXES[1],
    _CANDIDATE_INDEXES[2],
)

_CANDIDATE_TRIGGERS = (
    """
    CREATE TRIGGER trg_team_subagent_release_candidate_payload_immutable
    BEFORE UPDATE ON team_subagent_release_candidate
    WHEN NEW.id IS NOT OLD.id
      OR NEW.team_id IS NOT OLD.team_id
      OR NEW.subagent_id IS NOT OLD.subagent_id
      OR NEW.revision_id IS NOT OLD.revision_id
      OR NEW.base_live_release_id IS NOT OLD.base_live_release_id
      OR NEW.target_audience_json IS NOT OLD.target_audience_json
      OR NEW.target_audience_hash IS NOT OLD.target_audience_hash
      OR NEW.capability_fingerprint IS NOT OLD.capability_fingerprint
      OR NEW.dependency_fingerprint IS NOT OLD.dependency_fingerprint
      OR NEW.risk_compiler_version IS NOT OLD.risk_compiler_version
      OR NEW.risk_json IS NOT OLD.risk_json
      OR NEW.safety_report_id IS NOT OLD.safety_report_id
      OR NEW.eval_evidence_json IS NOT OLD.eval_evidence_json
      OR NEW.release_note IS NOT OLD.release_note
      OR NEW.candidate_hash IS NOT OLD.candidate_hash
      OR NEW.submitted_by IS NOT OLD.submitted_by
      OR NEW.submitted_at IS NOT OLD.submitted_at
    BEGIN
      SELECT RAISE(ABORT, 'team_subagent_release_candidate payload is immutable');
    END
    """,
    """
    CREATE TRIGGER trg_team_subagent_release_candidate_immutable_delete
    BEFORE DELETE ON team_subagent_release_candidate
    BEGIN
      SELECT RAISE(ABORT, 'team_subagent_release_candidate is immutable');
    END
    """,
)

_AGENT_EVAL_RUN_SCOPE = """
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
          AND candidate.state IN ('prepared','pending','approved')
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
"""

_TURN_AGENT_EXECUTION_REVISION = """
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
          AND candidate.state IN ('prepared','pending','approved')
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
"""


_TABLES = (
    """
    CREATE TABLE agent_revision_compatibility (
      revision_id          TEXT PRIMARY KEY REFERENCES team_subagent_revision(id) ON DELETE RESTRICT,
      team_id              TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
      agent_id             TEXT NOT NULL,
      base_revision_id     TEXT,
      compatibility_major  INTEGER NOT NULL CHECK(compatibility_major >= 1),
      verdict              TEXT NOT NULL CHECK(verdict IN ('compatible','breaking','unknown')),
      compiler_version     TEXT NOT NULL CHECK(length(compiler_version) BETWEEN 1 AND 120),
      contract_hash        TEXT NOT NULL CHECK(contract_hash LIKE 'sha256:%'),
      reason_codes_json    TEXT NOT NULL CHECK(
                             json_valid(reason_codes_json)
                             AND json_type(reason_codes_json)='array'
                             AND length(reason_codes_json) <= 65536
                           ),
      created_at           TEXT NOT NULL,
      UNIQUE(team_id, agent_id, revision_id),
      FOREIGN KEY(team_id, agent_id)
        REFERENCES team_subagent(team_id, id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id, agent_id, revision_id)
        REFERENCES team_subagent_revision(team_id, subagent_id, id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id, agent_id, base_revision_id)
        REFERENCES team_subagent_revision(team_id, subagent_id, id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE agent_eval_policy_revision (
      id             TEXT PRIMARY KEY CHECK(id LIKE 'aepr_%'),
      team_id        TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
      agent_id       TEXT NOT NULL,
      sequence       INTEGER NOT NULL CHECK(sequence >= 1),
      policy_json    TEXT NOT NULL CHECK(
                       json_valid(policy_json)
                       AND json_type(policy_json)='object'
                       AND length(policy_json) <= 262144
                     ),
      policy_hash    TEXT NOT NULL CHECK(policy_hash LIKE 'sha256:%'),
      created_by     TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
      created_at     TEXT NOT NULL,
      UNIQUE(team_id, agent_id, sequence),
      UNIQUE(team_id, agent_id, id),
      FOREIGN KEY(team_id, agent_id)
        REFERENCES team_subagent(team_id, id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE agent_eval_plan (
      id                              TEXT PRIMARY KEY CHECK(id LIKE 'aep_%'),
      team_id                         TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
      agent_id                        TEXT NOT NULL,
      candidate_id                    TEXT NOT NULL,
      sequence                        INTEGER NOT NULL CHECK(sequence >= 1),
      policy_revision_id              TEXT NOT NULL,
      baseline_release_id             TEXT,
      execution_workspace_id          TEXT NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
      execution_workspace_scope_hash  TEXT NOT NULL CHECK(execution_workspace_scope_hash LIKE 'sha256:%'),
      candidate_hash                  TEXT NOT NULL CHECK(candidate_hash LIKE 'sha256:%'),
      plan_json                       TEXT NOT NULL CHECK(
                                        json_valid(plan_json)
                                        AND json_type(plan_json)='object'
                                        AND length(plan_json) <= 262144
                                      ),
      plan_hash                       TEXT NOT NULL CHECK(plan_hash LIKE 'sha256:%'),
      compiler_version                TEXT NOT NULL CHECK(length(compiler_version) BETWEEN 1 AND 120),
      created_by                      TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
      created_at                      TEXT NOT NULL,
      UNIQUE(team_id, agent_id, candidate_id, sequence),
      UNIQUE(team_id, agent_id, id),
      UNIQUE(candidate_id, plan_hash),
      FOREIGN KEY(team_id, agent_id, candidate_id)
        REFERENCES team_subagent_release_candidate(team_id, subagent_id, id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id, agent_id, policy_revision_id)
        REFERENCES agent_eval_policy_revision(team_id, agent_id, id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id, agent_id, baseline_release_id)
        REFERENCES team_subagent_release(team_id, subagent_id, id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE agent_eval_plan_suite (
      plan_id                 TEXT NOT NULL REFERENCES agent_eval_plan(id) ON DELETE RESTRICT,
      suite_revision_id       TEXT NOT NULL REFERENCES agent_eval_suite_revision(id) ON DELETE RESTRICT,
      suite_kind              TEXT NOT NULL CHECK(suite_kind IN (
                                'platform_contract','owner_quality','category_benchmark'
                              )),
      execution_scope         TEXT NOT NULL CHECK(execution_scope IN ('paired','candidate_only')),
      required                INTEGER NOT NULL CHECK(required IN (0,1)),
      tier                    TEXT NOT NULL CHECK(tier IN ('critical','standard','diagnostic')),
      attempts                INTEGER NOT NULL CHECK(attempts BETWEEN 1 AND 5),
      fixture_bundle_json     TEXT NOT NULL CHECK(
                                json_valid(fixture_bundle_json)
                                AND json_type(fixture_bundle_json)='object'
                                AND length(fixture_bundle_json) <= 262144
                              ),
      fixture_bundle_hash     TEXT NOT NULL CHECK(fixture_bundle_hash LIKE 'sha256:%'),
      scorer_bundle_ref       TEXT NOT NULL CHECK(length(scorer_bundle_ref) BETWEEN 1 AND 240),
      scorer_bundle_hash      TEXT NOT NULL CHECK(scorer_bundle_hash LIKE 'sha256:%'),
      judge_policy_hash       TEXT NOT NULL CHECK(judge_policy_hash LIKE 'sha256:%'),
      order_index             INTEGER NOT NULL CHECK(order_index >= 0),
      PRIMARY KEY(plan_id, suite_revision_id),
      UNIQUE(plan_id, order_index)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE agent_eval_comparison_execution (
      id                   TEXT PRIMARY KEY CHECK(id LIKE 'aex_%'),
      team_id              TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
      agent_id             TEXT NOT NULL,
      candidate_id         TEXT NOT NULL,
      plan_id              TEXT NOT NULL,
      run_kind             TEXT NOT NULL CHECK(run_kind IN ('release','diagnostic')),
      selection_json       TEXT NOT NULL CHECK(
                             json_valid(selection_json)
                             AND json_type(selection_json)='object'
                             AND length(selection_json) <= 262144
                           ),
      selection_hash       TEXT NOT NULL CHECK(selection_hash LIKE 'sha256:%'),
      status               TEXT NOT NULL CHECK(status IN (
                             'queued','running','finalizing','completed','failed','cancelled'
                           )),
      idempotency_hash     TEXT NOT NULL CHECK(idempotency_hash LIKE 'sha256:%'),
      request_hash         TEXT NOT NULL CHECK(request_hash LIKE 'sha256:%'),
      actor_kind           TEXT NOT NULL CHECK(actor_kind IN ('user','automation')),
      actor_ref            TEXT NOT NULL CHECK(length(actor_ref) BETWEEN 1 AND 240),
      automation_grant_id  TEXT,
      generation           INTEGER NOT NULL DEFAULT 1 CHECK(generation >= 1),
      row_version          INTEGER NOT NULL DEFAULT 1 CHECK(row_version >= 1),
      requested_at         TEXT NOT NULL,
      started_at           TEXT,
      finalizing_at        TEXT,
      finished_at          TEXT,
      cancelled_at         TEXT,
      failure_code         TEXT,
      UNIQUE(team_id, actor_kind, actor_ref, idempotency_hash),
      UNIQUE(team_id, agent_id, id),
      CHECK(
        (actor_kind='user' AND automation_grant_id IS NULL)
        OR (actor_kind='automation' AND automation_grant_id IS NOT NULL)
      ),
      CHECK(status!='queued' OR (
        started_at IS NULL AND finalizing_at IS NULL AND finished_at IS NULL
        AND cancelled_at IS NULL AND failure_code IS NULL
      )),
      CHECK(status!='running' OR (
        started_at IS NOT NULL AND finalizing_at IS NULL AND finished_at IS NULL
        AND cancelled_at IS NULL AND failure_code IS NULL
      )),
      CHECK(status!='finalizing' OR (
        started_at IS NOT NULL AND finalizing_at IS NOT NULL AND finished_at IS NULL
        AND cancelled_at IS NULL AND failure_code IS NULL
      )),
      CHECK(status NOT IN ('completed','failed') OR (
        finished_at IS NOT NULL AND cancelled_at IS NULL
      )),
      CHECK(status!='cancelled' OR cancelled_at IS NOT NULL),
      FOREIGN KEY(team_id, agent_id, candidate_id)
        REFERENCES team_subagent_release_candidate(team_id, subagent_id, id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id, agent_id, plan_id)
        REFERENCES agent_eval_plan(team_id, agent_id, id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE agent_eval_comparison_run (
      execution_id       TEXT NOT NULL REFERENCES agent_eval_comparison_execution(id) ON DELETE RESTRICT,
      suite_revision_id  TEXT NOT NULL REFERENCES agent_eval_suite_revision(id) ON DELETE RESTRICT,
      subject_side       TEXT NOT NULL CHECK(subject_side IN ('baseline','candidate')),
      run_id             TEXT NOT NULL UNIQUE REFERENCES agent_eval_run(id) ON DELETE RESTRICT,
      PRIMARY KEY(execution_id, suite_revision_id, subject_side)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE agent_eval_attempt_identity (
      run_id                     TEXT NOT NULL,
      case_revision_id           TEXT NOT NULL,
      attempt                    INTEGER NOT NULL CHECK(attempt BETWEEN 1 AND 5),
      subject_provider_ref       TEXT,
      subject_provider_hash      TEXT CHECK(
                                   subject_provider_hash IS NULL
                                   OR subject_provider_hash LIKE 'sha256:%'
                                 ),
      judge_provider_ref         TEXT,
      judge_provider_hash        TEXT CHECK(
                                   judge_provider_hash IS NULL
                                   OR judge_provider_hash LIKE 'sha256:%'
                                 ),
      reproducibility            TEXT NOT NULL CHECK(reproducibility IN (
                                   'exact','attributed_only','unknown'
                                 )),
      created_at                 TEXT NOT NULL,
      PRIMARY KEY(run_id, case_revision_id, attempt),
      CHECK((subject_provider_ref IS NULL) = (subject_provider_hash IS NULL)),
      CHECK((judge_provider_ref IS NULL) = (judge_provider_hash IS NULL)),
      FOREIGN KEY(run_id, case_revision_id, attempt)
        REFERENCES agent_eval_case_result(run_id, case_revision_id, attempt) ON DELETE RESTRICT
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE agent_eval_snapshot (
      id                       TEXT PRIMARY KEY CHECK(id LIKE 'aevs_%'),
      team_id                  TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
      agent_id                 TEXT NOT NULL,
      candidate_id             TEXT NOT NULL,
      plan_id                  TEXT NOT NULL,
      execution_id             TEXT NOT NULL UNIQUE,
      readiness                TEXT NOT NULL CHECK(readiness IN (
                                 'ready','ready_with_risks','blocked','unknown'
                               )),
      identity_json            TEXT NOT NULL CHECK(
                                 json_valid(identity_json)
                                 AND json_type(identity_json)='object'
                                 AND length(identity_json) <= 262144
                               ),
      identity_hash            TEXT NOT NULL CHECK(identity_hash LIKE 'sha256:%'),
      summary_json             TEXT NOT NULL CHECK(
                                 json_valid(summary_json)
                                 AND json_type(summary_json)='object'
                                 AND length(summary_json) <= 262144
                               ),
      reviewer_projection_json TEXT NOT NULL CHECK(
                                 json_valid(reviewer_projection_json)
                                 AND json_type(reviewer_projection_json)='object'
                                 AND length(reviewer_projection_json) <= 262144
                               ),
      reviewer_projection_hash TEXT NOT NULL CHECK(reviewer_projection_hash LIKE 'sha256:%'),
      snapshot_hash            TEXT NOT NULL UNIQUE CHECK(snapshot_hash LIKE 'sha256:%'),
      created_at               TEXT NOT NULL,
      UNIQUE(team_id, agent_id, id),
      FOREIGN KEY(team_id, agent_id, candidate_id)
        REFERENCES team_subagent_release_candidate(team_id, subagent_id, id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id, agent_id, plan_id)
        REFERENCES agent_eval_plan(team_id, agent_id, id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id, agent_id, execution_id)
        REFERENCES agent_eval_comparison_execution(team_id, agent_id, id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE agent_eval_override (
      id                           TEXT PRIMARY KEY CHECK(id LIKE 'aeov_%'),
      team_id                      TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
      agent_id                     TEXT NOT NULL,
      candidate_id                 TEXT NOT NULL,
      plan_id                      TEXT NOT NULL,
      snapshot_id                  TEXT NOT NULL,
      override_hash                TEXT NOT NULL UNIQUE CHECK(override_hash LIKE 'sha256:%'),
      reason_overrides_json        TEXT NOT NULL CHECK(
                                     json_valid(reason_overrides_json)
                                     AND json_type(reason_overrides_json)='array'
                                     AND json_array_length(reason_overrides_json) >= 1
                                     AND length(reason_overrides_json) <= 65536
                                   ),
      reason_overrides_hash        TEXT NOT NULL CHECK(reason_overrides_hash LIKE 'sha256:%'),
      team_evidence_selection_json TEXT NOT NULL CHECK(
                                     json_valid(team_evidence_selection_json)
                                     AND json_type(team_evidence_selection_json)='object'
                                     AND length(team_evidence_selection_json) <= 65536
                                   ),
      team_evidence_selection_hash TEXT NOT NULL CHECK(team_evidence_selection_hash LIKE 'sha256:%'),
      team_evidence_json           TEXT NOT NULL CHECK(
                                     json_valid(team_evidence_json)
                                     AND json_type(team_evidence_json)='object'
                                     AND length(team_evidence_json) <= 262144
                                   ),
      team_evidence_hash           TEXT NOT NULL CHECK(team_evidence_hash LIKE 'sha256:%'),
      created_by                   TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
      created_at                   TEXT NOT NULL,
      UNIQUE(team_id, agent_id, id),
      FOREIGN KEY(team_id, agent_id, candidate_id)
        REFERENCES team_subagent_release_candidate(team_id, subagent_id, id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id, agent_id, plan_id)
        REFERENCES agent_eval_plan(team_id, agent_id, id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id, agent_id, snapshot_id)
        REFERENCES agent_eval_snapshot(team_id, agent_id, id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE agent_eval_candidate_control (
      candidate_id                 TEXT PRIMARY KEY,
      team_id                      TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
      agent_id                     TEXT NOT NULL,
      source_working_copy_id       TEXT NOT NULL REFERENCES team_subagent_working_copy(id) ON DELETE RESTRICT,
      source_working_copy_version  INTEGER NOT NULL CHECK(source_working_copy_version >= 1),
      source_working_copy_hash     TEXT NOT NULL CHECK(source_working_copy_hash LIKE 'sha256:%'),
      active_plan_id               TEXT,
      readiness                    TEXT NOT NULL CHECK(readiness IN (
                                     'missing','running','ready','ready_with_risks',
                                     'blocked','unknown','stale'
                                   )),
      stale_subject                TEXT CHECK(stale_subject IN ('candidate','evidence')),
      reason_codes_json            TEXT NOT NULL CHECK(
                                     json_valid(reason_codes_json)
                                     AND json_type(reason_codes_json)='array'
                                     AND length(reason_codes_json) <= 65536
                                   ),
      active_execution_id          TEXT,
      active_snapshot_id           TEXT,
      active_override_id           TEXT,
      prepared_actor_kind          TEXT NOT NULL CHECK(prepared_actor_kind IN ('user','automation')),
      prepared_actor_ref           TEXT NOT NULL CHECK(length(prepared_actor_ref) BETWEEN 1 AND 240),
      prepared_user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
      prepared_automation_grant_id TEXT,
      review_submitted_by          TEXT REFERENCES users(id) ON DELETE RESTRICT,
      review_submitted_at          TEXT,
      review_plan_id               TEXT,
      review_plan_hash             TEXT CHECK(review_plan_hash IS NULL OR review_plan_hash LIKE 'sha256:%'),
      review_snapshot_id           TEXT,
      review_snapshot_hash         TEXT CHECK(review_snapshot_hash IS NULL OR review_snapshot_hash LIKE 'sha256:%'),
      review_override_id           TEXT,
      review_override_hash         TEXT CHECK(review_override_hash IS NULL OR review_override_hash LIKE 'sha256:%'),
      row_version                  INTEGER NOT NULL DEFAULT 1 CHECK(row_version >= 1),
      created_at                   TEXT NOT NULL,
      updated_at                   TEXT NOT NULL,
      UNIQUE(team_id, agent_id, candidate_id),
      CHECK((readiness='stale') = (stale_subject IS NOT NULL)),
      CHECK(
        (prepared_actor_kind='user' AND prepared_automation_grant_id IS NULL
          AND prepared_actor_ref=prepared_user_id)
        OR (prepared_actor_kind='automation' AND prepared_automation_grant_id IS NOT NULL)
      ),
      CHECK(
        (review_submitted_by IS NULL AND review_submitted_at IS NULL
          AND review_plan_id IS NULL AND review_plan_hash IS NULL
          AND review_snapshot_id IS NULL AND review_snapshot_hash IS NULL
          AND review_override_id IS NULL AND review_override_hash IS NULL)
        OR (review_submitted_by IS NOT NULL AND review_submitted_at IS NOT NULL
          AND review_plan_id IS NOT NULL AND review_plan_hash IS NOT NULL
          AND review_snapshot_id IS NOT NULL AND review_snapshot_hash IS NOT NULL
          AND ((review_override_id IS NULL AND review_override_hash IS NULL)
            OR (review_override_id IS NOT NULL AND review_override_hash IS NOT NULL)))
      ),
      FOREIGN KEY(team_id, agent_id, candidate_id)
        REFERENCES team_subagent_release_candidate(team_id, subagent_id, id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id, agent_id, active_plan_id)
        REFERENCES agent_eval_plan(team_id, agent_id, id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id, agent_id, active_execution_id)
        REFERENCES agent_eval_comparison_execution(team_id, agent_id, id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id, agent_id, active_snapshot_id)
        REFERENCES agent_eval_snapshot(team_id, agent_id, id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id, agent_id, active_override_id)
        REFERENCES agent_eval_override(team_id, agent_id, id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id, agent_id, review_plan_id)
        REFERENCES agent_eval_plan(team_id, agent_id, id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id, agent_id, review_snapshot_id)
        REFERENCES agent_eval_snapshot(team_id, agent_id, id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id, agent_id, review_override_id)
        REFERENCES agent_eval_override(team_id, agent_id, id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE agent_release_eval_evidence (
      release_id       TEXT PRIMARY KEY,
      team_id          TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
      agent_id         TEXT NOT NULL,
      candidate_id     TEXT NOT NULL,
      plan_id          TEXT NOT NULL,
      snapshot_id      TEXT NOT NULL,
      override_id      TEXT,
      candidate_hash   TEXT NOT NULL CHECK(candidate_hash LIKE 'sha256:%'),
      plan_hash        TEXT NOT NULL CHECK(plan_hash LIKE 'sha256:%'),
      snapshot_hash    TEXT NOT NULL CHECK(snapshot_hash LIKE 'sha256:%'),
      override_hash    TEXT CHECK(override_hash IS NULL OR override_hash LIKE 'sha256:%'),
      readiness        TEXT NOT NULL CHECK(readiness IN ('ready','ready_with_risks')),
      created_at       TEXT NOT NULL,
      UNIQUE(team_id, agent_id, release_id),
      CHECK((override_id IS NULL) = (override_hash IS NULL)),
      FOREIGN KEY(team_id, agent_id, release_id)
        REFERENCES team_subagent_release(team_id, subagent_id, id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id, agent_id, candidate_id)
        REFERENCES team_subagent_release_candidate(team_id, subagent_id, id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id, agent_id, plan_id)
        REFERENCES agent_eval_plan(team_id, agent_id, id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id, agent_id, snapshot_id)
        REFERENCES agent_eval_snapshot(team_id, agent_id, id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id, agent_id, override_id)
        REFERENCES agent_eval_override(team_id, agent_id, id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE agent_version_notification_outbox (
      id                 TEXT PRIMARY KEY CHECK(id LIKE 'aevn_%'),
      team_id            TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
      agent_id           TEXT NOT NULL,
      recipient_user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
      event_type         TEXT NOT NULL CHECK(event_type IN (
                           'candidate.prepared','comparison.completed','candidate.ready',
                           'candidate.blocked','candidate.unknown','candidate.stale',
                           'review.submitted','review.rejected','release.regression'
                         )),
      candidate_id       TEXT,
      plan_id            TEXT,
      snapshot_id        TEXT,
      release_id         TEXT,
      dedupe_key         TEXT NOT NULL UNIQUE CHECK(length(dedupe_key) BETWEEN 1 AND 240),
      safe_payload_json  TEXT NOT NULL CHECK(
                           json_valid(safe_payload_json)
                           AND json_type(safe_payload_json)='object'
                           AND length(safe_payload_json) <= 262144
                         ),
      status             TEXT NOT NULL CHECK(status IN ('pending','leased','delivered','failed')),
      lease_generation   INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation >= 0),
      lease_owner        TEXT,
      lease_token_hash   TEXT CHECK(lease_token_hash IS NULL OR lease_token_hash LIKE 'sha256:%'),
      lease_expires_at   TEXT,
      attempt_count      INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
      available_at       TEXT NOT NULL,
      created_at         TEXT NOT NULL,
      settled_at         TEXT,
      outcome_code       TEXT,
      CHECK(
        candidate_id IS NOT NULL OR plan_id IS NOT NULL
        OR snapshot_id IS NOT NULL OR release_id IS NOT NULL
      ),
      CHECK(
        (status='pending' AND lease_generation=0 AND lease_owner IS NULL
          AND lease_token_hash IS NULL AND lease_expires_at IS NULL AND settled_at IS NULL)
        OR (status='leased' AND lease_generation >= 1 AND lease_owner IS NOT NULL
          AND lease_token_hash IS NOT NULL AND lease_expires_at IS NOT NULL AND settled_at IS NULL)
        OR (status IN ('delivered','failed') AND settled_at IS NOT NULL)
      ),
      FOREIGN KEY(team_id, agent_id)
        REFERENCES team_subagent(team_id, id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id, agent_id, candidate_id)
        REFERENCES team_subagent_release_candidate(team_id, subagent_id, id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id, agent_id, plan_id)
        REFERENCES agent_eval_plan(team_id, agent_id, id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id, agent_id, snapshot_id)
        REFERENCES agent_eval_snapshot(team_id, agent_id, id) ON DELETE RESTRICT,
      FOREIGN KEY(team_id, agent_id, release_id)
        REFERENCES team_subagent_release(team_id, subagent_id, id) ON DELETE RESTRICT
    )
    """,
)

_INDEXES = (
    """
    CREATE INDEX idx_agent_revision_compatibility_major
    ON agent_revision_compatibility(team_id, agent_id, compatibility_major, created_at DESC)
    """,
    """
    CREATE INDEX idx_agent_eval_policy_history
    ON agent_eval_policy_revision(team_id, agent_id, sequence DESC)
    """,
    """
    CREATE INDEX idx_agent_eval_plan_history
    ON agent_eval_plan(team_id, agent_id, candidate_id, sequence DESC)
    """,
    """
    CREATE UNIQUE INDEX uq_agent_eval_comparison_active
    ON agent_eval_comparison_execution(candidate_id)
    WHERE status IN ('queued','running','finalizing')
    """,
    """
    CREATE INDEX idx_agent_eval_comparison_history
    ON agent_eval_comparison_execution(team_id, agent_id, candidate_id, requested_at DESC)
    """,
    """
    CREATE INDEX idx_agent_eval_snapshot_history
    ON agent_eval_snapshot(team_id, agent_id, candidate_id, created_at DESC)
    """,
    """
    CREATE INDEX idx_agent_eval_override_history
    ON agent_eval_override(team_id, agent_id, candidate_id, created_at DESC)
    """,
    """
    CREATE INDEX idx_agent_version_notification_claim
    ON agent_version_notification_outbox(status, available_at, lease_expires_at, created_at, id)
    """,
)

_IMMUTABLE_TABLES = (
    "agent_revision_compatibility",
    "agent_eval_policy_revision",
    "agent_eval_plan",
    "agent_eval_plan_suite",
    "agent_eval_comparison_run",
    "agent_eval_attempt_identity",
    "agent_eval_snapshot",
    "agent_eval_override",
    "agent_release_eval_evidence",
)

_SPECIAL_TRIGGERS = (
    """
    CREATE TRIGGER trg_agent_owner_eval_candidate_transition
    BEFORE UPDATE ON team_subagent_release_candidate
    WHEN (NEW.state IS NOT OLD.state OR NEW.row_version IS NOT OLD.row_version) AND (
      NEW.row_version != OLD.row_version + 1 OR NOT (
        (OLD.state='prepared' AND NEW.state IN ('pending','approved','withdrawn','stale'))
        OR (OLD.state='pending' AND NEW.state IN ('approved','rejected','withdrawn','stale'))
        OR (OLD.state='approved' AND NEW.state='activated')
      )
    )
    BEGIN
      SELECT RAISE(ABORT, 'Agent Owner Eval candidate transition mismatch');
    END
    """,
    """
    CREATE TRIGGER trg_agent_eval_policy_owner_scope
    BEFORE INSERT ON agent_eval_policy_revision
    WHEN NOT EXISTS (
      SELECT 1
      FROM team_subagent agent
      JOIN users actor ON actor.id=NEW.created_by
      WHERE agent.team_id=NEW.team_id AND agent.id=NEW.agent_id
        AND COALESCE(agent.owner_user_id,agent.created_by)=actor.id
        AND actor.team_id=NEW.team_id AND actor.status='active'
    ) OR NEW.sequence != COALESCE(
      (SELECT MAX(sequence)+1 FROM agent_eval_policy_revision
       WHERE team_id=NEW.team_id AND agent_id=NEW.agent_id),
      1
    )
    BEGIN
      SELECT RAISE(ABORT, 'Agent Eval Policy owner scope mismatch');
    END
    """,
    """
    CREATE TRIGGER trg_agent_eval_plan_workspace_scope
    BEFORE INSERT ON agent_eval_plan
    WHEN NOT EXISTS (
      SELECT 1
      FROM workspaces workspace
      JOIN team_subagent agent ON agent.team_id=NEW.team_id AND agent.id=NEW.agent_id
      JOIN team_subagent_release_candidate candidate
        ON candidate.team_id=NEW.team_id
       AND candidate.subagent_id=NEW.agent_id
       AND candidate.id=NEW.candidate_id
      JOIN users actor ON actor.id=NEW.created_by
      WHERE workspace.id=NEW.execution_workspace_id AND workspace.team_id=NEW.team_id
        AND COALESCE(agent.owner_user_id,agent.created_by)=actor.id
        AND actor.team_id=NEW.team_id AND actor.status='active'
        AND candidate.state='prepared'
        AND candidate.candidate_hash=NEW.candidate_hash
        AND candidate.base_live_release_id IS NEW.baseline_release_id
    ) OR NEW.sequence != COALESCE(
      (SELECT MAX(sequence)+1 FROM agent_eval_plan
       WHERE team_id=NEW.team_id AND agent_id=NEW.agent_id
         AND candidate_id=NEW.candidate_id),
      1
    )
    BEGIN
      SELECT RAISE(ABORT, 'Agent Eval Plan workspace scope mismatch');
    END
    """,
    """
    CREATE TRIGGER trg_agent_eval_plan_suite_scope
    BEFORE INSERT ON agent_eval_plan_suite
    WHEN NOT EXISTS (
      SELECT 1
      FROM agent_eval_plan plan
      JOIN agent_eval_suite_revision revision ON revision.id=NEW.suite_revision_id
      JOIN agent_eval_suite suite ON suite.id=revision.suite_id
      WHERE plan.id=NEW.plan_id
        AND suite.team_id=plan.team_id
        AND suite.suite_kind=NEW.suite_kind
        AND revision.scorer_bundle_ref=NEW.scorer_bundle_ref
        AND (
          (suite.agent_source_kind='team' AND suite.agent_id=plan.agent_id)
          OR (suite.suite_kind='category_benchmark' AND suite.agent_id IS NULL)
        )
    )
    BEGIN
      SELECT RAISE(ABORT, 'Agent Eval Plan suite scope mismatch');
    END
    """,
    """
    CREATE TRIGGER trg_agent_eval_comparison_actor_scope
    BEFORE INSERT ON agent_eval_comparison_execution
    WHEN (
      NEW.actor_kind='user' AND NOT EXISTS (
        SELECT 1
        FROM team_subagent agent
        JOIN users actor ON actor.id=NEW.actor_ref
        WHERE agent.team_id=NEW.team_id AND agent.id=NEW.agent_id
          AND COALESCE(agent.owner_user_id,agent.created_by)=actor.id
          AND actor.team_id=NEW.team_id AND actor.status='active'
      )
    ) OR NOT EXISTS (
      SELECT 1
      FROM agent_eval_plan plan
      JOIN team_subagent_release_candidate candidate
        ON candidate.team_id=plan.team_id
       AND candidate.subagent_id=plan.agent_id
       AND candidate.id=plan.candidate_id
      WHERE plan.id=NEW.plan_id AND plan.team_id=NEW.team_id
        AND plan.agent_id=NEW.agent_id AND plan.candidate_id=NEW.candidate_id
        AND candidate.state='prepared'
    )
    BEGIN
      SELECT RAISE(ABORT, 'Agent Eval comparison actor scope mismatch');
    END
    """,
    """
    CREATE TRIGGER trg_agent_eval_comparison_run_scope
    BEFORE INSERT ON agent_eval_comparison_run
    WHEN NOT EXISTS (
      SELECT 1
      FROM agent_eval_comparison_execution execution
      JOIN agent_eval_plan plan ON plan.id=execution.plan_id
      JOIN agent_eval_plan_suite plan_suite
        ON plan_suite.plan_id=plan.id
       AND plan_suite.suite_revision_id=NEW.suite_revision_id
      JOIN agent_eval_run run ON run.id=NEW.run_id
      WHERE execution.id=NEW.execution_id
        AND run.team_id=execution.team_id
        AND run.agent_id=execution.agent_id
        AND run.agent_source_kind='team'
        AND run.suite_revision_id=NEW.suite_revision_id
        AND (
          (NEW.subject_side='candidate'
            AND run.candidate_id=execution.candidate_id
            AND run.release_id IS NULL)
          OR (NEW.subject_side='baseline'
            AND plan_suite.execution_scope='paired'
            AND plan.baseline_release_id IS NOT NULL
            AND run.release_id=plan.baseline_release_id
            AND run.candidate_id IS NULL)
        )
    )
    BEGIN
      SELECT RAISE(ABORT, 'Agent Eval comparison run scope mismatch');
    END
    """,
    """
    CREATE TRIGGER trg_agent_eval_snapshot_execution_scope
    BEFORE INSERT ON agent_eval_snapshot
    WHEN NOT EXISTS (
      SELECT 1 FROM agent_eval_comparison_execution execution
      WHERE execution.id=NEW.execution_id
        AND execution.team_id=NEW.team_id
        AND execution.agent_id=NEW.agent_id
        AND execution.candidate_id=NEW.candidate_id
        AND execution.plan_id=NEW.plan_id
        AND execution.run_kind='release'
        AND execution.status='completed'
    )
    BEGIN
      SELECT RAISE(ABORT, 'Agent Eval Snapshot execution scope mismatch');
    END
    """,
    """
    CREATE TRIGGER trg_agent_eval_candidate_control_identity_immutable
    BEFORE UPDATE ON agent_eval_candidate_control
    WHEN NEW.candidate_id IS NOT OLD.candidate_id
      OR NEW.team_id IS NOT OLD.team_id
      OR NEW.agent_id IS NOT OLD.agent_id
      OR NEW.source_working_copy_id IS NOT OLD.source_working_copy_id
      OR NEW.source_working_copy_version IS NOT OLD.source_working_copy_version
      OR NEW.source_working_copy_hash IS NOT OLD.source_working_copy_hash
      OR NEW.prepared_actor_kind IS NOT OLD.prepared_actor_kind
      OR NEW.prepared_actor_ref IS NOT OLD.prepared_actor_ref
      OR NEW.prepared_user_id IS NOT OLD.prepared_user_id
      OR NEW.prepared_automation_grant_id IS NOT OLD.prepared_automation_grant_id
      OR NEW.created_at IS NOT OLD.created_at
    BEGIN
      SELECT RAISE(ABORT, 'Agent Eval candidate control identity is immutable');
    END
    """,
    """
    CREATE TRIGGER trg_agent_eval_candidate_control_scope
    BEFORE INSERT ON agent_eval_candidate_control
    WHEN NOT EXISTS (
      SELECT 1
      FROM team_subagent_working_copy working
      JOIN team_subagent agent
        ON agent.team_id=working.team_id AND agent.id=working.subagent_id
      JOIN team_subagent_release_candidate candidate
        ON candidate.team_id=working.team_id
       AND candidate.subagent_id=working.subagent_id
       AND candidate.id=NEW.candidate_id
      JOIN users effective_owner ON effective_owner.id=NEW.prepared_user_id
      WHERE working.id=NEW.source_working_copy_id
        AND working.team_id=NEW.team_id AND working.subagent_id=NEW.agent_id
        AND working.row_version=NEW.source_working_copy_version
        AND candidate.state='prepared'
        AND COALESCE(agent.owner_user_id,agent.created_by)=effective_owner.id
        AND effective_owner.team_id=NEW.team_id AND effective_owner.status='active'
    )
    BEGIN
      SELECT RAISE(ABORT, 'Agent Eval candidate control scope mismatch');
    END
    """,
    """
    CREATE TRIGGER trg_agent_eval_candidate_control_projection_scope_insert
    BEFORE INSERT ON agent_eval_candidate_control
    WHEN (
      NEW.active_plan_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM agent_eval_plan plan
        WHERE plan.id=NEW.active_plan_id AND plan.candidate_id=NEW.candidate_id
      )
    ) OR (
      NEW.active_execution_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM agent_eval_comparison_execution execution
        WHERE execution.id=NEW.active_execution_id
          AND execution.candidate_id=NEW.candidate_id
          AND execution.plan_id=NEW.active_plan_id
      )
    ) OR (
      NEW.active_snapshot_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM agent_eval_snapshot snapshot
        WHERE snapshot.id=NEW.active_snapshot_id
          AND snapshot.candidate_id=NEW.candidate_id
          AND snapshot.plan_id=NEW.active_plan_id
      )
    ) OR (
      NEW.active_override_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM agent_eval_override override
        WHERE override.id=NEW.active_override_id
          AND override.candidate_id=NEW.candidate_id
          AND override.plan_id=NEW.active_plan_id
          AND override.snapshot_id=NEW.active_snapshot_id
      )
    ) OR (
      NEW.review_plan_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM agent_eval_plan plan
        JOIN agent_eval_snapshot snapshot ON snapshot.plan_id=plan.id
        WHERE plan.id=NEW.review_plan_id
          AND plan.plan_hash=NEW.review_plan_hash
          AND snapshot.id=NEW.review_snapshot_id
          AND snapshot.snapshot_hash=NEW.review_snapshot_hash
          AND snapshot.candidate_id=NEW.candidate_id
      )
    ) OR (
      NEW.review_override_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM agent_eval_override override
        WHERE override.id=NEW.review_override_id
          AND override.candidate_id=NEW.candidate_id
          AND override.plan_id=NEW.review_plan_id
          AND override.snapshot_id=NEW.review_snapshot_id
          AND override.override_hash=NEW.review_override_hash
      )
    ) OR (
      NEW.review_submitted_by IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM team_subagent agent
        JOIN users actor ON actor.id=NEW.review_submitted_by
        WHERE agent.team_id=NEW.team_id AND agent.id=NEW.agent_id
          AND COALESCE(agent.owner_user_id,agent.created_by)=actor.id
          AND actor.team_id=NEW.team_id AND actor.status='active'
          AND EXISTS (
            SELECT 1 FROM team_subagent_release_candidate candidate
            WHERE candidate.id=NEW.candidate_id AND candidate.state='pending'
          )
      )
    )
    BEGIN
      SELECT RAISE(ABORT, 'Agent Eval candidate control projection scope mismatch');
    END
    """,
    """
    CREATE TRIGGER trg_agent_eval_candidate_control_projection_scope_update
    BEFORE UPDATE ON agent_eval_candidate_control
    WHEN NEW.row_version != OLD.row_version + 1 OR (
      NEW.active_plan_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM agent_eval_plan plan
        WHERE plan.id=NEW.active_plan_id AND plan.candidate_id=NEW.candidate_id
      )
    ) OR (
      NEW.active_execution_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM agent_eval_comparison_execution execution
        WHERE execution.id=NEW.active_execution_id
          AND execution.candidate_id=NEW.candidate_id
          AND execution.plan_id=NEW.active_plan_id
      )
    ) OR (
      NEW.active_snapshot_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM agent_eval_snapshot snapshot
        WHERE snapshot.id=NEW.active_snapshot_id
          AND snapshot.candidate_id=NEW.candidate_id
          AND snapshot.plan_id=NEW.active_plan_id
      )
    ) OR (
      NEW.active_override_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM agent_eval_override override
        WHERE override.id=NEW.active_override_id
          AND override.candidate_id=NEW.candidate_id
          AND override.plan_id=NEW.active_plan_id
          AND override.snapshot_id=NEW.active_snapshot_id
      )
    ) OR (
      NEW.review_plan_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM agent_eval_plan plan
        JOIN agent_eval_snapshot snapshot ON snapshot.plan_id=plan.id
        WHERE plan.id=NEW.review_plan_id
          AND plan.plan_hash=NEW.review_plan_hash
          AND snapshot.id=NEW.review_snapshot_id
          AND snapshot.snapshot_hash=NEW.review_snapshot_hash
          AND snapshot.candidate_id=NEW.candidate_id
      )
    ) OR (
      NEW.review_override_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM agent_eval_override override
        WHERE override.id=NEW.review_override_id
          AND override.candidate_id=NEW.candidate_id
          AND override.plan_id=NEW.review_plan_id
          AND override.snapshot_id=NEW.review_snapshot_id
          AND override.override_hash=NEW.review_override_hash
      )
    ) OR (
      NEW.review_submitted_by IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM team_subagent agent
        JOIN users actor ON actor.id=NEW.review_submitted_by
        WHERE agent.team_id=NEW.team_id AND agent.id=NEW.agent_id
          AND COALESCE(agent.owner_user_id,agent.created_by)=actor.id
          AND actor.team_id=NEW.team_id AND actor.status='active'
          AND EXISTS (
            SELECT 1 FROM team_subagent_release_candidate candidate
            WHERE candidate.id=NEW.candidate_id AND candidate.state='pending'
          )
      )
    )
    BEGIN
      SELECT RAISE(ABORT, 'Agent Eval candidate control projection scope mismatch');
    END
    """,
    """
    CREATE TRIGGER trg_agent_eval_candidate_control_immutable_delete
    BEFORE DELETE ON agent_eval_candidate_control
    BEGIN
      SELECT RAISE(ABORT, 'Agent Eval candidate control is immutable');
    END
    """,
    """
    CREATE TRIGGER trg_agent_eval_comparison_identity_immutable
    BEFORE UPDATE ON agent_eval_comparison_execution
    WHEN NEW.id IS NOT OLD.id
      OR NEW.team_id IS NOT OLD.team_id
      OR NEW.agent_id IS NOT OLD.agent_id
      OR NEW.candidate_id IS NOT OLD.candidate_id
      OR NEW.plan_id IS NOT OLD.plan_id
      OR NEW.run_kind IS NOT OLD.run_kind
      OR NEW.selection_json IS NOT OLD.selection_json
      OR NEW.selection_hash IS NOT OLD.selection_hash
      OR NEW.idempotency_hash IS NOT OLD.idempotency_hash
      OR NEW.request_hash IS NOT OLD.request_hash
      OR NEW.actor_kind IS NOT OLD.actor_kind
      OR NEW.actor_ref IS NOT OLD.actor_ref
      OR NEW.automation_grant_id IS NOT OLD.automation_grant_id
      OR NEW.requested_at IS NOT OLD.requested_at
    BEGIN
      SELECT RAISE(ABORT, 'Agent Eval comparison identity is immutable');
    END
    """,
    """
    CREATE TRIGGER trg_agent_eval_comparison_transition
    BEFORE UPDATE ON agent_eval_comparison_execution
    WHEN NEW.row_version != OLD.row_version + 1 OR NOT (
      (OLD.status='queued' AND NEW.status IN ('running','failed','cancelled'))
      OR (OLD.status='running' AND NEW.status IN ('finalizing','failed','cancelled'))
      OR (OLD.status='finalizing' AND NEW.status IN ('completed','failed'))
    )
    BEGIN
      SELECT RAISE(ABORT, 'Agent Eval comparison transition mismatch');
    END
    """,
    """
    CREATE TRIGGER trg_agent_eval_comparison_immutable_delete
    BEFORE DELETE ON agent_eval_comparison_execution
    BEGIN
      SELECT RAISE(ABORT, 'Agent Eval comparison is immutable');
    END
    """,
    """
    CREATE TRIGGER trg_agent_eval_override_owner_scope
    BEFORE INSERT ON agent_eval_override
    WHEN NOT EXISTS (
      SELECT 1
      FROM team_subagent agent
      JOIN users actor ON actor.id=NEW.created_by
      WHERE agent.team_id=NEW.team_id AND agent.id=NEW.agent_id
        AND COALESCE(agent.owner_user_id,agent.created_by)=actor.id
        AND actor.team_id=NEW.team_id AND actor.status='active'
    ) OR NOT EXISTS (
      SELECT 1 FROM agent_eval_snapshot snapshot
      WHERE snapshot.id=NEW.snapshot_id AND snapshot.team_id=NEW.team_id
        AND snapshot.agent_id=NEW.agent_id AND snapshot.candidate_id=NEW.candidate_id
        AND snapshot.plan_id=NEW.plan_id
    )
    BEGIN
      SELECT RAISE(ABORT, 'Agent Eval override owner scope mismatch');
    END
    """,
    """
    CREATE TRIGGER trg_agent_release_eval_evidence_scope
    BEFORE INSERT ON agent_release_eval_evidence
    WHEN NOT EXISTS (
      SELECT 1
      FROM team_subagent_release release
      JOIN team_subagent_release_candidate candidate
        ON candidate.id=NEW.candidate_id
       AND candidate.team_id=NEW.team_id
       AND candidate.subagent_id=NEW.agent_id
      JOIN agent_eval_plan plan
        ON plan.id=NEW.plan_id
       AND plan.team_id=NEW.team_id
       AND plan.agent_id=NEW.agent_id
       AND plan.candidate_id=NEW.candidate_id
      JOIN agent_eval_snapshot snapshot
        ON snapshot.id=NEW.snapshot_id
       AND snapshot.team_id=NEW.team_id
       AND snapshot.agent_id=NEW.agent_id
       AND snapshot.candidate_id=NEW.candidate_id
       AND snapshot.plan_id=NEW.plan_id
      WHERE release.id=NEW.release_id
        AND release.team_id=NEW.team_id AND release.subagent_id=NEW.agent_id
        AND release.source_candidate_id=NEW.candidate_id
        AND candidate.candidate_hash=NEW.candidate_hash
        AND plan.plan_hash=NEW.plan_hash
        AND snapshot.snapshot_hash=NEW.snapshot_hash
        AND snapshot.readiness=NEW.readiness
        AND (NEW.override_id IS NULL OR EXISTS (
          SELECT 1 FROM agent_eval_override override
          WHERE override.id=NEW.override_id
            AND override.candidate_id=NEW.candidate_id
            AND override.plan_id=NEW.plan_id
            AND override.snapshot_id=NEW.snapshot_id
            AND override.override_hash=NEW.override_hash
        ))
    )
    BEGIN
      SELECT RAISE(ABORT, 'Agent release Eval evidence scope mismatch');
    END
    """,
    """
    CREATE TRIGGER trg_agent_version_notification_recipient_scope
    BEFORE INSERT ON agent_version_notification_outbox
    WHEN NOT EXISTS (
      SELECT 1 FROM users recipient
      WHERE recipient.id=NEW.recipient_user_id
        AND recipient.team_id=NEW.team_id AND recipient.status='active'
    )
    BEGIN
      SELECT RAISE(ABORT, 'Agent version notification recipient scope mismatch');
    END
    """,
    """
    CREATE TRIGGER trg_agent_version_notification_payload_immutable
    BEFORE UPDATE ON agent_version_notification_outbox
    WHEN NEW.id IS NOT OLD.id
      OR NEW.team_id IS NOT OLD.team_id
      OR NEW.agent_id IS NOT OLD.agent_id
      OR NEW.recipient_user_id IS NOT OLD.recipient_user_id
      OR NEW.event_type IS NOT OLD.event_type
      OR NEW.candidate_id IS NOT OLD.candidate_id
      OR NEW.plan_id IS NOT OLD.plan_id
      OR NEW.snapshot_id IS NOT OLD.snapshot_id
      OR NEW.release_id IS NOT OLD.release_id
      OR NEW.dedupe_key IS NOT OLD.dedupe_key
      OR NEW.safe_payload_json IS NOT OLD.safe_payload_json
      OR NEW.created_at IS NOT OLD.created_at
    BEGIN
      SELECT RAISE(ABORT, 'Agent version notification payload is immutable');
    END
    """,
    """
    CREATE TRIGGER trg_agent_version_notification_immutable_delete
    BEFORE DELETE ON agent_version_notification_outbox
    BEGIN
      SELECT RAISE(ABORT, 'Agent version notification is immutable');
    END
    """,
)


def _canonical_sql(value: str) -> str:
    return "".join(value.lower().replace("if not exists", "").split())


def _object_name(statement: str) -> str:
    canonical = " ".join(statement.split())
    words = canonical.split(" ")
    if words[1] == "UNIQUE":
        return words[3]
    return words[2]


def _candidate_incoming_fks(connection: Any) -> set[tuple[str, str, str, str, str]]:
    tables = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
    ).fetchall()
    result: set[tuple[str, str, str, str, str]] = set()
    for (table_name,) in tables:
        for row in connection.exec_driver_sql(f'PRAGMA foreign_key_list("{table_name}")').fetchall():
            if str(row[2]) == "team_subagent_release_candidate":
                result.add((str(table_name), str(row[2]), str(row[3]), str(row[4]), str(row[6])))
    return result


_EXPECTED_INCOMING_FKS = {
    ("team_subagent_release", "team_subagent_release_candidate", "team_id", "team_id", "RESTRICT"),
    ("team_subagent_release", "team_subagent_release_candidate", "subagent_id", "subagent_id", "RESTRICT"),
    ("team_subagent_release", "team_subagent_release_candidate", "source_candidate_id", "id", "RESTRICT"),
    ("agent_release_candidate_name_claim", "team_subagent_release_candidate", "team_id", "team_id", "RESTRICT"),
    ("agent_release_candidate_name_claim", "team_subagent_release_candidate", "agent_id", "subagent_id", "RESTRICT"),
    ("agent_release_candidate_name_claim", "team_subagent_release_candidate", "candidate_id", "id", "RESTRICT"),
    ("team_subagent_release_event", "team_subagent_release_candidate", "team_id", "team_id", "RESTRICT"),
    ("team_subagent_release_event", "team_subagent_release_candidate", "subagent_id", "subagent_id", "RESTRICT"),
    ("team_subagent_release_event", "team_subagent_release_candidate", "candidate_id", "id", "RESTRICT"),
    ("agent_eval_run", "team_subagent_release_candidate", "candidate_id", "id", "RESTRICT"),
}


def _preflight(connection: Any) -> None:
    columns = tuple(str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_xinfo(team_subagent_release_candidate)").fetchall())
    if columns != _CANDIDATE_COLUMNS:
        raise RuntimeError("Agent Owner Eval candidate columns drift")

    candidate_sql_row = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='team_subagent_release_candidate'",
    ).fetchone()
    candidate_sql = _canonical_sql(str(candidate_sql_row[0])) if candidate_sql_row else ""
    if "statein('pending','approved','rejected','withdrawn','activated','stale')" not in candidate_sql:
        raise RuntimeError("Agent Owner Eval candidate state contract drift")

    candidate_objects = {
        (str(row[0]), str(row[1]))
        for row in connection.exec_driver_sql(
            """
            SELECT type,name FROM sqlite_master
            WHERE tbl_name='team_subagent_release_candidate'
              AND type IN ('index','trigger') AND sql IS NOT NULL
            """,
        ).fetchall()
    }
    expected_objects = {
        ("index", "uq_agent_one_pending_candidate"),
        ("index", "idx_agent_candidate_inbox"),
        ("index", "idx_agent_candidate_history"),
        ("trigger", "trg_team_subagent_release_candidate_payload_immutable"),
        ("trigger", "trg_team_subagent_release_candidate_immutable_delete"),
    }
    if candidate_objects != expected_objects:
        raise RuntimeError("Agent Owner Eval candidate index/trigger inventory drift")
    for statement in (*_OLD_CANDIDATE_INDEXES, *_CANDIDATE_TRIGGERS):
        object_type = "trigger" if "CREATE TRIGGER" in statement else "index"
        object_name = _object_name(statement)
        row = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type=? AND name=?",
            (object_type, object_name),
        ).fetchone()
        if row is None or _canonical_sql(str(row[0])) != _canonical_sql(statement):
            raise RuntimeError(f"Agent Owner Eval candidate object semantics drift: {object_name}")
    if _candidate_incoming_fks(connection) != _EXPECTED_INCOMING_FKS:
        raise RuntimeError("Agent Owner Eval candidate incoming FK inventory drift")

    active_duplicate = connection.exec_driver_sql(
        """
        SELECT team_id,subagent_id,COUNT(*)
        FROM team_subagent_release_candidate
        WHERE state IN ('pending','approved')
        GROUP BY team_id,subagent_id HAVING COUNT(*) > 1
        LIMIT 1
        """,
    ).fetchone()
    if active_duplicate is not None:
        raise RuntimeError("Agent Owner Eval found multiple active candidates")

    for trigger_name in ("trg_agent_eval_run_scope", "trg_turn_agent_execution_revision"):
        row = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (trigger_name,),
        ).fetchone()
        current_statement = _AGENT_EVAL_RUN_SCOPE if trigger_name == "trg_agent_eval_run_scope" else _TURN_AGENT_EXECUTION_REVISION
        expected_statement = current_statement.replace("'prepared','pending','approved'", "'pending','approved'")
        if row is None or _canonical_sql(str(row[0])) != _canonical_sql(expected_statement):
            raise RuntimeError(f"Agent Owner Eval trigger contract drift: {trigger_name}")

    new_names = {"team_subagent_release_candidate_owner_eval_new"}
    new_names.update(_object_name(statement) for statement in (*_TABLES, *_INDEXES, *_SPECIAL_TRIGGERS))
    for table_name in _IMMUTABLE_TABLES:
        new_names.add(f"trg_{table_name}_immutable_update")
        new_names.add(f"trg_{table_name}_immutable_delete")
    placeholders = ",".join("?" for _ in new_names)
    existing = connection.exec_driver_sql(
        f"SELECT name FROM sqlite_master WHERE name IN ({placeholders}) LIMIT 1",
        tuple(sorted(new_names)),
    ).fetchone()
    if existing is not None:
        raise RuntimeError(f"Agent Owner Eval new schema object already exists: {existing[0]}")


def _copy_candidate(connection: Any) -> None:
    connection.exec_driver_sql(_CANDIDATE_DDL)
    projection = ",".join(_CANDIDATE_COLUMNS)
    connection.exec_driver_sql(
        f"""
        INSERT INTO team_subagent_release_candidate_owner_eval_new({projection})
        SELECT {projection} FROM team_subagent_release_candidate
        """,
    )
    old_count = connection.exec_driver_sql("SELECT COUNT(*) FROM team_subagent_release_candidate").scalar_one()
    new_count = connection.exec_driver_sql(
        "SELECT COUNT(*) FROM team_subagent_release_candidate_owner_eval_new",
    ).scalar_one()
    changed = connection.exec_driver_sql(
        f"""
        SELECT {projection} FROM team_subagent_release_candidate
        EXCEPT
        SELECT {projection} FROM team_subagent_release_candidate_owner_eval_new
        """,
    ).first()
    if old_count != new_count or changed is not None:
        raise RuntimeError("Agent Owner Eval candidate copy mismatch")

    connection.exec_driver_sql("DROP TRIGGER trg_team_subagent_release_candidate_payload_immutable")
    connection.exec_driver_sql("DROP TRIGGER trg_team_subagent_release_candidate_immutable_delete")
    connection.exec_driver_sql("DROP TABLE team_subagent_release_candidate")
    connection.exec_driver_sql(
        "ALTER TABLE team_subagent_release_candidate_owner_eval_new RENAME TO team_subagent_release_candidate",
    )
    for statement in (*_CANDIDATE_INDEXES, *_CANDIDATE_TRIGGERS):
        connection.exec_driver_sql(statement)


def _immutable_triggers(table_name: str) -> tuple[str, str]:
    return (
        f"""
        CREATE TRIGGER trg_{table_name}_immutable_update
        BEFORE UPDATE ON {table_name}
        BEGIN
          SELECT RAISE(ABORT, '{table_name} is immutable');
        END
        """,
        f"""
        CREATE TRIGGER trg_{table_name}_immutable_delete
        BEFORE DELETE ON {table_name}
        BEGIN
          SELECT RAISE(ABORT, '{table_name} is immutable');
        END
        """,
    )


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    _preflight(connection)
    foreign_keys = int(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
    if foreign_keys:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if int(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()):
            raise RuntimeError("Agent Owner Eval copy migration could not suspend foreign keys")

    connection.exec_driver_sql("SAVEPOINT agent_owner_eval_foundation_upgrade")
    try:
        # SQLite validates trigger bodies while renaming a rebuilt table.  Remove
        # the two external triggers before the old parent disappears, then restore
        # their prepared-aware definitions immediately after the copy.
        connection.exec_driver_sql("DROP TRIGGER trg_agent_eval_run_scope")
        connection.exec_driver_sql("DROP TRIGGER trg_turn_agent_execution_revision")
        _copy_candidate(connection)
        connection.exec_driver_sql(_AGENT_EVAL_RUN_SCOPE)
        connection.exec_driver_sql(_TURN_AGENT_EXECUTION_REVISION)
        for statement in _TABLES:
            connection.exec_driver_sql(statement)
        for statement in _INDEXES:
            connection.exec_driver_sql(statement)
        for table_name in _IMMUTABLE_TABLES:
            for statement in _immutable_triggers(table_name):
                connection.exec_driver_sql(statement)
        for statement in _SPECIAL_TRIGGERS:
            connection.exec_driver_sql(statement)
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"Agent Owner Eval foreign key check failed: {violations[0]}")
    except BaseException:
        connection.exec_driver_sql("ROLLBACK TO SAVEPOINT agent_owner_eval_foundation_upgrade")
        connection.exec_driver_sql("RELEASE SAVEPOINT agent_owner_eval_foundation_upgrade")
        if foreign_keys:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        raise
    connection.exec_driver_sql("RELEASE SAVEPOINT agent_owner_eval_foundation_upgrade")
    if foreign_keys:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore from snapshot to roll back.")
