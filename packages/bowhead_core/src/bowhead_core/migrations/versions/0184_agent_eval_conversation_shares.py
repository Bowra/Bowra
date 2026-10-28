"""Record which conversation Turns their authors shared with an Agent's Owner as evaluation-case sources.

A member may let the Owner of the team Agent that answered one of their Turns draft an evaluation
case from it (evaluation-credibility technical design §8, "Whose conversations"). One row is the
author's consent for one Turn; withdrawing deletes it. The row grants nothing by itself: every read
rechecks that the author still owns the Turn, the session is normal and undeleted, the author is
still an active member of an active Space, and the Turn is still bound to the same Agent.
"""

from alembic import context

revision = "0184_agent_eval_conversation_shares"
down_revision = "0183_agent_eval_point_examples"
branch_labels = None
depends_on = None

_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS agent_eval_conversation_share (
      turn_id   TEXT PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
      team_id   TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
      agent_id  TEXT NOT NULL CHECK(length(agent_id) BETWEEN 1 AND 200),
      user_id   TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
      shared_at TEXT NOT NULL
    )
    """,
    # The Owner's candidate list reads one Agent's shares, newest first.
    """
    CREATE INDEX IF NOT EXISTS idx_agent_eval_conversation_share_agent
    ON agent_eval_conversation_share(team_id, agent_id, shared_at)
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_eval_conversation_share_user ON agent_eval_conversation_share(user_id)",
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    for statement in _STATEMENTS:
        db.exec_driver_sql(statement)


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
