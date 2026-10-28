"""Store the Coach alignment loop and widen point calibration examples to chat Turns.

The alignment loop (alignment-loop technical design §3) lets an Agent Owner confirm a point by
answering whether Coach's verdict on a few real answers is right, instead of reviewing point text:

- ``agent_eval_alignment_loop``: one loop per Owner conversation with Coach, with the Owner's words,
  the judge identity pinned at its start and the points being aligned (nothing reaches a suite until
  the Owner's labels confirm a point);
- ``agent_eval_alignment_question``: one row per answer shown on a question card, with Coach's
  verdict and the Owner's label. A row for a chat Turn names it in ``turn_id`` and goes with the Turn;
- ``agent_eval_suite_review``: Coach's advice-only suite review cached per suite revision and prompt.

``agent_eval_point_example`` is rebuilt by copy so ``answer_ref`` also admits ``turn:<turn id>``; a
Turn example stores the question it answered (``task_text``) and names the Turn (``turn_id``, which
cascades). ``label_source`` records where every example came from; historical rows are backfilled
from their kind and answer source. The unique and lookup indexes are recreated; nothing else changes.
"""

from alembic import context

revision = "0186_agent_eval_alignment_loop"
down_revision = "0185_agent_answer_feedback"
branch_labels = None
depends_on = None

_EXAMPLE_TABLE = "agent_eval_point_example"
_SAVEPOINT = "agent_eval_alignment_loop"

# Exactly the table DDL created by 0183 (SQLite drops IF NOT EXISTS when storing it).
_EXPECTED_EXAMPLE_DDL = """
    CREATE TABLE agent_eval_point_example (
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
"""

_NEXT_EXAMPLE_DDL = """
    CREATE TABLE agent_eval_point_example_next (
      id                       TEXT PRIMARY KEY CHECK(id LIKE 'aepx_%'),
      team_id                  TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
      agent_id                 TEXT NOT NULL,
      suite_id                 TEXT NOT NULL REFERENCES agent_eval_suite(id) ON DELETE RESTRICT,
      point_id                 TEXT NOT NULL CHECK(length(point_id) BETWEEN 6 AND 18 AND point_id LIKE 'p_%'),
      case_revision_id         TEXT NOT NULL REFERENCES agent_eval_case_revision(id) ON DELETE RESTRICT,
      kind                     TEXT NOT NULL CHECK(kind IN ('good','bad','judged_wrongly')),
      answer_ref               TEXT NOT NULL CHECK(answer_ref LIKE 'attempt:%' OR answer_ref LIKE 'coach:%' OR answer_ref LIKE 'turn:%'),
      answer_text              TEXT CHECK(answer_text IS NULL OR length(answer_text) <= 12000),
      expected_met             INTEGER NOT NULL CHECK(expected_met IN (0,1)),
      graded_text_hash         TEXT,
      graded_prompt_version    TEXT,
      graded_model_fingerprint TEXT,
      graded_met               INTEGER CHECK(graded_met IS NULL OR graded_met IN (0,1)),
      graded_at                TEXT,
      created_by               TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
      created_at               TEXT NOT NULL,
      task_text                TEXT CHECK(task_text IS NULL OR length(task_text) BETWEEN 1 AND 4000),
      label_source             TEXT NOT NULL CHECK(label_source IN ('marked_good','coach_edit','judged_wrongly','owner_alignment')),
      turn_id                  TEXT REFERENCES turns(id) ON DELETE CASCADE,
      -- A generated answer written from a chat Turn (its question, or a minimal edit of its answer)
      -- goes with that Turn exactly like the Turn's own example.
      source_turn_id           TEXT REFERENCES turns(id) ON DELETE CASCADE,
      CHECK((answer_ref LIKE 'coach:%') = (answer_text IS NOT NULL)),
      CHECK(kind != 'good' OR expected_met = 1),
      CHECK(kind != 'bad' OR expected_met = 0),
      -- A chat Turn's question differs from the case task; the calibrator grades against the question.
      CHECK((answer_ref LIKE 'turn:%') = (turn_id IS NOT NULL)),
      CHECK(turn_id IS NULL OR (answer_ref = 'turn:' || turn_id AND task_text IS NOT NULL)),
      CHECK(source_turn_id IS NULL OR answer_ref LIKE 'coach:%')
    )
"""

_OLD_COLUMNS = (
    "id,team_id,agent_id,suite_id,point_id,case_revision_id,kind,answer_ref,answer_text,expected_met,"
    "graded_text_hash,graded_prompt_version,graded_model_fingerprint,graded_met,graded_at,created_by,created_at"
)
# Every historical example came from one of the three writers that existed before this revision.
_LABEL_SOURCE_BACKFILL = (
    "CASE WHEN kind='judged_wrongly' THEN 'judged_wrongly' WHEN answer_ref LIKE 'coach:%' THEN 'coach_edit' ELSE 'marked_good' END"
)

_NEW_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS agent_eval_alignment_loop (
      id                      TEXT PRIMARY KEY CHECK(id LIKE 'aeal_%'),
      team_id                 TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
      agent_id                TEXT NOT NULL CHECK(length(agent_id) BETWEEN 1 AND 200),
      suite_id                TEXT NOT NULL REFERENCES agent_eval_suite(id) ON DELETE RESTRICT,
      owner_id                TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
      session_id              TEXT NOT NULL CHECK(length(session_id) BETWEEN 1 AND 200),
      -- The Owner's own words. A loop started from a member's thumbs-down stores no comment and no
      -- member id: the comment is read live through the share while it is readable.
      words_source            TEXT NOT NULL CHECK(words_source IN ('owner','member_feedback')),
      owner_words             TEXT CHECK(owner_words IS NULL OR length(owner_words) BETWEEN 1 AND 500),
      words_by                TEXT CHECK(words_by IS NULL OR length(words_by) BETWEEN 1 AND 200),
      origin_turn_id          TEXT CHECK(origin_turn_id IS NULL OR length(origin_turn_id) BETWEEN 1 AND 200),
      judge_prompt_version    TEXT NOT NULL CHECK(length(judge_prompt_version) BETWEEN 1 AND 80),
      judge_model_fingerprint TEXT NOT NULL CHECK(judge_model_fingerprint LIKE 'sha256:%'),
      pinned_at               TEXT NOT NULL,
      state                   TEXT NOT NULL CHECK(state IN ('aligning','fixing','reported','abandoned')),
      baseline_comparison_id  TEXT,
      draft_points_json       TEXT NOT NULL DEFAULT '[]' CHECK(
                                json_valid(draft_points_json) AND json_type(draft_points_json)='array'
                                AND length(draft_points_json) <= 65536
                              ),
      report_json             TEXT CHECK(
                                report_json IS NULL OR (json_valid(report_json) AND json_type(report_json)='object'
                                AND length(report_json) <= 65536)
                              ),
      created_at              TEXT NOT NULL,
      updated_at              TEXT NOT NULL,
      CHECK((words_source = 'owner') = (owner_words IS NOT NULL)),
      CHECK((words_source = 'owner') = (words_by IS NOT NULL)),
      CHECK(words_source = 'owner' OR origin_turn_id IS NOT NULL)
    )
    """,
    # One open loop per Agent and Owner; starting another abandons the old one.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_eval_alignment_loop_open
    ON agent_eval_alignment_loop(team_id, agent_id, owner_id) WHERE state!='abandoned'
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_eval_alignment_loop_owner ON agent_eval_alignment_loop(owner_id)",
    """
    CREATE TABLE IF NOT EXISTS agent_eval_alignment_question (
      id                       TEXT PRIMARY KEY CHECK(id LIKE 'aeaq_%'),
      loop_id                  TEXT NOT NULL REFERENCES agent_eval_alignment_loop(id) ON DELETE CASCADE,
      point_id                 TEXT NOT NULL CHECK(length(point_id) BETWEEN 6 AND 18 AND point_id LIKE 'p_%'),
      point_text_hash          TEXT NOT NULL CHECK(point_text_hash LIKE 'sha256:%'),
      card_seq                 INTEGER NOT NULL CHECK(card_seq >= 1),
      row_index                INTEGER NOT NULL CHECK(row_index BETWEEN 1 AND 5),
      answer_ref               TEXT NOT NULL CHECK(answer_ref LIKE 'turn:%' OR answer_ref LIKE 'attempt:%' OR answer_ref LIKE 'coach:%'),
      turn_id                  TEXT REFERENCES turns(id) ON DELETE CASCADE,
      source_turn_id           TEXT REFERENCES turns(id) ON DELETE CASCADE,
      answer_text              TEXT CHECK(answer_text IS NULL OR length(answer_text) <= 12000),
      task_text                TEXT NOT NULL CHECK(length(task_text) BETWEEN 1 AND 4000),
      answer_excerpt           TEXT NOT NULL CHECK(length(answer_excerpt) <= 1200),
      judge_met                INTEGER CHECK(judge_met IS NULL OR judge_met IN (0,1)),
      judge_quote              TEXT CHECK(judge_quote IS NULL OR length(judge_quote) <= 300),
      judge_reason             TEXT CHECK(judge_reason IS NULL OR length(judge_reason) <= 120),
      graded_prompt_version    TEXT NOT NULL,
      graded_model_fingerprint TEXT NOT NULL CHECK(graded_model_fingerprint LIKE 'sha256:%'),
      pick_reason              TEXT NOT NULL CHECK(pick_reason IN (
                                 'marked_bad','judge_abstained','feedback_conflict','recent','counterexample','generated'
                               )),
      replacement              INTEGER NOT NULL DEFAULT 0 CHECK(replacement IN (0,1)),
      owner_label              TEXT CHECK(owner_label IS NULL OR owner_label IN ('right','wrong','unclear')),
      label_via                TEXT CHECK(label_via IS NULL OR label_via IN ('row','rest_right')),
      labelled_at              TEXT,
      created_at               TEXT NOT NULL,
      CHECK((answer_ref LIKE 'turn:%') = (turn_id IS NOT NULL)),
      CHECK(turn_id IS NULL OR answer_ref = 'turn:' || turn_id),
      CHECK(source_turn_id IS NULL OR answer_ref LIKE 'coach:%'),
      CHECK((answer_ref LIKE 'coach:%') = (answer_text IS NOT NULL)),
      CHECK((owner_label IS NULL) = (label_via IS NULL)),
      CHECK((owner_label IS NULL) = (labelled_at IS NULL)),
      UNIQUE(loop_id, card_seq, row_index)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_eval_alignment_question_point ON agent_eval_alignment_question(loop_id, point_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_eval_alignment_question_turn ON agent_eval_alignment_question(turn_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_eval_alignment_question_source_turn ON agent_eval_alignment_question(source_turn_id)",
    """
    CREATE TABLE IF NOT EXISTS agent_eval_suite_review (
      suite_revision_id TEXT NOT NULL REFERENCES agent_eval_suite_revision(id) ON DELETE CASCADE,
      prompt_version    TEXT NOT NULL CHECK(length(prompt_version) BETWEEN 1 AND 80),
      team_id           TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
      agent_id          TEXT NOT NULL CHECK(length(agent_id) BETWEEN 1 AND 200),
      result_json       TEXT NOT NULL CHECK(
                          json_valid(result_json) AND json_type(result_json)='object' AND length(result_json) <= 65536
                        ),
      created_at        TEXT NOT NULL,
      PRIMARY KEY(suite_revision_id, prompt_version)
    )
    """,
)


def _canonical(sql: str) -> str:
    return "".join(sql.lower().split())


def _rebuild_examples(db) -> None:
    row = db.exec_driver_sql("SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (_EXAMPLE_TABLE,)).fetchone()
    if row is None:
        raise RuntimeError(f"{_EXAMPLE_TABLE} is missing; upgrade through 0183 first")
    if "label_source" in row[0]:
        return
    if _canonical(row[0]) != _canonical(_EXPECTED_EXAMPLE_DDL):
        raise RuntimeError(f"{_EXAMPLE_TABLE} does not match the 0183 schema; refusing to rebuild it")
    dependents = db.exec_driver_sql(
        "SELECT type,name,sql FROM sqlite_schema WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name",
        (_EXAMPLE_TABLE,),
    ).fetchall()
    db.exec_driver_sql(_NEXT_EXAMPLE_DDL)
    db.exec_driver_sql(
        f"INSERT INTO {_EXAMPLE_TABLE}_next({_OLD_COLUMNS},label_source) SELECT {_OLD_COLUMNS},{_LABEL_SOURCE_BACKFILL} FROM {_EXAMPLE_TABLE}"
    )
    copied = db.exec_driver_sql(f"SELECT (SELECT COUNT(*) FROM {_EXAMPLE_TABLE}_next),(SELECT COUNT(*) FROM {_EXAMPLE_TABLE})").fetchone()
    if copied[0] != copied[1]:
        raise RuntimeError(f"{_EXAMPLE_TABLE} rebuild lost rows")
    db.exec_driver_sql(f"DROP TABLE {_EXAMPLE_TABLE}")
    db.exec_driver_sql(f"ALTER TABLE {_EXAMPLE_TABLE}_next RENAME TO {_EXAMPLE_TABLE}")
    for _kind, _name, sql in dependents:
        db.exec_driver_sql(sql)
    # Withdrawing a share and erasing an account find a Turn's examples by these columns.
    db.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS idx_agent_eval_point_example_turn ON {_EXAMPLE_TABLE}(turn_id)")
    db.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS idx_agent_eval_point_example_source_turn ON {_EXAMPLE_TABLE}(source_turn_id)")
    if db.exec_driver_sql(f"PRAGMA foreign_key_check({_EXAMPLE_TABLE})").fetchall():
        raise RuntimeError(f"{_EXAMPLE_TABLE} foreign keys broken by rebuild")


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql(f"SAVEPOINT {_SAVEPOINT}")
    try:
        for statement in _NEW_TABLES:
            db.exec_driver_sql(statement)
        _rebuild_examples(db)
    except BaseException:
        db.exec_driver_sql(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
        db.exec_driver_sql(f"RELEASE SAVEPOINT {_SAVEPOINT}")
        raise
    db.exec_driver_sql(f"RELEASE SAVEPOINT {_SAVEPOINT}")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
