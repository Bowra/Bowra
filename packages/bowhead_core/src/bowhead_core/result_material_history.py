"""Scoped native pointers for the existing reconstructed Turn projection."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from bowhead_router.types import Message

from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_scope import ReferenceRequestScope, require_reference_request_scope
from bowhead_core.result_materials import (
    MAX_BYTES,
    RECOVERY_GUIDANCE,
    MaterialOrigin,
    MaterialReference,
    MaterialReferences,
    bounded_references,
    model_reference_view,
    parse_references,
    recovery_enabled,
)

logger = logging.getLogger(__name__)
MAX_EVENTS_PER_TURN = 512
MAX_RETAINED_TURNS = 16
SUPPLEMENT_NAME = "bowhead_material_supplement_v1"


@dataclass(frozen=True)
class MaterialSupplement:
    scope: ReferenceRequestScope
    references: MaterialReferences
    message: Message


async def load_retained_material_supplement(ctx: Any, *, readers: tuple[str, ...]) -> MaterialSupplement | None:
    """Project bounded original pointers outside the immutable conversation.

    Only a previously authorized, published context head is eligible. The
    canonical suffix already has its own projection. This optional request
    appendix neither restores old Tool calls nor changes their saved messages.
    """
    history = getattr(ctx, "_model_context_history", None)
    generation = getattr(history, "generation_id", None)
    if not recovery_enabled() or not generation or getattr(ctx, "report_read_access", None) is None:
        return None
    try:
        scope = ReferenceRequestScope(team_id=ctx.team_id, user_id=ctx.user_id, workspace_id=ctx.workspace_id, session_id=ctx.session_id)
        async with open_shared_ctx(ctx.session_store.db_path) as db:
            await require_reference_request_scope(db, scope)
            async with db.execute(
                "SELECT h.published_turn_seq FROM model_context_conversation_heads h "
                "JOIN turns current ON current.session_id=h.session_id AND current.id=? "
                "WHERE h.session_id=? AND h.generation_id=? AND h.published_turn_seq<current.seq",
                (ctx.turn_id, ctx.session_id, generation),
            ) as cursor:
                head = await cursor.fetchone()
            if head is None:
                return None
            async with db.execute(
                "SELECT id FROM turns WHERE session_id=? AND seq<=? AND operation_kind IS NULL "
                "AND status IN ('completed','failed','cancelled','aborted') ORDER BY seq DESC LIMIT ?",
                (ctx.session_id, head[0], MAX_RETAINED_TURNS + 1),
            ) as cursor:
                turns = await cursor.fetchall()
        # Both scan work and output are bounded. The existing reader separately
        # caps each Turn's event count and each native metadata field's bytes.
        views = await load_material_history(ctx, [row[0] for row in turns[:MAX_RETAINED_TURNS]])
        if not views:
            return None
        references = bounded_references(
            (MaterialReference.model_validate(item) for view in views.values() for item in view["items"]),
            incomplete=len(turns) > MAX_RETAINED_TURNS or any(view["coverage"]["references"] != "complete" for view in views.values()),
            reason="history_scan_budget" if len(turns) > MAX_RETAINED_TURNS else "history_projection_incomplete",
        )
        view = model_reference_view(references, readers=readers)
        while True:
            content = json.dumps({"material_refs": view, "material_recovery_guidance": RECOVERY_GUIDANCE}, ensure_ascii=False, sort_keys=True)
            if len(content.encode()) <= MAX_BYTES:
                break
            view["items"].pop()
            coverage = view["coverage"]
            if coverage["references"] != "unknown":
                coverage["references"] = "partial"
            if coverage["omitted_count"] is not None:
                coverage["omitted_count"] += 1
            if "budget" not in coverage["reasons"]:
                coverage["reasons"].append("budget")
        # Final authorization need only cover identities actually being sent.
        visible = MaterialReferences.model_validate({key: view[key] for key in ("version", "coverage", "items")})
        return MaterialSupplement(scope, visible, Message(role="user", name=SUPPLEMENT_NAME, content=content))
    except Exception:
        logger.warning("retained_material_supplement_unavailable turn=%s", getattr(ctx, "turn_id", None))
        return None


async def authorize_material_supplement(ctx: Any, supplement: MaterialSupplement) -> bool:
    """Recheck optional pointer disclosure after potentially slow preparation."""
    try:
        scope = ReferenceRequestScope(team_id=ctx.team_id, user_id=ctx.user_id, workspace_id=ctx.workspace_id, session_id=ctx.session_id)
        if not recovery_enabled() or scope != supplement.scope:
            return False
        async with open_shared_ctx(ctx.session_store.db_path) as db:
            await require_reference_request_scope(db, scope)
        for item in supplement.references.items:
            if not await ctx.report_read_access.authorize_reference(item):
                return False
        return True
    except Exception:
        logger.warning("retained_material_supplement_authority_unavailable turn=%s", getattr(ctx, "turn_id", None))
        return False


async def load_material_history(ctx: Any, turn_ids: list[str]) -> dict[str, dict]:
    access = getattr(ctx, "report_read_access", None)
    if not recovery_enabled() or access is None or not turn_ids:
        return {}
    result = {}
    try:
        scope = ReferenceRequestScope(team_id=ctx.team_id, user_id=ctx.user_id, workspace_id=ctx.workspace_id, session_id=ctx.session_id)
        async with open_shared_ctx(ctx.session_store.db_path) as db:
            await require_reference_request_scope(db, scope)
            async with db.execute("SELECT seq FROM turns WHERE id=? AND session_id=?", (ctx.turn_id, ctx.session_id)) as cursor:
                current = await cursor.fetchone()
            if current is None:
                return {}
            for turn_id in dict.fromkeys(turn_ids):
                async with db.execute(
                    "SELECT 1 FROM turns WHERE id=? AND session_id=? AND seq<? AND status IN ('completed','failed','cancelled','aborted')",
                    (turn_id, ctx.session_id, current[0]),
                ) as cursor:
                    if await cursor.fetchone() is None:
                        continue
                async with db.execute(
                    "SELECT seq,event_type,json_extract(payload_json,'$.subagent_id'),json_extract(payload_json,'$.call_id'),"
                    "json_extract(payload_json,'$.profile'),json_extract(payload_json,'$.tool_name'),"
                    "CASE WHEN length(CAST(json_extract(payload_json,'$.result_meta.material_refs') AS BLOB))<=? "
                    "THEN json_extract(payload_json,'$.result_meta.material_refs') END,"
                    "CASE WHEN length(CAST(json_extract(payload_json,'$.material_refs') AS BLOB))<=? "
                    "THEN json_extract(payload_json,'$.material_refs') END,"
                    "json_extract(payload_json,'$.canvas_id'),COALESCE(json_extract(payload_json,'$.section.id'),json_extract(payload_json,'$.section_id')),"
                    "COALESCE(json_extract(payload_json,'$.section.rev'),json_extract(payload_json,'$.rev')), "
                    "CASE WHEN length(CAST(json_extract(payload_json,'$.result_meta') AS BLOB))<=? "
                    "THEN json_extract(payload_json,'$.result_meta') END "
                    "FROM turn_events WHERE turn_id=? AND json_valid(payload_json) "
                    "AND event_type IN ('agent_spawned','tool_result','step_done','lead.subagent_summary','canvas.section_added','canvas.section_patched') "
                    "ORDER BY seq LIMIT ?",
                    (MAX_BYTES, MAX_BYTES, MAX_BYTES, turn_id, MAX_EVENTS_PER_TURN + 1),
                ) as cursor:
                    rows = await cursor.fetchall()
                items, incomplete = [], len(rows) > MAX_EVENTS_PER_TURN
                rows = rows[:MAX_EVENTS_PER_TURN]
                producers = {r[2]: r[4] for r in rows if r[1] == "agent_spawned" and r[2]}
                # Direct receipts precede their aggregates in this canonical selection.
                ordered = sorted(rows, key=lambda r: (r[1] != "tool_result" or r[5] == "subagent_spawn", r[0]))
                for seq, kind, producer, call_id, _profile, tool, raw, summary, canvas_id, section_id, revision, legacy_raw in ordered:
                    payload = raw or summary
                    if payload:
                        try:
                            receipt = parse_references(json.loads(payload))
                        except (ValueError, TypeError):
                            receipt = None
                        if receipt is None:
                            incomplete = True
                            continue
                        incomplete |= receipt.coverage.references != "complete"
                        for item in receipt.items:
                            origin = item.origin
                            if origin.kind != "tool" or origin.turn_id != turn_id:
                                incomplete = True
                                continue
                            is_direct = kind == "tool_result" and tool != "subagent_spawn"
                            bound = (
                                (
                                    origin.call_id == call_id
                                    and (origin.producer_id == producer or origin.producer_id is None and producers.get(producer) == "lead")
                                )
                                if is_direct
                                else origin.producer_id in producers
                            )
                            if not bound:
                                incomplete = True
                                continue
                            items.append(item)
                    elif (
                        kind == "tool_result"
                        # Native events surface the flat/wrapped Python entry
                        # as python_sandbox; invocation names are not event names.
                        and tool in {"bi_query", "python_exec", "python_sandbox", "skill_invoke"}
                        and legacy_raw
                        and call_id
                        and producer in producers
                    ):
                        # Legacy native metadata is bounded but may have been capped;
                        # recover known pointers without inventing complete coverage.
                        incomplete = True
                        try:
                            legacy = json.loads(legacy_raw)
                            entries = legacy.get("sub_sources", []) if tool == "bi_query" else legacy.get("output_artifacts", [])
                            ids = [entry.get("artifact_id") for entry in entries if isinstance(entry, dict)]
                            if tool == "bi_query":
                                ids.insert(0, legacy.get("artifact_id"))
                            for identity in dict.fromkeys(ids):
                                if isinstance(identity, str) and identity.startswith("art_"):
                                    items.append(
                                        MaterialReference(
                                            kind="artifact",
                                            locator={"artifact_id": identity},
                                            relation="reused",
                                            origin=MaterialOrigin(
                                                kind="tool",
                                                turn_id=turn_id,
                                                producer_id=None if producers[producer] == "lead" else producer,
                                                call_id=call_id,
                                            ),
                                        )
                                    )
                        except (ValueError, TypeError, AttributeError):
                            continue
                    elif kind in {"canvas.section_added", "canvas.section_patched"} and canvas_id and section_id:
                        try:
                            items.append(
                                MaterialReference(
                                    kind="canvas_section",
                                    locator={"canvas_id": canvas_id, "section_id": section_id},
                                    observed_version={"rev": revision} if type(revision) is int and revision > 0 else {},
                                    relation="created" if kind.endswith("added") else "updated",
                                    origin=MaterialOrigin(kind="canvas_event", turn_id=turn_id, event_seq=seq),
                                )
                            )
                        except ValueError:
                            incomplete = True

                # Only an identical observed revision/effect proves a Canvas mirror.
                # Unknown revisions and distinct updates remain separate observations.
                def section_effect(item: MaterialReference) -> tuple[str | None, str | None, int | None, str]:
                    return (item.locator.get("canvas_id"), item.locator.get("section_id"), item.observed_version.get("rev"), item.relation)

                native_effects = {
                    section_effect(item)
                    for item in items
                    if item.origin.kind == "tool" and item.kind == "canvas_section" and "rev" in item.observed_version
                }
                items = [item for item in items if item.origin.kind != "canvas_event" or section_effect(item) not in native_effects]
                # One shared Turn allocation: native receipts precede event fallbacks.
                event_order = {(r[2], r[3]): r[0] for r in rows if r[1] == "tool_result"}
                items.sort(
                    key=lambda item: (
                        item.origin.kind == "canvas_event",
                        item.origin.event_seq
                        if item.origin.event_seq is not None
                        else event_order.get(
                            (item.origin.producer_id, item.origin.call_id),
                            next((r[0] for r in rows if r[1] == "tool_result" and r[3] == item.origin.call_id), MAX_EVENTS_PER_TURN),
                        ),
                    )
                )
                receipt = bounded_references(items, incomplete=incomplete, reason="legacy_receipt")
                allowed = []
                for item in receipt.items:
                    if await access.authorize_reference(item):
                        allowed.append(item)
                    else:
                        incomplete = True
                if receipt.items or incomplete:
                    projected = bounded_references(
                        allowed, incomplete=incomplete or receipt.coverage.references != "complete", reason="receipt_unavailable"
                    )
                    result[turn_id] = model_reference_view(projected)
    except Exception:
        logger.warning("material_history_unavailable turn=%s", getattr(ctx, "turn_id", None))
        # Never return a partly authorized raw-event fallback.
        return {}
    return result
