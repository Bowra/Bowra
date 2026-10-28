"""Prepare and exclusively claim the exact initial @Agent execution.

The reader grants no ownership. The standalone claim commits ownership and the
running transition together, but does not schedule a worker or prove resource use.
The HTTP writer and legacy execution barriers remain unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

import aiosqlite

from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore, ExactAgentExecutionBinding
from bowhead_core.chat_reference_storage import ChatReferenceStore, ReferencePlan, ReferenceScope
from bowhead_core.chat_references import (
    AgentResourceRef,
    CapabilityResourceRef,
    McpPromptResourceRef,
    McpSourceResourceRef,
    McpToolResourceRef,
    ReferenceNode,
    SkillResourceRef,
    requested_reference_operation,
)
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.session_store import _read_chat_turn_user_source_in_tx, _read_legacy_continuation_parent_in_tx

if TYPE_CHECKING:
    from bowhead_core.reference_mcp_execution import McpReferenceWorkerPreparation
    from bowhead_core.reference_mcp_prompt_execution import McpPromptReferenceWorkerPreparation
    from bowhead_core.skill_reference_execution import SkillReferenceWorkerPreparation


@dataclass(frozen=True, slots=True)
class ReferencePrimaryInput:
    source: ChatTurnUserSource
    plan: ReferencePlan
    binding: ExactAgentExecutionBinding
    automatic_receiver: AgentResourceRef | None = None


@dataclass(frozen=True, slots=True)
class ReferencePrimaryWorkerClaim:
    outcome: Literal["claimed", "already_running"]
    primary: ReferencePrimaryInput
    execution_generation: int


def _now() -> datetime:
    return datetime.now(UTC)


async def claim_reference_primary_for_worker(
    *,
    scope: ReferenceScope,
    turn_id: str,
    owner_id: str,
    expected_generation: int,
    bindings: AgentExecutionBindingStore,
    lease_seconds: int = 30,
) -> ReferencePrimaryWorkerClaim:
    """Commit one initial dispatch/execution winner, without scheduling anything.

    A duplicate (including the same owner retrying after an uncertain commit)
    only receives the existing execution pointer. It must never start another
    worker. Expiry, cancellation, resume and terminal recovery are not takeovers.
    Any future scheduler must recheck this lease immediately before side effects.
    """
    return await _claim_reference_primary_for_worker(
        scope=scope,
        turn_id=turn_id,
        owner_id=owner_id,
        expected_generation=expected_generation,
        bindings=bindings,
        lease_seconds=lease_seconds,
    )


async def _claim_reference_primary_for_worker(
    *,
    scope: ReferenceScope,
    turn_id: str,
    owner_id: str,
    expected_generation: int,
    bindings: AgentExecutionBindingStore,
    lease_seconds: int = 30,
    skills: SkillReferenceWorkerPreparation | None = None,
    mcp: McpReferenceWorkerPreparation | None = None,
    mcp_prompt: McpPromptReferenceWorkerPreparation | None = None,
) -> ReferencePrimaryWorkerClaim:
    """Shared initial ownership transaction; broader materials are server-only."""
    scope = ReferenceScope.model_validate(scope.model_dump(mode="python", exclude_none=True))
    if (
        not isinstance(owner_id, str)
        or not owner_id.strip()
        or type(expected_generation) is not int
        or expected_generation < 1
        or type(lease_seconds) is not int
        or not 1 <= lease_seconds <= 300
        or sum(item is not None for item in (skills, mcp, mcp_prompt)) > 1
    ):
        raise ValueError("reference_worker_claim_invalid")
    preparation = skills if skills is not None else mcp if mcp is not None else mcp_prompt
    async with open_shared_ctx(str(bindings.db_path)) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        try:
            # Retain the persisted Terminal protocol fence, including v1 history
            # with a cleared active marker. This is only the initial legacy contract.
            parent = await _read_legacy_continuation_parent_in_tx(db, turn_id=turn_id, session_id=scope.session_id)
            if parent is None:
                raise ValueError("reference_execution_fence_lost")
            reader = (
                read_reference_skill_primary_in_connection
                if skills is not None
                else read_reference_mcp_primary_in_connection
                if mcp is not None
                else read_reference_mcp_prompt_primary_in_connection
                if mcp_prompt is not None
                else read_reference_primary_in_connection
            )
            primary = await reader(db, scope=scope, turn_id=turn_id, bindings=bindings)
            deferred = primary.plan.orchestration_contract == "lead-delegation.v1"
            if deferred and await _lead_contract_missing(
                db,
                mcp=mcp is not None,
                prompt=mcp_prompt is not None,
                skill_only=primary.automatic_receiver is not None,
                capability=any(item.resource.kind == "capability" for item in primary.plan.admissions),
            ):
                raise ValueError("reference_lead_migration_required")
            if primary.automatic_receiver is not None:
                async with db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_reference_automatic_composition'") as cursor:
                    if skills is None or await cursor.fetchone() is None:
                        raise ValueError("reference_automatic_runtime_unavailable")
            async with db.execute(
                "SELECT d.*,t.execution_generation,t.status AS turn_status "
                "FROM chat_turn_spawn_dispatch d JOIN turns t ON t.id=d.turn_id WHERE d.turn_id=?",
                (turn_id,),
            ) as cursor:
                dispatch = await cursor.fetchone()
            # Read time after lock acquisition AND authorization reads. An expired
            # accepted dispatch is for recovery, not a fresh lease for a late worker.
            now = _now()
            deadline = datetime.fromisoformat(dispatch["lease_expires_at"]) if dispatch is not None else None
            from bowhead_core.chat_reference_runtime_store import (
                reference_dispatch_origin_valid_in_connection,
            )

            valid_dispatch_origin = await reference_dispatch_origin_valid_in_connection(
                db,
                row=dispatch,
                scope=scope,
                turn_id=turn_id,
            )
            if (
                dispatch is None
                or dispatch["session_id"] != scope.session_id
                or not valid_dispatch_origin
                or dispatch["lease_kind"] != "turn"
                or dispatch["cancel_requested_at"] is not None
                or dispatch["turn_status"] != "running"
                or dispatch["execution_generation"] != expected_generation
                or dispatch["lease_execution_generation"] != expected_generation
                or expected_generation != 1
                or deadline is None
                or deadline.tzinfo is None
                or deadline <= now
            ):
                raise ValueError("reference_execution_fence_lost")
            if (
                dispatch["state"] == "spawned"
                and dispatch["owner_id"]
                and (primary.binding.status == "running" or deferred and primary.binding.status == "admitted")
            ):
                from bowhead_core.chat_reference_runtime_store import require_primary_composition_in_connection

                if preparation is not None:
                    await preparation.require_existing(db, primary=primary, scope=scope, generation=expected_generation)
                else:
                    await require_primary_composition_in_connection(db, primary=primary, generation=expected_generation)
                await db.rollback()
                return ReferencePrimaryWorkerClaim("already_running", primary, expected_generation)
            if dispatch["state"] != "claimed" or dispatch["owner_id"] is not None or primary.binding.status != "admitted":
                raise ValueError("reference_execution_fence_lost")
            from bowhead_core.chat_reference_runtime_store import put_primary_composition_in_connection

            if preparation is not None:
                await preparation.revalidate(db, primary=primary, scope=scope)
            else:
                await put_primary_composition_in_connection(db, primary=primary, generation=expected_generation)
            now = _now()
            if deadline <= now:
                raise ValueError("reference_execution_fence_lost")
            # BEGIN IMMEDIATE serializes both updates with Stop, other claimers and
            # authority changes. A failed CAS or cancellation rolls both back.
            if not deferred:
                updated = await db.execute(
                    "UPDATE turn_agent_execution SET status='running',started_at=? WHERE id=? AND turn_id=? AND status='admitted' AND started_at IS NULL",
                    (now.isoformat(), primary.binding.execution_id, turn_id),
                )
                if updated.rowcount != 1:
                    raise ValueError("reference_primary_claim_conflict")
            updated = await db.execute(
                "UPDATE chat_turn_spawn_dispatch SET state='spawned',owner_id=?,lease_expires_at=?,updated_at=? "
                "WHERE turn_id=? AND state='claimed' AND owner_id IS NULL AND cancel_requested_at IS NULL "
                "AND lease_execution_generation=?",
                (owner_id, (now + timedelta(seconds=lease_seconds)).isoformat(), now.isoformat(), turn_id, expected_generation),
            )
            if updated.rowcount != 1:
                raise ValueError("reference_primary_claim_conflict")
            if preparation is not None:
                await preparation.persist(db, primary=primary, scope=scope, generation=expected_generation, owner_id=owner_id)
                # Material writes are inside this claim, never a later transaction.
                # The original accepted lease must survive all preparation writes.
                if deadline <= _now():
                    raise ValueError("reference_execution_fence_lost")
            await db.commit()
            status = "admitted" if deferred else "running"
            return ReferencePrimaryWorkerClaim("claimed", replace(primary, binding=replace(primary.binding, status=status)), expected_generation)
        except BaseException:
            await db.rollback()
            raise


async def _lead_contract_missing(
    db: aiosqlite.Connection, *, skill_only: bool = False, capability: bool = False, mcp: bool = False, prompt: bool = False
) -> bool:
    names = ("trg_reference_lead_delegation", "trg_reference_lead_postdispatch_attempt")
    if skill_only:
        names += ("trg_reference_lead_skill_only_plan",)
    if capability:
        names += ("trg_reference_lead_capability_plan",)
    if mcp:
        names += ("trg_reference_lead_mcp_plan",)
    if prompt:
        names += ("trg_reference_lead_prompt_plan",)
    async with db.execute(
        f"SELECT name FROM sqlite_master WHERE type='trigger' AND name IN ({','.join('?' for _ in names)})",
        names,
    ) as cursor:
        return len(await cursor.fetchall()) != len(names)


async def read_reference_primary_in_connection(
    db: aiosqlite.Connection, *, scope: ReferenceScope, turn_id: str, bindings: AgentExecutionBindingStore, read_output: bool = False
) -> ReferencePrimaryInput:
    """Load one consistent, authorized source/plan/execution snapshot, without writes."""
    return await _read_reference_primary(db, scope=scope, turn_id=turn_id, bindings=bindings, skill_reads=False, read_output=read_output)


async def read_reference_skill_primary_in_connection(
    db: aiosqlite.Connection,
    *,
    scope: ReferenceScope,
    turn_id: str,
    bindings: AgentExecutionBindingStore,
    read_output: bool = False,
    retained_plan_id: str | None = None,
) -> ReferencePrimaryInput:
    """Read an exact primary with its Skill baseline and optional read selections.

    Source/plan identity and current Agent access are verified here. Every Skill
    still needs current source, exact receiver and parameter checks. Automatic
    receivers come only from the immutable primary root, never a user mention.
    """
    return await _read_reference_primary(
        db, scope=scope, turn_id=turn_id, bindings=bindings, skill_reads=True, read_output=read_output, retained_plan_id=retained_plan_id
    )


async def read_reference_mcp_primary_in_connection(
    db: aiosqlite.Connection, *, scope: ReferenceScope, turn_id: str, bindings: AgentExecutionBindingStore, read_output: bool = False
) -> ReferencePrimaryInput:
    """Read one explicit Team Agent with exact MCP source/tool selections."""
    return await _read_reference_primary(
        db, scope=scope, turn_id=turn_id, bindings=bindings, skill_reads=False, mcp_reads=True, read_output=read_output
    )


async def read_reference_mcp_prompt_primary_in_connection(
    db: aiosqlite.Connection, *, scope: ReferenceScope, turn_id: str, bindings: AgentExecutionBindingStore, read_output: bool = False
) -> ReferencePrimaryInput:
    """Read one explicit Team Agent with exact MCP Prompt selections."""
    return await _read_reference_primary(
        db, scope=scope, turn_id=turn_id, bindings=bindings, skill_reads=False, mcp_prompts=True, read_output=read_output
    )


async def _read_reference_primary(
    db: aiosqlite.Connection,
    *,
    scope: ReferenceScope,
    turn_id: str,
    bindings: AgentExecutionBindingStore,
    skill_reads: bool,
    mcp_reads: bool = False,
    mcp_prompts: bool = False,
    read_output: bool = False,
    retained_plan_id: str | None = None,
) -> ReferencePrimaryInput:
    if not db.in_transaction:
        raise ValueError("reference_transaction_required")
    scope = ReferenceScope.model_validate(scope.model_dump(mode="python", exclude_none=True))
    plan = await ChatReferenceStore.get_plan_in_connection(db, scope=scope, turn_id=turn_id)
    if plan is None:
        raise ValueError("reference_plan_unavailable")
    source = await _read_chat_turn_user_source_in_tx(db, turn_id=turn_id, expected_user_id=scope.user_id, expected_workspace_id=scope.workspace_id)
    if source is None or source.input_document is None:
        raise ValueError("reference_source_unavailable")
    nodes = [node for node in source.input_document.nodes if isinstance(node, ReferenceNode)]
    agents = [node for node in nodes if isinstance(node.resource, AgentResourceRef)]
    skills = [node for node in nodes if isinstance(node.resource, SkillResourceRef)]
    capabilities = [node for node in nodes if isinstance(node.resource, CapabilityResourceRef)]
    mcp_sources = [node for node in nodes if isinstance(node.resource, McpSourceResourceRef)]
    mcp_tools = [node for node in nodes if isinstance(node.resource, McpToolResourceRef)]
    prompts = [node for node in nodes if isinstance(node.resource, McpPromptResourceRef)]
    if (
        source.parts is not None
        or bool(source.attachment_ids) != bool(source.attachment_refs)
        or len(agents) > 1
        or (not agents and (not skill_reads or not skills and not capabilities))
        or (mcp_reads and (len(agents) != 1 or agents[0].resource.source_kind != "team" or skills or capabilities or not (mcp_sources or mcp_tools)))
        or (
            mcp_prompts
            and (
                len(agents) != 1
                or agents[0].resource.source_kind != "team"
                or skills
                or capabilities
                or mcp_sources
                or mcp_tools
                or not 1 <= len(prompts) <= 3
            )
        )
        or any(agent.parameters for agent in agents)
        or len(nodes) != len(agents) + len(skills) + len(capabilities) + len(mcp_sources) + len(mcp_tools) + len(prompts)
        or len(capabilities) > 1
        or len(mcp_sources) > 3
        or len(mcp_tools) > 3
        or (not mcp_reads and (mcp_sources or mcp_tools))
        or (not mcp_prompts and prompts)
        or (len(skills) > 3 if skill_reads else bool(skills) or bool(capabilities))
        or len(plan.admissions) != len(nodes)
        or any(item.operation != requested_reference_operation(item.resource) for item in plan.admissions)
    ):
        raise ValueError("reference_start_unsupported")
    source.input_document.validate_task(
        has_attachments=bool(source.attachment_ids),
    )
    if source.attachment_ids:
        from bowhead_core.chat_attachment_access import validate_chat_attachment_access_in_connection

        await validate_chat_attachment_access_in_connection(
            db,
            refs=source.attachment_refs,
            runtime_ids=source.attachment_ids,
            user_id=scope.user_id,
            workspace_id=scope.workspace_id,
            session_id=scope.session_id,
        )
    if agents:
        resource = agents[0].resource
        policy_fingerprint = next(item.policy_digest for item in plan.admissions if item.node_id == agents[0].node_id)
        origin = "mention"
    else:
        # The immutable root is the sole primary identity for a Skill-only
        # source. Never manufacture a user Agent node or resolve a current name.
        async with db.execute(
            "SELECT * FROM turn_agent_binding WHERE turn_id=? AND team_id=? AND user_id=? AND binding_source='lead'",
            (turn_id, scope.team_id, scope.user_id),
        ) as cursor:
            cursor.row_factory = aiosqlite.Row
            root = await cursor.fetchone()
        if root is None:
            raise ValueError("reference_primary_unavailable")
        resource = AgentResourceRef(
            kind="agent",
            source_kind=root["agent_source_kind"],
            id=root["requested_agent_id"],
            version=root["revision_ref"],
            assignment_id=root["assignment_id"],
            release_id=root["release_id"],
        )
        policy_fingerprint, origin = root["policy_fingerprint"], "lead"
    binding = await bindings.read_reference_primary_in_connection(
        db,
        team_id=scope.team_id,
        user_id=scope.user_id,
        turn_id=turn_id,
        resource=resource,
        policy_fingerprint=policy_fingerprint,
        binding_source=origin,
        read_output=read_output,
        retained_plan_id=retained_plan_id,
    )
    return ReferencePrimaryInput(source=source, plan=plan, binding=binding, automatic_receiver=resource if not agents else None)
