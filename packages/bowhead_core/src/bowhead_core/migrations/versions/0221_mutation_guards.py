"""Replace broad authority writer gates with actual destructive mutations.

Revision ID: 0221_mutation_guards
Revises: 0220_sqlite_domains
"""

from alembic import context

revision = "0221_mutation_guards"
down_revision = "0220_sqlite_domains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from bowhead_core.sqlite_mutation_guards import mutation_triggers

    domain = context.config.attributes["bowhead_target"]
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT mutation_guards")
    try:
        names = db.exec_driver_sql("SELECT name FROM sqlite_schema WHERE type='trigger' AND name GLOB 'trg_domain_authority_*'").fetchall()
        for (name,) in names:
            if not name.replace("_", "").isalnum():
                raise RuntimeError("Unknown SQLite authority trigger identity")
            db.exec_driver_sql(f'DROP TRIGGER "{name}"')
        for statement in mutation_triggers(domain):
            db.exec_driver_sql(statement)
        db.exec_driver_sql("RELEASE mutation_guards")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO mutation_guards")
        db.exec_driver_sql("RELEASE mutation_guards")
        raise


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
