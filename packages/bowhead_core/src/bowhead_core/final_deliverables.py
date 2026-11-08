"""Select revised final files from exact native sandbox input/output references."""

from __future__ import annotations

import json
from typing import Any


def sandbox_input_artifact_ids(arguments: dict[str, Any]) -> list[str]:
    """Read declared inputs, never infer dependencies from executable source."""
    inner = arguments.get("input", arguments)
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except ValueError:
            return []
    inputs = inner.get("inputs") if isinstance(inner, dict) else None
    if not isinstance(inputs, dict):
        return []
    return list(
        dict.fromkeys(
            item["artifact_id"]
            for item in inputs.values()
            if isinstance(item, dict) and isinstance(item.get("artifact_id"), str) and item["artifact_id"].startswith("art_")
        )
    )[:32]


def step_input_artifact_ids(payload: dict[str, Any]) -> list[str]:
    """Use typed refs, or an intact historical argument object; ambiguity keeps files."""
    code = payload.get("code_input")
    if isinstance(code, dict) and isinstance(code.get("input_artifact_ids"), list):
        return [value for value in code["input_artifact_ids"] if isinstance(value, str)][:32]
    try:
        arguments = json.loads(payload.get("arguments_preview") or "{}")
    except (TypeError, ValueError):
        return []
    return sandbox_input_artifact_ids(arguments) if isinstance(arguments, dict) else []


def superseded_deliverable_ids(
    identities: dict[str, tuple[str, str]],
    events: list[tuple[str, dict[str, Any]]],
) -> set[str]:
    """Hide an input only when a successful sandbox call replaces that exact file."""
    starts: dict[str, dict[str, Any]] = {}
    superseded: set[str] = set()
    for event_type, payload in events:
        step_id = payload.get("step_id")
        if not isinstance(step_id, str):
            continue
        if event_type == "step_started" and payload.get("tool_name") == "python_sandbox":
            starts[step_id] = payload
        elif event_type == "step_done" and payload.get("state") == "success" and step_id in starts:
            inputs = step_input_artifact_ids(starts[step_id])
            meta = payload.get("result_meta")
            outputs = meta.get("output_artifacts") if isinstance(meta, dict) else None
            if not isinstance(outputs, list):
                continue
            for output in outputs:
                if not isinstance(output, dict) or output.get("data_role") == "stdout":
                    continue
                artifact_id = output.get("artifact_id")
                if not isinstance(artifact_id, str):
                    continue
                identity = identities.get(artifact_id)
                if identity is None or identity != (output.get("filename"), output.get("mime")):
                    continue
                parents = {parent for parent in inputs if parent != artifact_id and identities.get(parent) == identity}
                # Two same-name inputs or outputs do not prove which file was replaced.
                siblings = [item for item in outputs if isinstance(item, dict) and (item.get("filename"), item.get("mime")) == identity]
                if len(parents) == 1 and len(siblings) == 1:
                    superseded.update(parents)
    return superseded
