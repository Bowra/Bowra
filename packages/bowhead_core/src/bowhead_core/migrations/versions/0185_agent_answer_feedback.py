"""Record a member's thumbs-down on an answer a team Agent gave them.

One row per Turn: the author's score (only -1 exists), optional reason codes and comment. The row is
the author's own feedback; the Agent's Owner reads its reasons and comment only while the author's
`agent_eval_conversation_share` row for the same Turn exists, and every read rechecks the author's
access to the Turn (evaluation-credibility technical design §8). `owner_handled_at` and
`owner_handled_action` record when the Owner took a shared thumbs-down off their Pending list.
Clearing the thumbs-down deletes the row.

The existing `POST /turns/{id}/satisfaction` writes personal operational memory instead; that store is
private to its author and is not read here.
"""

from alembic import context

revision = "0185_agent_answer_feedback"
down_revision = "0184_agent_eval_conversation_shares"
branch_labels = None
depends_on = None

_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS agent_answer_feedback (
      turn_id              TEXT PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
      team_id              TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
      agent_id             TEXT NOT NULL CHECK(length(agent_id) BETWEEN 1 AND 200),
      user_id              TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
      score                INTEGER NOT NULL CHECK(score=-1),
      reason_codes         TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(reason_codes) AND json_type(reason_codes)='array'),
      comment              TEXT CHECK(comment IS NULL OR length(comment) BETWEEN 1 AND 500),
      created_at           TEXT NOT NULL,
      updated_at           TEXT NOT NULL,
      owner_handled_at     TEXT,
      owner_handled_action TEXT CHECK(owner_handled_action IS NULL OR owner_handled_action IN ('dismissed','coach')),
      CHECK((owner_handled_at IS NULL)=(owner_handled_action IS NULL))
    )
    """,
    # The Owner's Pending list reads one Agent's unhandled feedback.
    """
    CREATE INDEX IF NOT EXISTS idx_agent_answer_feedback_agent
    ON agent_answer_feedback(team_id, agent_id, owner_handled_at)
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_answer_feedback_user ON agent_answer_feedback(user_id)",
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    for statement in _STATEMENTS:
        db.exec_driver_sql(statement)


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
