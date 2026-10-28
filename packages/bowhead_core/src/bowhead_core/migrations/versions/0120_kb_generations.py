"""KB source bindings, immutable index generations and result provenance."""

from alembic import context

revision = "0120_kb_generations"
down_revision = "0119_subagent_draft_suggestions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    target = context.config.attributes["bowhead_target"]
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT kb_generations")
    try:
        if target == "files":
            db.exec_driver_sql("ALTER TABLE kb_files ADD COLUMN active_generation_id TEXT")
            db.exec_driver_sql("ALTER TABLE kb_files ADD COLUMN source_binding_json TEXT")
            db.exec_driver_sql("ALTER TABLE kb_files ADD COLUMN source_absent INTEGER NOT NULL DEFAULT 0 CHECK(source_absent IN (0,1))")
            db.exec_driver_sql("""CREATE TABLE kb_file_generations (
                generation_id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL REFERENCES kb_files(file_id) ON DELETE CASCADE,
                artifact_id TEXT NOT NULL,
                source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
                expected_chunk_count INTEGER NOT NULL CHECK(expected_chunk_count>0),
                index_version TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('writing','published','abandoned')),
                created_at TEXT NOT NULL,
                published_at TEXT
            )""")
            db.exec_driver_sql("CREATE INDEX idx_kb_generations_file ON kb_file_generations(file_id,created_at)")
            db.exec_driver_sql("""CREATE TRIGGER kb_generation_identity_immutable
                BEFORE UPDATE OF generation_id,file_id,artifact_id,source_sha256,expected_chunk_count,index_version,created_at
                ON kb_file_generations BEGIN SELECT RAISE(ABORT,'KB generation identity is immutable'); END""")
            db.exec_driver_sql("""CREATE TRIGGER kb_generation_terminal
                BEFORE UPDATE OF state ON kb_file_generations
                WHEN OLD.state IN ('published','abandoned') AND NEW.state != OLD.state
                BEGIN SELECT RAISE(ABORT,'KB generation state is terminal'); END""")
        elif target == "core":
            db.exec_driver_sql("ALTER TABLE turn_citations ADD COLUMN kb_generation_id TEXT")
            db.exec_driver_sql("ALTER TABLE turn_citations ADD COLUMN kb_source_check_state TEXT")
            db.exec_driver_sql("""CREATE TABLE turn_kb_source_checks (
                turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
                file_id TEXT NOT NULL,
                generation_id TEXT NOT NULL,
                check_state TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                PRIMARY KEY(turn_id,file_id,generation_id)
            )""")
        elif target == "matrix":
            db.exec_driver_sql("ALTER TABLE kb_matrix_cells ADD COLUMN kb_generation_id TEXT")
            db.exec_driver_sql("ALTER TABLE kb_matrix_cells ADD COLUMN kb_source_check_state TEXT")
        db.exec_driver_sql("RELEASE SAVEPOINT kb_generations")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT kb_generations")
        db.exec_driver_sql("RELEASE SAVEPOINT kb_generations")
        raise


def downgrade() -> None:
    raise NotImplementedError("Forward-only; restore matching database and application snapshots.")
