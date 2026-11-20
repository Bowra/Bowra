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


class StoppedTurnOwner(DocumentTurnOwner):
    """The worker that owned a Turn when the user stopped it.

    The Stop request commits the cancelled status before it signals the worker, so that
    worker is no longer the live owner. This capability lets it publish the stopped Turn's
    transcript and nothing else: it is accepted only by ``require_context_owner_in_tx``,
    and every fence that admits new work still requires the live owner.
    """


async def require_stopped_worker_in_tx(db: aiosqlite.Connection, *, worker: StoppedTurnOwner) -> None:
    """Admit a context write for a Turn the user stopped, by the worker that was running it.

    No later Turn may exist in the Session. Creating a Turn and this write go to the same
    database, so a successor either exists already, and this is refused, or is created
    afterwards and reads what this publishes. It never loads history that is replaced later.
    """
    from bowhead_core.ordinary_lead_continuation import OrdinaryWorkerUnavailable

    try:
        await require_reference_request_scope(db, context_scope(worker))
    except ValueError as error:
        raise OrdinaryWorkerUnavailable() from error
    async with db.execute(
        """SELECT 1 FROM turns t JOIN sessions s ON s.id=t.session_id JOIN workspaces w ON w.id=s.workspace_id
        JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id AND d.session_id=s.id
        WHERE t.id=? AND s.id=? AND s.user_id=? AND s.workspace_id=? AND w.team_id=?
          AND s.deleted_at IS NULL AND t.status='cancelled' AND t.error_kind='user_cancel'
          AND COALESCE(t.terminal_protocol_version,0)=0
          AND t.execution_generation=? AND d.owner_id=? AND d.lease_execution_generation=t.execution_generation
          AND d.cancel_requested_at IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM turns n WHERE n.session_id=t.session_id AND n.seq>t.seq)""",
        (worker.turn_id, worker.session_id, worker.user_id, worker.workspace_id, worker.team_id, worker.generation, worker.owner_id),
    ) as cursor:
        if await cursor.fetchone() is None:
            raise OrdinaryWorkerUnavailable()


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
    if isinstance(worker, StoppedTurnOwner):
        await require_stopped_worker_in_tx(db, worker=worker)
        return
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
