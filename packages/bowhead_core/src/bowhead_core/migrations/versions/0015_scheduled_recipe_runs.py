"""Bind scheduled jobs to solidified workflows and admit awaiting-input runs.

Adds the ``scheduled_jobs.recipe_ref`` / ``run_params`` columns and widens the
one-in-flight-run guarantee so a run parked on a clarification still blocks a
second concurrent run for the same job.

The partial unique index is DROPped and recreated under the same name on
purpose: ``CREATE UNIQUE INDEX IF NOT EXISTS`` is a silent no-op on an already
deployed database, so an in-place "create" would leave the *old* predicate
(``status = 'running'``) in force and let a cron tick start a second run while
one is awaiting input. Renaming is equally unsafe — ``ScheduledJobStore``
recreates ``idx_sjr_one_running`` with ``IF NOT EXISTS`` on every boot, which
would resurrect the old predicate under the old name.

Revision ID: 0011_scheduled_recipe_runs
Revises: 0010_composer_submission_protocol
Create Date: 2026-08-04
"""

from __future__ import annotations

from alembic import context

revision = "0015_scheduled_recipe_runs"
down_revision = "0014_terminal_decision_recovery"
branch_labels = None
depends_on = None

# Must stay byte-identical to ``scheduled_store._RUNNING_UNIQUE_INDEX`` minus the
# ``IF NOT EXISTS`` clause: the store recreates the index on every boot, so a
# divergent predicate here would give migrated and freshly-initialised databases
# different concurrency semantics.
_RUNNING_UNIQUE_INDEX = "CREATE UNIQUE INDEX idx_sjr_one_running ON scheduled_job_runs(job_id) WHERE status IN ('running','awaiting_input')"

# Resume needs to find the run a clarification belongs to by turn_id; runs are
# otherwise only indexed by (job_id, started_at).
_TURN_INDEX = "CREATE INDEX IF NOT EXISTS idx_sjr_turn ON scheduled_job_runs(turn_id) WHERE turn_id IS NOT NULL"

# Widening the predicate can newly conflict on a database that already carries
# duplicate in-flight rows (unclean shutdown before recover_running_runs ran).
# Keep the earliest row per job and retire the rest, so CREATE UNIQUE INDEX
# cannot IntegrityError and brick the deploy. ``started_at || '#' || id`` is a
# total order, so exactly one row per job survives.
_HEAL_DUPLICATE_IN_FLIGHT_RUNS = """
UPDATE scheduled_job_runs
   SET status = 'failed',
       finished_at = COALESCE(finished_at, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
       error = 'scheduled_run_interrupted'
 WHERE status IN ('running', 'awaiting_input')
   AND (started_at || '#' || id) > (
        SELECT MIN(peer.started_at || '#' || peer.id)
          FROM scheduled_job_runs AS peer
         WHERE peer.job_id = scheduled_job_runs.job_id
           AND peer.status IN ('running', 'awaiting_input')
   )
"""


def _tables() -> set[str]:
    connection = context.get_bind()
    return {str(row[0]) for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(table: str) -> set[str]:
    connection = context.get_bind()
    return {str(row[1]) for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}


def _add_column(table: str, column: str, definition: str) -> None:
    if column in _columns(table):
        return
    context.get_bind().exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return

    tables = _tables()
    if "scheduled_jobs" in tables:
        _add_column("scheduled_jobs", "recipe_ref", "TEXT")
        _add_column("scheduled_jobs", "run_params", "TEXT")

    # scheduled_job_runs is created by ScheduledJobStore.initialize() on a
    # never-started deployment; nothing to widen until it exists.
    if "scheduled_job_runs" not in tables:
        return

    connection = context.get_bind()
    connection.exec_driver_sql(_HEAL_DUPLICATE_IN_FLIGHT_RUNS)
    connection.exec_driver_sql("DROP INDEX IF EXISTS idx_sjr_one_running")
    connection.exec_driver_sql(_RUNNING_UNIQUE_INDEX)
    connection.exec_driver_sql(_TURN_INDEX)


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
