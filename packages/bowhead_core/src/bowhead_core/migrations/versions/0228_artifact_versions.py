"""Record file versions: a revised deliverable is a new version of the same file.

Revision ID: 0228_artifact_versions
Revises: 0227_observed_continuation_inputs

docs/sprints/file-versions-2026-11-10/tech-design.md §4. Three nullable columns:
``version_of`` (the lineage's highest row at insert time), ``lineage_id`` (the first
version's ID on every later version) and ``version_no`` (2, 3, …; NULL means 1).
A partial UNIQUE index keeps history linear, and the columns are immutable because
lineage decides who may read a file. There are no foreign keys: purge deletes rows.
"""

from alembic import context

revision = "0228_artifact_versions"
down_revision = "0227_observed_continuation_inputs"
branch_labels = None
depends_on = None

COLUMNS = (
    ("version_of", "TEXT"),
    ("lineage_id", "TEXT"),
    ("version_no", "INTEGER CHECK(version_no IS NULL OR version_no>=2)"),
)
LINEAGE_INDEX = "CREATE INDEX idx_artifacts_lineage ON artifacts(lineage_id)"
VERSION_OF_UNIQUE = "CREATE UNIQUE INDEX uq_artifacts_version_of ON artifacts(version_of) WHERE version_of IS NOT NULL"
VERSION_IMMUTABLE = """CREATE TRIGGER trg_artifact_version_immutable BEFORE UPDATE OF version_of,lineage_id,version_no ON artifacts
WHEN OLD.version_of IS NOT NEW.version_of OR OLD.lineage_id IS NOT NEW.lineage_id OR OLD.version_no IS NOT NEW.version_no
BEGIN SELECT RAISE(ABORT,'artifact_version_immutable'); END"""


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "content":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT artifact_versions")
    try:
        for column, declaration in COLUMNS:
            db.exec_driver_sql(f"ALTER TABLE artifacts ADD COLUMN {column} {declaration}")
        db.exec_driver_sql(LINEAGE_INDEX)
        db.exec_driver_sql(VERSION_OF_UNIQUE)
        db.exec_driver_sql(VERSION_IMMUTABLE)
        db.exec_driver_sql("RELEASE artifact_versions")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO artifact_versions")
        db.exec_driver_sql("RELEASE artifact_versions")
        raise


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
