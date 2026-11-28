"""Which files a run has given the user so far, stated to the model after each save.

docs/sprints/pptx-upload-edit-dogfood-2026-11-23/spike.md ("can the model label result files and work files").
A helper kept its deck in ``work_outputs`` across six saves so each call could carry it, never saved it
with ``purpose="deliverable"``, and returned "已产出新文件": the user got no file. The ``python_exec``
schema already says that work files are not shown to the user; what the model could not see was the
state of its run. Replayed at that decision with this note after the last Tool result, deepseek-flash
saved the deck as a result file in 7 of 8 runs, against 0 of 8 without it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# Calls that save sandbox outputs: the flat entry, its sandbox name, and the legacy Skill entry.
_SAVING_TOOLS = frozenset({"python_exec", "python_sandbox", "skill_invoke"})
_MAX_NAMES = 12


def _inner(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        return {}
    inner = arguments.get("input")
    return inner if isinstance(inner, dict) else arguments


def _saved_name(entry: str) -> str | None:
    from bowhead_skill.sandbox_deliverables import deliverable_saved_name

    try:
        return deliverable_saved_name(entry.strip())
    except Exception:  # noqa: BLE001 — an entry the tool refused names no saved file
        return None


def _call_files(invocation: Any) -> tuple[list[str], list[str]]:
    """(result files, work files) one call saved, by the name the user sees."""
    if getattr(invocation, "tool_name", None) not in _SAVING_TOOLS or getattr(invocation, "error", None):
        return [], []
    result = getattr(invocation, "result", None)
    outputs = result.get("output_artifacts") if isinstance(result, dict) else None
    if not isinstance(outputs, list) or not outputs:
        return [], []
    inner = _inner(getattr(invocation, "arguments", None))
    declared = inner.get("deliverable_outputs") if inner.get("purpose") == "deliverable" else None
    deliverable_names = (
        {name for name in (_saved_name(entry) for entry in declared if isinstance(entry, str) and entry.strip()) if name}
        if isinstance(declared, list)
        else set()
    )
    work = inner.get("work_outputs")
    work_names = {name for name in work if isinstance(name, str) and name} if isinstance(work, list) else set()
    results: list[str] = []
    works: list[str] = []
    for output in outputs:
        if not isinstance(output, dict) or not output.get("artifact_id") or output.get("data_role") in {"stdout", "structured"}:
            continue
        name = str(output.get("name") or "")
        shown = str(output.get("saved_as") or name)
        if name in deliverable_names:
            results.append(shown)
        elif name in work_names:
            works.append(shown)
    return results, works


def run_files(invocations: Iterable[Any]) -> tuple[list[str], list[str]]:
    """(result files, work files) the run has saved, in order; a file later saved as a result counts as one."""
    results: dict[str, None] = {}
    works: dict[str, None] = {}
    for invocation in invocations or ():
        call_results, call_works = _call_files(invocation)
        for name in call_results:
            works.pop(name, None)
            results[name] = None
        for name in call_works:
            if name not in results:
                works[name] = None
    return list(results), list(works)


def saved_files(invocations: Iterable[Any]) -> bool:
    """Whether these calls saved a result file or a work file."""
    return any(any(_call_files(invocation)) for invocation in invocations or ())


def _listed(names: list[str]) -> str:
    if not names:
        return "none"
    shown = ", ".join(names[:_MAX_NAMES])
    return shown + (f" (+{len(names) - _MAX_NAMES} more)" if len(names) > _MAX_NAMES else "")


def run_files_note(invocations: Iterable[Any]) -> str:
    """The run's result files and work files, and what each kind means for the user."""
    results, works = run_files(invocations)
    return (
        f"Result files the user will see from this run: {_listed(results)}. "
        f"Work files saved in this run, kept for later calls and not shown to the user: {_listed(works)}. "
        "Work files are saved and checked the same way as result files; only result files are shown to the user."
    )


__all__ = ["run_files", "run_files_note", "saved_files"]
