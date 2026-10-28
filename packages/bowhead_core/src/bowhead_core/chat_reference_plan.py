"""Reference requirements in the existing Plan graph, not a second scheduler.

The proposal points at immutable source material. Each selected step receives
its own ordinary Agent execution and must prove the current Plan worker lease.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, model_validator

from bowhead_core.chat_reference_storage import _row
from bowhead_core.chat_references import ContentHash, Identity, decode_reference_json

if TYPE_CHECKING:
    from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime
    from bowhead_core.chat_reference_storage import ReferenceCipher, ReferenceScope
    from bowhead_core.orchestrator import Orchestrator
    from bowhead_core.plan_store import PlanStep
    from bowhead_core.skill_read_resolution import SkillReadSourceResolver
    from bowhead_core.tool_registry import ToolRegistry
    from bowhead_core.types import TurnRequest

CONTRACT = "reference-plan.v1"


def require_readonly_definition(definition: Mapping[str, Any], registry: ToolRegistry) -> None:
    """Use the existing dependency parser and Bowhead-owned Tool effects."""
    from bowhead_core.agent_dependency_compiler import declared_dependencies

    tools, grants, legacy = declared_dependencies(definition)
    if legacy or any(grant.permissions != ("read",) for grant in grants):
        raise ValueError("reference_plan_readonly_baseline_required")
    require_readonly_tools(tools, registry)


def require_readonly_tools(tools: Sequence[str], registry: ToolRegistry | None) -> None:
    """Share the current native Tool effect check with metadata discovery."""
    from bowhead_core.terminal_effect_contract import EffectClass

    for name in tools:
        if registry is None:
            raise ValueError("reference_plan_readonly_baseline_required")
        try:
            spec = registry.get(name)
        except KeyError:
            raise ValueError("reference_plan_readonly_baseline_required") from None
        if (
            spec.team_tool_binding is not None
            or spec.needs_approval
            or spec.effect_spec is None
            or spec.effect_spec.effect_class is not EffectClass.READ_ONLY
        ):
            raise ValueError("reference_plan_readonly_baseline_required")


class ReferencePlanRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract: str = CONTRACT
    source_turn_id: Identity
    source_hash: ContentHash
    source_execution_id: Identity
    composition_digest: ContentHash
    profile: Identity
    revision: int = Field(ge=1)
    node_ids: tuple[Identity, ...]
    main_steps: tuple[Identity, ...]

    @model_validator(mode="after")
    def validate_requirements(self) -> Self:
        if (
            self.contract != CONTRACT
            or not 1 <= len(self.node_ids) <= 4
            or len(set(self.node_ids)) != len(self.node_ids)
            or not 1 <= len(self.main_steps) <= 40
            or len(set(self.main_steps)) != len(self.main_steps)
        ):
            raise ValueError("reference_plan_requirements_invalid")
        return self

    def canonical_json(self) -> str:
        return json.dumps({"reference": self.model_dump(mode="json")}, sort_keys=True, separators=(",", ":"))


def read_requirements(spec_json: str) -> ReferencePlanRequirements | None:
    if not spec_json:
        return None
    raw = decode_reference_json(spec_json, max_bytes=65536, max_depth=12)
    if not isinstance(raw, dict):
        raise ValueError("reference_plan_requirements_invalid")
    if "reference" not in raw:
        return None
    if set(raw) != {"reference"}:
        raise ValueError("reference_plan_requirements_invalid")
    return ReferencePlanRequirements.model_validate_json(json.dumps(raw["reference"]), strict=True)


def graph_requirements(requirements: ReferencePlanRequirements, steps: Sequence[PlanStep], *, revision: int) -> ReferencePlanRequirements:
    """Assign selected material only to the original executor's active steps."""
    main = tuple(step.id for step in steps if step.agent_name == requirements.profile and step.status != "superseded")
    if (
        len(steps) > 40
        or not main
        or any(
            step.status != "superseded"
            and (
                (step.id in main and (not step.included or step.status == "skipped"))
                or (step.id in requirements.main_steps and step.agent_name != requirements.profile)
            )
            for step in steps
        )
    ):
        raise ValueError("reference_plan_required_step_missing")
    return ReferencePlanRequirements.model_validate({**requirements.model_dump(), "revision": revision, "main_steps": main})


async def refresh_graph_in_connection(db: aiosqlite.Connection, plan_id: str) -> None:
    """Run inside the existing graph/revision transaction before commit."""
    from bowhead_core.plan_store import _row_to_step

    row = await _row(db, "SELECT spec_json,current_rev FROM plans WHERE id=?", (plan_id,))
    requirements = read_requirements(row["spec_json"]) if row is not None else None
    if requirements is None:
        return
    async with db.execute("SELECT * FROM plan_steps WHERE plan_id=? ORDER BY seq,id", (plan_id,)) as cursor:
        cursor.row_factory = aiosqlite.Row
        steps = [_row_to_step(item) for item in await cursor.fetchall()]
    updated = graph_requirements(requirements, steps, revision=row["current_rev"])
    await db.execute("UPDATE plans SET spec_json=? WHERE id=?", (updated.canonical_json(), plan_id))


async def read_original_material(db, *, sources, cipher, scope, requirements, plan_id=None):
    from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
    from bowhead_core.chat_reference_execution import read_reference_skill_primary_in_connection
    from bowhead_core.skill_read_extensions import current_extension_scope, revalidate_extension_sources
    from bowhead_core.skill_read_receiver import require_initial_skill_read_baseline
    from bowhead_core.skill_reference_execution import restore_skill_primary_composition_in_connection

    db.row_factory = aiosqlite.Row
    scope = await current_extension_scope(db, scope, requirements.source_turn_id)
    primary = await read_reference_skill_primary_in_connection(
        db, scope=scope, turn_id=requirements.source_turn_id, bindings=AgentExecutionBindingStore(sources.db_path), retained_plan_id=plan_id
    )
    if (
        primary.plan.orchestration_contract != "lead-delegation.v1"
        or primary.binding.status != "admitted"
        or primary.binding.agent_source_kind != "team"
        or primary.binding.runtime_profile_name != requirements.profile
        or primary.binding.execution_id != requirements.source_execution_id
        or primary.plan.source_hash != requirements.source_hash
        or tuple(item.node_id for item in primary.plan.admissions) != requirements.node_ids
        or any(item.resource.kind not in {"agent", "skill"} for item in primary.plan.admissions)
        or primary.automatic_receiver is not None
        or primary.source.attachment_ids
    ):
        raise ValueError("reference_plan_source_changed")
    composition, prepared, rendered = await restore_skill_primary_composition_in_connection(
        db, primary=primary, scope=scope, generation=1, cipher=cipher
    )
    if composition.digest != requirements.composition_digest:
        raise ValueError("reference_plan_source_changed")
    require_initial_skill_read_baseline(prepared.receiver, prepared.closure, prepared.baseline)
    if prepared.extension is not None:
        await revalidate_extension_sources(db, sources, prepared)
    else:
        for package in prepared.baseline.packages if prepared.baseline is not None else prepared.closure.items:
            await sources.revalidate_in_connection(db, package.proof)
    return primary, composition, rendered, prepared


async def proposal_requirements(runtime: ReferencePrimaryRuntime, steps: Sequence[PlanStep], registry: ToolRegistry) -> ReferencePlanRequirements:
    from bowhead_core._db import open_shared_ctx

    async with open_shared_ctx(runtime.bindings.db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        primary = await runtime._read(db)
        require_readonly_definition(primary.binding.definition, registry)
        composition = await runtime._composition(db, primary)
        requirements = ReferencePlanRequirements(
            source_turn_id=runtime.turn_id,
            source_hash=primary.plan.source_hash,
            source_execution_id=primary.binding.execution_id,
            composition_digest=composition.digest,
            profile=primary.binding.runtime_profile_name,
            revision=1,
            node_ids=tuple(item.node_id for item in primary.plan.admissions),
            main_steps=tuple(step.id for step in steps if step.agent_name == primary.binding.runtime_profile_name),
        )
        await read_original_material(db, sources=runtime.skill_sources, cipher=runtime.cipher, scope=runtime.scope, requirements=requirements)
        requirements = graph_requirements(requirements, steps, revision=1)
        await db.rollback()
    return requirements


async def read_plan_association(db: aiosqlite.Connection, *, turn_id: str) -> dict[str, Any] | None:
    """Read the existing Plan graph and exact executions without restoring text."""
    row = await _row(
        db,
        "SELECT p.*,r.composition_digest,r.execution_id AS source_execution_id,source.source_hash,e.runtime_profile_name,"
        "e.agent_id,e.revision_ref,e.release_id,e.policy_fingerprint,e.capability_fingerprint,w.team_id "
        "FROM plans p JOIN chat_reference_resolutions r ON r.turn_id=? AND r.resolution_seq=1 "
        "JOIN chat_reference_plans source ON source.turn_id=r.turn_id JOIN turns t ON t.id=source.turn_id "
        "JOIN sessions session ON session.id=t.session_id JOIN workspaces w ON w.id=session.workspace_id "
        "JOIN turn_agent_execution e ON e.id=r.execution_id AND e.turn_id=t.id "
        "WHERE json_extract(CASE WHEN json_valid(p.spec_json) THEN p.spec_json ELSE '{}' END,'$.reference.source_turn_id')=r.turn_id "
        "AND p.session_id=t.session_id AND p.user_id=session.user_id AND p.workspace_id=session.workspace_id "
        "AND e.agent_source_kind='team' AND e.team_id=w.team_id AND e.status='admitted' AND e.started_at IS NULL "
        "AND json_extract(source.plan_json,'$.orchestration_contract')='lead-delegation.v1'",
        (turn_id,),
    )
    if row is None:
        return None
    requirements = read_requirements(row["spec_json"])
    if requirements is None or (
        requirements.source_turn_id,
        requirements.source_execution_id,
        requirements.composition_digest,
        requirements.source_hash,
        requirements.profile,
        requirements.revision,
    ) != (turn_id, row["source_execution_id"], row["composition_digest"], row["source_hash"], row["runtime_profile_name"], row["current_rev"]):
        raise ValueError("reference_plan_source_changed")
    from bowhead_core.plan_store import _row_to_step

    async with db.execute("SELECT * FROM plan_steps WHERE plan_id=? ORDER BY seq,id", (row["id"],)) as cursor:
        cursor.row_factory = aiosqlite.Row
        steps = [_row_to_step(item) for item in await cursor.fetchall()]
    if graph_requirements(requirements, steps, revision=row["current_rev"]) != requirements:
        raise ValueError("reference_plan_required_step_changed")
    result = {
        "plan_id": row["id"],
        "revision": row["current_rev"],
        "status": row["status"],
        "execution_turn_id": row["execution_turn_id"],
        "steps": [],
    }
    for step in steps:
        if step.id not in requirements.main_steps:
            continue
        prefix = f"step:{step.id}:rev:"
        async with db.execute(
            "SELECT e.* FROM turn_agent_execution e JOIN turns t ON t.id=e.turn_id "
            "WHERE e.dispatch_source='plan' AND t.session_id=? AND substr(e.dispatch_key,1,?)=? ORDER BY e.created_at,e.id LIMIT 101",
            (row["session_id"], len(prefix), prefix),
        ) as cursor:
            cursor.row_factory = aiosqlite.Row
            executions = await cursor.fetchall()
        if len(executions) > 100:
            raise ValueError("reference_plan_execution_history_truncated")
        authorized = []
        for execution in executions:
            parts = execution["dispatch_key"][len(prefix) :].split(":")
            if (
                len(parts) != 3
                or parts[1] != "attempt"
                or not all(value.isdecimal() and str(int(value)) == value and int(value) > 0 for value in (parts[0], parts[2]))
                or int(parts[0]) > requirements.revision
                or any(
                    execution[key] != row[key]
                    for key in ("team_id", "agent_id", "revision_ref", "release_id", "policy_fingerprint", "capability_fingerprint")
                )
                or execution["agent_source_kind"] != "team"
            ):
                raise ValueError("reference_plan_execution_changed")
            authorized.append(
                {
                    "execution_id": execution["id"],
                    "turn_id": execution["turn_id"],
                    "revision": int(parts[0]),
                    "attempt": int(parts[2]),
                    "status": execution["status"],
                }
            )
        result["steps"].append({"step_id": step.id, "status": str(step.status), "executions": authorized})
    return result


async def proposal_pause_ready(db: aiosqlite.Connection, *, turn_id: str) -> bool:
    from bowhead_core.plan_store import PlanStatus

    try:
        association = await read_plan_association(db, turn_id=turn_id)
        # The original proposal has handed execution to the Plan. Finishing or
        # cancelling that Plan does not turn the handoff into a lost question.
        return association is not None and association["status"] in set(PlanStatus)
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class ReferencePlanStep:
    plan_id: str
    revision: int
    step_id: str
    turn_id: str
    execution_id: str

    async def require(self, db, *, scope, source_turn_id, owner_id, generation):
        row = await _row(
            db,
            "SELECT * FROM chat_reference_plan_step_scope WHERE plan_id=? AND plan_revision=? AND step_id=? "
            "AND execution_turn_id=? AND execution_id=? AND source_turn_id=?",
            (self.plan_id, self.revision, self.step_id, self.turn_id, self.execution_id, source_turn_id),
        )
        if (
            row is None
            or not owner_id
            or type(generation) is not int
            or row["owner_id"] != owner_id
            or row["generation"] != generation
            or any(row[key] != value for key, value in scope.identity().items())
        ):
            raise ValueError("reference_plan_execution_fence_lost")
        return read_requirements(row["spec_json"])


@dataclass(frozen=True, slots=True)
class ReferencePlanRuntime:
    """Live adapter around the existing Plan worker and source reader."""

    sources: SkillReadSourceResolver = field(repr=False)
    cipher: ReferenceCipher = field(repr=False)
    scope: ReferenceScope
    requirements: ReferencePlanRequirements
    plan_id: str
    turn_id: str
    owner_id: str = field(repr=False)
    generation: int
    registry: ToolRegistry = field(repr=False)

    async def validate_in_connection(self, db):
        from bowhead_core.chat_reference_runtime_store import require_live_chat_worker_in_connection

        db.row_factory = aiosqlite.Row
        dispatch = await require_live_chat_worker_in_connection(
            db, scope=self.scope, turn_id=self.turn_id, owner_id=self.owner_id, generation=self.generation
        )
        plan = await _row(db, "SELECT * FROM plans WHERE id=?", (self.plan_id,))
        if (
            self.generation != 1
            or dispatch["dispatch_kind"] != "start"
            or dispatch["parent_turn_id"] is not None
            or plan is None
            or plan["execution_turn_id"] != self.turn_id
            or plan["status"] != "running"
            or plan["session_id"] != self.scope.session_id
            or plan["user_id"] != self.scope.user_id
            or plan["workspace_id"] != self.scope.workspace_id
            or plan["current_rev"] != self.requirements.revision
            or read_requirements(plan["spec_json"]) != self.requirements
        ):
            raise ValueError("reference_plan_execution_fence_lost")
        if await read_plan_association(db, turn_id=self.requirements.source_turn_id) is None:
            raise ValueError("reference_plan_source_changed")
        material = await read_original_material(
            db, sources=self.sources, cipher=self.cipher, scope=self.scope, requirements=self.requirements, plan_id=self.plan_id
        )
        require_readonly_definition(material[0].binding.definition, self.registry)
        return material

    def is_main_step(self, step_id: str) -> bool:
        return step_id in self.requirements.main_steps

    async def admit_step(self, *, runner, spec, ctx):
        from bowhead_core._db import open_shared_ctx
        from bowhead_core.agent_execution_binding import ExactAgentBindingExpectation

        async with open_shared_ctx(self.sources.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            primary, _, _, _ = await self.validate_in_connection(db)
            step = await _row(db, "SELECT * FROM plan_steps WHERE id=? AND plan_id=?", (spec.task_id, self.plan_id))
            if (
                not self.is_main_step(spec.task_id)
                or step is None
                or step["status"] != "running"
                or not step["included"]
                or step["agent_name"] != primary.binding.runtime_profile_name
            ):
                raise ValueError("reference_plan_step_changed")
            choice = getattr(spec, "agent_choice", None)
            if choice is not None and any(
                getattr(choice, key, None) != getattr(primary.binding, key)
                for key in ("agent_id", "revision_ref", "release_id", "runtime_profile_name")
            ):
                raise ValueError("reference_plan_receiver_changed")
            prefix = f"step:{spec.task_id}:rev:"
            previous = await _row(
                db,
                "SELECT COUNT(*) AS count,COALESCE(SUM(EXISTS(SELECT 1 FROM chat_reference_model_invocations i "
                "WHERE i.execution_id=e.id AND i.status IN ('pending','delivery_unknown'))),0) AS unresolved "
                "FROM turn_agent_execution e WHERE e.dispatch_source='plan' AND substr(e.dispatch_key,1,?)=?",
                (len(prefix), prefix),
            )
            if previous["unresolved"]:
                raise ValueError("reference_model_invocation_unresolved")
            attempt = previous["count"] + 1
            await db.rollback()
        resource = next(item.resource for item in primary.plan.admissions if item.resource.kind == "agent")
        return await runner.admit_execution_binding(
            profile_name=primary.binding.runtime_profile_name,
            parent_turn_id=self.turn_id,
            parent_ctx=ctx,
            dispatch_key=f"step:{spec.task_id}:rev:{self.requirements.revision}:attempt:{attempt}",
            agent_id=resource.id,
            expected=ExactAgentBindingExpectation(
                agent_id=resource.id,
                assignment_id=resource.assignment_id,
                revision_ref=resource.version,
                release_id=resource.release_id,
                behavior_fingerprint=primary.binding.behavior_descriptor.behavior_fingerprint,
            ),
            dispatch_source="plan",
        )

    async def run_step(self, *, runner, spec, binding, ctx, run_kwargs):
        from bowhead_core._db import open_shared_ctx
        from bowhead_core.skill_model_access import AuthorizedSkillModelInput
        from bowhead_core.skill_model_invocation import reference_model_invocation_scope
        from bowhead_core.skill_read_access import ReferenceSkillReadAccess

        target = ReferencePlanStep(self.plan_id, self.requirements.revision, spec.task_id, self.turn_id, binding.execution_id)
        access = await ReferenceSkillReadAccess.bind(
            sources=self.sources,
            cipher=self.cipher,
            scope=self.scope,
            turn_id=self.requirements.source_turn_id,
            owner_id=self.owner_id,
            generation=self.generation,
            plan_step=target,
        )
        async with open_shared_ctx(self.sources.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            _, _, rendered, _ = await self.validate_in_connection(db)
            await db.rollback()
        ctx.reference_skill_access = access
        ctx.reference_skill_required = True
        ctx.reference_source_db_path = str(self.sources.db_path)
        required = AuthorizedSkillModelInput.for_receiver(rendered, access=access, ctx=ctx)
        run_kwargs = {**run_kwargs, "parent_ctx": ctx, "pre_minted_subagent_id": access.subagent_id, "required_model_input": required}
        with reference_model_invocation_scope(access=access, ctx=ctx):
            return await runner.run(**run_kwargs)


async def prepare_plan_runtime(orchestrator: Orchestrator, request: TurnRequest) -> ReferencePlanRuntime | None:
    """Rebuild live authority for the production confirmation Turn."""
    import inspect
    import os
    from pathlib import Path

    from bowhead_core._db import open_shared_ctx
    from bowhead_core.chat_reference_scope import ReferenceRequestScope, require_reference_request_scope
    from bowhead_core.chat_reference_storage import ReferenceCipher, ReferenceScope
    from bowhead_core.skill_read_resolution import SkillReadSourceResolver

    if not request.plan_execution_id:
        return None
    store = orchestrator.session_store
    async with open_shared_ctx(store.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        plan = await _row(db, "SELECT spec_json FROM plans WHERE id=?", (request.plan_execution_id,))
        requirements = read_requirements(plan["spec_json"]) if plan is not None else None
        if requirements is None:
            return None
        request_scope = ReferenceRequestScope(
            team_id=request.team_id, user_id=request.user_id, workspace_id=request.workspace_id, session_id=request.session_id
        )
        epoch = await require_reference_request_scope(db, request_scope)
        await db.rollback()
    scope = ReferenceScope(**request_scope.model_dump(), policy_epoch=epoch)
    sources = orchestrator.reference_skill_sources_factory(request_scope)
    if inspect.isawaitable(sources):
        sources = await sources
    if type(sources) is not SkillReadSourceResolver or Path(sources.db_path).resolve() != Path(store.db_path).resolve():
        raise ValueError("reference_plan_runtime_unavailable")
    runtime = ReferencePlanRuntime(
        sources,
        ReferenceCipher(os.environ.get("BOWHEAD_FERNET_KEY", "")),
        scope,
        requirements,
        request.plan_execution_id,
        request.turn_id,
        request.execution_owner_id,
        request.execution_generation,
        orchestrator.tool_registry,
    )
    async with open_shared_ctx(store.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await runtime.validate_in_connection(db)
        await db.rollback()
    return runtime
