"""Record the files and reports each person pins to the sidebar (result pins).

Revision ID: 0232_user_result_pins
Revises: 0231_user_settings_key_fence

docs/sprints/result-pins-2026-11-24/tech-design.md §3. One row per person and pinned target: a file
is pinned by its lineage root (the first version's artifact ID), so the pin keeps opening the newest
live version after older versions are pruned; a report is pinned by its canvas ID. No title or
snapshot is stored. The table sits in the execution file beside ``user_session_pins``; the run-data
cleanup reads it as a reference, and account erasure deletes a person's rows. A retired account
cannot receive a new row, through the same reviewed guard as ``user_session_pins``.
"""

from alembic import context

revision = "0232_user_result_pins"
down_revision = "0231_user_settings_key_fence"
branch_labels = None
depends_on = None

TABLE = """CREATE TABLE user_result_pins (
    user_id      TEXT NOT NULL,
    target_kind  TEXT NOT NULL CHECK(target_kind IN ('file', 'report')),
    target_id    TEXT NOT NULL,
    pinned_at    TEXT NOT NULL,
    PRIMARY KEY (user_id, target_kind, target_id)
)"""

TARGET_INDEX = "CREATE INDEX idx_user_result_pins_target ON user_result_pins(target_kind, target_id)"

# The compiled form of the reviewed guard registered in ``sqlite_domain_guards.json``: the account
# deletion ledger lives in core, so the execution file asks the runtime's domain predicate.
RETIRED_GUARD = (
    'CREATE TRIGGER trg_account_retired_user_result_pins_insert BEFORE INSERT ON "user_result_pins" '
    "WHEN bowhead_domain_predicate_v1('trg_account_retired_user_result_pins_insert',NEW.\"user_id\")=1 "
    "BEGIN SELECT RAISE(ABORT,'account is deleted'); END"
)

STATEMENTS = (TABLE, TARGET_INDEX, RETIRED_GUARD)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "execution":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT user_result_pins")
    try:
        for statement in STATEMENTS:
            db.exec_driver_sql(statement)
        db.exec_driver_sql("RELEASE user_result_pins")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO user_result_pins")
        db.exec_driver_sql("RELEASE user_result_pins")
        raise


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
