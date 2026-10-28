"""Let the platform contract suite gain revisions, appended on behalf of the Agent's Owner.

0040 refused any second revision of a platform-managed suite, so the platform contract could
never change. The replacement trigger keeps every other rule and still refuses Team admins;
only the Agent's Owner (the actor the platform upgrade runs as) may append the next revision.
Revisions stay immutable and sequential through the existing triggers.
"""

from alembic import context

revision = "0176_platform_contract_revisions"
down_revision = "0175_oauth_callback_uri"
branch_labels = None
depends_on = None

REVISION_SCOPE_TRIGGER = """
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
          LEFT JOIN team_subagent agent
            ON agent.team_id=suite.team_id AND agent.id=suite.agent_id
          WHERE suite.id=NEW.suite_id AND suite.platform_managed=1
            AND EXISTS (
              SELECT 1 FROM agent_eval_suite_revision existing
              WHERE existing.suite_id=suite.id
            )
            AND NEW.created_by IS NOT COALESCE(agent.owner_user_id,agent.created_by)
        )
        BEGIN
          SELECT RAISE(ABORT, 'Agent Eval suite revision scope mismatch');
        END
"""


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT platform_contract_revisions")
    try:
        db.exec_driver_sql("DROP TRIGGER IF EXISTS trg_agent_eval_suite_revision_scope")
        db.exec_driver_sql(REVISION_SCOPE_TRIGGER)
        db.exec_driver_sql("RELEASE SAVEPOINT platform_contract_revisions")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT platform_contract_revisions")
        db.exec_driver_sql("RELEASE SAVEPOINT platform_contract_revisions")
        raise


def downgrade() -> None:
    raise NotImplementedError("Restore matching database snapshots to roll back.")
