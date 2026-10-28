"""Viewer-scoped projection of persisted shortcut-reference facts.

The projection only reduces canonical source, composition, execution and model
receipt records.  It never infers use from answer text and never treats a
resource identifier as currently visible unless the caller supplies a fresh
authorization projection.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
from pydantic import TypeAdapter

from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_retention import reference_material_available_in_connection
from bowhead_core.chat_reference_storage import (
    MAX_REFERENCE_STORAGE_BYTES,
    ChatReferenceStore,
    ContentDescriptor,
    ReferenceCipher,
    ReferencePlan,
    ReferenceScope,
    _scope,
    _turn,
    reference_source_hash,
)
from bowhead_core.chat_references import (
    AgentResourceRef,
    InputDocument,
    ReferenceNode,
    ResourceRef,
    TextNode,
    decode_reference_json,
    requested_reference_operation,
)
from bowhead_core.reference_mcp_prompt_execution import ReferenceMcpPromptResultResolution
from bowhead_core.session_store import _read_chat_turn_user_source_in_tx

if TYPE_CHECKING:
    from bowhead_core.skill_reference_execution import SkillCompositionMember

MAX_PROJECTION_FACTS = 100
MAX_CONTENT_EXCERPT_CHARS = 4096
_RESOURCE_ADAPTER = TypeAdapter(ResourceRef)


class ReferenceProjectionError(ValueError):
    pass


def reference_resource_identity(resource: ResourceRef) -> str:
    """Canonical exact identity used only inside an authorized request."""

    return json.dumps(
        resource.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


async def _rows(
    db: aiosqlite.Connection,
    sql: str,
    args: tuple[object, ...],
) -> tuple[list[aiosqlite.Row], bool]:
    async with db.execute(sql, args) as cursor:
        cursor.row_factory = aiosqlite.Row
        rows = list(await cursor.fetchall())
    return rows[:MAX_PROJECTION_FACTS], len(rows) > MAX_PROJECTION_FACTS


def _text_spans(document: InputDocument, *, include_content: bool) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    offset = 0
    text_ordinal = 0
    for node_index, node in enumerate(document.nodes):
        if not isinstance(node, TextNode):
            continue
        length = len(node.text)
        item: dict[str, Any] = {
            "text_ordinal": text_ordinal,
            "node_index": node_index,
            "start_codepoint": offset,
            "end_codepoint": offset + length,
        }
        if include_content:
            item["text"] = node.text
        spans.append(item)
        offset += length
        text_ordinal += 1
    return spans


def _resource_projection(resource: ResourceRef, visible: Collection[str]) -> tuple[dict[str, Any] | None, bool]:
    allowed = reference_resource_identity(resource) in visible
    return (resource.model_dump(mode="json", exclude_none=True) if allowed else None), allowed


def _descriptor_matches(descriptor: ContentDescriptor, resource: ResourceRef, node_id: str) -> bool:
    return descriptor.resource == resource and descriptor.node_id == node_id


def _outcome(*, kind: str, turn_status: str, execution_status: str | None, stages: Collection[str]) -> str:
    if execution_status == "cancelled" or turn_status == "cancelled":
        return "cancelled"
    if execution_status in {"failed", "blocked"}:
        return execution_status
    if "delivery_unknown" in stages:
        return "partial"
    if kind == "agent" and "output_received" in stages:
        return "fulfilled"
    if kind == "skill" and "provider_request_completed" in stages and execution_status == "completed":
        return "fulfilled"
    if kind == "capability" and "output_contract_satisfied" in stages and execution_status == "completed":
        return "fulfilled"
    if kind in {"mcp_source", "mcp_tool"} and "tool_result_completed" in stages and execution_status == "completed":
        return "fulfilled"
    if (
        kind == "mcp_prompt"
        and {"prompt_result_committed", "provider_request_completed", "output_received"} <= set(stages)
        and execution_status == "completed"
        and "delivery_unknown" not in stages
    ):
        return "fulfilled"
    if turn_status in {"completed", "failed", "aborted"}:
        return "not_used" if not stages else "failed"
    return "pending"


def _content_projection(
    *,
    row: Mapping[str, Any],
    descriptor: ContentDescriptor,
    scope: ReferenceScope,
    turn_id: str,
    include_content: bool,
    cipher: ReferenceCipher | None,
    material_available: bool,
) -> dict[str, Any]:
    retained = row["retention_state"] == "available" and row["ciphertext"] is not None
    state = "purged" if not retained else "available" if material_available else "expired"
    item: dict[str, Any] = {
        "content_key": descriptor.content_key,
        "content_kind": descriptor.content_kind,
        "content_hash": descriptor.content_hash,
        "byte_count": descriptor.byte_count,
        "state": state,
    }
    if not include_content:
        item["content_state"] = "not_requested"
        return item
    if state != "available":
        item["content_state"] = "unavailable"
        return item
    if descriptor.content_kind in {"skill_read_closure", "skill_baseline", "skill_extension"}:
        # Full envelopes can contain unselected or currently private packages.
        # Authorized composed methods are projected separately, one at a time.
        item["content_state"] = "withheld_compound"
        return item
    if cipher is None:
        item["content_state"] = "key_unavailable"
        return item
    try:
        content = cipher.open(
            scope=scope,
            purpose="content",
            identity=f"{turn_id}/{descriptor.content_key}",
            ciphertext=str(row["ciphertext"]),
        ).decode("utf-8", errors="strict")
    except (ValueError, UnicodeError):
        item["content_state"] = "unavailable"
        return item
    item.update(
        {
            "content_state": "included",
            "content_excerpt": content[:MAX_CONTENT_EXCERPT_CHARS],
            "content_truncated": len(content) > MAX_CONTENT_EXCERPT_CHARS,
        }
    )
    return item


def _skill_member_projection(
    *,
    member: SkillCompositionMember,
    descriptor: ContentDescriptor,
    row: Mapping[str, Any],
    scope: ReferenceScope,
    turn_id: str,
    include_content: bool,
    cipher: ReferenceCipher | None,
    material_available: bool,
) -> dict[str, Any]:
    """Read only one authorized, composed method from the retained envelope.

    Baseline packages can include unused Skills, scripts and binary files.
    Their selected read codec is the same one used during original injection;
    neither the full package nor sibling plaintext is returned here.
    """
    from bowhead_core.skill_baseline_material import SKILL_BASELINE_CONTRACT
    from bowhead_core.skill_extension_material import CONTRACT as EXTENSION_CONTRACT
    from bowhead_core.skill_package_material import restore_skill_package
    from bowhead_core.skill_read_material import PreparedSkillRead, _json, validate_prepared_skill_read
    from bowhead_core.skill_read_snapshot import SKILL_READ_SNAPSHOT_CONTRACT

    retained = row["retention_state"] == "available" and row["ciphertext"] is not None
    available = retained and material_available
    item: dict[str, Any] = {
        "content_key": descriptor.content_key,
        "content_kind": "skill_read",
        "content_hash": member.content_hash,
        "materialization_hash": member.materialization_hash,
        "snapshot_content_hash": descriptor.content_hash,
        "byte_count": None,
        "state": "purged" if not retained else "available" if available else "expired",
        "content_state": "not_requested" if not include_content else "unavailable" if not available else "key_unavailable",
    }
    if not include_content or not available or cipher is None:
        return item
    try:
        content = cipher.open(scope=scope, purpose="content", identity=f"{turn_id}/{descriptor.content_key}", ciphertext=str(row["ciphertext"]))
        if len(content) != descriptor.byte_count or "sha256:" + hashlib.sha256(content).hexdigest() != descriptor.content_hash:
            raise ValueError("snapshot changed")
        saved = decode_reference_json(content, max_bytes=MAX_REFERENCE_STORAGE_BYTES, max_depth=20)
        if type(saved.get("schema_version")) is not int or saved["schema_version"] != 1:
            raise ValueError("snapshot version unavailable")
        if descriptor.content_kind == "skill_baseline" and saved.get("baseline_contract") == SKILL_BASELINE_CONTRACT:
            members = saved["members"]
        elif descriptor.content_kind == "skill_extension" and saved.get("contract") == EXTENSION_CONTRACT:
            members = saved["items"]
        elif descriptor.content_kind == "skill_read_closure" and saved.get("snapshot_contract") == SKILL_READ_SNAPSHOT_CONTRACT:
            members = saved["closure"]["items"]
        else:
            raise ValueError("snapshot contract unavailable")
        exact = member.resource.model_dump(mode="json", exclude_none=True)
        matches = [entry for entry in members if entry["proof"]["resource"] == exact]
        if len(matches) != 1:
            raise ValueError("snapshot member unavailable")
        selected = matches[0]
        if descriptor.content_kind == "skill_baseline":
            material = restore_skill_package(
                _json(selected["package"]),
                resource=member.resource,
                expected_name=selected["proof"]["name"],
                content_hash=selected["content_hash"],
            ).selected_read()
        else:
            material = PreparedSkillRead(
                base64.b64decode(selected["content"], validate=True), selected["content_hash"], selected["materialization_hash"]
            )
        if (material.content_hash, material.materialization_hash) != (member.content_hash, member.materialization_hash):
            raise ValueError("composed member changed")
        material = validate_prepared_skill_read(material, resource=member.resource)
        text = material.content.decode("utf-8", errors="strict")
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        item["content_state"] = "unavailable"
        return item
    item.update(
        content_state="included",
        byte_count=len(material.content),
        content_excerpt=text[:MAX_CONTENT_EXCERPT_CHARS],
        content_truncated=len(text) > MAX_CONTENT_EXCERPT_CHARS,
    )
    return item


async def _queued_source_locator(db: aiosqlite.Connection, *, scope: ReferenceScope, turn_id: str) -> dict[str, str] | None:
    """Keep the accepted submission identity after its child starts or ends."""
    async with db.execute(
        """
        SELECT q.client_submission_id,q.accepted_parent_turn_id
        FROM chat_reference_queued_inputs q
        WHERE q.materialized_turn_id=? AND q.state='materialized'
          AND q.team_id=? AND q.user_id=? AND q.workspace_id=? AND q.session_id=?
        """,
        (
            turn_id,
            scope.team_id,
            scope.user_id,
            scope.workspace_id,
            scope.session_id,
        ),
    ) as cursor:
        queue = await cursor.fetchone()
    if queue is None:
        return None
    return {
        "kind": "queued_submission",
        "submission_id": str(queue["client_submission_id"]),
        "accepted_parent_turn_id": str(queue["accepted_parent_turn_id"]),
        "turn_id": turn_id,
    }


async def _blocked_queue_projection(
    db: aiosqlite.Connection,
    *,
    scope: ReferenceScope,
    turn: aiosqlite.Row,
    turn_id: str,
    visible_resource_identities: frozenset[str],
    visibility_complete: bool,
    include_content: bool,
) -> dict[str, Any] | None:
    """Project an accepted reference that recovery blocked before execution."""
    if turn["status"] != "failed" or turn["error_kind"] != "reference_queue_blocked":
        return None
    locator = await _queued_source_locator(db, scope=scope, turn_id=turn_id)
    if locator is None:
        return None
    source = await _read_chat_turn_user_source_in_tx(
        db,
        turn_id=turn_id,
        expected_user_id=scope.user_id,
        expected_workspace_id=scope.workspace_id,
    )
    if source is None or source.input_document is None:
        raise ReferenceProjectionError("reference_projection_source_changed")
    selected = [node for node in source.input_document.nodes if isinstance(node, ReferenceNode)]
    if not selected:
        raise ReferenceProjectionError("reference_projection_source_changed")
    references = []
    for node in selected:
        resource, visible = _resource_projection(
            node.resource,
            visible_resource_identities,
        )
        references.append(
            {
                "node_id": node.node_id,
                "label": node.label if visible else None,
                "resource_kind": node.resource.kind,
                "resource": resource,
                "resource_visibility": "visible" if visible else "unavailable",
                "operation": requested_reference_operation(node.resource),
                "admission": "accepted",
                "preparation": "failed",
                "usage": [],
                "outcome": "blocked",
                "material": [],
            }
        )
    receiver_node = next(
        (node for node in selected if isinstance(node.resource, AgentResourceRef)),
        None,
    )
    receiver = None
    receiver_visible = None
    if receiver_node is not None:
        receiver, receiver_visible = _resource_projection(
            receiver_node.resource,
            visible_resource_identities,
        )
    reasons = {"execution_blocked_before_preparation"}
    if not visibility_complete:
        reasons.add("resource_visibility_unavailable")
    if receiver_visible is False or any(item["resource_visibility"] == "unavailable" for item in references):
        reasons.add("resource_hidden")
    result: dict[str, Any] = {
        "facts_version": 1,
        "turn_id": turn_id,
        "source_locator": {
            **locator,
            "text_spans": _text_spans(
                source.input_document,
                include_content=include_content,
            ),
        },
        "selection_identity": {
            "source_turn_id": turn_id,
            "source_hash": reference_source_hash(source),
            "node_ids": [node.node_id for node in selected],
        },
        "composition": {
            "resolution_seq": None,
            "composition_digest": None,
            "receiver_source": "mention" if receiver_node is not None else None,
            "receiver": receiver,
            "execution_id": None,
            "execution_status": "blocked",
        },
        "references": references,
        "delegated_payload": None,
        "model_contexts": [],
        "model_invocations": [],
        "cost_attribution": {
            "status": "not_applicable",
            "amount_usd": None,
            "reason": None,
        },
        "evidence_completeness": {
            "status": "partial",
            "reason_codes": sorted(reasons),
        },
        "content_included": include_content,
    }
    result["projection_etag"] = _hash(result)
    return result


async def _queued_input_projection(
    db: aiosqlite.Connection,
    *,
    scope: ReferenceScope,
    accepted_parent_turn_id: str,
    submission_id: str,
    visible_resource_identities: frozenset[str],
    visibility_complete: bool,
    include_content: bool,
    cipher: ReferenceCipher | None,
) -> dict[str, Any]:
    """Read an accepted source before or after materialization, never execute it."""
    async with db.execute(
        "SELECT * FROM chat_reference_queued_inputs WHERE team_id=? AND user_id=? AND workspace_id=? AND session_id=? "
        "AND accepted_parent_turn_id=? AND client_submission_id=?",
        (scope.team_id, scope.user_id, scope.workspace_id, scope.session_id, accepted_parent_turn_id, submission_id),
    ) as cursor:
        queued = await cursor.fetchone()
    if queued is None:
        raise ReferenceProjectionError("reference_projection_not_found")
    reasons: set[str] = set()
    if not visibility_complete:
        reasons.add("resource_visibility_unavailable")
    payload = None
    if cipher is None:
        reasons.add("content_key_unavailable")
    else:
        try:
            saved = await ChatReferenceStore(cipher).get_queued_input_in_connection(db, scope=scope, submission_id=submission_id)
            if saved is not None:
                payload = saved[0]
        except ValueError:
            # A scoped source container may outlive its private material.
            # Keep the acceptance/cancellation fact without reviving its body.
            reasons.add("original_input_unavailable")
    references = []
    if payload is not None:
        if payload.attachment_ids:
            reasons.add("attachment_evidence_unavailable")
        for node in payload.input_document.nodes:
            if not isinstance(node, ReferenceNode):
                continue
            resource, visible = _resource_projection(node.resource, visible_resource_identities)
            item = {
                "node_id": node.node_id,
                "resource_kind": node.resource.kind,
                "resource": resource,
                "resource_visibility": "visible" if visible else "unavailable",
                "label": node.label if visible else None,
            }
            if not visible:
                reasons.add("resource_hidden")
            elif include_content:
                item["parameters"] = node.parameters
            references.append(item)
    child_id = queued["materialized_turn_id"]
    result = {
        "facts_version": 1,
        "turn_id": accepted_parent_turn_id,
        "source_locator": {
            "kind": "queued_submission",
            "submission_id": submission_id,
            "accepted_parent_turn_id": accepted_parent_turn_id,
            "turn_id": child_id,
            "text_spans": _text_spans(payload.input_document, include_content=include_content) if payload is not None else [],
        },
        "selection_identity": {
            "submission_id": submission_id,
            "payload_hash": queued["payload_hash"],
            "node_ids": [item["node_id"] for item in references],
        },
        "queue": {
            "state": queued["state"],
            "row_version": queued["row_version"],
            "accepted_at": queued["created_at"],
            "cancel_reason": queued["cancel_reason"],
        },
        "references": references,
        # Materialization is not execution. Read the child's existing evidence
        # endpoint for actual composition, attempts, results and cost pointers.
        "execution_evidence_turn_id": child_id,
        "evidence_completeness": {"status": "partial" if reasons else "complete", "reason_codes": sorted(reasons)},
        "content_included": include_content,
    }
    result["projection_etag"] = _hash(result)
    return result


async def _continuation_associations(db, *, turn_id: str, invocations: Collection[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Project existing exact card bindings without question/answer content.

    A resolved card alone is not evidence of a resumed execution. Reuse the
    runtime's material/identity checks, but do not require a currently live
    worker when reading completed history.
    """
    from bowhead_core import chat_reference_bi_continuation as bi
    from bowhead_core import chat_reference_lead_continuation as lead
    from bowhead_core import chat_reference_lead_postdispatch as primary

    associations = []
    invalid = False
    for reader in (lead, primary):
        try:
            row = await reader.read_pause(db, turn_id)
            if row is None:
                continue
            candidates = [row]
            contexts = []
            if reader is lead and "question_round" in json.loads(row["resume_context_json"]):
                from bowhead_core.clarification_rounds import history_contexts, read_history, resume_generation_bounds

                history = await read_history(
                    db,
                    turn_id=turn_id,
                    user_id=row["original_user_id"],
                    workspace_id=row["original_workspace_id"],
                    session_id=row["original_session_id"],
                )
                candidates = [{**dict(row), **dict(card)} for card in history]
                contexts = history_contexts(history)
            for candidate in candidates:
                reader._context(candidate)
        except (ValueError, TypeError):
            invalid = True
            continue
        for index, row in enumerate(candidates):
            generation = None
            if reader is primary:
                async with db.execute(
                    "SELECT generation FROM chat_reference_lead_postdispatch_attempts WHERE turn_id=? AND clarification_id=? AND execution_id=?",
                    (turn_id, row["request_id"], row["primary_execution_id"]),
                ) as cursor:
                    attempt = await cursor.fetchone()
                generation = attempt["generation"] if attempt is not None else None
            elif row["status"] == "resolved" and contexts:
                lower, upper = resume_generation_bounds(contexts, index)
                async with db.execute(
                    "SELECT MAX(json_extract(payload_json,'$.execution_generation')) FROM turn_events "
                    "WHERE turn_id=? AND event_type='turn.resumed' AND json_valid(payload_json) "
                    "AND json_type(payload_json,'$.execution_generation')='integer' "
                    "AND json_extract(payload_json,'$.execution_generation')>? "
                    "AND (? IS NULL OR json_extract(payload_json,'$.execution_generation')<=?) "
                    "AND (json_type(payload_json,'$.request_id') IS NULL OR json_extract(payload_json,'$.request_id')=?)",
                    (turn_id, lower, upper, upper, row["request_id"]),
                ) as cursor:
                    generation = (await cursor.fetchone())[0]
            elif row["status"] == "resolved":
                async with db.execute(
                    "SELECT created_generation FROM chat_reference_resolutions "
                    "WHERE turn_id=? AND resolution_seq=2 AND execution_id=? AND created_generation>1",
                    (turn_id, row["primary_execution_id"]),
                ) as cursor:
                    delegation = await cursor.fetchone()
                generation = delegation["created_generation"] if delegation is not None else None
            associations.append(
                {
                    "contract": reader.CONTRACT,
                    "clarification_id": row["request_id"],
                    "execution_id": row["primary_execution_id"],
                    "status": row["status"],
                    "resume_generation": generation,
                }
            )
    pause = await bi.read_pause(db, turn_id)
    if pause is not None and pause["clarification_id"] is not None:
        try:
            question = await bi.require_question(db, pause, history=True)
        except (ValueError, TypeError):
            invalid = True
        else:
            associations.append(
                {
                    "contract": bi.CONTRACT,
                    "clarification_id": question["request_id"],
                    "execution_id": pause["execution_id"],
                    "status": question["status"],
                    "resume_generation": pause["resume_generation"],
                }
            )
    for association in associations:
        association.update(
            source_turn_id=turn_id,
            association_basis="persisted_binding",
            model_invocation_ids=[
                row["invocation_id"]
                for row in invocations
                if row["execution_id"] == association["execution_id"] and row["created_generation"] == association["resume_generation"]
            ],
        )
    return associations, invalid


async def _task_billing(db, *, scope: ReferenceScope, turn_id: str, plan: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
    """Link actual credit entries once; neither USD nor complete model usage."""
    turn_ids = {turn_id}
    if plan is not None:
        if plan["execution_turn_id"]:
            turn_ids.add(plan["execution_turn_id"])
        turn_ids.update(execution["turn_id"] for step in plan["steps"] for execution in step["executions"])
    placeholders = ",".join("?" for _ in turn_ids)
    async with db.execute(
        "SELECT l.id AS ledger_id,l.turn_id,l.delta,l.tokens_input,l.tokens_output,l.rate_card_version "
        "FROM credit_ledger l JOIN turns t ON t.id=l.turn_id JOIN sessions s ON s.id=t.session_id "
        f"WHERE l.turn_id IN ({placeholders}) AND l.reason='turn' "
        "AND l.user_id=? AND l.team_id=? AND l.workspace_id=? "
        "AND s.user_id=l.user_id AND s.workspace_id=l.workspace_id AND s.id=? "
        "ORDER BY l.created_at,l.id LIMIT ?",
        (*sorted(turn_ids), scope.user_id, scope.team_id, scope.workspace_id, scope.session_id, MAX_PROJECTION_FACTS + 1),
    ) as cursor:
        cursor.row_factory = aiosqlite.Row
        rows = await cursor.fetchall()
    truncated = len(rows) > MAX_PROJECTION_FACTS
    return {
        "unit": "credits",
        "scope": "reference_task",
        "attribution": "shared_unallocated",
        "coverage": "partial" if rows else "unavailable",
        "entries": [dict(row) for row in rows[:MAX_PROJECTION_FACTS]],
        "truncated": truncated,
    }, truncated


async def _task_physical_receipts(db, *, scope: ReferenceScope, turn_id: str, plan: dict[str, Any] | None) -> dict[str, Any]:
    turn_ids = {turn_id}
    if plan is not None:
        if plan["execution_turn_id"]:
            turn_ids.add(plan["execution_turn_id"])
        turn_ids.update(execution["turn_id"] for step in plan["steps"] for execution in step["executions"])
    placeholders = ",".join("?" for _ in turn_ids)
    async with db.execute(
        "SELECT u.*,t.status AS current_turn_status,t.execution_generation AS current_generation "
        "FROM llm_usage_events u JOIN turns t ON t.id=u.turn_id JOIN sessions s ON s.id=t.session_id "
        "JOIN workspaces w ON w.id=s.workspace_id "
        f"WHERE u.turn_id IN ({placeholders}) AND u.receipt_json IS NOT NULL "
        "AND u.user_id=? AND u.team_id=? AND u.workspace_id=? AND s.id=? "
        "AND u.user_id=s.user_id AND u.workspace_id=s.workspace_id AND u.team_id=w.team_id "
        "ORDER BY u.created_at,u.id LIMIT ?",
        (*sorted(turn_ids), scope.user_id, scope.team_id, scope.workspace_id, scope.session_id, MAX_PROJECTION_FACTS + 1),
    ) as cursor:
        cursor.row_factory = aiosqlite.Row
        rows = await cursor.fetchall()
    entries = []
    for row in rows[:MAX_PROJECTION_FACTS]:
        receipt = json.loads(row["receipt_json"])
        # Source bodies and tool arguments require their own current resource
        # authorization; this task receipt endpoint exposes only fingerprints.
        sources = [{key: value for key, value in source.items() if key != "tool_name"} for source in receipt["sources"]]
        entries.append(
            {
                "receipt_id": row["id"],
                "turn_id": row["turn_id"],
                "agent_type": row["agent_type"],
                "provider": row["provider"],
                "model": row["model"],
                "reason": row["reason"],
                "state": "unknown"
                if row["receipt_state"] == "pending"
                and (row["current_turn_status"] != "running" or row["current_generation"] != receipt["generation"])
                else row["receipt_state"],
                "stored_state": row["receipt_state"],
                "generation": receipt["generation"],
                "context_id": receipt["context_id"],
                "execution_id": receipt["execution_id"],
                "subagent_id": receipt["subagent_id"],
                "request_digest": receipt["request_digest"],
                "response_digest": row["response_digest"],
                "outcome_code": row["outcome_code"],
                "usage": {"input": row["tokens_input"], "output": row["tokens_output"], "cached": row["tokens_cached"]}
                if row["receipt_state"] == "completed"
                else None,
                "sources": sources,
                "source_coverage": receipt["source_coverage"],
            }
        )
    return {
        "scope": "reference_task",
        "coverage": "partial" if rows else "unavailable",
        "entries": entries,
        "truncated": len(rows) > MAX_PROJECTION_FACTS,
        "amount_usd": None,
    }


async def read_reference_projection(
    db_path: str | Path,
    *,
    scope: ReferenceScope,
    turn_id: str,
    visible_resource_identities: frozenset[str],
    visibility_complete: bool,
    include_content: bool = False,
    cipher: ReferenceCipher | None = None,
    submission_id: str | None = None,
) -> dict[str, Any]:
    """Read one reference Turn from a single SQLite snapshot.

    Exact resources and retained content are emitted only for identities in
    ``visible_resource_identities``.  A failed current-authorization lookup is
    represented as partial evidence instead of falling back to historical IDs.
    """

    scope = ReferenceScope.model_validate(scope.model_dump(mode="python"))
    if type(include_content) is not bool or not isinstance(visible_resource_identities, frozenset):
        raise ReferenceProjectionError("reference_projection_input_invalid")
    try:
        async with open_shared_ctx(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            try:
                await _scope(db, scope, write=False)
                turn = await _turn(db, scope, turn_id)
                if submission_id is not None:
                    return await _queued_input_projection(
                        db,
                        scope=scope,
                        accepted_parent_turn_id=turn_id,
                        submission_id=submission_id,
                        visible_resource_identities=visible_resource_identities,
                        visibility_complete=visibility_complete,
                        include_content=include_content,
                        cipher=cipher,
                    )
                async with db.execute("SELECT * FROM chat_reference_plans WHERE turn_id=?", (turn_id,)) as cursor:
                    cursor.row_factory = aiosqlite.Row
                    plan_row = await cursor.fetchone()
                if plan_row is None:
                    blocked = await _blocked_queue_projection(
                        db,
                        scope=scope,
                        turn=turn,
                        turn_id=turn_id,
                        visible_resource_identities=visible_resource_identities,
                        visibility_complete=visibility_complete,
                        include_content=include_content,
                    )
                    if blocked is not None:
                        return blocked
                    raise ReferenceProjectionError("reference_projection_not_found")
                plan = ReferencePlan.model_validate(decode_reference_json(plan_row["plan_json"], max_bytes=1024 * 1024, max_depth=20))
                source = await _read_chat_turn_user_source_in_tx(
                    db,
                    turn_id=plan.source_turn_id,
                    expected_user_id=scope.user_id,
                    expected_workspace_id=scope.workspace_id,
                )
                if source is None or source.input_document is None or reference_source_hash(source) != plan.source_hash:
                    raise ReferenceProjectionError("reference_projection_source_changed")

                resolutions, resolutions_truncated = await _rows(
                    db,
                    "SELECT * FROM chat_reference_resolutions WHERE turn_id=? ORDER BY resolution_seq LIMIT 101",
                    (turn_id,),
                )
                evidence, evidence_truncated = await _rows(
                    db,
                    "SELECT * FROM chat_reference_evidence WHERE turn_id=? ORDER BY created_at,evidence_key LIMIT 101",
                    (turn_id,),
                )
                snapshots, snapshots_truncated = await _rows(
                    db,
                    "SELECT * FROM chat_reference_content_snapshots WHERE turn_id=? ORDER BY content_key LIMIT 101",
                    (turn_id,),
                )
                contexts, contexts_truncated = await _rows(
                    db,
                    "SELECT * FROM chat_reference_model_contexts WHERE turn_id=? ORDER BY created_at,context_id LIMIT 101",
                    (turn_id,),
                )
                invocations, invocations_truncated = await _rows(
                    db,
                    "SELECT * FROM chat_reference_model_invocations WHERE turn_id=? ORDER BY created_at,invocation_id LIMIT 101",
                    (turn_id,),
                )
                if any(row["status"] == "pending" for row in invocations):
                    from bowhead_core.skill_model_status import live_reference_model_executions_in_connection

                    live = await live_reference_model_executions_in_connection(db, scope=scope, turn_id=turn_id)
                else:
                    live = set()
                invocations = [
                    {
                        **dict(row),
                        "stored_status": row["status"],
                        "status": "delivery_unknown"
                        if row["status"] == "pending" and (row["execution_id"], row["created_generation"]) not in live
                        else row["status"],
                    }
                    for row in invocations
                ]
                capability_executions, capabilities_truncated = await _rows(
                    db,
                    "SELECT * FROM turn_capability_execution WHERE turn_id=? ORDER BY created_at,id LIMIT 101",
                    (turn_id,),
                )
                async with db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_reference_mcp_calls'") as cursor:
                    mcp_call_table = await cursor.fetchone()
                if mcp_call_table is None:
                    mcp_calls, mcp_calls_truncated = [], False
                else:
                    mcp_calls, mcp_calls_truncated = await _rows(
                        db,
                        "SELECT * FROM chat_reference_mcp_calls WHERE turn_id=? ORDER BY call_seq LIMIT 101",
                        (turn_id,),
                    )
                async with db.execute(
                    "SELECT e.*,b.binding_source,b.primary_execution_id,b.assignment_id AS receiver_assignment_id, "
                    "CASE WHEN e.agent_source_kind='team' AND json_valid(tr.listing_json) THEN json_extract(tr.listing_json,'$.display_name') "
                    "WHEN e.agent_source_kind='system' AND json_valid(sr.definition_json) THEN json_extract(sr.definition_json,'$.display_name') END "
                    "AS receiver_label_snapshot FROM turn_agent_binding b "
                    "LEFT JOIN turn_agent_execution e ON e.id=b.primary_execution_id "
                    "LEFT JOIN team_subagent_revision tr ON e.agent_source_kind='team' AND tr.team_id=e.team_id "
                    "AND tr.subagent_id=e.agent_id AND tr.id=e.revision_ref "
                    "LEFT JOIN system_agent_effective_revision sr ON e.agent_source_kind='system' AND sr.team_id=e.team_id "
                    "AND sr.agent_id=e.agent_id AND sr.id=e.revision_ref WHERE b.turn_id=?",
                    (turn_id,),
                ) as cursor:
                    cursor.row_factory = aiosqlite.Row
                    execution = await cursor.fetchone()

                material_available = await reference_material_available_in_connection(db, turn_id=turn_id)
                continuations, continuation_invalid = await _continuation_associations(db, turn_id=turn_id, invocations=invocations)
                from bowhead_core.chat_reference_plan import read_plan_association

                plan_association = None
                plan_association_invalid = False
                try:
                    plan_association = await read_plan_association(db, turn_id=turn_id)
                except ValueError:
                    plan_association_invalid = True
                billing, billing_truncated = await _task_billing(db, scope=scope, turn_id=turn_id, plan=plan_association)
                descriptors: list[tuple[ContentDescriptor, aiosqlite.Row]] = []
                descriptor_invalid = False
                for row in snapshots:
                    try:
                        descriptor = ContentDescriptor.model_validate(decode_reference_json(row["descriptor_json"], max_bytes=65536, max_depth=20))
                    except (ValueError, TypeError):
                        descriptor_invalid = True
                        continue
                    descriptors.append((descriptor, row))

                composition: dict[str, Any] | None = None
                composition_invalid = False
                composition_row = next((row for row in resolutions if row["resolution_seq"] == 1), None)
                if resolutions:
                    try:
                        if composition_row is None:
                            raise ValueError("primary composition is unavailable")
                        composition = decode_reference_json(composition_row["composition_json"], max_bytes=1024 * 1024, max_depth=20)
                        if not isinstance(composition, dict):
                            raise ValueError("composition must be an object")
                    except (ValueError, TypeError):
                        composition_invalid = True
                        composition = None

                execution_id = str(execution["id"]) if execution is not None and execution["id"] is not None else None
                execution_status = str(execution["status"]) if execution is not None and execution["status"] is not None else None
                agent_executions, agents_truncated = await _rows(
                    db,
                    "SELECT id,parent_execution_id,dispatch_source,status,started_at,ended_at "
                    "FROM turn_agent_execution WHERE turn_id=? AND team_id=? "
                    "ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END,created_at,id LIMIT 101",
                    (turn_id, scope.team_id, execution_id),
                )
                # These receipts cover the reference primary, not Lead or helper
                # calls. Absence of a receipt never proves zero orchestration cost.
                lead_orchestration = plan.orchestration_contract == "lead-delegation.v1"
                cost_unavailable = bool(invocations) or lead_orchestration or any(row["started_at"] is not None for row in agent_executions)
                prompt_results = ()
                prompt_results_invalid = False
                if composition is not None and composition.get("runtime_policy") == "initial-primary-mcp-prompt.v1":
                    lead_prompt = plan.orchestration_contract == "lead-delegation.v1"
                    result_row = next(
                        (
                            row
                            for row in resolutions
                            if (row["idempotency_key"] == "reference:primary:mcp-prompt-results" if lead_prompt else row["resolution_seq"] == 2)
                        ),
                        None,
                    )
                    if result_row is not None:
                        try:
                            saved = ReferenceMcpPromptResultResolution.model_validate_json(result_row["composition_json"], strict=True)
                            if (
                                saved.runtime_policy != ("lead-mcp-prompt-results.v1" if lead_prompt else "initial-primary-mcp-prompt-results.v1")
                                or (result_row["resolution_seq"] <= 2 if lead_prompt else result_row["resolution_seq"] != 2)
                                or saved.digest != result_row["composition_digest"]
                                or saved.execution_id != execution_id
                                or saved.source_turn_id != plan.source_turn_id
                                or saved.primary_composition_digest != composition_row["composition_digest"]
                                or saved.prompt_selection_digest != composition.get("prompt_selection_digest")
                            ):
                                raise ValueError("Prompt result scope changed")
                            prompt_results = saved.results
                        except (ValueError, TypeError):
                            prompt_results_invalid = True
                common_stages: list[str] = []
                for row in evidence:
                    kind = str(row["kind"])
                    if kind not in common_stages:
                        common_stages.append(kind)
                if contexts and "context_assembled" not in common_stages:
                    common_stages.append("context_assembled")
                for row in invocations:
                    stage = {
                        "pending": "provider_request_started",
                        "completed": "provider_request_completed",
                        "not_applied": "provider_request_not_applied",
                        "delivery_unknown": "delivery_unknown",
                    }.get(str(row["status"]))
                    if stage and stage not in common_stages:
                        common_stages.append(stage)

                skill_members: list[dict[str, Any]] = []
                if composition is not None and composition.get("members"):
                    from bowhead_core.skill_reference_execution import SkillCompositionMember

                    try:
                        members = [SkillCompositionMember.model_validate(item) for item in composition["members"]]
                        if len(members) > MAX_PROJECTION_FACTS:
                            raise ValueError("composition member limit")
                        pointer = ContentDescriptor.model_validate(composition["content"])
                        compound = next(((descriptor, row) for descriptor, row in descriptors if descriptor == pointer), None)
                        material_execution_ids = (
                            {item["execution_id"] for step in plan_association["steps"] for item in step["executions"]}
                            if plan_association is not None
                            else {execution_id}
                        )
                        context_ids = [
                            row["context_id"]
                            for row in contexts
                            if row["composition_digest"] == composition_row["composition_digest"] and row["execution_id"] in material_execution_ids
                        ]
                        for member in members:
                            resource, visible = _resource_projection(member.resource, visible_resource_identities)
                            material = []
                            if visible and compound is not None:
                                descriptor, row = compound
                                material.append(
                                    _skill_member_projection(
                                        member=member,
                                        descriptor=descriptor,
                                        row=row,
                                        scope=scope,
                                        turn_id=turn_id,
                                        include_content=include_content,
                                        cipher=cipher,
                                        material_available=material_available,
                                    )
                                )
                            skill_members.append(
                                {
                                    "resource": resource,
                                    "resource_visibility": "visible" if visible else "unavailable",
                                    "node_id": member.node_id,
                                    "provenance": member.provenance,
                                    "stage": "context_assembled" if context_ids else "prepared",
                                    "context_ids": context_ids,
                                    "material": material,
                                }
                            )
                    except (ValueError, TypeError, KeyError):
                        composition_invalid = True
                        skill_members = []

                labels = {node.node_id: node.label for node in source.input_document.nodes if isinstance(node, ReferenceNode)}
                references: list[dict[str, Any]] = []
                for admission in plan.admissions:
                    resource, visible = _resource_projection(admission.resource, visible_resource_identities)
                    capability_execution = (
                        next(
                            (
                                row
                                for row in capability_executions
                                if row["agent_execution_id"] == execution_id
                                and row["composition_node_id"] == admission.node_id
                                and row["attempt_generation"] == 1
                                and row["capability_id"] == admission.resource.id
                                and row["capability_version"] == admission.resource.version
                            ),
                            None,
                        )
                        if admission.resource.kind == "capability"
                        else None
                    )
                    capability_status = str(capability_execution["status"]) if capability_execution is not None else None
                    if admission.resource.kind == "capability":
                        stages = []
                        if capability_execution is not None:
                            stages.append("implementation_selected")
                        if capability_execution is not None and capability_execution["started_at"] is not None:
                            stages.append("implementation_started")
                        if capability_status == "completed":
                            stages.append("output_contract_satisfied")
                    elif admission.resource.kind in {"mcp_source", "mcp_tool"}:
                        calls = [row for row in mcp_calls if row["node_id"] == admission.node_id]
                        stages = []
                        if calls:
                            stages.append("tool_call_started")
                        if any(row["status"] == "completed" and row["result_ok"] == 1 for row in calls):
                            stages.append("tool_result_completed")
                        if any(row["status"] in {"failed", "interrupted"} for row in calls):
                            stages.append("tool_result_failed")
                    elif admission.resource.kind == "mcp_prompt":
                        committed = any(
                            item.node_id == admission.node_id
                            and item.resource == admission.resource
                            and any(item.content == descriptor for descriptor, _row in descriptors)
                            for item in prompt_results
                        )
                        # Expansion alone is preparation. The same exact
                        # execution must also have a confirmed provider request
                        # and an output receipt to count as fulfilled.
                        stages = ["prompt_result_committed", *common_stages] if committed else []
                    else:
                        stages = list(common_stages if admission.resource.kind in {"agent", "skill"} else ())
                    material = [
                        _content_projection(
                            row=row,
                            descriptor=descriptor,
                            scope=scope,
                            turn_id=turn_id,
                            include_content=include_content,
                            cipher=cipher,
                            material_available=material_available,
                        )
                        for descriptor, row in descriptors
                        if visible and _descriptor_matches(descriptor, admission.resource, admission.node_id)
                    ]
                    if visible and admission.resource.kind == "skill" and skill_members:
                        material = next(
                            (item["material"] for item in skill_members if item["node_id"] == admission.node_id and item["resource"] == resource), []
                        )
                    prepared = (
                        "ready"
                        if admission.resource.kind == "agent" and composition is not None
                        else "ready"
                        if admission.resource.kind == "capability" and capability_execution is not None
                        else "ready"
                        if admission.resource.kind in {"mcp_source", "mcp_tool"}
                        and composition is not None
                        and any(
                            isinstance(item, dict)
                            and item.get("node_id") == admission.node_id
                            and item.get("resource") == admission.resource.model_dump(mode="json", exclude_none=True)
                            for item in composition.get("mcp_admissions", [])
                        )
                        else "ready"
                        if material
                        else "pending"
                    )
                    if any(item["state"] != "available" for item in material):
                        prepared = "failed"
                    projected_reference = {
                        "node_id": admission.node_id,
                        "label": labels.get(admission.node_id) if visible else None,
                        "resource_kind": admission.resource.kind,
                        "resource": resource,
                        "resource_visibility": "visible" if visible else "unavailable",
                        "operation": admission.operation,
                        "admission": "accepted",
                        "preparation": prepared,
                        "usage": stages,
                        "outcome": _outcome(
                            kind=admission.resource.kind,
                            turn_status=str(turn["status"]),
                            execution_status=capability_status if admission.resource.kind == "capability" else execution_status,
                            stages=stages,
                        ),
                        "material": material,
                    }
                    if plan_association is not None:
                        required_steps = plan_association["steps"]
                        complete = (
                            plan_association["status"] == "completed"
                            and not invocations_truncated
                            and bool(required_steps)
                            and all(
                                step["status"] == "done"
                                and any(
                                    execution["status"] == "completed"
                                    and any(
                                        receipt["execution_id"] == execution["execution_id"] and receipt["status"] == "completed"
                                        for receipt in invocations
                                    )
                                    for execution in step["executions"]
                                )
                                for step in required_steps
                            )
                            and bool(invocations)
                            and all(row["status"] in {"completed", "not_applied"} for row in invocations)
                        )
                        projected_reference["outcome"] = (
                            "fulfilled"
                            if complete
                            else "cancelled"
                            if plan_association["status"] == "cancelled"
                            else "blocked"
                            if plan_association["status"] in {"blocked", "failed"}
                            else "partial"
                            if any(row["status"] == "delivery_unknown" for row in invocations)
                            else "pending"
                        )
                    if admission.resource.kind == "capability" and visible and capability_execution is not None:
                        projected_reference["capability_execution"] = {
                            "execution_id": capability_execution["id"],
                            "attempt_generation": capability_execution["attempt_generation"],
                            "implementation": {
                                "id": capability_execution["implementation_id"],
                                "version": capability_execution["implementation_version"],
                            },
                            "output_contract": capability_execution["output_contract"],
                            "status": capability_execution["status"],
                            "failure_code": capability_execution["failure_code"],
                        }
                    if admission.resource.kind in {"mcp_source", "mcp_tool"} and visible:
                        projected_reference["mcp_calls"] = [
                            {
                                "call_id": row["call_id"],
                                "call_seq": row["call_seq"],
                                "operation_name": row["operation_name"],
                                "status": row["status"],
                                "result_ok": None if row["result_ok"] is None else bool(row["result_ok"]),
                                "result_hash": row["result_hash"],
                                "failure_code": row["failure_code"],
                            }
                            for row in mcp_calls
                            if row["node_id"] == admission.node_id
                        ]
                    references.append(projected_reference)

                receiver: dict[str, Any] | None = None
                receiver_source = None
                receiver_label = None
                receiver_visible: bool | None = None
                if composition is not None and isinstance(composition.get("resource"), dict):
                    try:
                        receiver_ref = _RESOURCE_ADAPTER.validate_python(composition["resource"])
                        receiver, receiver_visible = _resource_projection(receiver_ref, visible_resource_identities)
                        # Display the admitted revision's label, never today's name or an opaque ID.
                        if (
                            receiver_visible
                            and isinstance(receiver_ref, AgentResourceRef)
                            and execution is not None
                            and composition.get("execution_id") == execution_id
                            and (
                                execution["agent_id"],
                                execution["agent_source_kind"],
                                execution["revision_ref"],
                                execution["release_id"],
                            )
                            == (receiver_ref.id, receiver_ref.source_kind, receiver_ref.version, receiver_ref.release_id)
                            and (receiver_ref.source_kind == "system" or execution["receiver_assignment_id"] == receiver_ref.assignment_id)
                            and isinstance(execution["receiver_label_snapshot"], str)
                        ):
                            receiver_label = execution["receiver_label_snapshot"].strip()[:256] or None
                    except (ValueError, TypeError, AttributeError):
                        composition_invalid = True
                    receiver_source = composition.get("receiver_source") or "mention"

                model_invocations = [
                    {
                        "invocation_id": row["invocation_id"],
                        "context_id": row["context_id"],
                        "execution_id": row["execution_id"],
                        "generation": row["created_generation"],
                        "status": row["status"],
                        "stored_status": row["stored_status"],
                        "input_tokens": row["input_tokens"],
                        "output_tokens": row["output_tokens"],
                        "cached_tokens": row["cached_tokens"],
                        "cost_usd": None,
                    }
                    for row in invocations
                ]

                reasons = set()
                if plan_association_invalid:
                    reasons.add("plan_association_unavailable")
                if continuation_invalid:
                    reasons.add("continuation_association_unavailable")
                if not visibility_complete:
                    reasons.add("resource_visibility_unavailable")
                if composition is None or composition_invalid:
                    reasons.add("composition_unavailable")
                if descriptor_invalid:
                    reasons.add("material_descriptor_unavailable")
                if prompt_results_invalid:
                    reasons.add("prompt_result_unavailable")
                if any(
                    (
                        resolutions_truncated,
                        evidence_truncated,
                        snapshots_truncated,
                        contexts_truncated,
                        invocations_truncated,
                        capabilities_truncated,
                        mcp_calls_truncated,
                        agents_truncated,
                        billing_truncated,
                    )
                ):
                    reasons.add("facts_truncated")
                if cost_unavailable:
                    reasons.add("cost_unavailable")
                if lead_orchestration:
                    reasons.add("orchestration_usage_unavailable")
                if any(row["status"] == "delivery_unknown" for row in invocations):
                    reasons.add("model_delivery_unknown")
                if include_content and cipher is None:
                    reasons.add("content_key_unavailable")
                if include_content and (
                    any(material.get("content_state") != "included" for item in [*references, *skill_members] for material in item["material"])
                    or any(item["resource_visibility"] == "visible" and not item["material"] for item in skill_members)
                ):
                    reasons.add("injection_content_unavailable")
                if receiver_visible is False or any(item["resource_visibility"] == "unavailable" for item in [*references, *skill_members]):
                    reasons.add("resource_hidden")

                delegated_payload: dict[str, Any] | None = None
                if plan.orchestration_contract == "lead-delegation.v1":
                    from bowhead_core.chat_reference_delegation import ReferenceDelegation

                    delegation_row = next((row for row in resolutions if row["resolution_seq"] == 2), None)
                    if delegation_row is not None:
                        try:
                            actual = ReferenceDelegation.model_validate_json(delegation_row["composition_json"], strict=True)
                            if (
                                actual.digest != delegation_row["composition_digest"]
                                or actual.execution_id != execution_id
                                or actual.source_turn_id != plan.source_turn_id
                                or actual.source_hash != plan.source_hash
                                or actual.source_composition_digest != composition_row["composition_digest"]
                                or composition.get("orchestration_contract") != "lead-delegation.v1"
                                or actual.node_id != composition.get("node_id")
                                or actual.task.resource != _RESOURCE_ADAPTER.validate_python(composition["resource"])
                            ):
                                raise ValueError("delegation facts changed")
                            snapshot = next(((descriptor, row) for descriptor, row in descriptors if descriptor == actual.task), None)
                            if snapshot is None or snapshot[1]["retention_state"] != "available" or not material_available:
                                reasons.add("delegation_material_unavailable")
                            delegated_payload = {
                                "execution_id": execution_id,
                                "content_hash": actual.task_hash if receiver_visible else None,
                                "content": None,
                                "content_state": "not_requested" if not include_content else "unavailable",
                            }
                            if include_content and receiver_visible and material_available and cipher is not None and snapshot is not None:
                                descriptor, row = snapshot
                                if row["retention_state"] == "available" and row["ciphertext"] is not None:
                                    content = cipher.open(
                                        scope=scope,
                                        purpose="content",
                                        identity=f"{turn_id}/{descriptor.content_key}",
                                        ciphertext=str(row["ciphertext"]),
                                    )
                                    if (
                                        len(content) != descriptor.byte_count
                                        or "sha256:" + hashlib.sha256(content).hexdigest() != descriptor.content_hash
                                    ):
                                        raise ValueError("delegation task changed")
                                    material = decode_reference_json(content, max_bytes=MAX_REFERENCE_STORAGE_BYTES, max_depth=10)
                                    task = material.get("task")
                                    if not isinstance(task, str) or "sha256:" + hashlib.sha256(task.encode()).hexdigest() != actual.task_hash:
                                        raise ValueError("delegation task changed")
                                    delegated_payload["content"] = task
                                    delegated_payload["content_state"] = "included"
                        except (ValueError, TypeError, KeyError, AttributeError):
                            reasons.add("delegation_unavailable")
                            delegated_payload = None
                    elif execution_status in {"running", "completed", "failed"}:
                        reasons.add("delegation_unavailable")
                    if include_content and delegated_payload is not None and delegated_payload["content_state"] != "included":
                        reasons.add("delegation_content_unavailable")
                elif composition is not None:
                    delegated_payload = {
                        "execution_id": execution_id,
                        "content_hash": composition.get("task_hash"),
                        "content": source.input_document.text_content() if include_content else None,
                        "content_state": "included" if include_content else "not_requested",
                    }
                result: dict[str, Any] = {
                    "facts_version": 1,
                    "turn_id": turn_id,
                    "source_locator": {
                        "kind": "turn_source",
                        "turn_id": plan.source_turn_id,
                        "logical_source_turn_id": plan.logical_source_turn_id,
                        **(await _queued_source_locator(db, scope=scope, turn_id=plan.source_turn_id) or {}),
                        "text_spans": _text_spans(source.input_document, include_content=include_content),
                    },
                    "selection_identity": {
                        "source_turn_id": plan.source_turn_id,
                        "source_hash": plan.source_hash,
                        "node_ids": [item.node_id for item in plan.admissions],
                    },
                    "composition": {
                        "resolution_seq": composition_row["resolution_seq"] if composition_row is not None else None,
                        "composition_digest": composition_row["composition_digest"] if composition_row is not None else None,
                        "receiver_source": receiver_source,
                        "receiver_label": receiver_label,
                        "skill_members": skill_members,
                        "receiver": receiver,
                        "execution_id": execution_id,
                        "execution_status": execution_status,
                    },
                    "orchestration": {
                        "contract": plan.orchestration_contract,
                        "turn_status": turn["status"],
                        "execution_generation": turn["execution_generation"],
                        "model_receipt_scope": "reference_plan_steps" if plan_association is not None else "reference_primary",
                        "agent_executions": [
                            {
                                "execution_id": row["id"],
                                "parent_execution_id": row["parent_execution_id"],
                                "is_primary": row["id"] == execution_id,
                                "dispatch_source": row["dispatch_source"],
                                "status": row["status"],
                                "started_at": row["started_at"],
                                "ended_at": row["ended_at"],
                            }
                            for row in agent_executions
                        ],
                    },
                    "references": references,
                    "continuations": continuations,
                    "plan": plan_association,
                    "delegated_payload": delegated_payload,
                    "model_contexts": [
                        {
                            "context_id": row["context_id"],
                            "execution_id": row["execution_id"],
                            "generation": row["created_generation"],
                            "composition_digest": row["composition_digest"],
                            "input_hash": row["input_hash"],
                            "required_content_hash": row["required_content_hash"],
                            "state": row["retention_state"],
                        }
                        for row in contexts
                    ],
                    "model_invocations": model_invocations,
                    "billing": billing,
                    "physical_calls": await _task_physical_receipts(db, scope=scope, turn_id=turn_id, plan=plan_association),
                    "cost_attribution": {
                        "status": "unavailable" if cost_unavailable else "not_applicable",
                        "amount_usd": None,
                        "reason": "shared_or_unlinked" if cost_unavailable else None,
                    },
                    "evidence_completeness": {
                        "status": "partial" if reasons else "complete",
                        "reason_codes": sorted(reasons),
                    },
                    "content_included": include_content,
                }
                for association in continuations:
                    association["task_physical_receipt_ids"] = [
                        receipt["receipt_id"]
                        for receipt in result["physical_calls"]["entries"]
                        if receipt["turn_id"] == turn_id and receipt["generation"] == association["resume_generation"]
                    ]
                result["projection_etag"] = _hash(result)
                return result
            finally:
                await db.rollback()
    except ReferenceProjectionError:
        raise
    except (aiosqlite.Error, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ReferenceProjectionError("reference_projection_unavailable") from exc
