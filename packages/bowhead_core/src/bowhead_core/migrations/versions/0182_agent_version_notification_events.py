"""Admit ``proposal.created`` in the Agent Owner notification outbox.

The outbox is rebuilt by copy: every row is kept, the claim index and every
trigger already on the table (the 0073 guards and the 0179 account-retirement
rewrites) are recreated from their stored SQL. Two guards change on purpose:

- ``event_type`` also admits ``proposal.created``, and a proposal event needs
  no candidate, plan, snapshot or release identity (its proposal id is in the
  payload);
- the recipient-scope trigger accepts an active Team membership as well as the
  legacy ``users.team_id`` home Team, so an Owner who joined the Agent's Team
  as a second Team no longer aborts the transaction that writes the event.

Rows still ``pending`` at upgrade time are settled during the copy as
``delivered`` with outcome ``backfill_skipped``. Nothing consumed the outbox
before this revision, so every such row is a historical notice; the consumer
that ships with it must not deliver months-old readiness notices on first run.
Row count and every other column are preserved.
"""

from datetime import UTC, datetime

from alembic import context

revision = "0182_agent_version_notification_events"
down_revision = "0181_space_instruction_source"
branch_labels = None
depends_on = None

_TABLE = "agent_version_notification_outbox"
_RECIPIENT_TRIGGER = "trg_agent_version_notification_recipient_scope"

# Exactly the table DDL created by 0073; anything else means an unknown schema.
_EXPECTED_DDL = """
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
"""

_NEXT_DDL = """
    CREATE TABLE agent_version_notification_outbox_next (
      id                 TEXT PRIMARY KEY CHECK(id LIKE 'aevn_%'),
      team_id            TEXT NOT NULL REFERENCES teams(id) ON DELETE RESTRICT,
      agent_id           TEXT NOT NULL,
      recipient_user_id  TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
      event_type         TEXT NOT NULL CHECK(event_type IN (
                           'proposal.created',
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
        OR (event_type='proposal.created'
            AND json_type(safe_payload_json, '$.proposal_id') IS 'text')
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
"""

_COLUMNS = (
    "id,team_id,agent_id,recipient_user_id,event_type,candidate_id,plan_id,snapshot_id,release_id,"
    "dedupe_key,safe_payload_json,status,lease_generation,lease_owner,lease_token_hash,lease_expires_at,"
    "attempt_count,available_at,created_at,settled_at,outcome_code"
)

# Pending rows become settled history; every other row and column is copied as is.
_BACKFILL_OUTCOME = "backfill_skipped"
_SELECT_SETTLED = (
    "id,team_id,agent_id,recipient_user_id,event_type,candidate_id,plan_id,snapshot_id,release_id,"
    "dedupe_key,safe_payload_json,"
    "CASE WHEN status='pending' THEN 'delivered' ELSE status END,"
    "lease_generation,lease_owner,lease_token_hash,lease_expires_at,attempt_count,available_at,created_at,"
    "CASE WHEN status='pending' THEN :settled_at ELSE settled_at END,"
    "CASE WHEN status='pending' THEN :outcome ELSE outcome_code END"
)

_RECIPIENT_TRIGGER_SQL = """CREATE TRIGGER trg_agent_version_notification_recipient_scope
    BEFORE INSERT ON agent_version_notification_outbox
    WHEN NOT EXISTS (
      SELECT 1 FROM users recipient
      WHERE recipient.id=NEW.recipient_user_id AND recipient.status='active'
        AND (recipient.team_id=NEW.team_id OR EXISTS (
          SELECT 1 FROM team_members member
          WHERE member.user_id=recipient.id AND member.team_id=NEW.team_id
            AND member.status='active'
        ))
    )
    BEGIN
      SELECT RAISE(ABORT, 'Agent version notification recipient scope mismatch');
    END"""


def _canonical(sql: str) -> str:
    return "".join(sql.lower().split())


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    row = db.exec_driver_sql(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?",
        (_TABLE,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"{_TABLE} is missing; upgrade through 0073 first")
    if "'proposal.created'" in row[0]:
        return
    if _canonical(row[0]) != _canonical(_EXPECTED_DDL):
        raise RuntimeError(f"{_TABLE} does not match the 0073 schema; refusing to rebuild it")
    # Indexes and triggers are recreated from their stored SQL so later rewrites
    # (0179 account retirement) survive the copy; the recipient guard is replaced.
    dependents = db.exec_driver_sql(
        "SELECT type,name,sql FROM sqlite_schema WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name",
        (_TABLE,),
    ).fetchall()
    names = {name for _, name, _ in dependents}
    if _RECIPIENT_TRIGGER not in names or "idx_agent_version_notification_claim" not in names:
        raise RuntimeError(f"{_TABLE} is missing its 0073 index or recipient trigger")
    db.exec_driver_sql("SAVEPOINT agent_version_notification_events")
    try:
        db.exec_driver_sql(_NEXT_DDL)
        db.exec_driver_sql(
            f"INSERT INTO {_TABLE}_next({_COLUMNS}) SELECT {_SELECT_SETTLED} FROM {_TABLE}",
            {"settled_at": datetime.now(UTC).isoformat(), "outcome": _BACKFILL_OUTCOME},
        )
        copied = db.exec_driver_sql(f"SELECT (SELECT COUNT(*) FROM {_TABLE}_next),(SELECT COUNT(*) FROM {_TABLE})").fetchone()
        if copied[0] != copied[1]:
            raise RuntimeError(f"{_TABLE} rebuild lost rows")
        db.exec_driver_sql(f"DROP TABLE {_TABLE}")
        db.exec_driver_sql(f"ALTER TABLE {_TABLE}_next RENAME TO {_TABLE}")
        for kind, name, sql in dependents:
            db.exec_driver_sql(_RECIPIENT_TRIGGER_SQL if kind == "trigger" and name == _RECIPIENT_TRIGGER else sql)
        if db.exec_driver_sql(f"PRAGMA foreign_key_check({_TABLE})").fetchall():
            raise RuntimeError(f"{_TABLE} foreign keys broken by rebuild")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT agent_version_notification_events")
        db.exec_driver_sql("RELEASE SAVEPOINT agent_version_notification_events")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT agent_version_notification_events")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
