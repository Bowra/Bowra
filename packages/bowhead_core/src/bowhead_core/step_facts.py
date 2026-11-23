"""Facts a step row shows, worded by the interface in its current language (turn visibility FR3, FR4).

docs/sprints/turn-visibility-2026-11-20/tech-design.md §3.3. Step events keep their text fields
(`input_preview`, `output_preview`) for older readers; a `facts` object beside them carries the
plain facts instead of sentences, so the interface words them in Chinese, English, German or
French and never shows a MIME type, byte count, enum code or parser message. Events without
`facts` (earlier Turns) keep showing their stored text.

Every facts object is ``{"v": 1, "kind": <kind>, ...}``. Kinds:

- ``attachment_read``: an attachment parsed for the Turn. Started: ``filename``, ``size_bytes``.
  Done: ``chars_read``, ``truncated``.
- ``canvas_added`` / ``canvas_not_added``: a Canvas write. ``section_kind`` is the chart or section
  kind (``hbar``, ``image``); ``reason`` is the skip code.
- ``code_outputs``: a sandbox run that succeeded. ``count`` and ``types`` (file extensions in
  saving order) of the files it saved, its stdout record excluded.
- ``result_lookup``: a ``find_results`` call. ``result_kind`` and/or ``name``.
- ``arguments_retry``: the model's arguments could not be parsed; the call is sent back for repair.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

FACTS_VERSION = 1
_MAX_TYPES = 4
_MAX_NAME_CHARS = 80


def _facts(kind: str, **fields: Any) -> dict[str, Any]:
    return {"v": FACTS_VERSION, "kind": kind, **{key: value for key, value in fields.items() if value is not None}}


def attachment_started(filename: str | None, size_bytes: Any) -> dict[str, Any]:
    name = filename.strip() if isinstance(filename, str) and filename.strip() and filename != "?" else None
    size = int(size_bytes) if isinstance(size_bytes, int | float) and size_bytes >= 0 else None
    return _facts("attachment_read", filename=name, size_bytes=size)


def attachment_done(chars_read: int, truncated: bool) -> dict[str, Any]:
    return _facts("attachment_read", chars_read=max(int(chars_read), 0), truncated=bool(truncated))


def input_facts(tool_name: str, args: Any) -> dict[str, Any] | None:
    """Facts for a Tool call's step row when it starts; None leaves the stored text alone."""
    if not isinstance(args, dict):
        return None
    if "_args_parse_error" in args:
        # The router puts its repair hint for the model here; it is never a label.
        return _facts("arguments_retry")
    if tool_name == "find_results":
        from bowhead_core.result_lookup import KINDS

        kind = args.get("kind")
        name = args.get("name")
        return _facts(
            "result_lookup",
            result_kind=kind if isinstance(kind, str) and kind in KINDS else None,
            name=name.strip()[:_MAX_NAME_CHARS] if isinstance(name, str) and name.strip() else None,
        )
    return None


def _saved_outputs(result: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The saved files as records. The sandbox Tool lists them twice: `output_artifacts` holds the
    records (with `data_role` on the stdout record) and its top-level `outputs` only the bare names, so
    the records are read first. The runner's own `result_json.outputs` holds records too."""
    nested = result.get("result_json")
    for holder, key in ((result, "output_artifacts"), (nested, "output_artifacts"), (nested, "outputs"), (result, "outputs")):
        items = holder.get(key) if isinstance(holder, dict) else None
        if isinstance(items, list):
            return [
                item if isinstance(item, dict) else {"name": item} for item in items if isinstance(item, dict) or (isinstance(item, str) and item)
            ]
    return None


def output_facts(tool_name: str, result: Any, *, succeeded: bool) -> dict[str, Any] | None:
    """Facts for a Tool call's step row when it finishes; None leaves the stored text alone."""
    if not succeeded or not isinstance(result, dict):
        return None
    from bowhead_core.tool_category import is_canvas_add_tool

    if is_canvas_add_tool(tool_name):
        kind = result.get("kind") or tool_name.removeprefix("canvas_add_").removeprefix("add_")
        if result.get("skipped"):
            return _facts("canvas_not_added", reason=str(result["skipped"]))
        return _facts("canvas_added", section_kind=str(kind)) if kind else None
    if tool_name == "python_sandbox":
        outputs = _saved_outputs(result)
        if outputs is None:
            return None
        types: list[str] = []
        count = 0
        for item in outputs:
            if item.get("data_role") == "stdout":
                continue
            count += 1
            suffix = PurePosixPath(str(item.get("name") or item.get("filename") or "")).suffix.lower().lstrip(".")
            if suffix and suffix not in types and len(types) < _MAX_TYPES:
                types.append(suffix)
        return _facts("code_outputs", count=count, types=types)
    return None
