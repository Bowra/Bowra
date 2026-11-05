"""Publish an exact model view through its original Turn terminal transaction."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from bowhead_router.context_policy import TargetContextBudget
from bowhead_router.types import Message
from pydantic import BaseModel, ConfigDict, Field

from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_storage import ReferenceCipher
from bowhead_core.context_compaction import message_digest
from bowhead_core.context_dependencies import PreparedContextSourceValidator, adoption_source_validator
from bowhead_core.context_ownership import ContextOwner, TerminalContextFinalizationOwner, TerminalContextOwner
from bowhead_core.context_store import (
    ContextOrigin,
    ContextStoreError,
    PreparedContextAncestry,
    PreparedContextMessages,
    SourceValidator,
    prepare_context_messages,
    publish_conversation_context_in_tx,
    read_context_generation_in_tx,
    validate_prepared_context_in_tx,
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
    prepared_ancestry: PreparedContextAncestry | None = None

    @asynccontextmanager
    async def open_adoption_readers(self) -> AsyncIterator[ContextPublication]:
        if isinstance(self.validate_sources, PreparedContextSourceValidator):
            async with self.validate_sources.open_adoption_readers() as validator:
                yield replace(self, validate_sources=validator)
        else:
            yield self

    async def prepare_for_answer(self, final_answer: str) -> ContextPublication:
        messages, dependencies = deepcopy(self.messages), deepcopy(self.dependencies)
        values = [*messages, *([Message(role="assistant", content=final_answer)] if self.append_answer else [])]
        prepared = []
        for body in ([dependencies] if dependencies else []) + [values]:
            if isinstance(self.validate_sources, PreparedContextSourceValidator):
                # Revalidate changed final inputs before acquiring the writer;
                # adoption still repeats current authority in its own snapshot.
                await self.validate_sources.collect(body)
            prepared.append(
                await prepare_context_messages(
                    body,
                    worker=self.worker,
                    execution_id=self.execution_id,
                    lane="ordinary",
                    cipher=self.cipher,
                    db_path=self.db_path,
                    preparation=self.prepared_ancestry,
                )
            )
        return replace(self, messages=messages, dependencies=dependencies, prepared_messages=tuple(prepared))

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
                prepared_ancestry=self.prepared_ancestry,
                source_refs=(self.inherited_generation,) if self.inherited_generation else (),
                for_publication=True,
            )
        return current, values

    async def __call__(self, db, final_answer: str) -> None:
        """Legacy completion owns both staging and adoption in one transaction."""
        if isinstance(self.worker, TerminalContextOwner):
            raise ContextStoreError("context_terminal_candidate_required")
        working = replace(
            self,
            validate_sources=adoption_source_validator(self.validate_sources, db),
            prepared_ancestry=self.prepared_ancestry.for_transaction() if self.prepared_ancestry is not None else None,
        )
        generation, _ = await working._stage(db, final_answer)
        await publish_conversation_context_in_tx(
            db,
            worker=self.worker,
            execution_id=self.execution_id,
            generation_id=generation,
            expected_conversation_generation=self.inherited_generation,
            cipher=self.cipher,
            validate_sources=working.validate_sources,
            prepared_ancestry=working.prepared_ancestry,
        )
        if not self.append_answer:
            from bowhead_core.context_activity import recover_context_publications_in_connection

            await recover_context_publications_in_connection(db, turn_id=self.worker.turn_id)

    async def stage_terminal(self, final_answer: str) -> StagedTerminalContext:
        """Freeze content while running; the original finalizer later adopts it."""
        if not isinstance(self.worker, TerminalContextOwner):
            raise ContextStoreError("context_terminal_owner_required")
        prepared = await self.prepare_for_answer(final_answer)
        async with prepared.open_adoption_readers() as adoption, open_shared_ctx(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            working = replace(
                adoption,
                validate_sources=adoption_source_validator(prepared.validate_sources, db),
                prepared_ancestry=prepared.prepared_ancestry.for_transaction() if prepared.prepared_ancestry is not None else None,
            )
            generation, messages = await working._stage(db, final_answer)
            await db.commit()
        return StagedTerminalContext(
            publication=prepared,
            projection=TerminalContextProjection(
                execution_id=self.execution_id,
                generation_id=generation,
                expected_head_id=self.inherited_generation,
                content_digest=message_digest(messages),
                content_state="conversation" if self.append_answer else "maintenance",
            ),
            final_answer=final_answer,
        )


@dataclass(frozen=True)
class StagedTerminalContext:
    publication: ContextPublication
    projection: TerminalContextProjection
    final_answer: str | None = None

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
        validation = adoption_source_validator(self.publication.validate_sources, db)
        ancestry = self.publication.prepared_ancestry.for_transaction() if self.publication.prepared_ancestry is not None else None
        verified = await validate_prepared_context_in_tx(
            db,
            worker=worker,
            execution_id=self.projection.execution_id,
            lane="ordinary",
            generation_id=self.projection.generation_id,
            cipher=self.publication.cipher,
            validate_sources=validation,
            preparation=ancestry,
            for_publication=True,
        )
        if verified and ancestry is not None and self.final_answer is not None:
            digest = ancestry.records[self.projection.generation_id][1].digest
            answer_matches = self.final_answer == final_answer
        else:
            retained = await read_context_generation_in_tx(
                db,
                worker=worker,
                execution_id=self.projection.execution_id,
                lane="ordinary",
                generation_id=self.projection.generation_id,
                cipher=self.publication.cipher,
                validate_sources=validation,
                for_publication=True,
            )
            digest = message_digest(retained.messages)
            answer_matches = not self.publication.append_answer or retained.messages[-1] == Message(role="assistant", content=final_answer)
        if (
            digest != self.projection.content_digest
            or (self.publication.append_answer and not answer_matches)
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
            validate_sources=validation,
            prepared_ancestry=ancestry,
        )
