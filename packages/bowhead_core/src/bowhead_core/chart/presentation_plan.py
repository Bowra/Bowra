"""Bounded native presentation plans and literal Artifact evidence.

Each admitted field needs native rendering, source projection and static export.
Same-result linked focus, ordered time windows, shared numeric scales and bounded
guided reading are admitted alongside literal notes. Other interaction fields
remain rejected until their native, projection and export consumers exist.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from bowhead_core.chart.expression import structured
from bowhead_core.chart.guided_reading import focus_plan, has_focus, normalize_guide_declaration, validate_guides
from bowhead_core.chart.types import ChartColumn

MAX_PLAN_BYTES = 20_000
_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_VIEW_FIELDS = {"annotations", "table_trends", "analytical_views"}
_FOCUS_FIELDS = {"view_bindings", "interactions", "bindings"}


class ChartPlanError(ValueError):
    def __init__(self, code: str, path: str, message: str, *, corrective_ids: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.corrective_ids = list(dict.fromkeys(corrective_ids or []))[:5]


def _fail(code: str, path: str, message: str) -> None:
    raise ChartPlanError(code, "/presentation_plan" + path, message)


def _budget(value: Any) -> None:
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (ValueError, TypeError, RecursionError) as exc:
        _fail("plan_shape", "", f"The plan must be finite JSON: {type(exc).__name__}")
    if size > MAX_PLAN_BYTES:
        _fail("expression_too_large", "", "The presentation plan exceeds 20000 bytes")


def parse_plan_input(value: Any, *, root_fields: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            input_size = len(value.encode("utf-8"))
        except UnicodeError:
            _fail("plan_shape", "", "The presentation plan must contain valid Unicode")
        if input_size > MAX_PLAN_BYTES:
            _fail("expression_too_large", "", "The presentation plan exceeds 20000 bytes")
        try:
            value = structured(value, "presentation_plan")
        except (ValueError, RecursionError):
            _fail("plan_shape", "", "Use one JSON object for the presentation plan")
    _budget(value)
    if not isinstance(value, dict):
        _fail("plan_shape", "", "Use one JSON object for the presentation plan")
    if type(value.get("version")) is not int or value["version"] not in (1, 2, 3):
        _fail("plan_version", "/version", "Use presentation plan version 1, 2 or 3")
    if root_fields:
        if set(root_fields) - {"evidence_notes", "guide_steps"}:
            _fail("plan_shape", "", "Only unambiguous literal notes and reading steps can be relocated")
        for key, declaration in root_fields.items():
            if value.get(key) and value[key] != declaration:
                _fail("plan_shape", "/" + key, "Root and nested declarations must not conflict")
        value = {**value, **root_fields}
        _budget(value)
    if set(value) - {"version", "evidence_notes", "view_bindings", "interactions", "scale_groups", "guide_steps", *_VIEW_FIELDS}:
        _fail("plan_shape", "", "The plan contains unsupported or server-owned fields")
    if "annotations" in value:
        from bowhead_core.chart.annotations import validate_annotation_input

        validate_annotation_input(value["annotations"], version=value["version"])
    if "table_trends" in value:
        from bowhead_core.chart.contextual_reading import validate_trend_input

        if value["version"] < 2:
            _fail("plan_version", "/table_trends", "Table trends require presentation plan version 2")
        validate_trend_input(value["table_trends"], allow_omitted_binding=True)
    if "analytical_views" in value:
        from bowhead_core.chart.analytical import validate_analytical_input

        if value["version"] != 3:
            _fail("plan_version", "/analytical_views", "Analytical views require version 3")
        validate_analytical_input(value["analytical_views"])
    scales = "scale_groups" in value
    if scales:
        from bowhead_core.chart.shared_scales import validate_scale_input

        validate_scale_input(value["scale_groups"])
    value = normalize_guide_declaration(value)
    _budget(value)
    validate_guides(value)
    focus = has_focus(value)
    if focus:
        from bowhead_core.chart.linked_focus import validate_focus_input

        derived = validate_focus_input(focus_plan(value), derive_bindings=True, defer_equivalence=True, allow_omitted_binding=True)
        if "view_bindings" not in value and derived:
            value = {**value, "view_bindings": derived}
    notes = value.get("evidence_notes", [])
    if (
        not isinstance(notes, list)
        or len(notes) > 3
        or (
            not focus
            and not scales
            and not notes
            and "guide_steps" not in value
            and "annotations" not in value
            and "table_trends" not in value
            and "analytical_views" not in value
        )
    ):
        _fail("plan_shape", "/evidence_notes", "Provide literal evidence notes or an eligible linked focus plan")
    seen: set[str] = set()
    for i, note in enumerate(notes):
        path = f"/evidence_notes/{i}"
        if not isinstance(note, dict) or set(note) - {"id", "measure_keys", "ref_id", "excerpt", "locator"}:
            _fail("plan_shape", path, "Notes support only id, measure_keys, ref_id, excerpt and optional locator")
        nid = note.get("id")
        if not isinstance(nid, str) or not _ID.fullmatch(nid) or nid in seen:
            _fail("plan_shape", path + "/id", "Note IDs must be unique lowercase identifiers")
        seen.add(nid)
        keys = note.get("measure_keys")
        if (
            not isinstance(keys, list)
            or not 1 <= len(keys) <= 12
            or any(not isinstance(key, str) or not key for key in keys)
            or len(set(keys)) != len(keys)
        ):
            _fail("plan_shape", path + "/measure_keys", "Use 1 to 12 distinct exact measure keys")
        if not isinstance(note.get("ref_id"), str) or not note["ref_id"]:
            _fail("plan_shape", path + "/ref_id", "Choose an existing authorized Artifact reference")
        if not isinstance(note.get("excerpt"), str) or not note["excerpt"].strip() or len(note["excerpt"]) > 300:
            _fail("plan_shape", path + "/excerpt", "Copy a literal excerpt of at most 300 characters")
        if "locator" in note and (not isinstance(note["locator"], str) or not note["locator"].strip() or len(note["locator"]) > 160):
            _fail("plan_shape", path + "/locator", "Use a literal location from the representation, at most 160 characters")
    from bowhead_core.chart.contextual_reading import validate_annotation_evidence

    validate_annotation_evidence(value)
    return value


def evidence_notes_schema() -> dict[str, Any]:
    from bowhead_core.chart.analytical import analytical_schema

    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["version"],
        "description": "Optional native notes, eligible scale_groups, linked_focus, time_window or guided_steps. Copy exact Artifact notes; never invent methodology. For interactions and Table trends, omit binding_id when the source and target views determine one eligible grain; the host resolves it. Omit view_bindings entirely. Supply a current chart_catalog ID only to disambiguate an actual corrective error. Declare at least two views with unique original observations for focus/windows. A time window uses view_ids, not from_view/to_views, and has no model-authored bounds. Scale groups share the exact numeric axis of compatible same-kind views; the host derives domains. For guided reading, supply 1 to 4 guide_steps with existing view_id and a short title; the host derives the guided_steps preset, so no redundant interaction declaration is needed. Guides have no binding_id or scene overrides. Ordinary charts need no plan. Prepared frame_player targets complete scatter/Table views with one entity key and at most 120 time frames; omit static_frame to use the latest supplied frame. Observed point/time-interval annotations use exact keys and measures; Version 2 additionally admits explicit note_ids, source-supplied event/coverage roles and table_trends. Never invent thresholds, event dates, coverage or causal explanations.",
        "properties": {
            "version": {"type": "integer", "enum": [1, 2, 3]},
            "analytical_views": analytical_schema(),
            "annotations": {
                "type": "array",
                "minItems": 1,
                "maxItems": 6,
                "description": "Observed points or supplied time intervals only. Use an offered row/entity candidate for one original point tuple, or a time candidate with exact existing lower/upper keys. Target a native single-dimension view and its exact numeric measure. No model-authored values, thresholds, event dates or causal claims. Labels are optional short observational captions. In version 2, note_ids cite exact same-root notes; a cited label copies source text. Event/coverage roles require the exact structured source statement described in role.",
                "items": {
                    "oneOf": [
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["id", "view_id", "binding_id", "kind", "measure_key", "tuple"],
                            "properties": {
                                "id": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,31}$"},
                                "view_id": {"type": "string"},
                                "binding_id": {"type": "string"},
                                "kind": {"enum": ["point"]},
                                "measure_key": {"type": "string"},
                                "tuple": {"type": "array", "minItems": 1, "maxItems": 1, "items": {"type": ["string", "number", "null"]}},
                                "label": {"type": "string", "minLength": 1, "maxLength": 80},
                                "note_ids": {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": 3,
                                    "uniqueItems": True,
                                    "items": {"type": "string"},
                                    "description": "Version 2 only: exact same-root evidence note IDs applying to this measure. A cited label must copy literal source text.",
                                },
                                "role": {
                                    "enum": ["observation", "event", "coverage"],
                                    "description": "Version 2 only. Event/coverage requires one literal Artifact JSON excerpt with role, time_key, measure_keys, text and at (event) or lower/upper (coverage); annotation bounds and label must match exactly. Never infer causality or missing periods.",
                                },
                            },
                        },
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["id", "view_id", "binding_id", "kind", "measure_key", "lower", "upper"],
                            "properties": {
                                "id": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,31}$"},
                                "view_id": {"type": "string"},
                                "binding_id": {"type": "string"},
                                "kind": {"enum": ["interval"]},
                                "measure_key": {"type": "string"},
                                "lower": {"type": ["string", "number"]},
                                "upper": {"type": ["string", "number"]},
                                "label": {"type": "string", "minLength": 1, "maxLength": 80},
                                "note_ids": {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": 3,
                                    "uniqueItems": True,
                                    "items": {"type": "string"},
                                    "description": "Version 2 only: exact same-root evidence note IDs applying to this measure. A cited label must copy literal source text.",
                                },
                                "role": {
                                    "enum": ["observation", "event", "coverage"],
                                    "description": "Version 2 only. Event/coverage requires one literal Artifact JSON excerpt with role, time_key, measure_keys, text and at (event) or lower/upper (coverage); annotation bounds and label must match exactly. Never infer causality or missing periods.",
                                },
                            },
                        },
                    ]
                },
            },
            "table_trends": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "description": "Version 2: an entity per Table row with original first/last-period values, difference and observed trend. Specify view_id and the original numeric measure_key. Omit binding_id for the unique eligible entity/time frame. Keep the original long CSV; never pivot, aggregate or precompute a summary just to add trends. The host derives values from at most 120 existing periods/1200 complete unique observations; no aggregates, interpolation or same-view frame playback.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["view_id", "measure_key"],
                    "properties": {"view_id": {"type": "string"}, "binding_id": {"type": "string"}, "measure_key": {"type": "string"}},
                },
            },
            "scale_groups": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "description": "Optional shared numeric axes in 2 to 6 views. Use the same exact measure keys, units, percentage shape, native kind and complete prepared scope. The host derives fixed bounds; never author min/max. Native wide line/bar/hbar/combo/dual_axis/scatter/stacked_bar/area_stacked are supported; implicit multi-dimension pivots and percentage stacks are not.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["view_ids", "axis"],
                    "properties": {
                        "view_ids": {"type": "array", "minItems": 2, "maxItems": 6, "uniqueItems": True, "items": {"type": "string"}},
                        "axis": {"type": "string", "enum": ["x", "y", "y1"]},
                    },
                },
            },
            "interactions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": {
                    "oneOf": [
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["preset", "from_view", "to_views"],
                            "properties": {
                                "preset": {"type": "string", "enum": ["linked_focus"]},
                                "from_view": {"type": "string"},
                                "to_views": {"type": "array", "minItems": 1, "maxItems": 5, "uniqueItems": True, "items": {"type": "string"}},
                                "binding_id": {"type": "string"},
                            },
                        },
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["preset", "view_ids"],
                            "properties": {
                                "preset": {"type": "string", "enum": ["time_window"]},
                                "view_ids": {"type": "array", "minItems": 2, "maxItems": 6, "uniqueItems": True, "items": {"type": "string"}},
                                "binding_id": {"type": "string"},
                            },
                        },
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["preset", "view_ids"],
                            "properties": {
                                "preset": {"type": "string", "enum": ["frame_player"]},
                                "view_ids": {"type": "array", "minItems": 1, "maxItems": 6, "uniqueItems": True, "items": {"type": "string"}},
                                "binding_id": {"type": "string"},
                                "static_frame": {
                                    "type": ["string", "number"],
                                    "description": "Optional exact prepared key. Omit for the latest supplied frame; never invent dates.",
                                },
                            },
                        },
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["preset"],
                            "properties": {"preset": {"type": "string", "enum": ["guided_steps"]}},
                        },
                    ]
                },
            },
            "guide_steps": {
                "type": "array",
                "description": "Usually supply only this ordered list. The host adds its guided_steps declaration if omitted; do not repeat binding IDs, scenes or controller state.",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["title", "view_id"],
                    "properties": {
                        "title": {"type": "string", "minLength": 1, "maxLength": 80},
                        "view_id": {"type": "string"},
                        "annotation_id": {
                            "type": "string",
                            "description": "Optional exact ID from this plan's observed annotations in the same view.",
                        },
                    },
                },
            },
            "evidence_notes": {
                "type": "array",
                "description": "These notes belong inside presentation_plan.evidence_notes, never at the add_chart root.",
                "minItems": 1,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "measure_keys", "ref_id", "excerpt"],
                    "properties": {
                        "id": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,31}$"},
                        "measure_keys": {"type": "array", "minItems": 1, "maxItems": 12, "uniqueItems": True, "items": {"type": "string"}},
                        "ref_id": {
                            "type": "string",
                            "description": "An authorized data/Artifact ref ID from available_refs; a real ID must also be registered there.",
                        },
                        "excerpt": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 300,
                            "description": "Exact text from the selected Artifact representation. Catalog scope, prompts, chart titles and model explanations are not Artifact excerpts.",
                        },
                        "locator": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 160,
                            "description": "Optional literal heading/location present in the same parsed representation.",
                        },
                    },
                },
            },
        },
    }


async def artifact_text_representation(artifact: Any, store: Any) -> tuple[str, str] | None:
    """Reuse the resource-bounded attachment parser; never accept truncated text."""
    from bowhead_core.attachments_injector import extract_attachment_bounded
    from bowhead_core.chat_reference_attachment_snapshot import MAX_LEAD_ATTACHMENT_FILE_BYTES

    if not isinstance(getattr(artifact, "size", None), int) or artifact.size > MAX_LEAD_ATTACHMENT_FILE_BYTES:
        return None
    raw = await store.backend.read_bounded(artifact.path, limit=MAX_LEAD_ATTACHMENT_FILE_BYTES)
    if len(raw) > MAX_LEAD_ATTACHMENT_FILE_BYTES:
        return None
    prepared = await extract_attachment_bounded(artifact, raw)
    if prepared.error or prepared.truncated or prepared.summary_only or not prepared.extracted_text:
        return None
    text = prepared.extracted_text
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _literal_json_excerpt(excerpt: str, source: str) -> str:
    """Recover formatting only, then retain the exact bounded source text."""
    literal = source.strip()
    if not 0 < len(literal) <= 300:
        return excerpt

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    try:
        submitted = json.loads(excerpt, object_pairs_hook=unique_object)
        observed = json.loads(literal, object_pairs_hook=unique_object)
        if isinstance(submitted, dict) and isinstance(observed, dict):
            if json.dumps(submitted, sort_keys=True, allow_nan=False) == json.dumps(observed, sort_keys=True, allow_nan=False):
                return literal
    except (ValueError, TypeError, RecursionError):
        pass
    return excerpt


async def verify_plan_notes(plan: dict[str, Any], columns: Sequence[ChartColumn], ctx: Any) -> list[dict[str, Any]]:
    from bowhead_core.agents._lead_context import run_ref_allowlist

    numeric = {column.key for column in columns if column.type == "numeric"}
    allowlist = run_ref_allowlist(ctx)
    registered = allowlist.real_ids_of_kinds({"data"}) if allowlist is not None else set()
    verified: list[dict[str, Any]] = []
    representations: dict[str, tuple[Any, tuple[str, str] | None]] = {}
    for i, note in enumerate(plan.get("evidence_notes", [])):
        path = f"/evidence_notes/{i}"
        if not set(note["measure_keys"]) <= numeric:
            _fail("binding_ineligible", path + "/measure_keys", "Evidence notes must target exact numeric columns of the admitted dataset")
        aid = allowlist.resolve(note["ref_id"]) if allowlist is not None else None
        if aid not in registered or not isinstance(aid, str) or not aid.startswith("art_"):
            _fail("evidence_unverified", path + "/ref_id", "Choose an authorized Artifact already registered in available_refs")
        if aid not in representations:
            art = await ctx.artifact_store.get(aid, check_created_by=ctx.user_id)
            if art is None:
                _fail("evidence_unverified", path + "/ref_id", "The selected Artifact is unavailable")
            try:
                representation = await artifact_text_representation(art, ctx.artifact_store)
            except (OSError, ValueError):
                representation = None
            representations[aid] = (art, representation)
        art, representation = representations[aid]
        excerpt = note["excerpt"]
        if representation is not None and excerpt not in representation[0]:
            excerpt = _literal_json_excerpt(excerpt, representation[0])
        if representation is None or excerpt not in representation[0] or (note.get("locator") and note["locator"] not in representation[0]):
            _fail(
                "evidence_unverified", path, "Copy literal text and an optional literal heading from the current untruncated Artifact representation"
            )
        verified.append(
            {
                "id": note["id"],
                "measure_keys": list(note["measure_keys"]),
                "excerpt": excerpt,
                **({"locator": note["locator"]} if "locator" in note else {}),
                "source_ref": {"id": aid, "artifact_id": aid, "tool": art.source, "label": art.filename},
                "verified_content_digest": representation[1],
            }
        )
    return verified


def compile_notes_plan(
    spec: Mapping[str, Any],
    notes: list[dict[str, Any]],
    *,
    artifact_id: str,
    raw: bytes,
    columns: Sequence[ChartColumn],
    input_plan: Mapping[str, Any] | None = None,
    rows: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    semantics = [
        {
            "key": c.key,
            "type": c.type,
            "role": c.role,
            "format": c.format,
            "unit": c.unit,
            "percent_scale": c.percent_scale,
            "percent_shape_unknown": c.percent_shape_unknown,
            "name": c.source_header or c.header,
            "y_axis_id": c.y_axis_id,
        }
        for c in columns
    ]
    dependencies = note_text_dependencies(spec, [note["id"] for note in notes])
    plan = {
        "version": input_plan.get("version", 1) if input_plan else 1,
        "dataset": {
            "artifact_id": artifact_id,
            "content_digest": hashlib.sha256(raw).hexdigest(),
            "schema_digest": hashlib.sha256(json.dumps(semantics, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        },
        "evidence_notes": notes,
        "text_dependencies": dependencies,
    }
    if input_plan is not None and "interactions" in input_plan:
        from bowhead_core.chart.linked_focus import compile_focus

        validate_guides(input_plan, spec)
        if has_focus(input_plan):
            compiled_focus = compile_focus(focus_plan(input_plan), spec, columns, rows, raw=raw)
            compiled_interactions = iter(compiled_focus["interactions"])
            compiled_focus["interactions"] = [
                item if item.get("preset") == "guided_steps" else next(compiled_interactions) for item in input_plan["interactions"]
            ]
            plan.update(compiled_focus)
        else:
            plan["interactions"] = input_plan["interactions"]
        if "guide_steps" in input_plan:
            plan["guide_steps"] = input_plan["guide_steps"]
            plan["text_dependencies"].update(
                {
                    f"/presentation_plan/guide_steps/{index}/title": {"note_ids": [note["id"] for note in notes], "dataset": True}
                    for index in range(len(input_plan["guide_steps"]))
                }
            )
    if input_plan is not None and "scale_groups" in input_plan:
        from bowhead_core.chart.shared_scales import compile_scale_groups

        plan["scale_groups"] = compile_scale_groups(input_plan["scale_groups"], spec)
    if input_plan is not None and "annotations" in input_plan:
        from bowhead_core.chart.annotations import compile_annotations

        plan["annotations"] = compile_annotations(input_plan["annotations"], spec, columns, rows, raw=raw, version=plan["version"])
        plan["text_dependencies"].update(
            {
                f"/presentation_plan/annotations/{index}/label": {"note_ids": [note["id"] for note in notes], "dataset": True}
                for index, item in enumerate(plan["annotations"])
                if "label" in item
            }
        )
    from bowhead_core.chart.contextual_reading import compile_trends, validate_annotation_evidence, validate_saved_trends

    if input_plan is not None and "table_trends" in input_plan:
        plan["table_trends"] = compile_trends(input_plan["table_trends"], spec, columns, rows, raw=raw)
    from bowhead_core.chart.analytical import compile_analytical_views, validate_saved_analytical

    if input_plan is not None and "analytical_views" in input_plan:
        plan["analytical_views"] = compile_analytical_views(input_plan["analytical_views"], spec, notes)
    validate_saved_analytical(plan, spec)
    validate_annotation_evidence(plan)
    validate_saved_trends(plan, spec)
    _budget(plan)
    return plan


def text_pointer_target(spec: Mapping[str, Any], parts: list[str]) -> Any:
    if parts[0] == "presentation_plan":
        return spec["presentation_plan"][parts[1]][int(parts[2])]
    return spec if len(parts) == 1 else spec["views"][int(parts[1])]["spec"]


def note_text_dependencies(spec: Mapping[str, Any], note_ids: list[str]) -> dict[str, Any]:
    paths = [
        prefix + "/" + key
        for prefix, chart in [("", spec), *((f"/views/{i}/spec", view["spec"]) for i, view in enumerate(spec.get("views", [])))]
        for key in ("title", "subtitle")
        if isinstance(chart.get(key), str)
    ]
    paths.extend(f"/presentation_plan/guide_steps/{i}/title" for i in range(len(spec.get("presentation_plan", {}).get("guide_steps", []))))
    paths.extend(
        f"/presentation_plan/annotations/{i}/label"
        for i, item in enumerate(spec.get("presentation_plan", {}).get("annotations", []))
        if "label" in item
    )
    return {pointer: {"note_ids": note_ids, "dataset": True} for pointer in paths}


def redact_note_text(spec: dict[str, Any], dependencies: Mapping[str, Any], *, unavailable: set[str], dataset_available: bool) -> None:
    for pointer, dependency in dependencies.items():
        if not (set(dependency["note_ids"]) & unavailable or (dependency["dataset"] and not dataset_available)):
            continue
        parts = pointer.lstrip("/").split("/")
        chart = text_pointer_target(spec, parts)
        if parts[-1] == "title":
            chart["title"] = ""
        else:
            chart.pop(parts[-1], None)


async def verify_saved_plan_evidence(sections: Sequence[Mapping[str, Any]], store: Any, access: Mapping[str, bool]) -> dict[tuple[str, str], bool]:
    """Verify only explicitly saved dependencies, once per authorized Artifact.

    Dataset hashing streams existing bytes off the event loop. Note text reuses
    the current bounded parser; digests cannot grant read access themselves.
    """
    requests: dict[str, dict[str, set[str]]] = {}
    literals: dict[tuple[str, str], list[str]] = {}
    for section in sections:
        spec = section.get("spec") if section.get("kind") == "chart" else None
        plan = spec.get("presentation_plan") if isinstance(spec, Mapping) else None
        if not isinstance(plan, Mapping):
            continue
        try:
            validate_stored_notes_plan(spec)
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        dataset = plan["dataset"]
        requests.setdefault(dataset["artifact_id"], {"bytes": set(), "text": set()})["bytes"].add(dataset["content_digest"])
        for note in plan["evidence_notes"]:
            aid = note["source_ref"]["artifact_id"]
            requests.setdefault(aid, {"bytes": set(), "text": set()})["text"].add(note["verified_content_digest"])
            literals.setdefault((aid, note["verified_content_digest"]), []).extend(
                [note["excerpt"], *([note["locator"]] if note.get("locator") else [])]
            )
    verified: dict[tuple[str, str], bool] = {}
    for aid, digests in requests.items():
        for digest in digests["bytes"] | digests["text"]:
            verified[(aid, digest)] = False
        if not access.get(aid, False) or store is None:
            continue
        try:
            artifact = await store.get(aid)
            if artifact is None:
                continue
            if digests["bytes"]:

                def hash_stream(stream: Any) -> str:
                    h = hashlib.sha256()
                    while chunk := stream.read(64 * 1024):
                        h.update(chunk)
                    return h.hexdigest()

                async with store.backend.read_stream(artifact.path) as stream:
                    digest = await asyncio.to_thread(hash_stream, stream)
                if digest in digests["bytes"]:
                    verified[(aid, digest)] = True
            if digests["text"]:
                representation = await artifact_text_representation(artifact, store)
                if (
                    representation is not None
                    and representation[1] in digests["text"]
                    and all(text in representation[0] for text in literals[(aid, representation[1])])
                ):
                    verified[(aid, representation[1])] = True
        except (OSError, ValueError, AttributeError):
            # A missing, stale or unverifiable dependency is a projected availability state.
            continue
    return verified


def validate_stored_notes_plan(spec: Mapping[str, Any]) -> None:
    plan = spec.get("presentation_plan")
    _budget(plan)
    if (
        not isinstance(plan, dict)
        or set(plan)
        - {
            "version",
            "dataset",
            "evidence_notes",
            "text_dependencies",
            "scale_groups",
            "guide_steps",
            "annotations",
            "table_trends",
            "analytical_views",
            *_FOCUS_FIELDS,
        }
        or not {"version", "dataset", "evidence_notes", "text_dependencies"} <= set(plan)
    ):
        _fail("plan_shape", "", "Stored evidence plans require the complete canonical envelope")
    if type(plan["version"]) is not int or plan["version"] not in (1, 2, 3):
        _fail("plan_version", "/version", "Use presentation plan version 1, 2 or 3")
    dataset = plan["dataset"]
    if (
        not isinstance(dataset, dict)
        or set(dataset) != {"artifact_id", "content_digest", "schema_digest"}
        or not isinstance(dataset["artifact_id"], str)
        or not dataset["artifact_id"].startswith("art_")
        or dataset["artifact_id"] != spec.get("data_artifact_id")
    ):
        _fail("plan_shape", "/dataset", "The sealed dataset must match the root chart")
    if any(not isinstance(dataset[key], str) or not _DIGEST.fullmatch(dataset[key]) for key in ("content_digest", "schema_digest")):
        _fail("plan_shape", "/dataset", "Dataset digests must be SHA-256 hex values")
    notes = plan["evidence_notes"]
    if (
        not isinstance(notes, list)
        or len(notes) > 3
        or (
            not notes
            and "interactions" not in plan
            and "scale_groups" not in plan
            and "annotations" not in plan
            and "table_trends" not in plan
            and "analytical_views" not in plan
        )
    ):
        _fail("plan_shape", "/evidence_notes", "Stored plans require verified notes or linked focus")
    validate_guides(plan, spec)
    from bowhead_core.chart.annotations import validate_saved_annotations

    validate_saved_annotations(plan, spec)
    from bowhead_core.chart.contextual_reading import validate_annotation_evidence, validate_saved_trends

    validate_annotation_evidence(plan)
    validate_saved_trends(plan, spec)
    from bowhead_core.chart.analytical import validate_saved_analytical

    validate_saved_analytical(plan, spec)
    if has_focus(plan):
        from bowhead_core.chart.linked_focus import validate_saved_focus

        validate_saved_focus(focus_plan(plan), spec)
    if "scale_groups" in plan:
        from bowhead_core.chart.shared_scales import validate_saved_scales

        validate_saved_scales(plan, spec)
    charts = [spec, *(view["spec"] for view in spec.get("views", []))]
    numeric = {column["key"] for chart in charts for column in chart.get("columns", []) if column.get("type") == "numeric"}
    ids: set[str] = set()
    for i, note in enumerate(notes):
        path = f"/evidence_notes/{i}"
        if not isinstance(note, dict) or set(note) - {"id", "measure_keys", "excerpt", "locator", "source_ref", "verified_content_digest"}:
            _fail("plan_shape", path, "Stored notes contain unsupported fields")
        source = note.get("source_ref")
        if (
            not isinstance(source, dict)
            or set(source) != {"id", "artifact_id", "tool", "label"}
            or not isinstance(source.get("artifact_id"), str)
            or not source["artifact_id"].startswith("art_")
            or source.get("id") != source["artifact_id"]
            or any(not isinstance(source.get(key), str) for key in ("tool", "label"))
        ):
            _fail("plan_shape", path + "/source_ref", "Stored notes require a canonical Artifact source")
        synthetic = {key: value for key, value in note.items() if key in {"id", "measure_keys", "excerpt", "locator"}}
        synthetic["ref_id"] = source["artifact_id"]
        parse_plan_input({"version": 1, "evidence_notes": [synthetic]})
        if note["id"] in ids or not set(note["measure_keys"]) <= numeric:
            _fail("binding_ineligible", path, "Stored note IDs must be distinct and target retained measures")
        ids.add(note["id"])
        digest = note.get("verified_content_digest")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            _fail("plan_shape", path, "Stored notes require a verified representation digest")
    expected = {
        prefix + "/" + key
        for prefix, chart in [("", spec), *((f"/views/{i}/spec", view["spec"]) for i, view in enumerate(spec.get("views", [])))]
        for key in ("title", "subtitle")
        if isinstance(chart.get(key), str)
    }
    expected |= {f"/presentation_plan/guide_steps/{index}/title" for index in range(len(plan.get("guide_steps", [])))}
    expected |= {f"/presentation_plan/annotations/{index}/label" for index, item in enumerate(plan.get("annotations", [])) if "label" in item}
    dependencies = plan["text_dependencies"]
    if not isinstance(dependencies, dict) or set(dependencies) != expected:
        _fail("plan_shape", "/text_dependencies", "Protect every authored chart title and subtitle")
    for dependency in dependencies.values():
        if (
            not isinstance(dependency, dict)
            or set(dependency) != {"note_ids", "dataset"}
            or dependency["dataset"] is not True
            or dependency["note_ids"] != [note["id"] for note in notes]
        ):
            _fail("plan_shape", "/text_dependencies", "Text dependencies must retain all selected notes and the dataset")


def validate_projected_notes_plan(plan: Any, *, static: bool = False) -> None:
    """Validate a server-projected plan without treating aliases as read grants."""
    _budget(plan)
    if (
        not isinstance(plan, dict)
        or set(plan)
        - {
            "version",
            "dataset",
            "evidence_notes",
            "text_dependencies",
            "scale_groups",
            "guide_steps",
            "annotations",
            "table_trends",
            "analytical_views",
            *_FOCUS_FIELDS,
        }
        or not {"version", "dataset", "evidence_notes", "text_dependencies"} <= set(plan)
        or type(plan["version"]) is not int
        or plan["version"] not in (1, 2, 3)
    ):
        _fail("plan_shape", "", "A complete supported projected evidence plan is required")
    dataset = plan["dataset"]
    if (
        not isinstance(dataset, dict)
        or set(dataset) - {"artifact_id", "content_digest", "schema_digest", "availability"}
        or not isinstance(dataset.get("artifact_id"), str)
        or dataset.get("availability") not in {"available", "unavailable"}
    ):
        _fail("plan_shape", "/dataset", "The projected dataset needs an availability state")
    if dataset["availability"] == "available":
        if any(not isinstance(dataset.get(key), str) or not _DIGEST.fullmatch(dataset[key]) for key in ("content_digest", "schema_digest")):
            _fail("plan_shape", "/dataset", "Available datasets require sealed digests")
    elif set(dataset) != {"artifact_id", "availability"}:
        _fail("plan_shape", "/dataset", "Unavailable datasets cannot retain sealed digests")
    notes = plan["evidence_notes"]
    if (
        not isinstance(notes, list)
        or len(notes) > 3
        or (
            not notes
            and "interactions" not in plan
            and "scale_groups" not in plan
            and "annotations" not in plan
            and "table_trends" not in plan
            and "analytical_views" not in plan
        )
    ):
        _fail("plan_shape", "/evidence_notes", "Projected plans require notes or linked focus")
    validate_guides(plan, projected=True)
    from bowhead_core.chart.annotations import validate_saved_annotations

    validate_saved_annotations(plan, projected=True)
    from bowhead_core.chart.contextual_reading import validate_annotation_evidence, validate_saved_trends

    validate_annotation_evidence(plan, projected=True)
    validate_saved_trends(plan, projected=True)
    from bowhead_core.chart.analytical import validate_saved_analytical

    validate_saved_analytical(plan, projected=True)
    if has_focus(plan):
        from bowhead_core.chart.linked_focus import validate_saved_focus

        validate_saved_focus(focus_plan(plan), projected=True)
    if "scale_groups" in plan:
        from bowhead_core.chart.shared_scales import validate_saved_scales

        validate_saved_scales(plan)
    ids: set[str] = set()
    for i, note in enumerate(notes):
        if not isinstance(note, dict) or set(note) - {
            "id",
            "measure_keys",
            "source_ref",
            "availability",
            "excerpt",
            "locator",
            "verified_content_digest",
        }:
            _fail("plan_shape", f"/evidence_notes/{i}", "Unexpected projected note fields")
        nid, keys, source = note.get("id"), note.get("measure_keys"), note.get("source_ref")
        if (
            not isinstance(nid, str)
            or not _ID.fullmatch(nid)
            or nid in ids
            or not isinstance(keys, list)
            or not 1 <= len(keys) <= 12
            or any(not isinstance(key, str) or not key for key in keys)
            or len(set(keys)) != len(keys)
        ):
            _fail("plan_shape", f"/evidence_notes/{i}", "Invalid projected note identity")
        ids.add(nid)
        source_key = "source_ref" if static else "id"
        if (
            not isinstance(source, dict)
            or not isinstance(source.get(source_key), str)
            or (static and not re.fullmatch(r"source-ordinal:[1-9][0-9]*", source[source_key]))
        ):
            _fail("plan_shape", f"/evidence_notes/{i}/source_ref", "A projected source reference is required")
        if note.get("availability") == "available":
            synthetic = {key: value for key, value in note.items() if key in {"id", "measure_keys", "excerpt", "locator"}}
            synthetic["ref_id"] = source[source_key]
            parse_plan_input({"version": 1, "evidence_notes": [synthetic]})
            if not isinstance(note.get("verified_content_digest"), str) or not _DIGEST.fullmatch(note["verified_content_digest"]):
                _fail("plan_shape", f"/evidence_notes/{i}", "Available notes require verified digests")
        elif note.get("availability") != "unavailable" or set(note) & {"excerpt", "locator", "verified_content_digest"}:
            _fail("plan_shape", f"/evidence_notes/{i}", "Unavailable notes cannot retain literal evidence")
    dependencies = plan["text_dependencies"]
    if not isinstance(dependencies, dict):
        _fail("plan_shape", "/text_dependencies", "Projected dependencies must be an object")
    for pointer, dependency in dependencies.items():
        if (
            not re.fullmatch(
                r"/(?:title|subtitle|views/[0-5]/spec/(?:title|subtitle)|presentation_plan/guide_steps/[0-3]/title|presentation_plan/annotations/[0-5]/label)",
                pointer,
            )
            or not isinstance(dependency, dict)
            or set(dependency) != {"note_ids", "dataset"}
            or dependency["dataset"] is not True
            or dependency["note_ids"] != [note["id"] for note in notes]
        ):
            _fail("plan_shape", "/text_dependencies", "Projected dependencies must preserve all selected notes")
