"""Initial @Agent composition, dispatch evidence and exact worker fencing.

Composition and dispatch are different facts. Neither claims a provider request,
resource use, output quality or authorization to extend an Agent's baseline.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, model_validator

from bowhead_core.agent_behavior_contract import AgentBehaviorDescriptor, canonical_fingerprint
from bowhead_core.chat_reference_storage import ReferenceScope, _row, _time, _transaction
from bowhead_core.chat_references import AgentResourceRef, ContentHash, Identity, NodeId, ReferenceNode

if TYPE_CHECKING:
    from bowhead_core.chat_reference_execution import ReferencePrimaryInput


class ReferencePrimaryComposition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1]
    orchestration_contract: Literal["lead-delegation.v1"] | None = None
    receiver_kind: Literal["agent"]
    delegation_role: Literal["primary"]
    runtime_policy: Literal["initial-primary.v1"] = "initial-primary.v1"
    tool_deny: tuple[str, ...] = ("clarification_ask", "plan_propose", "subagent_spawn")
    source_turn_id: Identity
    source_hash: ContentHash
    node_id: NodeId | None = None
    receiver_source: Literal["lead"] | None = None
    resource: AgentResourceRef
    execution_id: Identity
    policy_fingerprint: ContentHash
    capability_fingerprint: Identity
    baseline: AgentBehaviorDescriptor
    task_hash: ContentHash

    @model_validator(mode="after")
    def receiver_origin(self):
        if (self.receiver_source == "lead") != (self.node_id is None):
            raise ValueError("reference_composition_receiver_origin")
        return self

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json", exclude_none=True), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @property
    def digest(self) -> str:
        return canonical_fingerprint(self.model_dump(mode="json", exclude_none=True))


def primary_composition(primary: ReferencePrimaryInput) -> ReferencePrimaryComposition:
    """Compile only the admitted baseline and original task; no temporary grants."""
    document = primary.source.input_document
    binding = primary.binding
    if document is None or binding.behavior_descriptor is None:
        raise ValueError("reference_composition_unavailable")
    nodes = [node for node in document.nodes if isinstance(node, ReferenceNode)]
    if (
        len(nodes) != 1
        or not isinstance(nodes[0].resource, AgentResourceRef)
        or nodes[0].parameters
        or len(primary.plan.admissions) != 1
        or primary.plan.admissions[0].node_id != nodes[0].node_id
        or primary.plan.admissions[0].resource != nodes[0].resource
        or primary.plan.admissions[0].operation != "delegate"
    ):
        # A preparation reader may return a broader source. Never silently drop
        # its Skill/parameter selections to fit the initial worker composition.
        raise ValueError("reference_composition_unavailable")
    return _primary_baseline_composition(primary, nodes[0])


def _primary_baseline_composition(primary: ReferencePrimaryInput, node: ReferenceNode | None) -> ReferencePrimaryComposition:
    """Common exact Agent fields; the caller validates its entire input shape."""
    document, binding = primary.source.input_document, primary.binding
    if (node is None) != (primary.automatic_receiver is not None):
        raise ValueError("reference_composition_receiver_origin")
    return ReferencePrimaryComposition(
        schema_version=1,
        orchestration_contract=primary.plan.orchestration_contract,
        receiver_kind="agent",
        delegation_role="primary",
        source_turn_id=primary.source.turn_id,
        source_hash=primary.plan.source_hash,
        node_id=node.node_id if node is not None else None,
        receiver_source="lead" if node is None else None,
        resource=node.resource if node is not None else primary.automatic_receiver,
        execution_id=binding.execution_id,
        policy_fingerprint=binding.policy_fingerprint,
        capability_fingerprint=binding.capability_fingerprint,
        baseline=binding.behavior_descriptor,
        task_hash="sha256:" + hashlib.sha256(document.text_content().encode()).hexdigest(),
    )


async def put_primary_composition_in_connection(
    db: aiosqlite.Connection, *, primary: ReferencePrimaryInput, generation: int
) -> ReferencePrimaryComposition:
    """Append the initial composition alongside the claim in the caller's transaction."""
    _transaction(db)
    composition = primary_composition(primary)
    return await _put_primary_composition_in_connection(db, composition=composition, generation=generation)


async def _put_primary_composition_in_connection(
    db: aiosqlite.Connection, *, composition: ReferencePrimaryComposition, generation: int
) -> ReferencePrimaryComposition:
    _transaction(db)
    turn_id = composition.source_turn_id
    encoded = composition.canonical_json()
    row = await _row(db, "SELECT * FROM chat_reference_resolutions WHERE turn_id=? AND resolution_seq=1", (turn_id,))
    if row is not None:
        if row["composition_json"] != encoded or row["composition_digest"] != composition.digest or row["created_generation"] != generation:
            raise ValueError("reference_composition_conflict")
        return composition
    await db.execute(
        "INSERT INTO chat_reference_resolutions(turn_id,resolution_seq,schema_version,kind,idempotency_key,source_turn_id,"
        "created_generation,execution_id,composition_digest,composition_json,created_at) VALUES (?,1,1,'composition','reference:primary',?,?,?,?,?,?)",
        (
            turn_id,
            turn_id,
            generation,
            composition.execution_id,
            composition.digest,
            encoded,
            datetime.now(UTC).isoformat(),
        ),
    )
    return composition


async def require_primary_composition_in_connection(
    db: aiosqlite.Connection, *, primary: ReferencePrimaryInput, generation: int
) -> ReferencePrimaryComposition:
    _transaction(db)
    expected = primary_composition(primary)
    row = await _row(db, "SELECT * FROM chat_reference_resolutions WHERE turn_id=? AND resolution_seq=1", (primary.source.turn_id,))
    if row is None:
        raise ValueError("reference_composition_unavailable")
    if (
        row["composition_json"] != expected.canonical_json()
        or row["composition_digest"] != expected.digest
        or row["created_generation"] != (1 if primary.plan.orchestration_contract == "lead-delegation.v1" else generation)
        or row["execution_id"] != expected.execution_id
    ):
        raise ValueError("reference_composition_changed")
    return expected


async def require_live_chat_worker_in_connection(
    db: aiosqlite.Connection,
    *,
    scope: ReferenceScope,
    turn_id: str,
    owner_id: str,
    generation: int,
) -> aiosqlite.Row:
    """An exact live owner, never an idempotent running-status authorization."""
    _transaction(db)
    row = await _row(
        db,
        "SELECT d.*,t.execution_generation,t.status AS turn_status,s.active_turn_id,s.steer_buffer_json,s.user_id,s.workspace_id,w.team_id FROM chat_turn_spawn_dispatch d "
        "JOIN turns t ON t.id=d.turn_id AND t.session_id=d.session_id JOIN sessions s ON s.id=t.session_id JOIN workspaces w ON w.id=s.workspace_id "
        "WHERE d.turn_id=? AND d.session_id=?",
        (turn_id, scope.session_id),
    )
    if (
        type(generation) is not int
        or generation < 1
        or not owner_id
        or row is None
        or row["state"] != "spawned"
        or row["owner_id"] != owner_id
        or row["lease_kind"] != ("turn" if generation == 1 else "resume")
        or row["execution_generation"] != generation
        or row["lease_execution_generation"] != generation
        or row["turn_status"] != "running"
        or row["active_turn_id"] != turn_id
        or row["team_id"] != scope.team_id
        or row["user_id"] != scope.user_id
        or row["workspace_id"] != scope.workspace_id
        or row["cancel_requested_at"] is not None
        or _time(row["lease_expires_at"]) <= datetime.now(UTC)
    ):
        raise ValueError("reference_execution_fence_lost")
    return row


async def require_reference_worker_in_connection(
    db: aiosqlite.Connection,
    *,
    scope: ReferenceScope,
    turn_id: str,
    owner_id: str,
    generation: int,
) -> None:
    row = await require_live_chat_worker_in_connection(db, scope=scope, turn_id=turn_id, owner_id=owner_id, generation=generation)
    if not await reference_dispatch_origin_valid_in_connection(db, row=row, scope=scope, turn_id=turn_id):
        raise ValueError("reference_execution_fence_lost")
    if generation != 1:
        from bowhead_core.chat_reference_continuation import require_resume

        await require_resume(db, turn_id=turn_id, generation=generation)
    from bowhead_core.chat_reference_retention import reference_material_available_in_connection

    if not await reference_material_available_in_connection(db, turn_id=turn_id):
        raise ValueError("reference_execution_fence_lost")


async def reference_dispatch_origin_valid_in_connection(
    db: aiosqlite.Connection,
    *,
    row: aiosqlite.Row | None,
    scope: ReferenceScope,
    turn_id: str,
) -> bool:
    """Accept an initial dispatch or its exact materialized queue child."""
    _transaction(db)
    if row is None:
        return False
    if row["dispatch_kind"] == "start":
        return row["parent_turn_id"] is None
    if (
        row["dispatch_kind"] != "follow_up"
        or not isinstance(row["parent_turn_id"], str)
        or not row["parent_turn_id"]
        or not isinstance(row["source_submission_id"], str)
        or not row["source_submission_id"]
    ):
        return False
    try:
        source_submission_ids = json.loads(
            str(row["source_submission_ids_json"]),
        )
    except (TypeError, ValueError):
        return False
    if source_submission_ids != [row["source_submission_id"]]:
        return False
    queued = await _row(
        db,
        "SELECT 1 FROM chat_turn_spawn_dispatch d JOIN sessions s ON s.id=d.session_id "
        "JOIN workspaces w ON w.id=s.workspace_id "
        "WHERE d.turn_id=? AND d.parent_turn_id=? AND d.source_submission_id=? "
        "AND s.id=? AND s.user_id=? AND s.workspace_id=? AND w.team_id=? AND " + REFERENCE_RECOVERY_DISPATCH_SQL,
        (turn_id, row["parent_turn_id"], row["source_submission_id"], scope.session_id, scope.user_id, scope.workspace_id, scope.team_id),
    )
    return queued is not None


# Outer q is the accepted queue row; qh is its current same-session holder.
# The accepted origin stays immutable when predecessors pass the queue on.
REFERENCE_QUEUE_HOLDER_SQL = """EXISTS (
          WITH RECURSIVE lineage(turn_id) AS (
            SELECT qh.id
            UNION
            SELECT prior.parent_turn_id FROM chat_turn_spawn_dispatch prior
            JOIN lineage ON lineage.turn_id=prior.turn_id
            JOIN turns ancestor ON ancestor.id=prior.parent_turn_id AND ancestor.session_id=q.session_id
            WHERE prior.session_id=q.session_id AND prior.dispatch_kind='follow_up'
              AND prior.turn_id<>q.accepted_parent_turn_id
          )
          SELECT 1 FROM lineage WHERE turn_id=q.accepted_parent_turn_id
            AND NOT EXISTS (SELECT 1 FROM lineage WHERE turn_id=q.materialized_turn_id)
        )"""


def _reference_dispatch_origin_sql(alias: str) -> str:
    """SQL counterpart of ``reference_dispatch_origin_valid_in_connection``."""
    if alias not in {"d", "rd"}:
        raise ValueError("reference dispatch alias is unsupported")
    return f"""(
  ({alias}.dispatch_kind='start' AND {alias}.parent_turn_id IS NULL)
  OR (
    {alias}.dispatch_kind='follow_up' AND {alias}.parent_turn_id IS NOT NULL
    AND {alias}.source_submission_id IS NOT NULL
    AND {alias}.source_submission_ids_json=json_array({alias}.source_submission_id)
    AND EXISTS (
      SELECT 1 FROM chat_reference_queued_inputs q
      JOIN sessions qs ON qs.id=q.session_id
      JOIN workspaces qw ON qw.id=qs.workspace_id
      JOIN turns qp ON qp.id=q.accepted_parent_turn_id
      JOIN turns qc ON qc.id=q.materialized_turn_id
      JOIN turns qh ON qh.id={alias}.parent_turn_id AND qh.session_id=q.session_id
      WHERE q.team_id=qw.team_id AND q.user_id=qs.user_id
        AND q.workspace_id=qs.workspace_id
        AND q.session_id={alias}.session_id
        AND q.client_submission_id={alias}.source_submission_id
        AND {REFERENCE_QUEUE_HOLDER_SQL}
        AND q.state='materialized'
        AND q.materialized_turn_id={alias}.turn_id
        AND qp.session_id=q.session_id
        AND qc.session_id=q.session_id
    )
  )
)"""


REFERENCE_RUNTIME_DISPATCH_SQL = _reference_dispatch_origin_sql("rd")
REFERENCE_RECOVERY_DISPATCH_SQL = _reference_dispatch_origin_sql("d")


# Used only after checking that revision 0066 is installed. This predicate does
# not grant execution; it identifies records understood by terminal recovery.
REFERENCE_RUNTIME_SQL = f"""EXISTS (
 SELECT 1 FROM chat_reference_resolutions rr
 JOIN turn_agent_binding rb ON rb.turn_id=rr.turn_id AND rb.primary_execution_id=rr.execution_id
 JOIN turn_agent_execution re ON re.id=rr.execution_id AND re.turn_id=rr.turn_id
 JOIN chat_turn_spawn_dispatch rd ON rd.turn_id=rr.turn_id AND rd.session_id=t.session_id
 WHERE rr.turn_id=t.id AND rr.source_turn_id=t.id AND rr.resolution_seq=1
 AND rr.created_generation=1 AND t.execution_generation=1
 AND rd.lease_execution_generation=1 AND rd.lease_kind='turn'
 AND {REFERENCE_RUNTIME_DISPATCH_SQL}
)"""

# An accepted initial primary may expire or be deleted before its first claim.
# Recovery may close that reservation, but must never manufacture a composition
# or invoke it. Root binding and immutable admission identify this narrow shape.
# A mixed plan binds one exact primary through its root. The user source either
# carries that exact Agent or omits it for a Lead receiver, plus the bounded
# initial Skill, Capability and MCP operations. Those selections do not require
# current source authority merely to settle an execution that never reached
# first claim.
REFERENCE_RECOVERY_SQL = f"""({REFERENCE_RUNTIME_SQL} OR EXISTS (
 SELECT 1 FROM chat_reference_plans p
 JOIN turn_agent_binding b ON b.turn_id=p.turn_id
 JOIN turn_agent_execution e ON e.id=b.primary_execution_id AND e.turn_id=p.turn_id
 JOIN chat_turn_spawn_dispatch d ON d.turn_id=p.turn_id AND d.session_id=t.session_id
 LEFT JOIN json_each(p.plan_json,'$.admissions') chosen
   ON json_extract(chosen.value,'$.resource.kind')='agent'
 WHERE p.turn_id=t.id AND p.source_turn_id=t.id AND p.logical_source_turn_id=t.id AND p.snapshot_owner_turn_id=t.id
 AND json_array_length(p.plan_json,'$.admissions') BETWEEN 1 AND 10
 AND (SELECT COUNT(*) FROM json_each(p.plan_json,'$.admissions') a
      WHERE json_extract(a.value,'$.resource.kind')='agent')<=1
 AND (SELECT COUNT(*) FROM json_each(p.plan_json,'$.admissions') a
      WHERE json_extract(a.value,'$.resource.kind')='skill')<=3
 AND (SELECT COUNT(*) FROM json_each(p.plan_json,'$.admissions') a
      WHERE json_extract(a.value,'$.resource.kind')='capability')<=1
 AND (SELECT COUNT(*) FROM json_each(p.plan_json,'$.admissions') a
      WHERE json_extract(a.value,'$.resource.kind')='mcp_source')<=3
 AND (SELECT COUNT(*) FROM json_each(p.plan_json,'$.admissions') a
      WHERE json_extract(a.value,'$.resource.kind')='mcp_tool')<=3
 AND (SELECT COUNT(*) FROM json_each(p.plan_json,'$.admissions') a
      WHERE json_extract(a.value,'$.resource.kind')='mcp_prompt')<=3
 AND (
   (SELECT COUNT(*) FROM json_each(p.plan_json,'$.admissions') a
      WHERE json_extract(a.value,'$.resource.kind')='mcp_prompt')=0
   OR (
     (SELECT COUNT(*) FROM json_each(p.plan_json,'$.admissions') a
        WHERE json_extract(a.value,'$.resource.kind')='mcp_prompt') BETWEEN 1 AND 3
     AND (SELECT COUNT(*) FROM json_each(p.plan_json,'$.admissions') a
        WHERE json_extract(a.value,'$.resource.kind')='agent')=1
     AND json_array_length(p.plan_json,'$.admissions')=1+
       (SELECT COUNT(*) FROM json_each(p.plan_json,'$.admissions') a
          WHERE json_extract(a.value,'$.resource.kind')='mcp_prompt')
   )
 )
 AND NOT EXISTS (
   SELECT 1 FROM json_each(p.plan_json,'$.admissions') a WHERE COALESCE(
     (json_extract(a.value,'$.resource.kind')='agent' AND json_extract(a.value,'$.operation')='delegate')
     OR (json_extract(a.value,'$.resource.kind')='skill' AND json_extract(a.value,'$.operation')='read')
     OR (json_extract(a.value,'$.resource.kind')='capability'
         AND json_extract(a.value,'$.resource.id')='cap_visualize'
         AND json_extract(a.value,'$.resource.version')=1
         AND json_extract(a.value,'$.operation')='execute')
     OR (json_extract(a.value,'$.resource.kind')='mcp_source'
         AND json_extract(a.value,'$.operation')='provide'
         AND json_type(a.value,'$.resource.source_id')='text'
         AND json_type(a.value,'$.resource.local_contract_revision')='text'
         AND json_type(a.value,'$.resource.connection_binding_id')='text'
         AND json_type(a.value,'$.resource.connection_subject_fingerprint')='text')
     OR (json_extract(a.value,'$.resource.kind')='mcp_tool'
         AND json_extract(a.value,'$.operation')='invoke'
         AND json_type(a.value,'$.resource.source_id')='text'
         AND json_type(a.value,'$.resource.local_contract_revision')='text'
         AND json_type(a.value,'$.resource.connection_binding_id')='text'
         AND json_type(a.value,'$.resource.connection_subject_fingerprint')='text'
         AND json_type(a.value,'$.resource.operation_name')='text'
         AND json_type(a.value,'$.resource.operation_contract_revision')='text')
     OR (json_extract(a.value,'$.resource.kind')='mcp_prompt'
         AND json_extract(a.value,'$.operation')='expand'
         AND json_type(a.value,'$.resource.source_id')='text'
         AND json_type(a.value,'$.resource.local_contract_revision')='text'
         AND json_type(a.value,'$.resource.connection_binding_id')='text'
         AND json_type(a.value,'$.resource.connection_subject_fingerprint')='text'
         AND json_type(a.value,'$.resource.prompt_name')='text'
         AND json_type(a.value,'$.resource.prompt_contract_revision')='text'),0)=0)
 AND (
   (b.binding_source='mention' AND e.dispatch_source='explicit'
     AND (SELECT COUNT(*) FROM json_each(p.plan_json,'$.admissions') a WHERE json_extract(a.value,'$.resource.kind')='agent')=1
     AND json_extract(chosen.value,'$.resource.id')=e.agent_id
     AND json_extract(chosen.value,'$.resource.source_kind')=e.agent_source_kind
     AND json_extract(chosen.value,'$.resource.version')=e.revision_ref
     AND json_extract(chosen.value,'$.resource.assignment_id') IS b.assignment_id
     AND json_extract(chosen.value,'$.resource.release_id') IS b.release_id
     AND json_extract(chosen.value,'$.policy_digest')=e.policy_fingerprint)
   OR (b.binding_source='lead' AND e.dispatch_source='lead'
     AND json_array_length(p.plan_json,'$.admissions') BETWEEN 1 AND 4
     AND NOT EXISTS (SELECT 1 FROM json_each(p.plan_json,'$.admissions') a
       WHERE json_extract(a.value,'$.resource.kind') NOT IN ('skill','capability'))))
 AND e.status IN ('admitted','failed','cancelled')
 AND e.parent_execution_id IS NULL AND e.dispatch_key='reference:primary'
 AND b.team_id=e.team_id AND b.requested_agent_id=e.agent_id AND b.revision_ref=e.revision_ref
 AND b.agent_source_kind=e.agent_source_kind AND b.release_id IS e.release_id
 AND b.policy_fingerprint=e.policy_fingerprint AND b.capability_fingerprint=e.capability_fingerprint
 AND t.execution_generation=1 AND d.lease_execution_generation=1
 AND d.lease_kind='turn' AND {REFERENCE_RECOVERY_DISPATCH_SQL}
 AND d.owner_id IS NULL AND d.state IN ('claimed','drain_pending')
 AND NOT EXISTS (SELECT 1 FROM chat_reference_resolutions r WHERE r.turn_id=t.id)
))"""


# Recovery includes reserved/closed pauses, even after retention expires. This
# identifies ownership for settlement; execution still requires a live pause.
REFERENCE_BI_RESUME_SQL = f"""EXISTS (
 SELECT 1 FROM chat_reference_bi_continuations c
 JOIN chat_reference_resolutions r ON r.turn_id=c.turn_id AND r.resolution_seq=1
 JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id AND d.session_id=t.session_id
 WHERE c.turn_id=t.id AND c.execution_id=r.execution_id AND c.composition_digest=r.composition_digest
 AND c.resume_generation=t.execution_generation AND d.lease_execution_generation=t.execution_generation
 AND d.lease_kind='resume' AND {REFERENCE_RECOVERY_DISPATCH_SQL}
)"""

REFERENCE_LEAD_RESUME_SQL = f"""EXISTS (
 SELECT 1 FROM chat_reference_plans p
 JOIN chat_reference_resolutions r ON r.turn_id=p.turn_id AND r.resolution_seq=1
 JOIN clarification_requests q ON q.turn_id=p.turn_id
 JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id AND d.session_id=t.session_id
 WHERE p.turn_id=t.id AND json_extract(p.plan_json,'$.orchestration_contract')='lead-delegation.v1'
 AND CASE WHEN json_valid(q.resume_context_json) THEN json_extract(q.resume_context_json,'$.reference_continuation.contract') END='reference-lead-predispatch.v1'
 AND CASE WHEN json_valid(q.resume_context_json) THEN json_extract(q.resume_context_json,'$.reference_continuation.execution_id') END=r.execution_id
 AND CASE WHEN json_valid(q.resume_context_json) THEN json_extract(q.resume_context_json,'$.reference_continuation.source_hash') END=p.source_hash
 AND CASE WHEN json_valid(q.resume_context_json) THEN json_extract(q.resume_context_json,'$.reference_continuation.composition_digest') END=r.composition_digest
 AND q.session_id=d.session_id
 AND NOT EXISTS (SELECT 1 FROM chat_reference_resolutions started
                 WHERE started.turn_id=p.turn_id AND started.resolution_seq=2
                 AND started.created_generation<t.execution_generation)
 AND t.execution_generation>1 AND d.lease_execution_generation=t.execution_generation
 AND d.lease_kind='resume' AND {REFERENCE_RECOVERY_DISPATCH_SQL}
)"""

REFERENCE_LEAD_POSTDISPATCH_RESUME_SQL = f"""EXISTS (
 SELECT 1 FROM chat_reference_plans p
 JOIN chat_reference_resolutions original ON original.turn_id=p.turn_id AND original.resolution_seq=1
 JOIN chat_reference_resolutions actual ON actual.turn_id=p.turn_id AND actual.resolution_seq=2
 JOIN turn_agent_execution e ON e.id=actual.execution_id
 JOIN clarification_requests q ON q.turn_id=p.turn_id
 JOIN chat_turn_spawn_dispatch d ON d.turn_id=t.id AND d.session_id=t.session_id
 WHERE p.turn_id=t.id AND json_extract(p.plan_json,'$.orchestration_contract')='lead-delegation.v1'
 AND actual.execution_id=original.execution_id AND actual.created_generation=1
 AND json_extract(actual.composition_json,'$.runtime_policy')='lead-delegation.v1'
 AND CASE WHEN json_valid(q.resume_context_json) THEN json_extract(q.resume_context_json,'$.reference_continuation.contract') END='reference-lead-postdispatch.v1'
 AND CASE WHEN json_valid(q.resume_context_json) THEN json_extract(q.resume_context_json,'$.reference_continuation.execution_id') END=e.id
 AND CASE WHEN json_valid(q.resume_context_json) THEN json_extract(q.resume_context_json,'$.reference_continuation.source_hash') END=p.source_hash
 AND CASE WHEN json_valid(q.resume_context_json) THEN json_extract(q.resume_context_json,'$.reference_continuation.composition_digest') END=original.composition_digest
 AND CASE WHEN json_valid(q.resume_context_json) THEN json_extract(q.resume_context_json,'$.reference_continuation.delegation_digest') END=actual.composition_digest
 AND q.session_id=d.session_id
 AND t.execution_generation=2 AND d.lease_execution_generation=2
 AND d.lease_kind='resume' AND {REFERENCE_RECOVERY_DISPATCH_SQL}
)"""


def _late_lead_postdispatch_resume_sql() -> str:
    # Import only after store initialization: the immutable migration retains
    # the exact reviewed origin predicate and imports older store factories.
    from importlib import import_module

    origin = import_module("bowhead_core.migrations.versions.0209_reference_question_origin").late_origin_metadata_sql(
        plan="p", original="original", actual="actual", card="q", dispatch="d"
    )
    return REFERENCE_LEAD_POSTDISPATCH_RESUME_SQL.replace("actual.created_generation=1", "actual.created_generation>1", 1).replace(
        "t.execution_generation=2 AND d.lease_execution_generation=2",
        "t.execution_generation>actual.created_generation AND d.lease_execution_generation=t.execution_generation "
        "AND NOT EXISTS (SELECT 1 FROM chat_reference_lead_postdispatch_attempts used "
        "WHERE used.turn_id=t.id AND used.generation<>t.execution_generation) "
        f"AND {origin}",
        1,
    )


async def is_reference_runtime_in_connection(db: aiosqlite.Connection, *, turn_id: str) -> bool:
    _transaction(db)
    if await _row(db, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_reference_resolutions'", ()) is None:
        return False
    if await _row(db, f"SELECT 1 FROM turns t WHERE t.id=? AND {REFERENCE_RUNTIME_SQL}", (turn_id,)) is not None:
        return True
    from bowhead_core.chat_reference_bi_continuation import installed

    if await _row(db, f"SELECT 1 FROM turns t WHERE t.id=? AND {REFERENCE_LEAD_RESUME_SQL}", (turn_id,)) is not None:
        return True
    if await _row(db, f"SELECT 1 FROM turns t WHERE t.id=? AND {REFERENCE_LEAD_POSTDISPATCH_RESUME_SQL}", (turn_id,)) is not None:
        return True
    if await _row(db, f"SELECT 1 FROM turns t WHERE t.id=? AND {_late_lead_postdispatch_resume_sql()}", (turn_id,)) is not None:
        return True
    if not await installed(db):
        return False
    return (
        await _row(
            db,
            f"SELECT 1 FROM turns t WHERE t.id=? AND {REFERENCE_BI_RESUME_SQL}",
            (turn_id,),
        )
        is not None
    )


async def is_reference_recoverable_in_connection(db: aiosqlite.Connection, *, turn_id: str) -> bool:
    _transaction(db)
    if await _row(db, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_reference_resolutions'", ()) is None:
        return False
    return (
        await is_reference_runtime_in_connection(db, turn_id=turn_id)
        or await _row(db, f"SELECT 1 FROM turns t WHERE t.id=? AND {REFERENCE_RECOVERY_SQL}", (turn_id,)) is not None
    )


async def primary_continuations_installed(db: aiosqlite.Connection) -> bool:
    async with db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_reference_primary_continuations'") as cursor:
        return await cursor.fetchone() is not None


async def primary_continuation_count_in_connection(db: aiosqlite.Connection, turn_id: str) -> int:
    """Lead dispatches of the admitted primary after its first one in this Turn."""
    if not await primary_continuations_installed(db):
        return 0
    async with db.execute("SELECT COUNT(*) FROM chat_reference_primary_continuations WHERE turn_id=?", (turn_id,)) as cursor:
        return int((await cursor.fetchone())[0])


async def close_primary_continuation_in_connection(
    db: aiosqlite.Connection,
    *,
    turn_id: str,
    dispatch_seq: int | None,
    output: tuple[str, str] | None,
    failure_code: str | None,
) -> None:
    """Settle an open continuation and return the primary execution to `completed`.

    `dispatch_seq=None` closes whichever continuation is still open (worker
    stop, cancellation); it is a no-op when none is. The first dispatch already
    completed the primary task, so its execution never becomes a failed primary.
    """
    _transaction(db)
    if dispatch_seq is None and not await primary_continuations_installed(db):
        return
    row = await _row(
        db,
        "SELECT * FROM chat_reference_primary_continuations WHERE turn_id=? AND state='started'"
        + (" AND dispatch_seq=?" if dispatch_seq is not None else ""),
        (turn_id,) if dispatch_seq is None else (turn_id, dispatch_seq),
    )
    if row is None:
        if dispatch_seq is None:
            return
        raise ValueError("reference_continuation_fence_lost")
    now = datetime.now(UTC).isoformat()
    if output is not None:
        output_hash, ciphertext = output
        await db.execute(
            "UPDATE chat_reference_primary_continuations SET state='completed',output_hash=?,output_ciphertext=?,ended_at=? "
            "WHERE turn_id=? AND dispatch_seq=? AND state='started'",
            (output_hash, ciphertext, now, turn_id, row["dispatch_seq"]),
        )
    else:
        await db.execute(
            "UPDATE chat_reference_primary_continuations SET state='failed',failure_code=?,ended_at=? WHERE turn_id=? AND dispatch_seq=? AND state='started'",
            (failure_code or "reference_continuation_failed", now, turn_id, row["dispatch_seq"]),
        )
    # Restore the first dispatch's completion time; this continuation's own
    # duration is its row's started_at/ended_at.
    reopened = await db.execute(
        "UPDATE turn_agent_execution SET status='completed',ended_at=?,failure_code=NULL WHERE id=? AND status='running'",
        (row["primary_ended_at"], row["execution_id"]),
    )
    if reopened.rowcount != 1 and dispatch_seq is not None:
        raise ValueError("reference_output_fence_lost")
    # A settlement sweep (dispatch_seq=None) may find the execution already
    # closed by another path; the continuation row is still closed above.


async def settle_reference_primary_in_connection(db: aiosqlite.Connection, *, turn_id: str, status: str, now: str) -> None:
    """Close a quiescent worker's ledger, never replay tools or invent outputs.

    Caller owns the exact settlement lease or has fenced an expired owner in
    the same writer transaction. External effects retain their own ledger.
    """
    _transaction(db)
    if not await is_reference_recoverable_in_connection(db, turn_id=turn_id):
        from bowhead_core.documents.agent_caller import _document_reference_lifecycle_in_tx

        native = await _document_reference_lifecycle_in_tx(db, turn_id=turn_id)
        if native is None or status == "completed" and native != "consumed":
            raise ValueError("reference_recovery_not_supported")
    row = await _row(
        db, "SELECT e.id,e.status FROM turn_agent_binding b JOIN turn_agent_execution e ON e.id=b.primary_execution_id WHERE b.turn_id=?", (turn_id,)
    )
    from bowhead_core.chat_reference_bi_continuation import installed
    from bowhead_core.chat_reference_continuation import pause_ready

    # A continuation interrupted before its own settlement leaves the primary
    # running; the first dispatch's completed result remains authoritative.
    # Close it before any outcome, including a pause, reads the execution.
    await close_primary_continuation_in_connection(db, turn_id=turn_id, dispatch_seq=None, output=None, failure_code="reference_worker_stopped")
    row = await _row(
        db, "SELECT e.id,e.status FROM turn_agent_binding b JOIN turn_agent_execution e ON e.id=b.primary_execution_id WHERE b.turn_id=?", (turn_id,)
    )
    if status == "clarification_pending":
        from bowhead_core.documents.agent_caller import document_reference_pause_in_tx

        if row["status"] == "completed" and await document_reference_pause_in_tx(db, turn_id=turn_id, execution_id=row["id"]):
            return
        if row["status"] not in ("running", "admitted") or not await pause_ready(db, turn_id=turn_id):
            raise ValueError("reference_continuation_not_supported")
        return
    if await installed(db):
        await db.execute("UPDATE chat_reference_bi_continuations SET state='closed' WHERE turn_id=? AND state<>'closed'", (turn_id,))
    if status == "completed":
        from bowhead_core.skill_model_status import require_reference_model_receipts_complete_in_connection

        await require_reference_model_receipts_complete_in_connection(db, turn_id=turn_id)
        output = await _row(db, "SELECT 1 FROM chat_reference_evidence WHERE execution_id=? AND kind='output_received'", (row["id"],))
        if row["status"] != "completed" or output is None:
            raise ValueError("reference_required_primary_incomplete")
        return
    if row["status"] not in ("admitted", "running"):
        return
    # A stopped owner does not prove non-delivery. Keep pending physical rows
    # intact for original late callbacks; the status reader projects them as
    # unknown once the worker is fenced. Never manufacture usage or a retry.
    cancelled = status in ("cancelled", "aborted")
    await db.execute(
        "UPDATE turn_capability_execution SET status=?,ended_at=?,failure_code=? WHERE agent_execution_id=? AND status IN ('selected','running')",
        ("cancelled" if cancelled else "blocked", now, None if cancelled else "reference_worker_stopped", row["id"]),
    )
    await db.execute(
        "UPDATE turn_agent_execution SET status=?,ended_at=?,failure_code=? WHERE id=? AND status IN ('admitted','running')",
        ("cancelled" if cancelled else "failed", now, None if cancelled else "reference_worker_stopped", row["id"]),
    )
