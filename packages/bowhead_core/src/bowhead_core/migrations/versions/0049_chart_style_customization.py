"""Add the chart-style V2 persistence and renderer claim foundation."""

from __future__ import annotations

from alembic import context

revision = "0049_chart_style_customization"
down_revision = "0048_document_inputs"
branch_labels = None
depends_on = None

_EXPORT_JOBS_CREATE = """CREATE TABLE canvas_export_jobs (
    id TEXT PRIMARY KEY, canvas_id TEXT NOT NULL, fmt TEXT NOT NULL, status TEXT NOT NULL,
    url TEXT, artifact_id TEXT, filename TEXT, size INTEGER, error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    contract_version INTEGER NOT NULL DEFAULT 1, profile TEXT, parity_contract TEXT, surface_token TEXT,
    phase TEXT NOT NULL DEFAULT 'queued', quality_status TEXT NOT NULL DEFAULT 'pending',
    warning_count INTEGER NOT NULL DEFAULT 0, snapshot_id TEXT, manifest_artifact_id TEXT,
    requested_by TEXT, idempotency_key TEXT, request_sha256 TEXT, error_code TEXT, progress_json TEXT,
    attempt INTEGER NOT NULL DEFAULT 0, worker_id TEXT, runner_kind TEXT, runner_task_id TEXT,
    lease_expires_at TEXT, started_at TEXT, finished_at TEXT, expires_at TEXT,
    renderer_contract_version INTEGER NOT NULL DEFAULT 1
)"""

_OBJECTS = (
    (
        "table",
        "team_chart_styles",
        """CREATE TABLE team_chart_styles (
            id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=28 AND substr(id,1,4)='tcs_' AND substr(id,5) NOT GLOB '*[^0-9a-f]*'),
            team_id TEXT NOT NULL,
            lifecycle TEXT NOT NULL CHECK(lifecycle IN ('active','inactive')),
            active_revision_id TEXT,
            active_name_normalized TEXT,
            fallback_builtin_preset_id TEXT NOT NULL CHECK(length(fallback_builtin_preset_id)>0),
            row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version>=1),
            create_request_id TEXT,
            create_request_hash TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(team_id,id),
            UNIQUE(team_id,created_by,create_request_id),
            CHECK((create_request_id IS NULL)=(create_request_hash IS NULL)),
            CHECK((active_revision_id IS NULL)=(active_name_normalized IS NULL))
        )""",
    ),
    (
        "table",
        "team_chart_style_revisions",
        """CREATE TABLE team_chart_style_revisions (
            id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=29 AND substr(id,1,5)='tcsr_' AND substr(id,6) NOT GLOB '*[^0-9a-f]*'),
            team_id TEXT NOT NULL,
            style_id TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK(sequence>=1),
            schema_version INTEGER NOT NULL CHECK(schema_version>=2),
            token_schema_version INTEGER NOT NULL CHECK(token_schema_version>=2),
            resolver_policy_version TEXT NOT NULL,
            name TEXT NOT NULL,
            name_normalized TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            base_preset_id TEXT NOT NULL,
            base_preset_revision TEXT NOT NULL,
            base_preset_checksum TEXT NOT NULL CHECK(base_preset_checksum GLOB 'sha256:*'),
            authoring_base_snapshot_json TEXT NOT NULL CHECK(json_valid(authoring_base_snapshot_json)),
            authoring_base_snapshot_checksum TEXT NOT NULL CHECK(authoring_base_snapshot_checksum GLOB 'sha256:*'),
            overrides_json TEXT NOT NULL CHECK(json_valid(overrides_json)),
            resolved_snapshot_json TEXT NOT NULL CHECK(json_valid(resolved_snapshot_json)),
            snapshot_checksum TEXT NOT NULL CHECK(snapshot_checksum GLOB 'sha256:*'),
            revision_checksum TEXT NOT NULL CHECK(revision_checksum GLOB 'sha256:*'),
            validation_policy_version TEXT NOT NULL,
            validation_json TEXT NOT NULL CHECK(json_valid(validation_json)),
            operation TEXT NOT NULL CHECK(operation IN ('publish','restore')),
            source_revision_id TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(team_id,style_id,id),
            UNIQUE(style_id,sequence),
            CHECK((operation='publish' AND source_revision_id IS NULL) OR (operation='restore' AND source_revision_id IS NOT NULL)),
            FOREIGN KEY(team_id,style_id) REFERENCES team_chart_styles(team_id,id)
        )""",
    ),
    (
        "table",
        "team_chart_style_drafts",
        """CREATE TABLE team_chart_style_drafts (
            style_id TEXT PRIMARY KEY NOT NULL,
            team_id TEXT NOT NULL,
            schema_version INTEGER NOT NULL CHECK(schema_version>=2),
            based_on_revision_id TEXT,
            base_preset_id TEXT NOT NULL,
            base_preset_revision TEXT NOT NULL,
            base_preset_checksum TEXT NOT NULL CHECK(base_preset_checksum GLOB 'sha256:*'),
            authoring_base_snapshot_json TEXT NOT NULL CHECK(json_valid(authoring_base_snapshot_json)),
            authoring_base_snapshot_checksum TEXT NOT NULL CHECK(authoring_base_snapshot_checksum GLOB 'sha256:*'),
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            overrides_json TEXT NOT NULL CHECK(json_valid(overrides_json)),
            row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version>=1),
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(team_id,style_id),
            FOREIGN KEY(team_id,style_id) REFERENCES team_chart_styles(team_id,id) ON DELETE CASCADE
        )""",
    ),
    (
        "table",
        "team_chart_style_registry_state",
        """CREATE TABLE team_chart_style_registry_state (
            team_id TEXT PRIMARY KEY NOT NULL,
            registry_version INTEGER NOT NULL DEFAULT 0 CHECK(registry_version>=0),
            updated_by TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
    ),
    (
        "index",
        "idx_team_chart_styles_active_name",
        """CREATE UNIQUE INDEX idx_team_chart_styles_active_name
            ON team_chart_styles(team_id,active_name_normalized)
            WHERE lifecycle='active' AND active_revision_id IS NOT NULL""",
    ),
    (
        "index",
        "idx_team_chart_styles_list",
        """CREATE INDEX idx_team_chart_styles_list
            ON team_chart_styles(team_id,lifecycle,updated_at DESC,id DESC)""",
    ),
    (
        "index",
        "idx_team_chart_style_revisions_list",
        """CREATE INDEX idx_team_chart_style_revisions_list
            ON team_chart_style_revisions(team_id,style_id,sequence DESC)""",
    ),
    (
        "trigger",
        "trg_team_chart_style_active_revision_insert",
        """CREATE TRIGGER trg_team_chart_style_active_revision_insert BEFORE INSERT ON team_chart_styles
            WHEN NEW.active_revision_id IS NOT NULL
            BEGIN SELECT RAISE(ABORT,'chart style active revision unavailable'); END""",
    ),
    (
        "trigger",
        "trg_team_chart_style_active_revision",
        """CREATE TRIGGER trg_team_chart_style_active_revision BEFORE UPDATE ON team_chart_styles
            WHEN (NEW.active_revision_id IS NULL)<>(NEW.active_name_normalized IS NULL)
              OR (NEW.active_revision_id IS NOT NULL AND NOT EXISTS(
                SELECT 1 FROM team_chart_style_revisions r
                WHERE r.id=NEW.active_revision_id AND r.team_id=NEW.team_id AND r.style_id=NEW.id
                  AND r.name_normalized=NEW.active_name_normalized))
            BEGIN SELECT RAISE(ABORT,'chart style active revision unavailable'); END""",
    ),
    (
        "trigger",
        "trg_team_chart_style_update",
        """CREATE TRIGGER trg_team_chart_style_update BEFORE UPDATE ON team_chart_styles
            WHEN NEW.id IS NOT OLD.id OR NEW.team_id IS NOT OLD.team_id
              OR NEW.fallback_builtin_preset_id IS NOT OLD.fallback_builtin_preset_id
              OR NEW.create_request_id IS NOT OLD.create_request_id OR NEW.create_request_hash IS NOT OLD.create_request_hash
              OR NEW.created_by IS NOT OLD.created_by OR NEW.created_at IS NOT OLD.created_at
              OR NEW.row_version<>OLD.row_version+1
            BEGIN SELECT RAISE(ABORT,'chart style identity is immutable'); END""",
    ),
    (
        "trigger",
        "trg_team_chart_style_revision_scope",
        """CREATE TRIGGER trg_team_chart_style_revision_scope BEFORE INSERT ON team_chart_style_revisions
            WHEN NEW.source_revision_id IS NOT NULL AND NOT EXISTS(
              SELECT 1 FROM team_chart_style_revisions r
              WHERE r.id=NEW.source_revision_id AND r.team_id=NEW.team_id AND r.style_id=NEW.style_id)
            BEGIN SELECT RAISE(ABORT,'chart style source revision unavailable'); END""",
    ),
    (
        "trigger",
        "trg_team_chart_style_revision_update",
        """CREATE TRIGGER trg_team_chart_style_revision_update BEFORE UPDATE ON team_chart_style_revisions
            BEGIN SELECT RAISE(ABORT,'chart style revision is immutable'); END""",
    ),
    (
        "trigger",
        "trg_team_chart_style_revision_delete",
        """CREATE TRIGGER trg_team_chart_style_revision_delete BEFORE DELETE ON team_chart_style_revisions
            BEGIN SELECT RAISE(ABORT,'chart style revision is immutable'); END""",
    ),
    (
        "trigger",
        "trg_team_chart_style_draft_scope",
        """CREATE TRIGGER trg_team_chart_style_draft_scope BEFORE INSERT ON team_chart_style_drafts
            WHEN NEW.based_on_revision_id IS NOT NULL AND NOT EXISTS(
              SELECT 1 FROM team_chart_style_revisions r
              WHERE r.id=NEW.based_on_revision_id AND r.team_id=NEW.team_id AND r.style_id=NEW.style_id)
            BEGIN SELECT RAISE(ABORT,'chart style draft base revision unavailable'); END""",
    ),
    (
        "trigger",
        "trg_team_chart_style_draft_update",
        """CREATE TRIGGER trg_team_chart_style_draft_update BEFORE UPDATE ON team_chart_style_drafts
            WHEN NEW.style_id IS NOT OLD.style_id OR NEW.team_id IS NOT OLD.team_id
              OR NEW.schema_version IS NOT OLD.schema_version OR NEW.based_on_revision_id IS NOT OLD.based_on_revision_id
              OR NEW.base_preset_id IS NOT OLD.base_preset_id OR NEW.base_preset_revision IS NOT OLD.base_preset_revision
              OR NEW.base_preset_checksum IS NOT OLD.base_preset_checksum
              OR NEW.authoring_base_snapshot_json IS NOT OLD.authoring_base_snapshot_json
              OR NEW.authoring_base_snapshot_checksum IS NOT OLD.authoring_base_snapshot_checksum
              OR NEW.created_by IS NOT OLD.created_by OR NEW.created_at IS NOT OLD.created_at
              OR NEW.row_version<>OLD.row_version+1
            BEGIN SELECT RAISE(ABORT,'chart style draft identity is immutable'); END""",
    ),
    (
        "trigger",
        "trg_team_chart_style_published_delete",
        """CREATE TRIGGER trg_team_chart_style_published_delete BEFORE DELETE ON team_chart_styles
            WHEN OLD.active_revision_id IS NOT NULL OR EXISTS(
              SELECT 1 FROM team_chart_style_revisions r WHERE r.team_id=OLD.team_id AND r.style_id=OLD.id)
            BEGIN SELECT RAISE(ABORT,'published chart style is retained'); END""",
    ),
    (
        "trigger",
        "trg_team_chart_style_registry_update",
        """CREATE TRIGGER trg_team_chart_style_registry_update BEFORE UPDATE ON team_chart_style_registry_state
            WHEN NEW.team_id IS NOT OLD.team_id OR NEW.registry_version<>OLD.registry_version+1
            BEGIN SELECT RAISE(ABORT,'chart style registry version must advance once'); END""",
    ),
)

_CLAIM_INDEX = (
    "index",
    "idx_export_jobs_claim_target",
    """CREATE INDEX idx_export_jobs_claim_target
        ON canvas_export_jobs(status,renderer_contract_version,fmt,profile,created_at,id)""",
)


def _normalized(sql: str) -> str:
    return "".join(sql.lower().split())


def _ensure_object(connection: object, kind: str, name: str, ddl: str) -> None:
    sql = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_schema WHERE type=? AND name=?",
        (kind, name),
    ).scalar()
    if sql is None:
        connection.exec_driver_sql(ddl)
    elif _normalized(sql) != _normalized(ddl):
        raise RuntimeError(f"chart style schema drift: {name}")


def _ensure_renderer_contract_column(connection: object) -> None:
    table = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='canvas_export_jobs'"
    ).scalar()
    if table is None:
        connection.exec_driver_sql(_EXPORT_JOBS_CREATE)
        return
    row = connection.exec_driver_sql(
        'SELECT type,"notnull",dflt_value,pk FROM pragma_table_info(\'canvas_export_jobs\') '
        "WHERE name='renderer_contract_version'"
    ).first()
    if row is None:
        connection.exec_driver_sql(
            "ALTER TABLE canvas_export_jobs ADD COLUMN renderer_contract_version INTEGER NOT NULL DEFAULT 1"
        )
    elif tuple(row) != ("INTEGER", 1, "1", 0):
        raise RuntimeError("chart style schema drift: canvas_export_jobs.renderer_contract_version")


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    for kind, name, ddl in _OBJECTS:
        _ensure_object(connection, kind, name, ddl)
    _ensure_renderer_contract_column(connection)
    _ensure_object(connection, *_CLAIM_INDEX)


def downgrade() -> None:
    raise NotImplementedError("Forward-only; retain chart-style revisions and renderer claim identities.")
