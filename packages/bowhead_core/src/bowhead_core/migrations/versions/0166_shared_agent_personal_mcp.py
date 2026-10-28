"""Allow owner-scoped personal MCP bindings on shared Agents."""

from alembic import context

revision = "0166_shared_agent_personal_mcp"
down_revision = "0165_agent_collaboration"
branch_labels = None
depends_on = None

SHARING_TRIGGERS = (
    *(f"personal_mcp_{table}_{action}" for table in (
        "team_subagent_working_copy",
        "team_subagent_release_candidate",
        "team_subagent_release",
    ) for action in ("insert", "update")),
    "personal_mcp_agent_share",
)


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    db.exec_driver_sql("SAVEPOINT shared_agent_personal_mcp")
    try:
        for name in SHARING_TRIGGERS:
            db.exec_driver_sql(f"DROP TRIGGER IF EXISTS {name}")
        db.exec_driver_sql("RELEASE SAVEPOINT shared_agent_personal_mcp")
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT shared_agent_personal_mcp")
        db.exec_driver_sql("RELEASE SAVEPOINT shared_agent_personal_mcp")
        raise


def downgrade() -> None:
    raise NotImplementedError("Bowhead does not support schema downgrade")
