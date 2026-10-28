"""Access retained Skills for an exact live primary; no name-based package lookup.

The initial mixed scheduler binds this access for its versioned tool adapter.
Historical context reconstruction remains unsupported. Each call reauthorizes
the entire retained closure and receiver; a handle is a pointer to that check,
never a transferable permission or lease.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.agents._lead_context import get_current_subagent_id
from bowhead_core.chat_reference_execution import read_reference_skill_primary_in_connection
from bowhead_core.chat_reference_plan import ReferencePlanStep
from bowhead_core.chat_reference_runtime_store import require_reference_worker_in_connection
from bowhead_core.chat_reference_storage import ReferenceCipher, ReferenceScope
from bowhead_core.chat_references import SkillResourceRef, _bounded_json
from bowhead_core.skill_baseline_read import read_baseline_package, snapshot_skill_read_arguments
from bowhead_core.skill_read_receiver import (
    INVOKABLE_BASELINE_RUNTIME_POLICIES,
    PreparedReceiverSkillReads,
    SkillReadReceiverError,
    require_initial_skill_invocation_baseline,
    require_initial_skill_read_baseline,
)
from bowhead_core.skill_read_resolution import ResolvedSkillPackage, SkillReadSourceResolver
from bowhead_core.skill_reference_execution import ReferenceSkillPrimaryComposition, restore_skill_primary_composition_in_connection

if TYPE_CHECKING:
    import aiosqlite

    from bowhead_core.agent_dependency_compiler import ExactSkillGrant
    from bowhead_core.skill_read_render import RenderedSkillReads
    from bowhead_core.tool_registry import ToolExecutionContext


class SkillReadAccessError(ValueError):
    """Finite internal error without method text, paths or private parameters."""


def _require_supported_baseline(prepared: PreparedReceiverSkillReads, policy: str) -> None:
    try:
        if policy in INVOKABLE_BASELINE_RUNTIME_POLICIES:
            require_initial_skill_invocation_baseline(prepared.receiver, prepared.closure, prepared.baseline)
        else:
            require_initial_skill_read_baseline(prepared.receiver, prepared.closure, prepared.baseline)
    except SkillReadReceiverError:
        raise SkillReadAccessError("reference_skill_tool_baseline_unavailable") from None


@dataclass(frozen=True, slots=True)
class ResolvedSkillReadHandle:
    scope: ReferenceScope
    turn_id: str
    execution_id: str
    subagent_id: str
    generation: int
    composition_digest: str
    content_key: str
    resource: SkillResourceRef
    content_hash: str
    materialization_hash: str
    operation: Literal["read"] = "read"
    resolution_seq: Literal[1] = 1
    parameter_source: Literal["initial-source.v1", "agent-baseline.v1"] = "initial-source.v1"


def _handles(
    composition: ReferenceSkillPrimaryComposition,
    scope: ReferenceScope,
    generation: int,
    prepared: PreparedReceiverSkillReads,
    execution_id: str | None = None,
) -> tuple[ResolvedSkillReadHandle, ...]:
    execution_id = execution_id or composition.execution_id
    selected = tuple(
        ResolvedSkillReadHandle(
            scope=scope,
            turn_id=composition.source_turn_id,
            execution_id=execution_id,
            subagent_id="sub_" + hashlib.sha256(execution_id.encode()).hexdigest()[:20],
            generation=generation,
            composition_digest=composition.digest,
            content_key=composition.content.content_key,
            resource=member.resource,
            content_hash=member.content_hash,
            materialization_hash=member.materialization_hash,
        )
        for member in composition.members
    )
    if prepared.baseline is None:
        return selected
    return selected + tuple(
        ResolvedSkillReadHandle(
            scope=scope,
            turn_id=composition.source_turn_id,
            execution_id=execution_id,
            subagent_id="sub_" + hashlib.sha256(execution_id.encode()).hexdigest()[:20],
            generation=generation,
            composition_digest=composition.digest,
            content_key=composition.content.content_key,
            resource=item.proof.resource,
            content_hash=item.material.content_hash,
            materialization_hash=item.proof.resource.version,
            parameter_source="agent-baseline.v1",
        )
        for item in prepared.baseline.packages
    )


async def _load_current_in_connection(
    db,
    *,
    sources: SkillReadSourceResolver,
    cipher: ReferenceCipher,
    scope: ReferenceScope,
    turn_id: str,
    owner_id: str,
    generation: int,
    plan_step: ReferencePlanStep | None = None,
) -> tuple[ReferenceSkillPrimaryComposition, RenderedSkillReads, PreparedReceiverSkillReads]:
    # Caller owns BEGIN IMMEDIATE and the final commit/rollback fence.
    if plan_step is not None:
        from bowhead_core.chat_reference_plan import read_original_material

        requirements = await plan_step.require(db, scope=scope, source_turn_id=turn_id, owner_id=owner_id, generation=generation)
        _, composition, rendered, prepared = await read_original_material(
            db, sources=sources, cipher=cipher, scope=scope, requirements=requirements, plan_id=plan_step.plan_id
        )
        await plan_step.require(db, scope=scope, source_turn_id=turn_id, owner_id=owner_id, generation=generation)
        return composition, rendered, prepared
    from bowhead_core.skill_read_extensions import current_extension_scope, revalidate_extension_sources

    scope = await current_extension_scope(db, scope, turn_id)
    await require_reference_worker_in_connection(db, scope=scope, turn_id=turn_id, owner_id=owner_id, generation=generation)
    primary = await read_reference_skill_primary_in_connection(db, scope=scope, turn_id=turn_id, bindings=AgentExecutionBindingStore(sources.db_path))
    if primary.binding.status != "running":
        raise SkillReadAccessError("reference_skill_access_unavailable")
    composition, prepared, rendered = await restore_skill_primary_composition_in_connection(
        db, primary=primary, scope=scope, generation=generation, cipher=cipher
    )
    if prepared.extension is not None:
        await revalidate_extension_sources(db, sources, prepared)
    else:
        for item in prepared.baseline.packages if prepared.baseline is not None else prepared.closure.items:
            await sources.revalidate_in_connection(db, item.proof)
    # Time may pass while awaiting each member; never extend the lease
    # or return material after the original owner/retention fence ends.
    await require_reference_worker_in_connection(db, scope=scope, turn_id=turn_id, owner_id=owner_id, generation=generation)
    return composition, rendered, prepared


async def _load_current(
    *,
    sources: SkillReadSourceResolver,
    cipher: ReferenceCipher,
    scope: ReferenceScope,
    turn_id: str,
    owner_id: str,
    generation: int,
    plan_step: ReferencePlanStep | None = None,
) -> tuple[ReferenceSkillPrimaryComposition, RenderedSkillReads, PreparedReceiverSkillReads]:
    # Acquire the writer reservation before reading mutable authority, so a long
    # lived read transaction cannot silently authorize against an old snapshot.
    # All filesystem preparation was completed before the original claim.
    async with open_shared_ctx(sources.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            return await _load_current_in_connection(
                db, sources=sources, cipher=cipher, scope=scope, turn_id=turn_id, owner_id=owner_id, generation=generation, plan_step=plan_step
            )
        finally:
            await db.rollback()


@dataclass(frozen=True, slots=True)
class ReferenceSkillReadAccess:
    sources: SkillReadSourceResolver = field(repr=False)
    cipher: ReferenceCipher = field(repr=False)
    scope: ReferenceScope
    turn_id: str
    owner_id: str = field(repr=False)
    generation: int
    composition_digest: str
    handles: tuple[ResolvedSkillReadHandle, ...]
    execution_id: str
    extension_read_names: tuple[str, ...] = ()
    plan_step: ReferencePlanStep | None = None

    @property
    def receiver_turn_id(self) -> str:
        return self.plan_step.turn_id if self.plan_step is not None else self.turn_id

    async def require_worker_in_connection(self, db) -> None:
        if self.plan_step is not None:
            await self.plan_step.require(db, scope=self.scope, source_turn_id=self.turn_id, owner_id=self.owner_id, generation=self.generation)
        else:
            await require_reference_worker_in_connection(
                db, scope=self.scope, turn_id=self.turn_id, owner_id=self.owner_id, generation=self.generation
            )

    @property
    def subagent_id(self) -> str:
        return "sub_" + hashlib.sha256(self.execution_id.encode()).hexdigest()[:20]

    @property
    def source_db_path(self) -> str:
        return str(self.sources.db_path)

    @classmethod
    async def bind(
        cls,
        *,
        sources: SkillReadSourceResolver,
        cipher: ReferenceCipher,
        scope: ReferenceScope,
        turn_id: str,
        owner_id: str,
        generation: int,
        plan_step: ReferencePlanStep | None = None,
    ) -> ReferenceSkillReadAccess:
        """Server-only binding from the stored running owner and composition."""
        try:
            scope = ReferenceScope.model_validate(scope.model_dump(mode="python", warnings="error"))
            composition, _, prepared = await _load_current(
                sources=sources, cipher=cipher, scope=scope, turn_id=turn_id, owner_id=owner_id, generation=generation, plan_step=plan_step
            )
            execution_id = plan_step.execution_id if plan_step is not None else composition.execution_id
            return cls(
                sources,
                cipher,
                scope,
                turn_id,
                owner_id,
                generation,
                composition.digest,
                _handles(composition, scope, generation, prepared, execution_id),
                execution_id,
                tuple(sorted(item.proof.name for item in prepared.closure.items)) if prepared.extension else (),
                plan_step,
            )
        except Exception:
            raise SkillReadAccessError("reference_skill_access_unavailable") from None

    def _require_receiver(self, ctx: ToolExecutionContext, handle: ResolvedSkillReadHandle | None = None) -> None:
        # The shared ctx's _current_subagent_id is not a concurrency authority.
        # Actual SubagentRunner calls set/reset this per-task ContextVar.
        if (
            (
                handle is not None
                and (
                    type(handle) is not ResolvedSkillReadHandle
                    or type(handle.generation) is not int
                    or type(handle.resolution_seq) is not int
                    or type(handle.scope.policy_epoch) is not int
                    or handle.execution_id != self.execution_id
                    or handle.subagent_id != self.subagent_id
                )
            )
            or type(self.generation) is not int
            or type(self.scope.policy_epoch) is not int
            or any(getattr(ctx, key, None) != value for key, value in {**self.scope.identity(), "turn_id": self.receiver_turn_id}.items())
            or get_current_subagent_id() != self.subagent_id
        ):
            raise SkillReadAccessError("reference_skill_receiver_changed")

    async def read(self, handle: ResolvedSkillReadHandle, *, ctx: ToolExecutionContext, file: str | None = None) -> dict:
        return await self._read(handle, ctx=ctx, file=file)

    def require_model_receiver(self, ctx: ToolExecutionContext) -> None:
        if ctx.reference_skill_required is not True or ctx.reference_skill_access is not self:
            raise SkillReadAccessError("reference_skill_access_unavailable")
        self._require_receiver(ctx)

    def _check_model_material(
        self,
        material: tuple[ReferenceSkillPrimaryComposition, RenderedSkillReads, PreparedReceiverSkillReads],
        *,
        ctx: ToolExecutionContext,
    ) -> tuple[ReferenceSkillPrimaryComposition, RenderedSkillReads, PreparedReceiverSkillReads]:
        self.require_model_receiver(ctx)
        composition, _, prepared = material
        if (
            composition.digest != self.composition_digest or (self.plan_step is None and composition.execution_id != self.execution_id)
        ) or self.handles != _handles(composition, self.scope, self.generation, prepared, self.execution_id):
            raise SkillReadAccessError("reference_skill_handle_changed")
        _require_supported_baseline(prepared, composition.runtime_policy)
        return material

    async def read_model_material_in_connection(
        self, db: aiosqlite.Connection, *, ctx: ToolExecutionContext
    ) -> tuple[ReferenceSkillPrimaryComposition, RenderedSkillReads, PreparedReceiverSkillReads]:
        """Reauthorize original input inside the caller's receipt transaction."""
        self.require_model_receiver(ctx)
        material = await _load_current_in_connection(
            db,
            sources=self.sources,
            cipher=self.cipher,
            scope=self.scope,
            turn_id=self.turn_id,
            owner_id=self.owner_id,
            generation=self.generation,
            plan_step=self.plan_step,
        )
        return self._check_model_material(material, ctx=ctx)

    async def read_model_input(self, *, ctx: ToolExecutionContext) -> RenderedSkillReads:
        """Authorize the entire original input at the actual receiver boundary."""
        try:
            await ctx.ensure_reference_skill_context()
            self.require_model_receiver(ctx)
            material = await _load_current(
                sources=self.sources,
                cipher=self.cipher,
                scope=self.scope,
                turn_id=self.turn_id,
                owner_id=self.owner_id,
                generation=self.generation,
                plan_step=self.plan_step,
            )
            _, rendered, _ = self._check_model_material(material, ctx=ctx)
            return rendered
        except Exception:
            raise SkillReadAccessError("reference_skill_access_unavailable") from None

    async def _invocation_package(
        self, tool_name: str, arguments: dict, ctx: ToolExecutionContext
    ) -> tuple[ResolvedSkillPackage, tuple[ExactSkillGrant, ...]]:
        if self.plan_step is not None or tool_name not in {"skill_invoke", "python_exec"}:
            raise SkillReadAccessError("reference_skill_operation_unavailable")
        name = "python_sandbox" if tool_name == "python_exec" else str(arguments.get("name") or "").strip()
        await ctx.ensure_reference_skill_context()
        if ctx.reference_skill_required is not True or ctx.reference_skill_access is not self:
            raise SkillReadAccessError("reference_skill_access_unavailable")
        self._require_receiver(ctx)
        composition, _, prepared = await _load_current(
            sources=self.sources,
            cipher=self.cipher,
            scope=self.scope,
            turn_id=self.turn_id,
            owner_id=self.owner_id,
            generation=self.generation,
            plan_step=self.plan_step,
        )
        if (
            composition.runtime_policy not in INVOKABLE_BASELINE_RUNTIME_POLICIES
            or (composition.digest != self.composition_digest or composition.execution_id != self.execution_id)
            or self.handles != _handles(composition, self.scope, self.generation, prepared, self.execution_id)
        ):
            raise SkillReadAccessError("reference_skill_operation_unavailable")
        _require_supported_baseline(prepared, composition.runtime_policy)
        grant = next((grant for grant in prepared.receiver.grants if grant.name == name and "invoke" in grant.permissions), None)
        if grant is None or ctx.skill_runtime_grants != prepared.receiver.grants:
            raise SkillReadAccessError("reference_skill_operation_unavailable")
        if prepared.baseline is None:
            raise SkillReadAccessError("reference_skill_operation_unavailable")
        package = next(
            (
                item
                for item in prepared.baseline.packages
                if (item.proof.resource.source_kind, item.proof.resource.id, item.proof.resource.version) == (grant.kind, grant.id, grant.revision)
            ),
            None,
        )
        if package is None:
            raise SkillReadAccessError("reference_skill_operation_unavailable")
        self._require_receiver(ctx)
        if ctx.reference_skill_required is not True or ctx.reference_skill_access is not self:
            raise SkillReadAccessError("reference_skill_access_unavailable")
        return package, prepared.receiver.grants

    async def authorize_tool(self, tool_name: str, arguments: dict, ctx: ToolExecutionContext) -> None:
        if tool_name == "skill_read":
            await self.read_tool(arguments, ctx)
        else:
            await self._invocation_package(tool_name, arguments, ctx)

    async def execute_tool(self, tool_name: str, handler: Callable[..., Awaitable[Any]], arguments: dict, ctx: ToolExecutionContext) -> Any:
        from bowhead_core.assigned_skill_execution import dispatch_retained_reference_skill

        try:
            _bounded_json(arguments, max_depth=20, max_bytes=1024 * 1024)
            arguments = json.loads(json.dumps(arguments, ensure_ascii=False, allow_nan=False))
            ctx = copy.copy(ctx)
            package, grants = await self._invocation_package(tool_name, arguments, ctx)
        except Exception:
            return {"ok": False, "error_kind": "reference_skill_unavailable", "error": "The exact Skill invocation context is unavailable."}

        async def authorize(current_tool: str, current_arguments: dict, current_ctx: ToolExecutionContext) -> None:
            current_package, current_grants = await self._invocation_package(current_tool, current_arguments, current_ctx)
            if current_package != package or current_grants != grants:
                raise SkillReadAccessError("reference_skill_handle_changed")

        return await dispatch_retained_reference_skill(
            handler, arguments, ctx, sources=self.sources, package=package, grants=grants, tool_name=tool_name, authorizer=authorize
        )

    async def read_tool(self, arguments: dict, ctx: ToolExecutionContext) -> dict:
        # Each policy preserves the entire original baseline. Read-only policies
        # never inherit invocation rights from the new complete-baseline policy.
        try:
            arguments = snapshot_skill_read_arguments(arguments)
        except ValueError:
            raise SkillReadAccessError("reference_skill_arguments_unavailable") from None
        return await self._read(None, ctx=ctx, name=arguments["name"], file=arguments.get("file") or None, arguments=arguments)

    async def _read(
        self,
        handle: ResolvedSkillReadHandle | None,
        *,
        ctx: ToolExecutionContext,
        file: str | None = None,
        name: str | None = None,
        arguments: dict | None = None,
    ) -> dict:
        """Read original selected-method data or the separate Agent baseline.

        Selected methods keep their original JSON parameters and declarations.
        Baseline SOP arguments use the ordinary bounded substitution contract.
        Every file comes from retained bytes, never from the current filesystem.
        The returned data may only be used at this checked boundary. A later
        model/tool invocation must authorize again, including after revocation.
        """
        try:
            self._require_receiver(ctx, handle)
            composition, rendered, prepared = await _load_current(
                sources=self.sources,
                cipher=self.cipher,
                scope=self.scope,
                turn_id=self.turn_id,
                owner_id=self.owner_id,
                generation=self.generation,
                plan_step=self.plan_step,
            )
            canonical = _handles(composition, self.scope, self.generation, prepared, self.execution_id)
            selected_members = rendered.document()["skills"]
            if name is not None:
                _require_supported_baseline(prepared, composition.runtime_policy)
                if prepared.baseline is not None:
                    resources = [item.proof.resource for item in prepared.baseline.packages if item.proof.name == name]
                    matches = [current for current in canonical if current.parameter_source == "agent-baseline.v1" and current.resource in resources]
                    # Explicit selected entry files keep their original JSON
                    # parameters. Ordinary name reads preserve the Agent's SOP.
                    selected = next(
                        (member for member in selected_members if member["resource"] in [resource.model_dump(mode="json") for resource in resources]),
                        None,
                    )
                    if selected is not None and file and any(item["path"] == file for item in selected["files"]):
                        if any(arguments.get(key, "") != "" for key in ("ref", "args")):
                            raise SkillReadAccessError("reference_skill_arguments_unavailable")
                        matches = [
                            current for current in canonical if current.parameter_source == "initial-source.v1" and current.resource in resources
                        ]
                else:
                    if any(arguments.get(key, "") != "" for key in ("ref", "args")):
                        raise SkillReadAccessError("reference_skill_arguments_unavailable")
                    matches = [current for current, item in zip(canonical, prepared.closure.items, strict=True) if item.proof.name == name]
                if not matches and prepared.extension is not None:
                    if any((arguments or {}).get(key, "") != "" for key in ("ref", "args")):
                        raise SkillReadAccessError("reference_skill_arguments_unavailable")
                    resources = [item.proof.resource for item in prepared.closure.items if item.proof.name == name]
                    matches = [current for current in canonical if current.parameter_source == "initial-source.v1" and current.resource in resources]
                if len(matches) != 1:
                    raise SkillReadAccessError("reference_skill_alias_unavailable")
                handle = matches[0]
            self._require_receiver(ctx, handle)
            # Compare with independently rebuilt canonical handles, never just
            # the caller's cached handle list or a matching display name.
            if (
                composition.digest != self.composition_digest or (self.plan_step is None and composition.execution_id != self.execution_id)
            ) or handle not in canonical:
                raise SkillReadAccessError("reference_skill_handle_changed")
            if handle.parameter_source == "agent-baseline.v1":
                _require_supported_baseline(prepared, composition.runtime_policy)
                package = next(item for item in prepared.baseline.packages if item.proof.resource == handle.resource)
                invoke_allowed = composition.runtime_policy in INVOKABLE_BASELINE_RUNTIME_POLICIES and any(
                    grant.name == package.proof.name and "invoke" in grant.permissions for grant in prepared.receiver.grants
                )
                result = read_baseline_package(
                    package.material, arguments or {"name": package.proof.name, **({"file": file} if file else {})}, invoke_allowed=invoke_allowed
                )
                selected = next((member for member in selected_members if member["resource"] == handle.resource.model_dump(mode="json")), None)
                if selected is not None and not file and not (arguments or {}).get("ref"):
                    result["selected_method"] = selected
                return result
            member = next(item for item in selected_members if item["resource"] == handle.resource.model_dump(mode="json"))
            if file is not None:
                if type(file) is not str or not any(item["path"] == file for item in member["files"]):
                    raise SkillReadAccessError("reference_skill_file_unavailable")
                member["files"] = [item for item in member["files"] if item["path"] == file]
            return member
        except SkillReadAccessError:
            raise
        except Exception:
            raise SkillReadAccessError("reference_skill_access_unavailable") from None


def reference_read_scope(original, ctx):
    """Add only selected read names to the receiver's call-local early filter.

    This grants no tool, exact identity or invoke permission. Every read still
    resolves a canonical retained handle under fresh worker/source/policy checks.
    """
    from bowhead_skill.runtime_scope import ExactSkillRuntimeScope

    access = getattr(ctx, "reference_skill_access", None)
    if type(access) is not ReferenceSkillReadAccess or not access.extension_read_names:
        return original
    access._require_receiver(ctx)
    if original is None or getattr(ctx, "reference_skill_required", False) is not True:
        raise SkillReadAccessError("reference_skill_receiver_changed")
    return ExactSkillRuntimeScope(tuple(sorted(set(original.read_names) | set(access.extension_read_names))), original.invoke_names)
