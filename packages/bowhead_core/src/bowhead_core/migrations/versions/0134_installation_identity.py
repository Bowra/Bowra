"""Record durable installation provenance without guessing existing editions."""

from alembic import context

revision = "0134_installation_identity"
down_revision = "0133_reference_plan_steps"
branch_labels = None
depends_on = None


def upgrade():
    if context.config.attributes["bowhead_target"] != "core":
        return
    context.get_bind().exec_driver_sql("""
        CREATE TABLE installation_identity (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            installation_id TEXT NOT NULL UNIQUE,
            origin_edition TEXT NOT NULL CHECK (origin_edition IN ('community', 'business')),
            current_edition TEXT NOT NULL CHECK (current_edition IN ('community', 'business')),
            setup_complete INTEGER NOT NULL DEFAULT 0 CHECK (setup_complete IN (0, 1)),
            created_at TEXT NOT NULL,
            CHECK (origin_edition != 'business' OR current_edition = 'business')
        )
    """)
