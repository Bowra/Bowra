"""Durable internal Coach authority and non-resetting provider-call budgets."""

from alembic import context

revision = "0143_coach_ci_delegation"
down_revision = "0142_case_only_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT coach_ci_delegation")
    try:
        for statement in _DDL:
            db.exec_driver_sql(statement)
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT coach_ci_delegation")
        db.exec_driver_sql("RELEASE SAVEPOINT coach_ci_delegation")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT coach_ci_delegation")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")


_DDL = (
    """CREATE TABLE coach_ci_delegation (
       id TEXT PRIMARY KEY CHECK(id LIKE 'acig_%'),
       team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
       agent_id TEXT NOT NULL REFERENCES team_subagent(id) ON DELETE RESTRICT,
       owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
       workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
       source_session_id TEXT NOT NULL, source_turn_id TEXT NOT NULL,
       proposal_id TEXT NOT NULL REFERENCES agent_change_proposal(id) ON DELETE RESTRICT,
       request_key TEXT NOT NULL, request_hash TEXT NOT NULL,
       context_json TEXT NOT NULL CHECK(json_valid(context_json) AND json_type(context_json)='object' AND length(context_json)<=262144),
       context_hash TEXT NOT NULL,
       max_provider_calls INTEGER NOT NULL CHECK(max_provider_calls BETWEEN 1 AND 200),
       max_comparisons INTEGER NOT NULL CHECK(max_comparisons BETWEEN 1 AND 5),
       duration_seconds INTEGER NOT NULL CHECK(duration_seconds BETWEEN 60 AND 3600),
       state TEXT NOT NULL CHECK(state IN ('requested','authorized','revoked','exhausted','unknown')),
       row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version>=1),
       created_at TEXT NOT NULL, authorized_at TEXT, expires_at TEXT, stopped_at TEXT, stop_reason TEXT,
       candidate_id TEXT REFERENCES team_subagent_release_candidate(id) ON DELETE RESTRICT,
       plan_id TEXT REFERENCES agent_eval_plan(id) ON DELETE RESTRICT,
       preparation_json TEXT CHECK(preparation_json IS NULL OR (json_valid(preparation_json) AND json_type(preparation_json)='object')),
       UNIQUE(team_id,owner_user_id,request_key),
       CHECK((authorized_at IS NULL AND expires_at IS NULL) OR (authorized_at IS NOT NULL AND expires_at IS NOT NULL)),
       CHECK((candidate_id IS NULL AND plan_id IS NULL AND preparation_json IS NULL) OR
             (candidate_id IS NOT NULL AND plan_id IS NOT NULL AND preparation_json IS NOT NULL))
    )""",
    "CREATE INDEX idx_coach_ci_owner ON coach_ci_delegation(team_id,owner_user_id,agent_id,created_at)",
    """CREATE TRIGGER trg_coach_ci_identity BEFORE UPDATE ON coach_ci_delegation WHEN
       NEW.id IS NOT OLD.id OR NEW.team_id IS NOT OLD.team_id OR NEW.agent_id IS NOT OLD.agent_id OR
       NEW.owner_user_id IS NOT OLD.owner_user_id OR NEW.workspace_id IS NOT OLD.workspace_id OR
       NEW.source_session_id IS NOT OLD.source_session_id OR NEW.source_turn_id IS NOT OLD.source_turn_id OR
       NEW.proposal_id IS NOT OLD.proposal_id OR NEW.request_key IS NOT OLD.request_key OR NEW.request_hash IS NOT OLD.request_hash OR
       NEW.context_json IS NOT OLD.context_json OR NEW.context_hash IS NOT OLD.context_hash OR
       NEW.max_provider_calls IS NOT OLD.max_provider_calls OR NEW.max_comparisons IS NOT OLD.max_comparisons OR
       NEW.duration_seconds IS NOT OLD.duration_seconds OR NEW.created_at IS NOT OLD.created_at OR
       (OLD.authorized_at IS NOT NULL AND (NEW.authorized_at IS NOT OLD.authorized_at OR NEW.expires_at IS NOT OLD.expires_at)) OR
       (OLD.candidate_id IS NOT NULL AND (NEW.candidate_id IS NOT OLD.candidate_id OR NEW.plan_id IS NOT OLD.plan_id OR NEW.preparation_json IS NOT OLD.preparation_json))
       BEGIN SELECT RAISE(ABORT,'Coach CI identity and budget are immutable'); END""",
    """CREATE TRIGGER trg_coach_ci_transition BEFORE UPDATE ON coach_ci_delegation WHEN NOT (
       NEW.row_version=OLD.row_version+1 AND (
         (OLD.state='requested' AND NEW.state IN ('authorized','revoked')) OR
         (OLD.state='authorized' AND NEW.state IN ('authorized','revoked','exhausted','unknown'))
       )) BEGIN SELECT RAISE(ABORT,'Coach CI transition is invalid'); END""",
    "ALTER TABLE agent_eval_comparison_execution ADD COLUMN coach_delegation_id TEXT REFERENCES coach_ci_delegation(id) ON DELETE RESTRICT",
    "ALTER TABLE agent_eval_comparison_execution ADD COLUMN coach_client_intent_hash TEXT CHECK(coach_client_intent_hash IS NULL OR coach_client_intent_hash LIKE 'sha256:%')",
    """CREATE TRIGGER trg_coach_ci_execution_identity BEFORE UPDATE ON agent_eval_comparison_execution
       WHEN NEW.coach_delegation_id IS NOT OLD.coach_delegation_id OR NEW.coach_client_intent_hash IS NOT OLD.coach_client_intent_hash
       BEGIN SELECT RAISE(ABORT,'Coach CI execution origin is immutable'); END""",
    """CREATE TRIGGER trg_coach_ci_execution_scope BEFORE INSERT ON agent_eval_comparison_execution
       WHEN (NEW.coach_delegation_id IS NULL)!=(NEW.coach_client_intent_hash IS NULL) OR (NEW.coach_delegation_id IS NOT NULL AND NOT EXISTS (
         SELECT 1 FROM coach_ci_delegation g WHERE g.id=NEW.coach_delegation_id AND g.state='authorized'
         AND g.team_id=NEW.team_id AND g.agent_id=NEW.agent_id AND g.owner_user_id=NEW.actor_ref
         AND NEW.actor_kind='user' AND NEW.automation_grant_id IS NULL AND NEW.automation_context_json IS NULL
         AND g.candidate_id=NEW.candidate_id AND g.plan_id=NEW.plan_id
       )) BEGIN SELECT RAISE(ABORT,'Coach CI execution requires exact internal authority'); END""",
    """CREATE TABLE coach_ci_invocation (
       id TEXT PRIMARY KEY CHECK(id LIKE 'acii_%'),
       delegation_id TEXT NOT NULL REFERENCES coach_ci_delegation(id) ON DELETE RESTRICT,
       run_id TEXT NOT NULL REFERENCES agent_eval_run(id) ON DELETE RESTRICT,
       job_id TEXT NOT NULL REFERENCES agent_eval_case_job(id) ON DELETE RESTRICT,
       lease_generation INTEGER NOT NULL CHECK(lease_generation>=1),
       provider TEXT NOT NULL, model TEXT NOT NULL, adapter_contract_key TEXT NOT NULL, request_digest TEXT NOT NULL,
       state TEXT NOT NULL CHECK(state IN ('pending','completed','not_applied','unknown')),
       tokens_input INTEGER, tokens_output INTEGER, tokens_cached INTEGER,
       created_at TEXT NOT NULL, settled_at TEXT, response_digest TEXT, outcome_code TEXT,
       CHECK((state='pending' AND settled_at IS NULL) OR (state!='pending' AND settled_at IS NOT NULL)),
       CHECK(tokens_input IS NULL OR tokens_input>=0), CHECK(tokens_output IS NULL OR tokens_output>=0),
       CHECK(tokens_cached IS NULL OR tokens_cached>=0)
    )""",
    "CREATE INDEX idx_coach_ci_invocation_budget ON coach_ci_invocation(delegation_id,state)",
    """CREATE TRIGGER trg_coach_ci_invocation_identity BEFORE UPDATE ON coach_ci_invocation WHEN
       OLD.state!='pending' OR NEW.state='pending' OR NEW.id IS NOT OLD.id OR NEW.delegation_id IS NOT OLD.delegation_id OR
       NEW.run_id IS NOT OLD.run_id OR NEW.job_id IS NOT OLD.job_id OR NEW.lease_generation IS NOT OLD.lease_generation OR
       NEW.provider IS NOT OLD.provider OR NEW.model IS NOT OLD.model OR NEW.adapter_contract_key IS NOT OLD.adapter_contract_key OR
       NEW.request_digest IS NOT OLD.request_digest OR NEW.created_at IS NOT OLD.created_at
       BEGIN SELECT RAISE(ABORT,'Coach CI invocation identity or settlement is immutable'); END""",
    """CREATE TRIGGER trg_coach_ci_invocation_no_delete BEFORE DELETE ON coach_ci_invocation
       BEGIN SELECT RAISE(ABORT,'Coach CI budget usage cannot be deleted'); END""",
)
