"""Index native artifact presentation receipts without rewriting event history."""

from alembic import context

revision = "0198_artifact_presentation"
down_revision = "0197_knowledge_read_state"
branch_labels = None
depends_on = None

INDEX = "idx_turn_events_artifact_presentation"
DDL = """CREATE UNIQUE INDEX idx_turn_events_artifact_presentation
ON turn_events(turn_id, json_extract(payload_json,'$.result_meta.artifact_presentation_receipt.call_key'))
WHERE event_type='tool_result' AND CASE WHEN json_valid(payload_json) THEN
json_type(payload_json,'$.result_meta.artifact_presentation.contract')='text'
AND json_extract(payload_json,'$.result_meta.artifact_presentation.origin') IN ('explicit_tool','producer_declared')
AND json_type(payload_json,'$.result_meta.artifact_presentation.items')='array'
AND json_array_length(payload_json,'$.result_meta.artifact_presentation.items') BETWEEN 1 AND 32
AND length(json_extract(payload_json,'$.result_meta.artifact_presentation_receipt.call_key'))=64
END"""


def upgrade() -> None:
    if context.config.attributes["bowhead_target"] != "core":
        return
    db = context.get_bind()
    if db.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name=?", (INDEX,)).fetchone():
        raise RuntimeError("Artifact presentation index schema drift")
    db.exec_driver_sql(DDL)


def downgrade() -> None:
    raise NotImplementedError("Restore the paired database snapshot to roll back.")
