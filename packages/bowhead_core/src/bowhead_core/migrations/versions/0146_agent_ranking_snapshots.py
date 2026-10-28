"""Record conservative ranking coverage and immutable aggregate snapshots."""

from alembic import context

revision = "0146_agent_ranking_snapshots"
down_revision = "0145_initial_eval_suite_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT agent_rankings")
    try:
        for statement in _DDL:
            db.exec_driver_sql(statement)
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT agent_rankings")
        db.exec_driver_sql("RELEASE SAVEPOINT agent_rankings")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT agent_rankings")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")


_DDL = (
    """CREATE TABLE agent_ranking_coverage (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        complete_days_from TEXT NOT NULL
    )""",
    # Historical rows predate this producer's qualification contract. Never
    # claim complete historical coverage from the oldest surviving execution.
    "INSERT INTO agent_ranking_coverage VALUES(1,strftime('%Y-%m-%dT00:00:00+00:00','now','start of day','+1 day'))",
    """CREATE TABLE agent_ranking_snapshot (
        id TEXT PRIMARY KEY CHECK(id LIKE 'ars_%'),
        team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
        ranking_kind TEXT NOT NULL CHECK(ranking_kind IN ('popular','trending')),
        domain_id TEXT,
        window_start TEXT NOT NULL,
        window_end TEXT NOT NULL,
        algorithm_version INTEGER NOT NULL CHECK(algorithm_version=1),
        privacy_policy_version INTEGER NOT NULL CHECK(privacy_policy_version=1),
        input_watermark TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('ready','insufficient_sample')),
        rows_json TEXT NOT NULL CHECK(json_valid(rows_json) AND json_type(rows_json)='array'),
        generated_at TEXT NOT NULL,
        CHECK(state='ready' OR json_array_length(rows_json)=0)
    )""",
    "CREATE UNIQUE INDEX uq_agent_ranking_snapshot_input ON agent_ranking_snapshot(team_id,ranking_kind,ifnull(domain_id,''),window_end,input_watermark)",
    "CREATE INDEX idx_agent_ranking_snapshot_retention ON agent_ranking_snapshot(generated_at)",
    """CREATE INDEX idx_agent_ranking_completed_execution ON turn_agent_execution(team_id,julianday(ended_at))
       WHERE status='completed' AND parent_execution_id IS NULL""",
    """CREATE TRIGGER trg_agent_ranking_snapshot_immutable BEFORE UPDATE ON agent_ranking_snapshot
       BEGIN SELECT RAISE(ABORT,'Agent ranking snapshots are immutable'); END""",
)
