"""Record the Plugin versions each Agent release candidate was validated with.

Adds nullable `team_subagent_release_candidate.plugin_versions_json`: a JSON array of
`{"kind": "team"|"builtin", "id", "version"}` written when the candidate is frozen. Plugins float
to their latest version after release, so the Agent details and release pages compare these with
the current versions to suggest re-running evaluation (Capability Center tech design §13).

The constant null default means `ADD COLUMN` copies no table; historical candidates stay null and
are never backfilled. A trigger keeps the value immutable like the rest of the candidate payload.
Only core installs DDL, inside one savepoint; every other target advances its ledger unchanged.
"""

from alembic import context

revision = "0159_release_plugin_versions"
down_revision = "0158_memory_notification_links"
branch_labels = None
depends_on = None

PLUGIN_VERSIONS_COLUMN_DDL = (
    "ALTER TABLE team_subagent_release_candidate ADD COLUMN plugin_versions_json TEXT "
    "CHECK(plugin_versions_json IS NULL OR json_valid(plugin_versions_json))"
)
PLUGIN_VERSIONS_IMMUTABLE_TRIGGER_DDL = """
CREATE TRIGGER IF NOT EXISTS trg_team_subagent_release_candidate_plugin_versions_immutable
BEFORE UPDATE OF plugin_versions_json ON team_subagent_release_candidate
WHEN NEW.plugin_versions_json IS NOT OLD.plugin_versions_json
BEGIN
  SELECT RAISE(ABORT, 'team_subagent_release_candidate plugin versions are immutable');
END
"""


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    connection = context.get_bind()
    connection.exec_driver_sql("SAVEPOINT release_plugin_versions")
    try:
        columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(team_subagent_release_candidate)").fetchall()}
        if "plugin_versions_json" not in columns:
            connection.exec_driver_sql(PLUGIN_VERSIONS_COLUMN_DDL)
        connection.exec_driver_sql(PLUGIN_VERSIONS_IMMUTABLE_TRIGGER_DDL)
    except BaseException:
        connection.exec_driver_sql("ROLLBACK TO SAVEPOINT release_plugin_versions")
        connection.exec_driver_sql("RELEASE SAVEPOINT release_plugin_versions")
        raise
    connection.exec_driver_sql("RELEASE SAVEPOINT release_plugin_versions")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
