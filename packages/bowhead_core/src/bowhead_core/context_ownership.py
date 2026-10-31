"""Reuse the owning Turn protocol's live capability for context-only work."""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite

from bowhead_core.chat_reference_scope import ReferenceRequestScope, require_reference_request_scope
from bowhead_core.document_chat_continuation import DocumentTurnOwner
from bowhead_core.ordinary_lead_continuation import require_live_worker_in_tx
from bowhead_core.terminal_protocol import ExecutionGenerationContext, PreparedFinalizationContext


@dataclass(frozen=True, slots=True)
class TerminalContextOwner:
    execution: ExecutionGenerationContext
    user_id: str
    workspace_id: str

    @property
    def team_id(self) -> str:
        return self.execution.scope.team_id

    @property
    def session_id(self) -> str:
        return self.execution.scope.session_id

    @property
    def turn_id(self) -> str:
        return self.execution.scope.turn_id

    @property
    def generation(self) -> int:
        return self.execution.scope.generation

    @property
    def owner_id(self) -> str:
        return self.execution.owner_instance_id


@dataclass(frozen=True, slots=True)
class TerminalContextFinalizationOwner(TerminalContextOwner):
    prepared: PreparedFinalizationContext


ContextOwner = DocumentTurnOwner | TerminalContextOwner


def context_owner(ctx) -> ContextOwner | None:
    """Resolve only the capability supplied by the original Turn runner."""
    execution = getattr(ctx, "terminal_execution_context", None)
    if execution is not None:
        if not isinstance(execution, ExecutionGenerationContext) or (
            execution.scope.team_id,
            execution.scope.session_id,
            execution.scope.turn_id,
        ) != (ctx.team_id, ctx.session_id, ctx.turn_id):
            raise ValueError("Context execution scope does not match its Turn")
        return TerminalContextOwner(execution, ctx.user_id, ctx.workspace_id)
    owner_id = getattr(ctx, "execution_owner_id", None)
    generation = getattr(ctx, "execution_generation", None)
    if isinstance(owner_id, str) and owner_id and type(generation) is int and generation >= 1:
        from bowhead_core.ordinary_lead_runtime import worker_identity

        return worker_identity(ctx)
    return None


def context_scope(owner: ContextOwner) -> ReferenceRequestScope:
    return ReferenceRequestScope(
        team_id=owner.team_id,
        user_id=owner.user_id,
        workspace_id=owner.workspace_id,
        session_id=owner.session_id,
    )


async def require_context_owner_in_tx(db: aiosqlite.Connection, *, worker: ContextOwner) -> None:
    if isinstance(worker, DocumentTurnOwner):
        await require_live_worker_in_tx(db, worker=worker)
        return
    if not isinstance(worker, TerminalContextOwner):
        raise TypeError("Context ownership requires an original Turn capability")
    from bowhead_core.terminal_protocol_store import require_context_finalization_in_connection, require_live_execution_in_connection

    await require_reference_request_scope(db, context_scope(worker))
    if isinstance(worker, TerminalContextFinalizationOwner):
        await require_context_finalization_in_connection(db, context=worker.prepared)
    else:
        await require_live_execution_in_connection(db, context=worker.execution)
