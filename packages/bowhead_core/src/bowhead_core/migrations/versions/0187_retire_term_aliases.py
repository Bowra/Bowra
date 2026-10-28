"""Retire configured term aliases and their recoverable personal-memory copies."""

import json

from alembic import context

revision = "0187_retire_term_aliases"
down_revision = "0186_agent_eval_alignment_loop"
branch_labels = None
depends_on = None

_RETIRED_KEYS = frozenset({"glossary", "personal_glossary", "affected_glossary", "restored_glossary"})


def _clean_snapshot(value: object) -> object:
    """Keep unrelated reset data while removing retired resources and counts."""
    if isinstance(value, dict):
        return {key: _clean_snapshot(item) for key, item in value.items() if key not in _RETIRED_KEYS}
    if isinstance(value, list):
        return [
            _clean_snapshot(item)
            for item in value
            if not (isinstance(item, dict) and (item.get("kind") == "glossary" or item.get("resource_type") == "personal_glossary"))
        ]
    return value


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    tables = {row[0] for row in db.exec_driver_sql("SELECT name FROM sqlite_schema WHERE type='table'")}
    db.exec_driver_sql("SAVEPOINT retire_term_aliases")
    try:
        # This table was optional in early installations; fresh installations must not recreate it.
        db.exec_driver_sql("DROP TABLE IF EXISTS user_glossary")
        if "account_deletion_rows" in tables:
            db.exec_driver_sql("DELETE FROM account_deletion_rows WHERE table_name='user_glossary'")

        for table in ("memory_entries", "memory_candidates", "memory_candidate_suppressions"):
            if table in tables:
                db.exec_driver_sql(f"DELETE FROM {table} WHERE kind='glossary'")
        if "memory_reset_snapshot_items" in tables:
            db.exec_driver_sql(
                "DELETE FROM memory_reset_snapshot_items WHERE resource_type='personal_glossary' "
                "OR (json_valid(payload_json) AND json_extract(payload_json,'$.kind')='glossary')"
            )
        if "memory_reset_snapshots" in tables:
            db.exec_driver_sql("DELETE FROM memory_reset_snapshots WHERE target_resource_type='personal_glossary'")
            for row_id, payload, counts in db.exec_driver_sql("SELECT id,snapshot_json,counts_json FROM memory_reset_snapshots").fetchall():
                try:
                    original_payload, original_counts = json.loads(payload), json.loads(counts)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"Cannot retire term aliases: reset snapshot {row_id} contains invalid JSON. Repair or remove this snapshot before upgrading."
                    ) from exc
                cleaned_payload, cleaned_counts = _clean_snapshot(original_payload), _clean_snapshot(original_counts)
                if cleaned_payload != original_payload or cleaned_counts != original_counts:
                    db.exec_driver_sql(
                        "UPDATE memory_reset_snapshots SET snapshot_json=?,counts_json=? WHERE id=?",
                        (json.dumps(cleaned_payload, ensure_ascii=False), json.dumps(cleaned_counts, ensure_ascii=False), row_id),
                    )
        # Prompt snapshots containing retired terminology cannot be recalled as current knowledge.
        # Other snapshots and historical chat/clarification events keep their original bytes.
        if "memory_pack_snapshots" in tables:
            db.exec_driver_sql(
                "DELETE FROM memory_pack_snapshots WHERE instr(content,'- User business glossary:')>0 "
                "OR CASE WHEN json_valid(sections_breakdown_json) "
                "THEN coalesce(json_extract(sections_breakdown_json,'$.glossary'),0)>0 ELSE 0 END"
            )
    except BaseException:
        db.exec_driver_sql("ROLLBACK TO SAVEPOINT retire_term_aliases")
        db.exec_driver_sql("RELEASE SAVEPOINT retire_term_aliases")
        raise
    db.exec_driver_sql("RELEASE SAVEPOINT retire_term_aliases")


def downgrade() -> None:
    raise NotImplementedError("Restore the matching database snapshot to roll back.")
