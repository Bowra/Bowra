"""Publish an exact model view through its original Turn terminal transaction."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from bowhead_router.context_policy import TargetContextBudget
from bowhead_router.types import Message
from pydantic import BaseModel, ConfigDict, Field

from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_storage import ReferenceCipher
from bowhead_core.context_compaction import message_digest
from bowhead_core.context_dependencies import adoption_source_validator
from bowhead_core.context_ownership import ContextOwner, TerminalContextFinalizationOwner, TerminalContextOwner
from bowhead_core.context_store import (
    ContextOrigin,
    ContextStoreError,
    PreparedContextMessages,
    SourceValidator,
    prepare_context_messages,
    publish_conversation_context_in_tx,
    read_context_generation_in_tx,
    write_context_in_tx,
)


class TerminalContextProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["model_context_publication_v1"] = "model_context_publication_v1"
    contract_version: Literal["1"] = "1"
    execution_id: str = Field(min_length=1, max_length=128)
    generation_id: str = Field(pattern=r"^mcg_[0-9a-f]{32}$")
    expected_head_id: str | None = Field(pattern=r"^mcg_[0-9a-f]{32}$")
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_state: Literal["conversation", "maintenance"] = "conversation"


@dataclass(frozen=True)
class ContextPublication:
    db_path: str | Path
    worker: ContextOwner
    execution_id: str
    cipher: ReferenceCipher
    validate_sources: SourceValidator
    messages: list[Message]
    dependencies: list[Message]
    budget: TargetContextBudget
    expected_generation: str | None
    inherited_generation: str | None
    append_answer: bool = True
    prepared_messages: tuple[PreparedContextMessages, ...] = ()

    async def prepare_for_answer(self, final_answer: str) -> ContextPublication:
        values = [*self.messages, *([Message(role="assistant", content=final_answer)] if self.append_answer else [])]
        prepared = []
        for body in ([self.dependencies] if self.dependencies else []) + [values]:
            prepared.append(
                await prepare_context_messages(body, worker=self.worker, execution_id=self.execution_id, lane="ordinary", cipher=self.cipher)
            )
        return replace(self, prepared_messages=tuple(prepared))

    async def _stage(self, db, final_answer: str) -> tuple[str, list[Message]]:
        current = self.expected_generation
        values = [*self.messages, *([Message(role="assistant", content=final_answer)] if self.append_answer else [])]
        if not self.append_answer:
            if final_answer:
                raise ContextStoreError("context_maintenance_answer_forbidden")
            async with db.execute("SELECT operation_kind FROM turns WHERE id=?", (self.worker.turn_id,)) as cursor:
                operation = await cursor.fetchone()
            if operation is None or operation[0] != "context_maintenance":
                raise ContextStoreError("context_maintenance_action_required")
        origin = ContextOrigin(
            provider=self.budget.provider,
            model=self.budget.model,
            configuration_version=self.budget.configuration_version,
        )
        validation = adoption_source_validator(self.validate_sources, db)
        for index, body in enumerate(([self.dependencies] if self.dependencies else []) + [values]):
            current = await write_context_in_tx(
                db,
                worker=self.worker,
                execution_id=self.execution_id,
                lane="ordinary",
                expected_generation=current,
                messages=body,
                policy=self.budget.policy,
                origin=origin,
                cipher=self.cipher,
                validate_sources=validation,
                prepared_messages=self.prepared_messages[index] if self.prepared_messages else None,
                source_refs=(self.inherited_generation,) if self.inherited_generation else (),
                for_publication=True,
            )
        return current, values

    async def __call__(self, db, final_answer: str) -> None:
        """Legacy completion owns both staging and adoption in one transaction."""
        if isinstance(self.worker, TerminalContextOwner):
            raise ContextStoreError("context_terminal_candidate_required")
        generation, _ = await self._stage(db, final_answer)
        await publish_conversation_context_in_tx(
            db,
            worker=self.worker,
            execution_id=self.execution_id,
            generation_id=generation,
            expected_conversation_generation=self.inherited_generation,
            cipher=self.cipher,
            validate_sources=self.validate_sources,
        )
        if not self.append_answer:
            from bowhead_core.context_activity import recover_context_publications_in_connection

            await recover_context_publications_in_connection(db, turn_id=self.worker.turn_id)

    async def stage_terminal(self, final_answer: str) -> StagedTerminalContext:
        """Freeze content while running; the original finalizer later adopts it."""
        if not isinstance(self.worker, TerminalContextOwner):
            raise ContextStoreError("context_terminal_owner_required")
        prepared = await self.prepare_for_answer(final_answer)
        async with open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            generation, messages = await prepared._stage(db, final_answer)
            await db.commit()
        return StagedTerminalContext(
            publication=self,
            projection=TerminalContextProjection(
                execution_id=self.execution_id,
                generation_id=generation,
                expected_head_id=self.inherited_generation,
                content_digest=message_digest(messages),
                content_state="conversation" if self.append_answer else "maintenance",
            ),
        )


@dataclass(frozen=True)
class StagedTerminalContext:
    publication: ContextPublication
    projection: TerminalContextProjection

    async def adopt(self, db, *, context, plan, selected, final_answer: str) -> None:
        from bowhead_core.terminal_protocol import CandidateOutcome, PreparedFinalizationContext, TerminalStatus

        if selected.outcome_variant is not CandidateOutcome.PRIMARY or selected.status is not TerminalStatus.COMPLETED:
            return
        original = self.publication.worker
        if not isinstance(original, TerminalContextOwner) or not isinstance(context, PreparedFinalizationContext):
            raise ContextStoreError("context_terminal_finalization_required")
        frozen = [value for value in plan.projection_extensions if value.get("kind") == self.projection.kind]
        if frozen != [self.projection.model_dump()] or plan.scope != original.execution.scope:
            raise ContextStoreError("context_terminal_projection_changed")
        worker = TerminalContextFinalizationOwner(
            execution=original.execution,
            user_id=original.user_id,
            workspace_id=original.workspace_id,
            prepared=context,
        )
        retained = await read_context_generation_in_tx(
            db,
            worker=worker,
            execution_id=self.projection.execution_id,
            lane="ordinary",
            generation_id=self.projection.generation_id,
            cipher=self.publication.cipher,
            validate_sources=self.publication.validate_sources,
            for_publication=True,
        )
        if (
            message_digest(retained.messages) != self.projection.content_digest
            or (self.publication.append_answer and retained.messages[-1] != Message(role="assistant", content=final_answer))
            or (not self.publication.append_answer and final_answer)
        ):
            raise ContextStoreError("context_terminal_content_changed")
        await publish_conversation_context_in_tx(
            db,
            worker=worker,
            execution_id=self.projection.execution_id,
            generation_id=self.projection.generation_id,
            expected_conversation_generation=self.projection.expected_head_id,
            cipher=self.publication.cipher,
            validate_sources=self.publication.validate_sources,
        )
