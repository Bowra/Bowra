"""Store calibration examples and the calibration outcome of expected evaluation points.

A model-graded point may block a release only after it judges a good answer as met and a bad one
as not met, under its current text, judge prompt and evaluation model (evaluation-credibility
technical design §7). Examples belong to the point inside its suite; the outcome row keeps the
last calibration's identity, a failure reason and Coach's suggested rewording.
"""

from alembic import context

revision = "0183_agent_eval_point_examples"
down_revision = "0182_agent_version_notification_events"
branch_labels = None
depends_on = None

_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS agent_eval_point_example (
      id                       TEXT PRIMARY KEY CHECK(id LIKE 'aepx_%'),
      team_id                  TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
      agent_id                 TEXT NOT NULL,
      suite_id                 TEXT NOT NULL REFERENCES agent_eval_suite(id) ON DELETE RESTRICT,
      point_id                 TEXT NOT NULL CHECK(length(point_id) BETWEEN 6 AND 18 AND point_id LIKE 'p_%'),
      case_revision_id         TEXT NOT NULL REFERENCES agent_eval_case_revision(id) ON DELETE RESTRICT,
      kind                     TEXT NOT NULL CHECK(kind IN ('good','bad','judged_wrongly')),
      answer_ref               TEXT NOT NULL CHECK(answer_ref LIKE 'attempt:%' OR answer_ref LIKE 'coach:%'),
      answer_text              TEXT CHECK(answer_text IS NULL OR length(answer_text) <= 12000),
      expected_met             INTEGER NOT NULL CHECK(expected_met IN (0,1)),
      graded_text_hash         TEXT,
      graded_prompt_version    TEXT,
      graded_model_fingerprint TEXT,
      graded_met               INTEGER CHECK(graded_met IS NULL OR graded_met IN (0,1)),
      graded_at                TEXT,
      created_by               TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
      created_at               TEXT NOT NULL,
      CHECK((answer_ref LIKE 'coach:%') = (answer_text IS NOT NULL)),
      CHECK(kind != 'good' OR expected_met = 1),
      CHECK(kind != 'bad' OR expected_met = 0)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_eval_point_example_point ON agent_eval_point_example(suite_id, point_id)",
    # Marking the same answer twice for one point records one example.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_eval_point_example_answer
    ON agent_eval_point_example(suite_id, point_id, kind, answer_ref)
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_eval_point_calibration (
      suite_id          TEXT NOT NULL REFERENCES agent_eval_suite(id) ON DELETE RESTRICT,
      point_id          TEXT NOT NULL,
      team_id           TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
      agent_id          TEXT NOT NULL,
      text_hash         TEXT NOT NULL,
      prompt_version    TEXT NOT NULL,
      model_fingerprint TEXT NOT NULL,
      state             TEXT NOT NULL CHECK(state IN ('calibrated','failed')),
      failure_reason    TEXT CHECK((state = 'failed') = (failure_reason IS NOT NULL)),
      rewording         TEXT CHECK(rewording IS NULL OR length(rewording) <= 300),
      updated_at        TEXT NOT NULL,
      PRIMARY KEY(suite_id, point_id)
    )
    """,
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    for statement in _STATEMENTS:
        db.exec_driver_sql(statement)


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
