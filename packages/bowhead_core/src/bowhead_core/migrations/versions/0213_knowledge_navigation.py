"""Store versioned source navigation and bounded description work."""

from alembic import context

revision = "0213_knowledge_navigation"
down_revision = "0212_document_word_authoring"
branch_labels = None
depends_on = None

STATEMENTS = (
    """CREATE TABLE kb_source_navigation (
        file_id TEXT PRIMARY KEY REFERENCES kb_files(file_id) ON DELETE CASCADE,
        generation_id TEXT,
        membership_revision INTEGER NOT NULL,
        labels_override INTEGER NOT NULL DEFAULT 0 CHECK(labels_override IN (0,1)),
        revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0)
    )""",
    """CREATE TABLE kb_source_descriptions (
        id TEXT PRIMARY KEY,
        file_id TEXT NOT NULL REFERENCES kb_files(file_id) ON DELETE CASCADE,
        team_id TEXT NOT NULL,
        membership_revision INTEGER NOT NULL,
        generation_id TEXT,
        source_version INTEGER NOT NULL,
        origin_key TEXT NOT NULL,
        method TEXT NOT NULL CHECK(method IN ('manual','standalone','matrix')),
        schema_version INTEGER NOT NULL DEFAULT 1 CHECK(schema_version=1),
        generator_version INTEGER,
        revision INTEGER NOT NULL CHECK(revision>=1),
        text TEXT NOT NULL CHECK(length(text)<=500),
        topics_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(topics_json)),
        chunks_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(chunks_json)),
        coverage TEXT NOT NULL DEFAULT 'sampled',
        provider TEXT,
        model TEXT,
        matrix_workspace_id TEXT,
        matrix_json TEXT CHECK(matrix_json IS NULL OR json_valid(matrix_json)),
        actor_user_id TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(file_id,origin_key)
    )""",
    "CREATE INDEX idx_kb_source_descriptions_scope ON kb_source_descriptions(team_id,matrix_workspace_id,file_id,generation_id)",
    """CREATE TABLE kb_source_categories (
        id TEXT PRIMARY KEY,
        team_id TEXT NOT NULL,
        scope TEXT NOT NULL CHECK(scope IN ('team','space','personal')),
        scope_id TEXT NOT NULL,
        name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
        aliases_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(aliases_json)),
        parent_id TEXT REFERENCES kb_source_categories(id),
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
        actor_user_id TEXT NOT NULL,
        UNIQUE(team_id,scope,scope_id,name)
    )""",
    "CREATE INDEX idx_kb_source_categories_scope ON kb_source_categories(team_id,scope,scope_id)",
    """CREATE TABLE kb_source_category_files (
        file_id TEXT NOT NULL REFERENCES kb_files(file_id) ON DELETE CASCADE,
        category_id TEXT NOT NULL REFERENCES kb_source_categories(id) ON DELETE CASCADE,
        generation_id TEXT,
        membership_revision INTEGER NOT NULL,
        PRIMARY KEY(file_id,category_id)
    )""",
    "CREATE INDEX idx_kb_source_category_files_category ON kb_source_category_files(category_id,file_id)",
    """CREATE TABLE kb_source_description_jobs (
        id TEXT PRIMARY KEY,
        team_id TEXT NOT NULL,
        file_id TEXT NOT NULL REFERENCES kb_files(file_id) ON DELETE CASCADE,
        admission_key TEXT NOT NULL,
        actor_user_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        generation_id TEXT NOT NULL,
        source_version INTEGER NOT NULL CHECK(source_version>=1),
        membership_revision INTEGER NOT NULL,
        manual_revision INTEGER NOT NULL,
        assignment_fingerprint TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','claimed','dispatching','ready','failed','superseded')),
        lease_owner TEXT,
        lease_until REAL,
        reserved_attempts INTEGER NOT NULL DEFAULT 0 CHECK(reserved_attempts BETWEEN 0 AND 2),
        usage_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(usage_json)),
        error_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(team_id,admission_key)
    )""",
    "CREATE INDEX idx_kb_source_description_jobs_pending ON kb_source_description_jobs(state,lease_until,created_at)",
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "files":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT knowledge_navigation")
    try:
        for statement in STATEMENTS:
            db.exec_driver_sql(statement)
        db.exec_driver_sql("RELEASE knowledge_navigation")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO knowledge_navigation")
        db.exec_driver_sql("RELEASE knowledge_navigation")
        raise


def downgrade() -> None:
    raise NotImplementedError("Restore the paired database snapshot to roll back.")
