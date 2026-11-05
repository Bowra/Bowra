"""Keep internal producer classification out of destructive mutation fences.

Revision ID: 0222_artifact_producer_metadata
Revises: 0221_mutation_guards
"""

from alembic import context

from bowhead_core.sqlite_domain_runtime import _AUTHORITY

revision = "0222_artifact_producer_metadata"
down_revision = "0221_mutation_guards"
branch_labels = None
depends_on = None

_columns = [column for column in _AUTHORITY["content"]["artifacts"] if column not in {"filename", "producer_kind"}]
_names = ",".join(f'"{column}"' for column in _columns)
_changed = " OR ".join(f'OLD."{column}" IS NOT NEW."{column}"' for column in _columns)
ARTIFACT_UPDATE_TRIGGER = (
    f'CREATE TRIGGER "trg_domain_mutation_artifacts_update" BEFORE UPDATE OF {_names} ON "artifacts" WHEN {_changed} '
    "BEGIN SELECT bowhead_domain_mutation_v2('artifacts','update'); END"
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "content":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT artifact_producer_metadata")
    try:
        db.exec_driver_sql('DROP TRIGGER "trg_domain_mutation_artifacts_update"')
        db.exec_driver_sql(ARTIFACT_UPDATE_TRIGGER)
        db.exec_driver_sql("RELEASE artifact_producer_metadata")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO artifact_producer_metadata")
        db.exec_driver_sql("RELEASE artifact_producer_metadata")
        raise


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
