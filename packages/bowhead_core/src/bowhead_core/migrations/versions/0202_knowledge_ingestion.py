"""Version original upload intent and atomic knowledge membership actions."""
from alembic import context

revision = "0202_knowledge_ingestion"
down_revision = "0201_document_authoring"
branch_labels = None
depends_on = None

OPERATIONS = """CREATE TABLE kb_membership_operations (
 id TEXT PRIMARY KEY,
 action_key TEXT NOT NULL UNIQUE,
 intent_hash TEXT NOT NULL,
 action TEXT NOT NULL CHECK(action IN ('add','remove')),
 actor_user_id TEXT NOT NULL,
 team_id TEXT NOT NULL,
 workspace_id TEXT NOT NULL,
 artifact_id TEXT NOT NULL,
 file_id TEXT NOT NULL REFERENCES kb_files(file_id) ON DELETE CASCADE,
 destination_json TEXT NOT NULL CHECK(json_valid(destination_json)),
 source_fingerprint TEXT NOT NULL,
 action_ref TEXT NOT NULL,
 origin TEXT NOT NULL CHECK(origin IN ('kb_ui','original_request','admin_import')),
 membership_revision INTEGER NOT NULL CHECK(membership_revision>=1),
 state TEXT NOT NULL CHECK(state IN ('applied','removed')),
 created_at TEXT NOT NULL
)"""
UPLOAD_DEFAULT = """CREATE TRIGGER trg_artifact_knowledge_file_only AFTER INSERT ON artifacts
WHEN NEW.knowledge_intent_json IS NULL AND NEW.kind='user_upload'
BEGIN UPDATE artifacts SET knowledge_intent_json=CASE WHEN NEW.source IN ('kb_upload','kb_bundle_upload') THEN '{"schema_version":1,"mode":"add_to_knowledge","origin":"kb_ui"}' ELSE '{"schema_version":1,"mode":"file_only","origin":"ordinary_upload"}' END WHERE id=NEW.id; END"""
UPLOAD_IMMUTABLE = """CREATE TRIGGER trg_artifact_knowledge_intent_immutable BEFORE UPDATE OF knowledge_intent_json ON artifacts
WHEN OLD.knowledge_intent_json IS NOT NULL AND NEW.knowledge_intent_json IS NOT OLD.knowledge_intent_json
BEGIN SELECT RAISE(ABORT,'knowledge_upload_intent_immutable'); END"""


def upgrade() -> None:
    db = context.get_bind()
    target = context.config.attributes["bowhead_target"]
    db.exec_driver_sql("SAVEPOINT knowledge_ingestion")
    try:
        if target == "core":
            db.exec_driver_sql("ALTER TABLE turns ADD COLUMN knowledge_user_request INTEGER NOT NULL DEFAULT 0 CHECK(knowledge_user_request IN (0,1))")
            db.exec_driver_sql("ALTER TABLE artifacts ADD COLUMN knowledge_intent_json TEXT CHECK(knowledge_intent_json IS NULL OR COALESCE((json_valid(knowledge_intent_json) AND json_type(knowledge_intent_json,'$.schema_version')='integer' AND json_extract(knowledge_intent_json,'$.schema_version')=1 AND json_extract(knowledge_intent_json,'$.mode') IN ('file_only','add_to_knowledge') AND json_extract(knowledge_intent_json,'$.origin') IN ('ordinary_upload','kb_ui','original_request','admin_import') AND json_remove(knowledge_intent_json,'$.schema_version','$.mode','$.origin')='{}'),0))")
            db.exec_driver_sql(UPLOAD_DEFAULT)
            db.exec_driver_sql(UPLOAD_IMMUTABLE)
        elif target == "files":
            db.exec_driver_sql("ALTER TABLE kb_files ADD COLUMN membership_revision INTEGER NOT NULL DEFAULT 1 CHECK(membership_revision>=1)")
            db.exec_driver_sql("ALTER TABLE kb_files ADD COLUMN ingestion_action_id TEXT")
            db.exec_driver_sql(OPERATIONS)
            db.exec_driver_sql("CREATE INDEX idx_kb_membership_operations_file ON kb_membership_operations(file_id,created_at)")
        db.exec_driver_sql("RELEASE knowledge_ingestion")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO knowledge_ingestion")
        db.exec_driver_sql("RELEASE knowledge_ingestion")
        raise


def downgrade() -> None:
    raise NotImplementedError("Restore the paired database snapshot to roll back.")
