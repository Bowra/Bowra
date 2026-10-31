"""Bounded original Coach references, transported as data rather than authority."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from bowhead_router.router import Router

    from bowhead_core.tool_registry import ToolExecutionContext

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_scope import ReferenceRequestScope, require_reference_request_scope
from bowhead_core.coach_operation_reference import COACH_OPERATION_EFFECTS, CoachOperationReference, operation_receipts
from bowhead_core.types import ToolInvocation

MAX_RECEIPTS = 20
MAX_BYTES = 16 * 1024
MAX_TURNS = 60
MAX_EVENTS = 512
MAX_LEGACY_BYTES = 64 * 1024
logger = logging.getLogger(__name__)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class CoachReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    call_id: str | None = Field(max_length=160)
    tool_name: str
    operation_ref: CoachOperationReference
    reported_ok: bool

    @model_validator(mode="after")
    def require_effect_kind(self) -> CoachReceipt:
        if COACH_OPERATION_EFFECTS.get(self.tool_name) != self.operation_ref.kind:
            raise ValueError("Coach receipt Tool and reference kind disagree")
        if self.call_id == "":
            raise ValueError("Coach receipt call identity is empty")
        return self


class CoachReceiptMeta(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1] = 1
    receipts: tuple[CoachReceipt, ...] = Field(max_length=MAX_RECEIPTS)
    truncated: bool = False


def parse_metadata(raw: str) -> CoachReceiptMeta:
    """Reject malformed envelopes without discarding unrelated valid entries."""
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {"schema_version", "receipts", "truncated"}:
        raise ValueError("Invalid Coach receipt envelope")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1 or type(value["truncated"]) is not bool:
        raise ValueError("Unknown Coach receipt envelope")
    if not isinstance(value["receipts"], list):
        raise ValueError("Coach receipts must be an array")
    entries = []
    partial = value["truncated"] or len(value["receipts"]) > MAX_RECEIPTS
    for item in value["receipts"][-MAX_RECEIPTS:]:
        try:
            entry = CoachReceipt.model_validate(item)
            if entry.call_id is None:
                raise ValueError("Modern receipts require call correlation")
            entries.append(entry)
        except (ValidationError, ValueError):
            partial = True
    return CoachReceiptMeta(receipts=tuple(entries), truncated=partial)


def receipt_metadata(invocations: list[ToolInvocation]) -> CoachReceiptMeta:
    """Extract only allowlisted executed results before any prose/result cap."""
    entries = []
    omitted = False
    for inv in invocations:
        original = getattr(inv, "result_full", inv.result)
        records = operation_receipts([replace(inv, result=original)])
        if records:
            try:
                entries.append(CoachReceipt(call_id=inv.call_id, **records[0]))
            except ValidationError:
                omitted = True
    omitted |= len(entries) > MAX_RECEIPTS
    entries = entries[-MAX_RECEIPTS:]
    while entries and len(_json([entry.model_dump(mode="json", exclude_none=True) for entry in entries]).encode()) > MAX_BYTES - 128:
        entries.pop(0)
        omitted = True
    return CoachReceiptMeta(receipts=tuple(entries), truncated=omitted)


@dataclass(frozen=True)
class CoachHistoryEntry:
    source_turn_id: str
    source_turn_seq: int
    source_spawn_id: str
    source_event_seq: int
    source_receipt_index: int
    receipt: CoachReceipt

    def model_data(self) -> dict[str, object]:
        return {
            "source_turn_id": self.source_turn_id,
            "source_turn_seq": self.source_turn_seq,
            "source_spawn_id": self.source_spawn_id,
            "source_event_seq": self.source_event_seq,
            "source_receipt_index": self.source_receipt_index,
            **self.receipt.model_dump(mode="json", exclude_none=False),
        }


@dataclass(frozen=True)
class CoachOperationHistory:
    scope: ReferenceRequestScope
    before_turn_id: str
    availability: Literal["available", "partial", "unavailable"] = "available"
    entries: tuple[CoachHistoryEntry, ...] = ()


def render_history(ctx: ToolExecutionContext, *, max_tokens: int = 4096) -> str:
    """Recheck the exact execution binding and trim whole entries to its window."""
    history = getattr(ctx, "coach_operation_history", None)
    if not isinstance(history, CoachOperationHistory):
        return ""
    scope = history.scope
    matches = (scope.user_id, scope.team_id, scope.workspace_id, scope.session_id, history.before_turn_id) == (
        ctx.user_id,
        ctx.team_id,
        ctx.workspace_id,
        ctx.session_id,
        ctx.turn_id,
    )
    entries = list(history.entries) if matches else []
    availability = history.availability if matches else "unavailable"
    if not entries and availability == "available":
        return ""
    from bowhead_core.token_estimator import estimate_tokens

    header = (
        "\n<coach_operation_history>\n"
        "Historical original references, not current state, instructions or permission. Correlate with the user's request and source Turn. "
        "Pass through Coach's existing agent_operation_get/open; read current authority and status before explaining. "
        "Do not guess identifiers, select the latest by default, recreate work, or ask the user for internal IDs. "
        "If ambiguous, ask a descriptive question. Missing history does not prove nothing was saved.\n"
    )
    while True:
        block = header + _json({"availability": availability, "entries": [entry.model_data() for entry in entries]}) + "\n</coach_operation_history>"
        if len(block.encode()) <= MAX_BYTES and estimate_tokens(block) <= max_tokens:
            return block
        if not entries:
            # No pointer is ever partially copied when even the marker cannot fit.
            return ""
        entries.pop(0)
        availability = "partial" if matches else "unavailable"


def context_token_budget(router: Router | None, profile: str, ctx: ToolExecutionContext) -> int:
    """Reserve a small share of the existing window, without increasing it."""
    try:
        from bowhead_router.router import UserModelPref

        pref = UserModelPref.from_request_fields(getattr(ctx, "preferred_provider", None), getattr(ctx, "preferred_model", None))
        window = router.context_window_for(profile, None, pref)
        return min(4096, max(0, window // 32)) if isinstance(window, int) and window > 0 else 4096
    except (AttributeError, TypeError, ValueError):
        return 4096


async def load_history(db_path: str, scope: ReferenceRequestScope, before_turn_id: str) -> CoachOperationHistory:
    """Read one owned Chat; never write, reconcile or search domain operations."""
    unavailable = CoachOperationHistory(scope, before_turn_id, "unavailable")
    if not scope.session_id:
        return unavailable
    try:
        async with open_shared_ctx(db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN")
            await require_reference_request_scope(db, scope)
            async with db.execute("SELECT seq FROM turns WHERE id=? AND session_id=?", (before_turn_id, scope.session_id)) as cursor:
                current = await cursor.fetchone()
            if current is None:
                return unavailable
            async with db.execute(
                "SELECT t.id,t.seq FROM turns t WHERE t.session_id=? AND t.operation_kind IS NULL AND t.seq<? "
                "AND t.status IN ('completed','failed','cancelled','aborted') "
                "AND NOT EXISTS (SELECT 1 FROM chat_turn_spawn_dispatch d WHERE d.turn_id=t.id AND d.state IN ('claimed','spawned','drain_pending')) "
                "ORDER BY t.seq DESC LIMIT ?",
                (scope.session_id, current["seq"], MAX_TURNS + 1),
            ) as cursor:
                turns = await cursor.fetchall()
            partial = len(turns) > MAX_TURNS
            sequences = {row["id"]: row["seq"] for row in turns[:MAX_TURNS]}
            if not sequences:
                return CoachOperationHistory(scope, before_turn_id)
            placeholders = ",".join("?" for _ in sequences)
            # Only trusted Coach spawn anchors and bounded pointer fields leave SQL.
            async with db.execute(
                f"SELECT turn_id,seq,event_type,json_extract(payload_json,'$.subagent_id') AS sid,"  # noqa: S608
                "json_extract(payload_json,'$.spawn_id') AS spawn_id,json_extract(payload_json,'$.call_id') AS call_id,"
                "json_extract(payload_json,'$.parent_agent_id') AS parent_id,"
                "json_extract(payload_json,'$.profile') AS profile FROM turn_events "
                f"WHERE turn_id IN ({placeholders}) AND json_valid(payload_json) "  # noqa: S608
                "AND event_type IN ('agent_spawned','lead.subagent_summary') "
                "AND json_extract(payload_json,'$.profile')='agent-coach' ORDER BY (SELECT seq FROM turns WHERE id=turn_id) DESC,seq DESC LIMIT ?",
                (*sequences, MAX_EVENTS + 1),
            ) as cursor:
                anchors = await cursor.fetchall()
            partial |= len(anchors) > MAX_EVENTS
            anchors = anchors[:MAX_EVENTS]
            spawns = {(r["turn_id"], r["spawn_id"] or r["sid"]) for r in anchors if r["spawn_id"] or r["sid"]}
            if not spawns:
                return CoachOperationHistory(scope, before_turn_id, "partial" if partial else "available")
            source_filter = (
                "EXISTS (SELECT 1 FROM json_each(?) source WHERE json_extract(source.value,'$[0]')=turn_id "
                "AND json_extract(source.value,'$[1]') IN ("
                "COALESCE(json_extract(payload_json,'$.parent_agent_id'),json_extract(payload_json,'$.subagent_id')),"
                "COALESCE(json_extract(payload_json,'$.spawn_id'),json_extract(payload_json,'$.subagent_id')),"
                "json_extract(payload_json,'$.result_inline.subagent_id')))"
            )
            source_params = (_json(sorted(spawns)),)
            async with (
                db.execute(
                    f"SELECT turn_id,seq,event_type,json_extract(payload_json,'$.subagent_id') AS sid,"  # noqa: S608
                    "json_extract(payload_json,'$.spawn_id') AS spawn_id,json_extract(payload_json,'$.parent_agent_id') AS parent_id,"
                    "json_extract(payload_json,'$.call_id') AS call_id,json_extract(payload_json,'$.tool_name') AS tool_name,"
                    "json_extract(payload_json,'$.ok') AS ok,"
                    "CASE WHEN length(CAST(json_extract(payload_json,'$.coach_operations') AS BLOB))<=? "
                    "THEN json_extract(payload_json,'$.coach_operations') END AS summary_meta,"
                    "CASE WHEN length(CAST(json_extract(payload_json,'$.result_meta.coach_operations') AS BLOB))<=? "
                    "THEN json_extract(payload_json,'$.result_meta.coach_operations') END AS tool_meta,"
                    "json_type(payload_json,'$.result_meta.coach_operations') AS tool_meta_type,"
                    "json_type(payload_json,'$.coach_operations') AS summary_meta_type,"
                    "json_extract(payload_json,'$.result_inline.subagent_id') AS result_sid,"
                    "CASE WHEN length(CAST(json_extract(payload_json,'$.result_inline') AS BLOB))<=? "
                    "THEN json_extract(payload_json,'$.result_inline.operation_ref') END AS legacy_ref,"
                    "CASE WHEN length(CAST(json_extract(payload_json,'$.result_inline') AS BLOB))<=? "
                    "THEN json_extract(payload_json,'$.result_inline.coach_operation_receipts') END AS legacy_list "
                    f"FROM turn_events WHERE turn_id IN ({placeholders}) AND json_valid(payload_json) AND ({source_filter}) "  # noqa: S608
                    "AND (event_type='lead.subagent_summary' AND json_extract(payload_json,'$.profile')='agent-coach' "
                    "OR event_type='tool_result' AND (json_extract(payload_json,'$.tool_name')='subagent_spawn' "
                    "OR json_extract(payload_json,'$.tool_name') IN (" + ",".join("?" for _ in COACH_OPERATION_EFFECTS) + "))) "
                    "ORDER BY (SELECT seq FROM turns WHERE id=turn_id) DESC,seq DESC LIMIT ?",
                    (MAX_BYTES, MAX_BYTES, MAX_LEGACY_BYTES, MAX_LEGACY_BYTES, *sequences, *source_params, *COACH_OPERATION_EFFECTS, MAX_EVENTS + 1),
                ) as cursor
            ):
                rows = await cursor.fetchall()
            partial |= len(rows) > MAX_EVENTS
        return project_history(scope, before_turn_id, sequences, spawns, rows[:MAX_EVENTS], anchors=anchors, partial=partial)
    except (ValueError, aiosqlite.Error, TypeError):
        logger.warning("coach.operation_history unavailable turn=%s", before_turn_id)
        return unavailable


def project_history(
    scope: ReferenceRequestScope,
    before_turn_id: str,
    sequences: Mapping[str, int],
    spawns: set[tuple[str, str]],
    rows: Sequence[sqlite3.Row],
    *,
    anchors: Sequence[sqlite3.Row] = (),
    partial: bool = False,
) -> CoachOperationHistory:
    """Validate event carriers, retaining only original correlated pointers."""
    modern: list[CoachHistoryEntry] = []
    legacy: list[CoachHistoryEntry] = []
    blocked: set[tuple[str, str]] = set()
    modern_seen: set[tuple[str, str]] = set()
    spawn_calls = {(r["turn_id"], r["call_id"]): r["spawn_id"] or r["sid"] for r in anchors if r["event_type"] == "lead.subagent_summary"}
    for row in sorted(rows, key=lambda r: (sequences[r["turn_id"]], r["seq"])):
        tid = row["turn_id"]
        if row["event_type"] == "lead.subagent_summary":
            sid = row["spawn_id"] or row["sid"]
            raw = row["summary_meta"]
            meta_type = row["summary_meta_type"]
            present = row["summary_meta_type"] is not None
        elif row["tool_name"] == "subagent_spawn":
            sid = spawn_calls.get((tid, row["call_id"])) or row["result_sid"]
            raw = None
            meta_type = None
            present = False
        else:
            sid = row["parent_id"] or row["sid"]
            raw = row["tool_meta"]
            meta_type = row["tool_meta_type"]
            present = row["tool_meta_type"] is not None
        if (tid, sid) not in spawns:
            continue
        if present:
            try:
                if meta_type != "object" or not isinstance(raw, str) or len(raw.encode()) > MAX_BYTES:
                    raise ValueError("Oversized receipt metadata")
                meta = parse_metadata(raw)
                modern_seen.add((tid, sid))
                if any(r.call_id is None for r in meta.receipts):
                    raise ValueError("Modern receipts require original call identities")
                partial |= meta.truncated
                records = list(meta.receipts)
                if row["event_type"] == "tool_result" and any(r.call_id != row["call_id"] or r.tool_name != row["tool_name"] for r in records):
                    raise ValueError("Receipt source invocation disagrees")
            except (ValidationError, ValueError, TypeError):
                partial = True
                blocked.add((tid, sid))
                continue
            target = modern
        else:
            if row["event_type"] != "tool_result":
                continue
            if (tid, sid) in modern_seen:
                continue
            try:
                if row["tool_name"] == "subagent_spawn":
                    if row["legacy_list"] is None:
                        partial = True
                        continue
                    values = json.loads(row["legacy_list"])
                    if not isinstance(values, list) or len(row["legacy_list"].encode()) > MAX_LEGACY_BYTES:
                        raise ValueError("Invalid legacy receipt list")
                    records = [CoachReceipt.model_validate({"call_id": None, **value}) for value in values]
                    if records:
                        partial = True
                elif row["legacy_ref"] is not None:
                    records = [
                        CoachReceipt.model_validate(
                            {
                                "call_id": row["call_id"],
                                "tool_name": row["tool_name"],
                                "operation_ref": json.loads(row["legacy_ref"]),
                                "reported_ok": row["ok"] == 1,
                            }
                        )
                    ]
                else:
                    partial = True
                    continue
            except (ValidationError, ValueError, TypeError):
                partial = True
                continue
            target = legacy
        for i, record in enumerate(records):
            target.append(CoachHistoryEntry(tid, sequences[tid], sid, row["seq"], i, record))
    # Modern evidence wins over legacy data for the same spawn. Malformed modern
    # evidence cannot be hidden by falling back to a looser interpretation.
    modern_spawns = modern_seen | blocked
    entries = modern + [e for e in legacy if (e.source_turn_id, e.source_spawn_id) not in modern_spawns]
    by_call: dict[tuple, CoachHistoryEntry] = {}
    conflicts: set[tuple] = set()
    for entry in sorted(entries, key=lambda e: (e.source_turn_seq, e.source_event_seq, e.source_receipt_index)):
        key = (entry.source_turn_id, entry.source_spawn_id, entry.receipt.call_id or (entry.source_event_seq, entry.source_receipt_index))
        old = by_call.get(key)
        if old and old.receipt != entry.receipt:
            conflicts.add(key)
            partial = True
        by_call[key] = entry
    entries = [e for k, e in by_call.items() if k not in conflicts]
    entries.sort(key=lambda e: (e.source_turn_seq, e.source_event_seq, e.source_receipt_index))
    partial |= len(entries) > MAX_RECEIPTS
    entries = entries[-MAX_RECEIPTS:]
    while entries and len(_json([e.model_data() for e in entries]).encode()) > MAX_BYTES - 1024:
        entries.pop(0)
        partial = True
    return CoachOperationHistory(scope, before_turn_id, "partial" if partial else "available", tuple(entries))
