"""Internal document admission journal; no runner, file scans or backfill."""

from __future__ import annotations

from alembic import context

revision = "0047_document_admission"
down_revision = "0046_artifact_purge_claims"
branch_labels = None
depends_on = None

_OBJECTS = (
    (
        "table",
        "document_jobs",
        """CREATE TABLE document_jobs (
        id TEXT PRIMARY KEY NOT NULL,
        team_id TEXT NOT NULL, workspace_id TEXT NOT NULL, requested_by TEXT NOT NULL,
        session_id TEXT NOT NULL, origin_turn_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK(action='generate'),
        idempotency_key TEXT NOT NULL,
        request_json TEXT NOT NULL CHECK(json_valid(request_json) AND json_type(request_json)='object'),
        request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
        user_source_sha256 TEXT NOT NULL CHECK(length(user_source_sha256)=64),
        proposal_revision INTEGER NOT NULL DEFAULT 1 CHECK(proposal_revision=1),
        status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','cancelled')),
        phase TEXT NOT NULL DEFAULT 'resolving' CHECK(phase IN ('resolving','terminal')),
        cancel_reason TEXT CHECK(cancel_reason IN ('user_requested','session_deleted')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(team_id,workspace_id,requested_by,idempotency_key),
        UNIQUE(id,team_id,workspace_id,requested_by),
        CHECK((status='queued' AND phase='resolving' AND cancel_reason IS NULL)
           OR (status='cancelled' AND phase='terminal' AND cancel_reason IS NOT NULL))
    )""",
    ),
    (
        "table",
        "document_command_receipts",
        """CREATE TABLE document_command_receipts (
        team_id TEXT NOT NULL, workspace_id TEXT NOT NULL, owner_user_id TEXT NOT NULL,
        command_id TEXT NOT NULL, command_kind TEXT NOT NULL CHECK(command_kind IN ('submit','cancel')),
        request_hash TEXT NOT NULL CHECK(length(request_hash)=64), target_id TEXT NOT NULL,
        result_json TEXT NOT NULL CHECK(json_valid(result_json) AND json_type(result_json)='object'),
        created_at TEXT NOT NULL,
        PRIMARY KEY(team_id,workspace_id,owner_user_id,command_id),
        FOREIGN KEY(target_id,team_id,workspace_id,owner_user_id)
            REFERENCES document_jobs(id,team_id,workspace_id,requested_by)
    )""",
    ),
    (
        "table",
        "document_job_links",
        """CREATE TABLE document_job_links (
        job_id TEXT NOT NULL, team_id TEXT NOT NULL, workspace_id TEXT NOT NULL, owner_user_id TEXT NOT NULL,
        session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
        relation TEXT NOT NULL CHECK(relation='submitted'), command_id TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,command_id,relation),
        FOREIGN KEY(job_id,team_id,workspace_id,owner_user_id)
            REFERENCES document_jobs(id,team_id,workspace_id,requested_by),
        FOREIGN KEY(team_id,workspace_id,owner_user_id,command_id)
            REFERENCES document_command_receipts(team_id,workspace_id,owner_user_id,command_id)
    )""",
    ),
    (
        "table",
        "document_job_events",
        """CREATE TABLE document_job_events (
        job_id TEXT NOT NULL REFERENCES document_jobs(id), event_no INTEGER NOT NULL,
        attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt=0),
        proposal_revision INTEGER NOT NULL DEFAULT 1 CHECK(proposal_revision=1),
        type TEXT NOT NULL CHECK(type IN ('job.created','job.cancelled')),
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'),
        created_at TEXT NOT NULL, PRIMARY KEY(job_id,event_no),
        CHECK((event_no=1 AND type='job.created') OR (event_no=2 AND type='job.cancelled'))
    )""",
    ),
    (
        "index",
        "idx_document_links_session",
        """CREATE INDEX idx_document_links_session
        ON document_job_links(team_id,workspace_id,owner_user_id,session_id,job_id)""",
    ),
    (
        "index",
        "idx_document_jobs_session_queued",
        """CREATE INDEX idx_document_jobs_session_queued
        ON document_jobs(session_id) WHERE status='queued'""",
    ),
    (
        "trigger",
        "trg_document_job_admission",
        """CREATE TRIGGER trg_document_job_admission BEFORE INSERT ON document_jobs
        WHEN NEW.status<>'queued' OR NEW.phase<>'resolving'
          OR EXISTS(SELECT 1 FROM document_jobs WHERE id=NEW.id OR
            (team_id=NEW.team_id AND workspace_id=NEW.workspace_id AND requested_by=NEW.requested_by AND idempotency_key=NEW.idempotency_key))
          OR NOT EXISTS(SELECT 1 FROM chat_turn_user_sources u JOIN turns t ON t.id=u.turn_id
            JOIN sessions s ON s.id=t.session_id JOIN workspaces w ON w.id=s.workspace_id
            JOIN teams team ON team.id=w.team_id JOIN users actor ON actor.id=s.user_id
            JOIN team_members tm ON tm.team_id=w.team_id AND tm.user_id=s.user_id
            JOIN workspace_members wm ON wm.workspace_id=w.id AND wm.user_id=s.user_id
            WHERE u.turn_id=NEW.origin_turn_id AND u.session_id=NEW.session_id AND s.id=NEW.session_id
              AND u.user_id=NEW.requested_by AND s.user_id=NEW.requested_by
              AND u.workspace_id=NEW.workspace_id AND s.workspace_id=NEW.workspace_id
              AND w.team_id=NEW.team_id AND u.user_text=t.user_text
              AND t.status='running' AND s.deleted_at IS NULL AND w.status='active'
              AND team.status='active' AND actor.status='active' AND tm.status='active' AND wm.status='active')
        BEGIN SELECT RAISE(ABORT,'document admission unavailable'); END""",
    ),
    (
        "trigger",
        "trg_document_job_identity",
        """CREATE TRIGGER trg_document_job_identity BEFORE UPDATE ON document_jobs
        WHEN NEW.id IS NOT OLD.id OR NEW.team_id IS NOT OLD.team_id OR NEW.workspace_id IS NOT OLD.workspace_id
          OR NEW.requested_by IS NOT OLD.requested_by OR NEW.session_id IS NOT OLD.session_id
          OR NEW.origin_turn_id IS NOT OLD.origin_turn_id OR NEW.action IS NOT OLD.action
          OR NEW.idempotency_key IS NOT OLD.idempotency_key OR NEW.request_json IS NOT OLD.request_json
          OR NEW.request_hash IS NOT OLD.request_hash OR NEW.user_source_sha256 IS NOT OLD.user_source_sha256
          OR NEW.proposal_revision IS NOT OLD.proposal_revision OR NEW.created_at IS NOT OLD.created_at
          OR OLD.status<>'queued' OR NEW.status<>'cancelled'
        BEGIN SELECT RAISE(ABORT,'document request is immutable'); END""",
    ),
    (
        "trigger",
        "trg_document_job_created",
        """CREATE TRIGGER trg_document_job_created AFTER INSERT ON document_jobs
        BEGIN INSERT INTO document_job_events(job_id,event_no,type,payload_json,created_at)
          VALUES(NEW.id,1,'job.created','{}',NEW.created_at); END""",
    ),
    (
        "trigger",
        "trg_document_job_cancelled",
        """CREATE TRIGGER trg_document_job_cancelled AFTER UPDATE OF status ON document_jobs
        WHEN OLD.status='queued' AND NEW.status='cancelled'
        BEGIN INSERT INTO document_job_events(job_id,event_no,type,payload_json,created_at)
          VALUES(NEW.id,2,'job.cancelled',json_object('reason',NEW.cancel_reason),NEW.updated_at); END""",
    ),
    (
        "trigger",
        "trg_document_link_scope",
        """CREATE TRIGGER trg_document_link_scope BEFORE INSERT ON document_job_links
        WHEN NOT EXISTS(SELECT 1 FROM document_jobs j JOIN document_command_receipts r ON r.target_id=j.id
            AND r.team_id=j.team_id AND r.workspace_id=j.workspace_id AND r.owner_user_id=j.requested_by
            WHERE j.id=NEW.job_id AND j.team_id=NEW.team_id AND j.workspace_id=NEW.workspace_id
              AND j.requested_by=NEW.owner_user_id AND j.session_id=NEW.session_id AND j.origin_turn_id=NEW.turn_id
              AND r.command_id=NEW.command_id AND r.command_kind='submit')
        BEGIN SELECT RAISE(ABORT,'document link scope mismatch'); END""",
    ),
    (
        "trigger",
        "trg_document_session_deleted",
        """CREATE TRIGGER trg_document_session_deleted AFTER UPDATE OF deleted_at ON sessions
        WHEN OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL
        BEGIN UPDATE document_jobs SET status='cancelled',phase='terminal',cancel_reason='session_deleted',
            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE session_id=OLD.id AND status='queued'; END""",
    ),
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    objects = list(_OBJECTS)
    for table, key in (
        ("document_jobs", "id=NEW.id"),
        (
            "document_command_receipts",
            "team_id=NEW.team_id AND workspace_id=NEW.workspace_id AND owner_user_id=NEW.owner_user_id AND command_id=NEW.command_id",
        ),
        ("document_job_links", "job_id=NEW.job_id AND command_id=NEW.command_id AND relation=NEW.relation"),
        ("document_job_events", "job_id=NEW.job_id AND event_no=NEW.event_no"),
    ):
        operations = ("DELETE",) if table == "document_jobs" else ("UPDATE", "DELETE")
        for operation in operations:
            name = f"trg_{table}_{operation.lower()}"
            objects.append(
                (
                    "trigger",
                    name,
                    f"CREATE TRIGGER {name} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'document journal is retained'); END",
                )
            )
        name = f"trg_{table}_no_replace"
        objects.append(
            (
                "trigger",
                name,
                f"CREATE TRIGGER {name} BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE {key}) BEGIN SELECT RAISE(ABORT,'document journal identity exists'); END",
            )
        )
    connection = context.get_bind()
    for kind, name, ddl in objects:
        sql = connection.exec_driver_sql("SELECT sql FROM sqlite_schema WHERE type=? AND name=?", (kind, name)).scalar()
        if sql is None:
            connection.exec_driver_sql(ddl)
        elif "".join(sql.lower().split()) != "".join(ddl.lower().split()):
            raise RuntimeError(f"document admission schema drift: {name}")


def downgrade() -> None:
    raise NotImplementedError("Forward-only migration; restore a matching database backup.")
