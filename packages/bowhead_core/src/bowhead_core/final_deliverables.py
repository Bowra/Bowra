"""Select the final files a Turn's answer shows.

Three rules hide a file from the answer; the run trace, Results and Files keep every file:

- A successful sandbox call that read exactly one same-name file and wrote one replaces that input.
- A helper that writes a file with the same name, type and folder path again in one Turn rewrote it: a
  sandbox call starts in an empty directory, so a rewrite is a new file of that name. Only its newest
  copy is shown. Copies from different helpers stay, because parallel helpers may deliver independent
  files, and so do same-name files of different declared folders (``site/a/index.html``,
  ``site/b/index.html``).
- Once the Turn selected a file explicitly (its sandbox call declared it as a deliverable, or
  ``present_artifacts`` named a version of it), a sandbox file nobody selected is a draft. Files from
  other producers (Canvas exports, render Tools) have no declaration step and always stay.
"""

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


def rewritten_deliverable_ids(
    identities: dict[str, tuple[str, str]],
    events: list[tuple[str, dict[str, Any]]],
) -> set[str]:
    """Hide all but the newest copy a helper wrote of one file name, type and path in this Turn.

    ``identities`` maps the Turn's delivered files to ``(filename, mime)``; ``events`` are the Turn's
    ``step_started``/``step_done`` events in order. A file no step reports as its output, or whose
    reported name or type differs from the stored file, is never hidden. The reported ``path`` of a
    declared nested file is part of the key; steps recorded before it was kept have none.
    """
    helper_of: dict[str, str] = {}
    copies: dict[tuple[str, tuple[str, str], str], list[str]] = {}
    for event_type, payload in events:
        step_id = payload.get("step_id")
        if not isinstance(step_id, str):
            continue
        if event_type == "step_started":
            helper = payload.get("subagent_id")
            helper_of[step_id] = helper if isinstance(helper, str) else ""
            continue
        meta = payload.get("result_meta")
        outputs = meta.get("output_artifacts") if isinstance(meta, dict) else None
        if event_type != "step_done" or step_id not in helper_of or not isinstance(outputs, list):
            continue
        for output in outputs:
            if not isinstance(output, dict) or output.get("data_role") == "stdout":
                continue
            artifact_id = output.get("artifact_id")
            identity = identities.get(artifact_id) if isinstance(artifact_id, str) else None
            if identity is None or identity != (output.get("filename"), output.get("mime")):
                continue
            path = output.get("path")
            written = copies.setdefault((helper_of[step_id], identity, path if isinstance(path, str) else ""), [])
            if artifact_id not in written:
                written.append(artifact_id)
    return {artifact_id for written in copies.values() for artifact_id in written[:-1]}


#: The step name every python_sandbox entry is surfaced as (python_exec and skill_invoke of python_sandbox too).
_SANDBOX_STEP = "python_sandbox"
_SELECTION_ORIGINS = ("producer_declared", "explicit_tool")


def unselected_sandbox_file_ids(roots: dict[str, str], events: list[tuple[str, dict[str, Any]]]) -> set[str]:
    """Hide sandbox files no call declared and the Lead did not present, once the Turn selected any file.

    ``roots`` maps the Turn's delivered files to their lineage root, so presenting or declaring any
    version selects the file. ``events`` are the Turn's ``step_started``/``step_done`` events in order;
    a ``step_done`` carries the call's ``artifact_presentation``. A Turn that selected nothing keeps
    every file (116 2026-11-08..22: 8 of 138 Turns change, 40 of 280 cards, all drafts the answers
    never named; docs/sprints/stray-result-files-2026-11-22).
    """
    tool_of: dict[str, str] = {}
    from_sandbox: set[str] = set()
    selected: set[str] = set()
    for event_type, payload in events:
        step_id = payload.get("step_id")
        if not isinstance(step_id, str):
            continue
        if event_type == "step_started":
            tool_of[step_id] = str(payload.get("tool_name") or "")
            continue
        meta = payload.get("result_meta")
        if event_type != "step_done" or not isinstance(meta, dict):
            continue
        declaration = meta.get("artifact_presentation")
        items = declaration.get("items") if isinstance(declaration, dict) and declaration.get("origin") in _SELECTION_ORIGINS else None
        for item in items if isinstance(items, list) else []:
            artifact_id = item.get("artifact_id") if isinstance(item, dict) else None
            if isinstance(artifact_id, str):
                selected.add(roots.get(artifact_id, artifact_id))
        outputs = meta.get("output_artifacts")
        if tool_of.get(step_id) == _SANDBOX_STEP and isinstance(outputs, list):
            from_sandbox.update(
                output["artifact_id"] for output in outputs if isinstance(output, dict) and isinstance(output.get("artifact_id"), str)
            )
    if not selected:
        return set()
    return {artifact_id for artifact_id, root in roots.items() if artifact_id in from_sandbox and root not in selected}
