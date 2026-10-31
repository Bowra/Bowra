"""Exact private Canvas material and bounded model views, never source authority."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from bowhead_core.documents.request import DocumentRequestError

CONTRACT = "document-canvas-material-v1"
FILENAME = "canvas-material.json"
MAX_BYTES = 32 * 1024 * 1024
PAGE_SIZE = 12
FIELD_PAGE_SIZE = 32


def encode_material(*, canvas_id: str, session_id: str, projection_token: str, sections: Sequence[dict], datasets: Mapping[str, dict]) -> bytes:
    """Serialize already-authorized, projected sources without recomputing data.

    Dataset records contain base64 of exact pinned bytes. Saved view rows remain
    separate from linked complete files, including Top-N and table previews.
    The original document adapter supplies scope and source authorization.
    """
    value = {
        "contract": CONTRACT,
        "canvas_id": canvas_id,
        "session_id": session_id,
        "projection_token": projection_token,
        "sections": list(sections),
        "datasets": dict(datasets),
    }
    try:
        content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError) as error:
        raise DocumentRequestError("document_canvas_material_invalid") from error
    if len(content) > MAX_BYTES:
        raise DocumentRequestError("document_inputs_too_large")
    return content


def dataset_record(*, filename: str, mime: str, content: bytes) -> dict:
    if len(content) > MAX_BYTES:
        raise DocumentRequestError("document_inputs_too_large")
    return {
        "filename": filename,
        "mime": mime,
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "encoding": "base64",
        "content": base64.b64encode(content).decode("ascii"),
    }


def _preview(value: Any, *, depth: int = 0) -> Any:
    """A visibly labeled sample; the isolated script still receives every byte."""
    if isinstance(value, str):
        return value[:160]
    if isinstance(value, dict):
        return {key: _preview(item, depth=depth + 1) for key, item in list(value.items())[:12]} if depth < 4 else "[structured value]"
    if isinstance(value, list):
        return [_preview(item, depth=depth + 1) for item in value[:12]] if depth < 4 else "[structured value]"
    return value


def _views(section: dict) -> list[tuple[str | None, dict]]:
    spec = section.get("spec")
    if not isinstance(spec, dict):
        return []
    return [
        (None, spec),
        *((view["id"], view["spec"]) for view in spec.get("views", []) if isinstance(view, dict) and isinstance(view.get("spec"), dict)),
    ]


def _view_index(view_id: str | None, spec: dict, *, script_path: str) -> dict:
    return {
        "view_id": view_id,
        "script_path": script_path,
        "kind": spec.get("kind"),
        "title": _preview(spec.get("title")),
        "saved_rows": len(spec.get("rows") or []),
        "declared_row_count": spec.get("row_count"),
        "data_input_alias": spec.get("data_input_alias"),
        "top_n": spec.get("top_n"),
    }


def read_material(content: bytes, *, section_id: str | None = None, view_id: str | None = None, offset: int = 0) -> dict:
    """Read a bounded directory or field page; do not flatten child datasets."""
    if type(offset) is not int or offset < 0 or len(content) > MAX_BYTES:
        raise DocumentRequestError("document_canvas_selection_invalid")
    try:
        material = json.loads(content)
        if (
            material["contract"] != CONTRACT
            or not isinstance(material["canvas_id"], str)
            or not isinstance(material["projection_token"], str)
            or not isinstance(material["sections"], list)
            or not isinstance(material["datasets"], dict)
        ):
            raise ValueError("Invalid Canvas material")
    except (ValueError, KeyError, TypeError) as error:
        raise DocumentRequestError("document_canvas_material_invalid") from error
    result = {
        "contract": CONTRACT,
        "canvas_id": material["canvas_id"],
        "projection_token": material["projection_token"],
        "source_sha256": hashlib.sha256(content).hexdigest(),
        "complete_data": "Read the bound JSON input in the final script; this Tool returns bounded previews only.",
        "script_json_shape": {
            "sections": "list; each section stores its root data in section['spec'], and children in section['spec']['views'][i]['spec']",
            "identities": "Raw sections and child views use ['id']; section_id/view_id name Tool selections, not raw JSON fields. Non-chart sections may have no spec.",
            "datasets": "dict keyed by alias; material['datasets'][alias]['content'] is base64 of the exact linked file",
            "row_sample": "Tool preview only; the complete JSON stores original rows in the selected spec['rows']",
        },
    }
    if section_id is None:
        if view_id is not None:
            raise DocumentRequestError("document_canvas_selection_invalid")
        selected = material["sections"][offset : offset + PAGE_SIZE]
        result["sections"] = [
            {
                "section_id": section["id"],
                "rev": section["rev"],
                "kind": section["kind"],
                "views": [
                    _view_index(
                        vid,
                        spec,
                        script_path=f"material['sections'][{offset + index}]['spec']"
                        + (f"['views'][{view_index - 1}]['spec']" if vid is not None else ""),
                    )
                    for view_index, (vid, spec) in enumerate(_views(section))
                ],
            }
            for index, section in enumerate(selected)
        ]
        result["selection"] = {"kind": "sections", "offset": offset, "total": len(material["sections"]), "returned": len(selected)}
        result["datasets"] = {
            alias: {key: _preview(value) for key, value in record.items() if key != "content"}
            for alias, record in list(material["datasets"].items())[:PAGE_SIZE]
        }
        result["dataset_count"] = len(material["datasets"])
        result["dataset_metadata_sampled"] = len(material["datasets"]) > PAGE_SIZE
    else:
        section = next((item for item in material["sections"] if item["id"] == section_id), None)
        if section is None:
            raise DocumentRequestError("document_canvas_section_unavailable")
        result.update({"section_id": section_id, "rev": section["rev"], "kind": section["kind"]})
        views = dict(_views(section))
        if views:
            spec = views.get(view_id)
            if spec is None:
                raise DocumentRequestError("document_canvas_view_unavailable")
            columns = spec.get("columns") or [{"key": key} for key in dict.fromkeys(key for row in spec.get("rows", []) for key in row)]
            selected = columns[offset : offset + FIELD_PAGE_SIZE]
            section_index = material["sections"].index(section)
            view_index = next(index for index, (vid, _) in enumerate(_views(section)) if vid == view_id)
            script_path = f"material['sections'][{section_index}]['spec']" + (f"['views'][{view_index - 1}]['spec']" if view_id is not None else "")
            result.update(_view_index(view_id, spec, script_path=script_path))
            result["fields"] = [_preview(column) for column in selected]
            result["row_sample"] = [{column["key"]: _preview(row.get(column["key"])) for column in selected} for row in (spec.get("rows") or [])[:3]]
            result["presentation_sample"] = _preview({key: value for key, value in spec.items() if key not in {"rows", "columns", "views"}})
            result["selection"] = {"kind": "fields", "offset": offset, "total": len(columns), "returned": len(selected)}
        else:
            if view_id is not None or offset:
                raise DocumentRequestError("document_canvas_selection_invalid")
            result["content_sample"] = _preview({key: value for key, value in section.items() if key not in {"id", "rev", "kind"}})
            result["selection"] = {"kind": "section", "offset": 0, "total": 1, "returned": 1}
    selection = result["selection"]
    selection["next_offset"] = (
        selection["offset"] + selection["returned"] if selection["offset"] + selection["returned"] < selection["total"] else None
    )
    result["sample_limits"] = {"rows": 3, "string_characters": 160, "nested_items": 12, "complete_input_unchanged": True}
    if len(json.dumps(result, ensure_ascii=False).encode()) > 60 * 1024:
        raise DocumentRequestError("document_data_limit")
    return result
