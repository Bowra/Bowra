"""Explicit user maintenance intent, independent of business conversation input."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer


class ContextActionRejected(ValueError):
    """A finite maintenance refusal before the original admission transaction commits."""

    def __init__(
        self, code: Literal["context_conversation_head_changed", "context_maintenance_session_busy", "context_maintenance_pending_operation"]
    ):
        super().__init__(code)
        self.code = code


class ContextMaintenanceAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    contract: Literal["context-maintenance.v1"] = "context-maintenance.v1"
    expected_generation: str | None = Field(pattern=r"^mcg_[0-9a-f]{32}$")

    @model_serializer(mode="wrap")
    def serialize_action(self, handler: SerializerFunctionWrapHandler) -> dict:
        """Keep the required absence fence in receipts that omit optional nulls."""
        data = handler(self)
        data["expected_generation"] = self.expected_generation
        return data


async def validate_context_action_in_tx(db, *, source) -> None:
    """Check the admitted source under the original Turn writer transaction."""
    action = source.context_maintenance
    if action is None:
        return
    if not db.in_transaction:
        raise ValueError("context_maintenance_transaction_required")
    async with db.execute(
        "SELECT s.active_turn_id,h.generation_id FROM sessions s "
        "LEFT JOIN model_context_conversation_heads h ON h.session_id=s.id "
        "WHERE s.id=? AND s.user_id=? AND s.workspace_id=? AND s.deleted_at IS NULL",
        (source.session_id, source.user_id, source.workspace_id),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None or row[1] != action.expected_generation:
        raise ContextActionRejected("context_conversation_head_changed")
    if row[0] not in (None, source.turn_id):
        raise ContextActionRejected("context_maintenance_session_busy")
    async with db.execute(
        "SELECT 1 FROM turns WHERE session_id=? AND id<>? AND operation_kind IS NULL "
        "AND status IN ('running','awaiting_user','awaiting_approval','clarification_pending','approval_pending') LIMIT 1",
        (source.session_id, source.turn_id),
    ) as cursor:
        if await cursor.fetchone() is not None:
            raise ContextActionRejected("context_maintenance_pending_operation")


def context_action_projection(action: ContextMaintenanceAction | None) -> dict:
    return {"context_maintenance": action.model_dump()} if action is not None else {}
