"""Source dependencies retained beside model projections, never inside summaries."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from copy import copy, deepcopy
from dataclasses import dataclass, field

from bowhead_router.types import Message
from pydantic import BaseModel, ConfigDict, Field


@asynccontextmanager
async def _source_connection(ctx, connection=None):
    """Use the adoption snapshot; standalone preparation owns a read transaction."""
    if connection is not None:
        yield connection
        return
    from bowhead_core._db import open_shared_ctx

    async with open_shared_ctx(ctx.session_store.db_path) as db:
        await db.execute("BEGIN")
        try:
            yield db
        finally:
            await db.rollback()


@dataclass
class ContextSourceObservations:
    """Original reader evidence shared by a Turn; never a resume capability."""

    tool_materials: dict = field(default_factory=dict)
    pending_skill_reads: dict = field(default_factory=dict)


class KnowledgeContextDependencies(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    knowledge_sources: dict[str, str | None] = Field(min_length=1)


class NavigationContextDependencies(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    knowledge_navigation: dict[str, dict] = Field(min_length=1)


class MaterialContextDependencies(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    turn_id: str = Field(min_length=1, max_length=128)
    tools: list[dict] = Field(min_length=1)


async def capture_execution_dependencies(ctx) -> list[Message]:
    """Capture original source-reader proofs, never derive permission from text."""
    from bowhead_core.context_attachments import capture_attachment_dependencies
    from bowhead_core.session_store import _read_chat_turn_user_source_in_tx

    result = capture_context_dependencies(ctx)
    async with _source_connection(ctx) as db:
        source = await _read_chat_turn_user_source_in_tx(
            db, turn_id=ctx.turn_id, expected_user_id=ctx.user_id, expected_workspace_id=ctx.workspace_id
        )
        if source is None:
            if getattr(ctx, "_psi_parsed_results", None) or getattr(ctx, "_psi_pinned_snapshot", None):
                raise ValueError("context_attachment_source_unavailable")
            return result
        retained = await capture_attachment_dependencies(ctx, db, source)
    if retained is not None:
        result.append(Message(role="user", name="context_attachment_dependencies_v1", content=retained.model_dump_json()))
    return result


def capture_context_dependencies(ctx) -> list[Message]:
    from bowhead_core.agent_knowledge_policy import KnowledgeAccessError

    sources = dict(ctx.knowledge_source_uses)
    state = getattr(ctx, "ordinary_lead_state", None)
    tools = []
    if state is not None:
        for identity, generation in state.knowledge_sources.items():
            if identity in sources and sources[identity] != generation:
                raise KnowledgeAccessError("knowledge_source_changed")
            sources[identity] = generation
    result = []
    if sources:
        retained = KnowledgeContextDependencies(knowledge_sources=sources)
        result.append(Message(role="user", name="context_source_dependencies_v1", content=retained.model_dump_json()))
    if state is not None:
        checkpoint = getattr(state, "checkpoint", None)
        tools = (
            [tool for entry in checkpoint.helper_materials() for tool in entry.get("tools", [])]
            if checkpoint is not None
            else list(getattr(state, "tool_materials", {}).values())
        )
    observed = getattr(ctx, "context_source_observations", None)
    if observed is not None:
        tools.extend(observed.tool_materials.values())
    navigation = getattr(ctx, "knowledge_navigation_uses", {})
    if navigation:
        retained_navigation = NavigationContextDependencies(knowledge_navigation=deepcopy(navigation))
        result.append(Message(role="user", name="context_navigation_dependencies_v1", content=retained_navigation.model_dump_json()))
    if tools:
        material = MaterialContextDependencies(turn_id=ctx.turn_id, tools=deepcopy(tools))
        result.append(Message(role="user", name="context_material_dependencies_v1", content=material.model_dump_json()))
    return result


async def validate_context_dependencies(
    ctx,
    messages: list[Message],
    *,
    read_bytes: bool = True,
    validated_materials: set[str] | None = None,
    connection=None,
    prepared_dependencies: dict[tuple[str, str], BaseModel] | None = None,
) -> None:
    from bowhead_core.agent_knowledge_policy import KnowledgeAccessError
    from bowhead_core.knowledge_source_runtime import record_source_uses, validate_retained_sources

    for message in messages:
        if message.name == "context_attachment_dependencies_v1":
            from bowhead_core.context_attachments import AttachmentContextDependencies, validate_attachment_dependencies
            from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable
            from bowhead_core.session_store import _read_chat_turn_user_source_in_tx

            try:
                if message.role != "user" or message.tool_call_id or message.tool_calls or message.images:
                    raise ValueError("Invalid context dependency shape")
                attachment = (prepared_dependencies or {}).get((message.name, message.content))
                if not isinstance(attachment, AttachmentContextDependencies):
                    attachment = AttachmentContextDependencies.model_validate(json.loads(message.content))
                source_ctx = copy(ctx)
                source_ctx.turn_id = attachment.turn_id
                async with _source_connection(ctx, connection) as db:
                    source = await _read_chat_turn_user_source_in_tx(
                        db, turn_id=attachment.turn_id, expected_user_id=ctx.user_id, expected_workspace_id=ctx.workspace_id
                    )
                    await validate_attachment_dependencies(source_ctx, db, source, attachment, read_bytes=read_bytes)
            except (ValueError, KeyError, TypeError, OSError) as exc:
                raise OrdinaryContinuationUnavailable() from exc
            continue
        if message.name == "context_material_dependencies_v1":
            from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable
            from bowhead_core.ordinary_lead_material import revalidate_material_authority_in_connection, revalidate_tool_materials

            try:
                if message.role != "user" or message.tool_call_id or message.tool_calls or message.images:
                    raise ValueError("Invalid context dependency shape")
                material = (prepared_dependencies or {}).get((message.name, message.content))
                if not isinstance(material, MaterialContextDependencies):
                    material = MaterialContextDependencies.model_validate(json.loads(message.content))
                if any(
                    record.get("kind")
                    not in {"attachment_read", "skill_read", "skill_list", "sandbox", "uncertain_sandbox", "read_result", "rejected"}
                    or not isinstance(record.get("skills"), list)
                    for record in material.tools
                ):
                    raise ValueError("Invalid context material shape")
                source_ctx = copy(ctx)
                source_ctx.turn_id = material.turn_id
                tools = material.tools
                pending = {}
                if validated_materials is not None:
                    pending = {json.dumps([material.turn_id, record], sort_keys=True, separators=(",", ":")): record for record in tools}
                    pending = {key: record for key, record in pending.items() if key not in validated_materials}
                    if not pending:
                        continue
                    tools = list(pending.values())
                if read_bytes:
                    async with (
                        _source_connection(ctx, connection) as db,
                        db.execute("SELECT 1 FROM turns WHERE id=? AND session_id=?", (material.turn_id, ctx.session_id)) as cursor,
                    ):
                        if await cursor.fetchone() is None:
                            raise ValueError("Context material source is unavailable")
                    await revalidate_tool_materials(source_ctx, tools, contract="ordinary-lead-loop.v13")
                async with _source_connection(ctx, connection) as db:
                    async with db.execute("SELECT 1 FROM turns WHERE id=? AND session_id=?", (material.turn_id, ctx.session_id)) as cursor:
                        if await cursor.fetchone() is None:
                            raise ValueError("Context material source is unavailable")
                    await revalidate_material_authority_in_connection(source_ctx, db, {"tools": tools}, contract="ordinary-lead-loop.v13")
                if validated_materials is not None:
                    validated_materials.update(pending)
            except (ValueError, KeyError, TypeError, OSError) as exc:
                raise OrdinaryContinuationUnavailable() from exc
            continue
        if message.name == "context_navigation_dependencies_v1":
            from bowhead_core.kb.source_navigation import record_navigation_uses, validate_navigation_dependencies

            if message.role != "user" or message.tool_call_id or message.tool_calls or message.images:
                raise KnowledgeAccessError("knowledge_source_provenance_unavailable")
            try:
                retained_navigation = NavigationContextDependencies.model_validate_json(message.content)
                await validate_navigation_dependencies(ctx, retained_navigation.knowledge_navigation)
            except ValueError as exc:
                raise KnowledgeAccessError("knowledge_source_provenance_unavailable") from exc
            record_navigation_uses(ctx, retained_navigation.knowledge_navigation)
            continue
        if message.name != "context_source_dependencies_v1":
            continue
        try:
            if message.role != "user" or message.tool_call_id or message.tool_calls or message.images:
                raise ValueError("Invalid context dependency shape")
            retained = KnowledgeContextDependencies.model_validate(json.loads(message.content))
        except ValueError:
            raise KnowledgeAccessError("knowledge_source_provenance_unavailable") from None
        await validate_retained_sources(ctx, retained.knowledge_sources)
        record_source_uses(ctx, retained.knowledge_sources)


_CONTEXT_DEPENDENCY_NAMES = frozenset(
    {"context_attachment_dependencies_v1", "context_material_dependencies_v1", "context_source_dependencies_v1", "context_navigation_dependencies_v1"}
)


class ContextSourceValidator:
    """Revalidate bytes before writing, then current authority during adoption."""

    def __init__(self, ctx):
        self.ctx = ctx

    async def __call__(self, messages: list[Message]) -> None:
        await self.validate(messages, read_bytes=True)

    async def validate(
        self,
        messages: list[Message],
        *,
        read_bytes: bool,
        validated_materials: set[str] | None = None,
        connection=None,
        prepared_dependencies: dict[tuple[str, str], BaseModel] | None = None,
        retained_sources: dict[str, str | None] | None = None,
        retained_navigation: dict[str, dict] | None = None,
    ) -> None:
        from bowhead_core.agent_knowledge_policy import KnowledgeAccessError
        from bowhead_core.knowledge_source_runtime import record_source_uses, validate_retained_sources, validate_source_messages

        await validate_source_messages(self.ctx, messages)
        await validate_context_dependencies(
            self.ctx,
            messages,
            read_bytes=read_bytes,
            validated_materials=validated_materials,
            connection=connection,
            prepared_dependencies=prepared_dependencies,
        )
        sources = dict(self.ctx.knowledge_source_uses)
        state = self.ctx.ordinary_lead_state
        for values in (state.knowledge_sources if state is not None else {}, retained_sources or {}):
            for identity, generation in values.items():
                if identity in sources and sources[identity] != generation:
                    raise KnowledgeAccessError("knowledge_source_changed")
                sources[identity] = generation
        navigation = dict(getattr(self.ctx, "knowledge_navigation_uses", None) or {})
        for identity, proof in (retained_navigation or {}).items():
            if identity in navigation and navigation[identity] != proof:
                raise KnowledgeAccessError("knowledge_source_changed")
            navigation[identity] = proof
        source_ctx = copy(self.ctx)
        source_ctx.knowledge_navigation_uses = navigation
        # One current-authority read per boundary, including every retained map.
        # This is not a cache: a later boundary rechecks sources and navigation.
        await validate_retained_sources(source_ctx, sources)
        if retained_sources:
            record_source_uses(self.ctx, retained_sources)
        if retained_navigation:
            from bowhead_core.kb.source_navigation import record_navigation_uses

            record_navigation_uses(self.ctx, retained_navigation)


class PreparedContextSourceValidator:
    """Call-local byte evidence; never reusable across model decisions or recovery."""

    def __init__(self, original: ContextSourceValidator):
        self.original = original
        self.verified: set[str] = set()
        self.collected_materials: set[str] = set()
        self.authority_dependencies: dict[str, Message] = {}
        self.verified_messages: set[str] = set()
        self.retained_sources: dict[str, str | None] = {}
        self.retained_navigation: dict[str, dict] = {}
        self.prepared_dependencies: dict[tuple[str, str], BaseModel] = {}

    @staticmethod
    def _source_messages(messages: list[Message]) -> set[str]:
        from bowhead_core.context_compaction import message_digest

        return {
            message_digest([message])
            for message in messages
            if message.name in _CONTEXT_DEPENDENCY_NAMES or message.role == "tool" or (message.role == "assistant" and message.tool_calls)
        }

    @staticmethod
    def _dependencies(messages: list[Message]) -> set[str]:
        from bowhead_core.context_compaction import message_digest

        return {message_digest([message]) for message in messages if message.name in _CONTEXT_DEPENDENCY_NAMES}

    async def collect(self, messages: list[Message]) -> None:
        from bowhead_core.agent_knowledge_policy import KnowledgeAccessError

        await self.original.validate(messages, read_bytes=True, validated_materials=self.collected_materials)
        self.verified.update(self._dependencies(messages))
        self.verified_messages.update(self._source_messages(messages))
        state = self.original.ctx.ordinary_lead_state
        for values in (self.original.ctx.knowledge_source_uses, state.knowledge_sources if state is not None else {}):
            for identity, generation in values.items():
                if identity in self.retained_sources and self.retained_sources[identity] != generation:
                    raise KnowledgeAccessError("knowledge_source_changed")
                self.retained_sources[identity] = generation
        for identity, proof in (getattr(self.original.ctx, "knowledge_navigation_uses", None) or {}).items():
            if identity in self.retained_navigation and self.retained_navigation[identity] != proof:
                raise KnowledgeAccessError("knowledge_source_changed")
            self.retained_navigation[identity] = deepcopy(proof)
        from bowhead_core.context_compaction import message_digest

        # Knowledge provenance has already been parsed and recorded in the
        # original context's source/navigation maps. Retain only the other
        # authority proofs, never full historical Tool-result bodies.
        for message in messages:
            if message.name in {"context_attachment_dependencies_v1", "context_material_dependencies_v1"}:
                self.authority_dependencies[message_digest([message])] = deepcopy(message)
                from bowhead_core.context_attachments import AttachmentContextDependencies

                model = AttachmentContextDependencies if message.name == "context_attachment_dependencies_v1" else MaterialContextDependencies
                self.prepared_dependencies[message.name, message.content] = model.model_validate_json(message.content)

    async def __call__(self, messages: list[Message]) -> None:
        if not self._dependencies(messages).issubset(self.verified):
            raise ValueError("context_source_preparation_changed")
        await self.original.validate(messages, read_bytes=False)

    def for_adoption(self, connection=None) -> Callable[[list[Message]], Awaitable[None]]:
        """Deduplicate exact material checks only inside one storage operation."""
        validated_materials: set[str] = set()
        dependencies = list(self.authority_dependencies.values())

        async def validate(messages: list[Message]) -> None:
            if not self._dependencies(messages).issubset(self.verified):
                raise ValueError("context_source_preparation_changed")
            # Authority readers may use a separate database. Recheck them at
            # each adoption boundary; only immutable provenance is prepared.
            await self.original.validate(
                dependencies,
                read_bytes=False,
                validated_materials=validated_materials,
                connection=connection,
                prepared_dependencies=self.prepared_dependencies,
                retained_sources=self.retained_sources,
                retained_navigation=self.retained_navigation,
            )
            if not self._source_messages(messages).issubset(self.verified_messages):
                # Changed Tool provenance keeps the original current reader.
                await self.original.validate(messages, read_bytes=False, validated_materials=validated_materials, connection=connection)

        return validate


def adoption_source_validator(validator: Callable[[list[Message]], Awaitable[None]], connection=None) -> Callable[[list[Message]], Awaitable[None]]:
    """Never retain current-authority evidence between transactions or retries."""
    return validator.for_adoption(connection) if isinstance(validator, PreparedContextSourceValidator) else validator
