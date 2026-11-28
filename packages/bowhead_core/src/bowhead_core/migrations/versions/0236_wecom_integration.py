"""Add Team-scoped WeCom application, identity and delivery state."""

from alembic import context

revision = "0236_wecom_integration"
down_revision = "0235_teams_personal_chat"
branch_labels = depends_on = None

DDL = (
    """CREATE TABLE wecom_connections (
        id TEXT PRIMARY KEY,
        team_id TEXT NOT NULL UNIQUE REFERENCES teams(id) ON DELETE CASCADE,
        corp_id TEXT NOT NULL,
        agent_id INTEGER NOT NULL,
        credentials TEXT NOT NULL,
        public_base TEXT NOT NULL,
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
        revision INTEGER NOT NULL,
        callback_verified INTEGER NOT NULL DEFAULT 0,
        api_verified INTEGER NOT NULL DEFAULT 0,
        UNIQUE(corp_id, agent_id)
    )""",
    """CREATE TABLE wecom_bindings (
        connection_id TEXT NOT NULL REFERENCES wecom_connections(id) ON DELETE CASCADE,
        member_id TEXT NOT NULL,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        email_fingerprint TEXT NOT NULL,
        expires INTEGER NOT NULL,
        workspace_id TEXT,
        PRIMARY KEY(connection_id, member_id),
        UNIQUE(connection_id, user_id)
    )""",
    """CREATE TABLE wecom_oauth_states (
        state_hash TEXT PRIMARY KEY,
        connection_id TEXT NOT NULL REFERENCES wecom_connections(id) ON DELETE CASCADE,
        browser_hash TEXT NOT NULL,
        revision INTEGER NOT NULL,
        redirect_uri TEXT NOT NULL,
        member_id TEXT,
        expires INTEGER NOT NULL
    )""",
    """CREATE TABLE wecom_jobs (
        id TEXT PRIMARY KEY,
        connection_id TEXT NOT NULL REFERENCES wecom_connections(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL,
        user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
        payload TEXT NOT NULL,
        delivery TEXT,
        cursor INTEGER NOT NULL DEFAULT -1,
        part INTEGER NOT NULL DEFAULT 0,
        progress INTEGER NOT NULL DEFAULT 0,
        due INTEGER,
        lease TEXT,
        created INTEGER NOT NULL
    )""",
    "CREATE INDEX idx_wecom_jobs_due ON wecom_jobs(due) WHERE due IS NOT NULL",
)


def upgrade():
    if context.config.attributes["bowhead_target"] == "core":
        for sql in DDL:
            context.get_bind().exec_driver_sql(sql)


def downgrade():
    pass
