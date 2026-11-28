"""Index delivered files and persist reviewed Team cleanup decisions."""

from alembic import context

revision = "0238_file_library"
down_revision = "0237_team_directory"
branch_labels = depends_on = None


def reference_guards(table, column="artifact_id", active="1", update_columns=None, *, lineage=False):
    predicate = "bowhead_file_retention_lineage_unavailable_v1" if lineage else "bowhead_file_retention_unavailable_v1"
    changes = ",".join(update_columns or (column,))
    return tuple(
        f"""CREATE TRIGGER trg_retention_{table}_{suffix} BEFORE {action} ON {table}
        WHEN {active} AND {predicate}(NEW.{column})
        BEGIN SELECT RAISE(ABORT,'Restore the file before adding this reference'); END"""
        for suffix, action in (("insert", "INSERT"), ("update", f"UPDATE OF {changes}"))
    )


STATEMENTS = {
    "files": reference_guards("kb_files", active="NEW.soft_deleted_at IS NULL", update_columns=("artifact_id", "soft_deleted_at")),
    "execution": (
        *reference_guards("turn_attachments"),
        *reference_guards("turn_citations"),
        *reference_guards("session_attachments", active="NEW.detached_at IS NULL", update_columns=("artifact_id", "detached_at")),
        *reference_guards("document_artifact_refs", active="NEW.released_at IS NULL", update_columns=("artifact_id", "released_at")),
        *reference_guards("user_result_pins", "target_id", "NEW.target_kind='file'", ("target_kind", "target_id"), lineage=True),
        "CREATE INDEX idx_library_origin_attachment ON session_attachments(artifact_id,pinned_at)",
        "CREATE INDEX idx_library_turn_attachment ON turn_attachments(artifact_id)",
        "CREATE INDEX idx_library_citation ON turn_citations(artifact_id)",
        "CREATE INDEX idx_turn_final_file_cards ON turn_events(turn_id,seq DESC) WHERE event_type='lead.final.payload'",
    ),
    "core": (
        *reference_guards(
            "resource_grants",
            "resource_id",
            "NEW.resource_kind='artifact' AND NEW.revoked_at IS NULL",
            ("resource_kind", "resource_id", "revoked_at", "expires_at"),
        ),
        """CREATE TABLE team_file_retention (
            team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            version INTEGER NOT NULL, days INTEGER CHECK(days IN (0,30,90)), effective_at TEXT NOT NULL,
            existing_candidates TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY(team_id,version))""",
        """CREATE TABLE file_retention_reviews (
            id TEXT PRIMARY KEY, team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            version INTEGER NOT NULL, days INTEGER, effective_days INTEGER NOT NULL, created_at TEXT NOT NULL,
            candidates TEXT NOT NULL, applied_version INTEGER, applied_existing INTEGER CHECK(applied_existing IN (0,1)))""",
        """CREATE TRIGGER trg_retention_policy_write BEFORE INSERT ON team_file_retention
        BEGIN SELECT bowhead_domain_authority_v1('teams'); END""",
        "CREATE INDEX idx_retention_reviews_team ON file_retention_reviews(team_id,applied_version)",
    ),
    "content": (
        """CREATE TABLE file_retention_state (
            artifact_id TEXT PRIMARY KEY REFERENCES artifacts(id) ON DELETE CASCADE,
            kept INTEGER NOT NULL DEFAULT 0, trashed_at TEXT, policy_version INTEGER)""",
        """CREATE TRIGGER trg_restore_auto_trashed_file AFTER UPDATE OF deleted_at ON artifacts
            WHEN OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS NULL
            BEGIN UPDATE file_retention_state SET kept=1,trashed_at=NULL
            WHERE artifact_id=NEW.id AND trashed_at=OLD.deleted_at; END""",
    ),
}


def upgrade():
    for statement in STATEMENTS.get(context.config.attributes["bowhead_target"], ()):
        context.get_bind().exec_driver_sql(statement)


def downgrade():
    raise NotImplementedError("Restore the coordinated database snapshot to roll back.")
