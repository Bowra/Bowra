"""Local team OpenAPI credentials and call log.

Revision ID: 0027_team_openapi
Revises: 0026_agent_marketplace_safety
"""
from alembic import context

revision = "0027_team_openapi"
down_revision = "0026_agent_marketplace_safety"
branch_labels = None
depends_on = None

def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("CREATE TABLE IF NOT EXISTS team_openapi_keys (id TEXT PRIMARY KEY, team_id TEXT NOT NULL, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, token_prefix TEXT NOT NULL, scopes_json TEXT NOT NULL, expires_at TEXT, status TEXT NOT NULL CHECK(status IN ('active','disabled','deleted')), created_at TEXT NOT NULL, last_used_at TEXT)")
    db.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_team_openapi_keys_team ON team_openapi_keys(team_id,created_at DESC)")
    db.exec_driver_sql("CREATE TABLE IF NOT EXISTS team_openapi_call_logs (id TEXT PRIMARY KEY, team_id TEXT, key_id TEXT, key_name TEXT, key_prefix TEXT, http_method TEXT NOT NULL, path TEXT NOT NULL, status_code INTEGER NOT NULL, request_summary TEXT, response_summary TEXT, created_at TEXT NOT NULL)")
    db.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_team_openapi_logs_team_time ON team_openapi_call_logs(team_id,created_at DESC)")
