"""Bounded native result pointers; neither content storage nor access grants."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

MAX_ITEMS = 32
MAX_BYTES = 8192
logger = logging.getLogger(__name__)


def recovery_enabled() -> bool:
    return os.environ.get("FEATURE_CONTEXT_MATERIAL_RECOVERY", "1") == "1"


class MaterialOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["tool", "canvas_event"]
    turn_id: str = Field(min_length=1, max_length=128)
    producer_id: str | None = Field(default=None, max_length=128)
    call_id: str | None = Field(default=None, max_length=128)
    event_seq: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_identity(self):
        if self.kind == "tool" and (not self.call_id or self.event_seq is not None):
            raise ValueError("Tool origin requires its original call identity")
        if self.kind == "canvas_event" and (self.event_seq is None or self.call_id is not None or self.producer_id is not None):
            raise ValueError("Canvas origin requires its persisted event sequence")
        return self


class MaterialReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["canvas_section", "canvas", "artifact", "coach_operation"]
    locator: dict[str, Any]
    observed_version: dict[str, Any] = Field(default_factory=dict)
    relation: Literal["created", "updated", "read", "reused"]
    origin: MaterialOrigin

    @model_validator(mode="after")
    def validate_locator(self):
        expected = {"canvas_section": {"canvas_id", "section_id"}, "canvas": {"canvas_id"}, "artifact": {"artifact_id"}}
        if self.kind == "coach_operation":
            from bowhead_core.coach_operation_reference import OPERATION_REFERENCE

            if set(self.locator) != {"operation_ref"}:
                raise ValueError("Invalid operation locator")
            OPERATION_REFERENCE.validate_python(self.locator["operation_ref"])
        elif set(self.locator) != expected[self.kind] or any(
            not isinstance(value, str) or not value or len(value) > 160 for value in self.locator.values()
        ):
            raise ValueError("Invalid native locator")
        for name, value in self.observed_version.items():
            if name == "rev" and type(value) is int and value > 0:
                continue
            if name == "sha256" and isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value):
                continue
            raise ValueError("Invalid observed version")
        return self


class ReferenceCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    references: Literal["complete", "partial", "unknown"] = "complete"
    omitted_count: int | None = Field(default=0, ge=0)
    reasons: list[str] = Field(default_factory=list, max_length=8)


class MaterialReferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: Literal[1] = 1
    coverage: ReferenceCoverage = Field(default_factory=ReferenceCoverage)
    items: list[MaterialReference] = Field(default_factory=list, max_length=MAX_ITEMS)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_references(value: Any) -> MaterialReferences | None:
    try:
        if len(_json(value).encode()) > MAX_BYTES:
            return None
        return MaterialReferences.model_validate(value)
    except (ValidationError, TypeError, ValueError):
        return None


def bounded_references(items: Iterable[MaterialReference], *, incomplete: bool = False, reason: str = "budget") -> MaterialReferences:
    unique = {_json(item.model_dump(exclude_none=True)): item for item in items}
    selected = list(unique.values())[:MAX_ITEMS]
    while True:
        omitted = len(unique) - len(selected)
        result = MaterialReferences(
            items=selected,
            coverage=ReferenceCoverage(
                references="unknown" if incomplete else "partial" if omitted else "complete",
                omitted_count=None if incomplete else omitted,
                reasons=([reason] if incomplete or omitted else []),
            ),
        )
        if len(result.model_dump_json().encode()) <= MAX_BYTES or not selected:
            return result
        selected.pop()


def aggregate_references(invocations: Iterable[Any]) -> MaterialReferences | None:
    receipts = [inv.material_refs for inv in invocations if type(getattr(inv, "material_refs", None)) is MaterialReferences]
    if not receipts:
        return None
    return bounded_references(
        (item for receipt in receipts for item in receipt.items),
        incomplete=any(receipt.coverage.references != "complete" for receipt in receipts),
        reason="receipt_unavailable",
    )


async def capture_material_references(inv: Any, ctx: Any, *, turn_id: str, producer_id: str | None) -> None:
    """Read acknowledged native receipts before caps; never retry their effects."""
    if not recovery_enabled():
        return
    if inv.tool_name == "subagent_spawn":
        inv.material_refs = aggregate_references(inv.nested)
        return
    tool = inv.tool_name
    canvas_tools = {"write_report", "add_chart", "add_image", "edit_chart"}
    artifact_tools = {"bi_query", "python_exec", "skill_invoke"}
    from bowhead_core.coach_operation_reference import COACH_OPERATION_EFFECTS, operation_receipts

    if tool not in canvas_tools | artifact_tools | COACH_OPERATION_EFFECTS.keys():
        return
    result = inv.result
    if not isinstance(result, dict) or result.get("stub"):
        return
    items: list[MaterialReference] = []
    try:
        origin = MaterialOrigin(kind="tool", turn_id=turn_id, producer_id=producer_id, call_id=inv.call_id)
        if tool in canvas_tools and result.get("ok", not result.get("error")) and inv.error is None:
            canvas = await ctx.canvas_store.get_canvas_by_session(ctx.session_id) if ctx.canvas_store else None
            if canvas is None:
                raise ValueError("Native Canvas receipt unavailable")
            ids = list(dict.fromkeys(result.get("section_ids") or [result.get("section_id")]))
            known = {section.id for section in await ctx.canvas_store.list_sections(canvas.id, include_hidden=True)}
            for identity in ids:
                if not identity or identity not in known:
                    raise ValueError("Native section receipt unavailable")
                relation = "updated" if tool == "edit_chart" or identity in (result.get("updated") or []) else "created"
                if inv.arguments.get("section_id") == identity:
                    relation = "updated"
                if result.get("updated") is not None and identity not in result.get("updated", []) and identity not in result.get("appended", []):
                    relation = "reused"
                version = {"rev": result["rev"]} if identity == result.get("section_id") and type(result.get("rev")) is int else {}
                items.append(
                    MaterialReference(
                        kind="canvas_section",
                        locator={"canvas_id": canvas.id, "section_id": identity},
                        observed_version=version,
                        relation=relation,
                        origin=origin,
                    )
                )
        elif tool in artifact_tools:
            entries = result.get("results") if tool == "bi_query" and isinstance(result.get("results"), list) else [result]
            ids = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if tool == "bi_query":
                    ids.append(entry.get("artifact_id") or entry.get("_ref_artifact_id"))
                ids.extend(item.get("artifact_id") for item in entry.get("output_artifacts", []) if isinstance(item, dict))
            for identity in dict.fromkeys(ids):
                if not identity:
                    continue
                artifact = await ctx.artifact_store.get(identity, check_created_by=ctx.user_id, reject_null_owner=True)
                if artifact is None:
                    raise ValueError("Native Artifact receipt unavailable")
                items.append(MaterialReference(kind="artifact", locator={"artifact_id": identity}, relation="created", origin=origin))
        else:
            for receipt in operation_receipts([inv]):
                items.append(
                    MaterialReference(kind="coach_operation", locator={"operation_ref": receipt["operation_ref"]}, relation="reused", origin=origin)
                )
        if items:
            inv.material_refs = bounded_references(items)
    except Exception:
        logger.warning("material_reference_projection_unavailable tool=%s call=%s", tool, inv.call_id)
        inv.material_refs = bounded_references(items, incomplete=True, reason="receipt_unavailable")


def model_reference_view(receipt: MaterialReferences, *, content: str = "omitted", readers: Iterable[str] = ()) -> dict[str, Any]:
    """Describe this carrier only; historical hints never grant current Tools."""
    view = receipt.model_dump(exclude_none=True)
    view["content_in_result"] = content
    available = set(readers)
    if "read_report" in available and any(item.kind in {"canvas", "canvas_section"} for item in receipt.items):
        view["reader"] = {"tool": "read_report", "scope": "current_session", "snapshot": "current"}
    # Reserve space for receiver-only fields within the same metadata budget.
    while len(_json(view).encode()) > MAX_BYTES and view["items"]:
        view["items"].pop()
        view["coverage"] = {"references": "partial", "omitted_count": None, "reasons": ["budget"]}
    return view


RECOVERY_GUIDANCE = (
    "Material references identify saved results, not their omitted content or permission to read them. "
    "When the answer needs omitted details, use a currently available original reader. "
    "Preserve scope and observed version. Do not repeat completed operations to recover their output. "
    "An unavailable read does not establish that the material never existed."
)
