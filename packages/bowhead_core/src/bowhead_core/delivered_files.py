"""Files delivered earlier in a session, as revision targets Lead can name.

Revision targets (docs/sprints/revision-target-2026-11-10/tech-design.md): the Canvas
report is one target; a file delivered in an earlier Turn is another. Lead sees the
session's delivered files by identity and names them in ``subagent_spawn.revise_files``;
the server validates identity and authority, never guesses from names. Everything here
is gated by ``FEATURE_REVISION_TARGETS`` (default on since 2026-11-10; ``0`` disables).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bowhead_core.artifact_store import Artifact, ArtifactStore
    from bowhead_core.subagent_resume import ResumeCheckpoint

logger = logging.getLogger(__name__)

DELIVERED_FILES_LIMIT = 12
MAX_REVISE_FILES = 8
# A file-target helper changes files only; the Canvas report is a separate revision target.
CANVAS_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "write_report",
        "remove_report_section",
        "add_chart",
        "edit_chart",
        "remove_chart",
        "canvas_add_external_iframe",
        "canvas_add_html_artifact",
        "canvas_add_insight",
        "canvas_add_spec_block",
        "canvas_create_design",
        "canvas_update_design_text",
        "canvas_flatten_design",
    }
)
# A target profile must be able to write a new file version.
FILE_PRODUCER_TOOLS: frozenset[str] = frozenset({"python_exec", "skill_invoke", "render_deck"})
REVISE_FILES_REASONS = frozenset(
    {"revise_files_invalid", "revise_files_not_found", "revise_files_document_flow", "revise_files_unavailable", "revise_files_not_latest"}
)
_TEMPLATE_DOCUMENT_PRODUCERS = frozenset({"template_document_v1", "template_document_internal_v1"})


def revision_targets_enabled() -> bool:
    """``FEATURE_REVISION_TARGETS``: Lead may name delivered files as revision targets (default on, 0 disables)."""
    return os.environ.get("FEATURE_REVISION_TARGETS", "1").strip().lower() in {"1", "true", "yes", "on"}


async def list_delivered_files(store: ArtifactStore, *, session_id: str, user_id: str, exclude_turn_id: str = "") -> list[Artifact]:
    """Newest files delivered in earlier Turns of this user's session."""
    if not session_id or not user_id:
        return []
    return await store.list_session_deliverables(session_id=session_id, user_id=user_id, exclude_turn_id=exclude_turn_id, limit=DELIVERED_FILES_LIMIT)


async def count_delivered_files(store: ArtifactStore, *, session_id: str, user_id: str, exclude_turn_id: str = "") -> int:
    """How many files ``list_delivered_files`` would list without its limit."""
    if not session_id or not user_id:
        return 0
    return await store.count_session_deliverables(session_id=session_id, user_id=user_id, exclude_turn_id=exclude_turn_id)


def _user_edit_producer() -> str:
    from bowhead_core.artifact_store import PRESENTATION_USER_EDIT_PRODUCER

    return PRESENTATION_USER_EDIT_PRODUCER


def file_type(artifact: Artifact) -> str:
    suffix = PurePosixPath(artifact.filename or "").suffix.lstrip(".").lower()
    return suffix or (artifact.mime or "file").split(";", 1)[0].strip()


def _age(created_at: str, now: datetime) -> str:
    try:
        moment = datetime.fromisoformat(created_at)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        minutes = int((now - moment).total_seconds() // 60)
    except (TypeError, ValueError):
        return "unknown age"
    if minutes < 60:
        return f"{max(minutes, 0)} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // 1440} days ago"


def quoted(value: Any, limit: int = 120) -> str:
    return " ".join(str(value or "").replace('"', "'").split())[:limit]


def render_delivered_files_block(
    files: Sequence[Artifact], *, checkpoints: Sequence[ResumeCheckpoint] = (), now: datetime | None = None, total: int | None = None
) -> str:
    """Turn-tail list of earlier deliverables; a helper run is named only when it is its Turn's only listed run.

    ``total`` is how many such files the session has. When the list shows fewer, its last
    line says so and names the lookup: a silent cut read as "that file was never made".
    """
    if not files:
        return ""
    current = now or datetime.now(UTC)
    by_turn: dict[str, list[str]] = {}
    for checkpoint in checkpoints:
        by_turn.setdefault(checkpoint.turn_id, []).append(checkpoint.id)
    lines = [
        "<delivered_files>",
        "Files delivered in earlier turns of this session. When the user asks to change one of these files (its content, charts, "
        "layout or format), that is a file revision, not a Canvas report revision: call subagent_spawn with "
        "revise_files=[<artifact_id>] and put the parts the user named in task or revise_scope. A listed checkpoint_id is the only "
        "resumable helper run of the turn that delivered the file and may hold the code that produced it; resume it when it did. "
        "Revise the Canvas report only when the user means the report on the canvas.",
        # Regression check M2 (the maintainer, 2026-11-14): a delivered file is one revision target of its own.
        "A delivered file, such as an HTML report, is a report in its own right, not a copy of the Canvas report: revising it "
        "changes only the file, and the Canvas report is not updated to match. When the user asked for the report as such a file "
        '(for example "做成HTML网页") or has been changing it, a further change to "the report" (a chapter, a chart, wording or '
        "numbers) means that file, even if a Canvas report was made too: use revise_files, not canvas_mode. If the user asked for "
        "both and it is unclear which one they mean, ask.",
        "For a lookup or analysis using an existing file as evidence, omit revise_files and name the source in the task.",
        # pptx-inline-edit design §6.6: a person can save a version by editing the file in Results.
        "edited_by=user marks a file whose latest version the user saved by editing it in place; a revision starts from that "
        "version and the user's edits are part of the file.",
    ]
    for artifact in files:
        line = f'- artifact_id={artifact.id} name="{quoted(artifact.filename)}" type={file_type(artifact)} {_age(artifact.created_at, current)}'
        if (artifact.version_no or 1) > 1:
            line += f" version={artifact.version_no}"
        if (artifact.producer_kind or "") == _user_edit_producer():
            line += " edited_by=user"
        producers = by_turn.get(artifact.created_by_turn or "", [])
        if len(producers) == 1:
            line += f" checkpoint_id={producers[0]}"
        lines.append(line)
    if total is not None and total > len(files):
        lines.append(f"Showing the newest {len(files)} of {total} delivered files. To find another, call find_results with part of its name.")
    lines.append("</delivered_files>")
    return "\n".join(lines)


@dataclass(frozen=True)
class FileTarget:
    artifact_id: str
    name: str
    type: str
    # File versions (docs/sprints/file-versions-2026-11-10/tech-design.md W3): a deliverable this session
    # produced can get a recorded next version; uploads and attachments are revised as new files.
    versionable: bool = False
    lineage_root: str = ""
    version_no: int = 1


async def resolve_revise_files(ctx: Any, raw: Any) -> tuple[list[FileTarget], dict[str, Any] | None]:
    """Validate Lead's revise_files by session membership and file authority; return targets or a refusal."""
    from bowhead_core.artifact_presentation import PresentationUnavailable, available_artifact

    ids = [str(item).strip() for item in raw] if isinstance(raw, list) else []
    ids = [item for item in dict.fromkeys(ids) if item]
    store = getattr(ctx, "artifact_store", None)
    session_id = str(getattr(ctx, "session_id", "") or "")
    user_id = str(getattr(ctx, "user_id", "") or "")
    turn_id = str(getattr(ctx, "turn_id", "") or "")
    if store is None or not session_id or not user_id:
        return [], refusal("revise_files_unavailable", ids=ids)
    if not ids or len(ids) > MAX_REVISE_FILES:
        return [], refusal("revise_files_invalid", ids=ids, available=await _inventory(store, session_id, user_id, turn_id))
    targets: list[FileTarget] = []
    missing: list[str] = []
    documents: list[str] = []
    try:
        for artifact_id in ids:
            members = await store.list_for_session(session_id=session_id, user_id=user_id, artifact_id=artifact_id, limit=1)
            if not members:
                missing.append(artifact_id)
                continue
            if (members[0].producer_kind or "") in _TEMPLATE_DOCUMENT_PRODUCERS:
                documents.append(artifact_id)
                continue
            try:
                artifact = await available_artifact(ctx, artifact_id)
            except PresentationUnavailable:
                missing.append(artifact_id)
                continue
            versionable, latest = await _version_eligibility(store, artifact, session_id=session_id, user_id=user_id)
            if latest:
                return [], refusal("revise_files_not_latest", ids=[artifact_id], latest=latest)
            targets.append(
                FileTarget(
                    artifact_id=artifact.id,
                    name=artifact.filename,
                    type=file_type(artifact),
                    versionable=versionable,
                    lineage_root=artifact.lineage_id or artifact.id,
                    version_no=artifact.version_no or 1,
                )
            )
        if not missing and not documents:
            return targets, None
        available = await _inventory(store, session_id, user_id, turn_id)
    except Exception as exc:  # noqa: BLE001 — refuse rather than revise an unverified file
        logger.warning("revise_files.lookup_failed turn=%s err=%s", turn_id, type(exc).__name__)
        return [], refusal("revise_files_unavailable", ids=ids)
    if documents:
        return [], refusal("revise_files_document_flow", ids=documents, available=available)
    return [], refusal("revise_files_not_found", ids=missing, available=available)


async def _version_eligibility(store: Any, artifact: Artifact, *, session_id: str, user_id: str) -> tuple[bool, str | None]:
    """``(versionable, latest_id)``: latest_id is set only when a newer version exists and must be targeted instead."""
    from bowhead_core.artifact_store import VersionTargetRefused

    if not hasattr(store, "check_version_target") or not await store.versions_supported():
        return False, None
    try:
        await store.check_version_target(artifact.id, user_id=user_id, session_id=session_id, workspace_id=None, mime=None)
    except VersionTargetRefused as refused:
        if refused.reason == "version_target_not_latest" and refused.latest_id:
            return False, refused.latest_id
        return False, None
    return True, None


async def recorded_versions(store: Any, targets: Sequence[FileTarget], turn_id: str) -> list[Artifact]:
    """New versions of the targets this Turn recorded, read from the store (a later failed call cannot hide them)."""
    roots = {target.lineage_root: target for target in targets if target.versionable and target.lineage_root}
    if not roots or not turn_id or not hasattr(store, "list_turn_versions"):
        return []
    try:
        return await store.list_turn_versions(sorted(roots), turn_id=turn_id)
    except Exception:  # noqa: BLE001 — facts fall back to the declared outputs
        logger.warning("revise_files.recorded_versions_failed turn=%s", turn_id, exc_info=True)
        return []


async def _inventory(store: Any, session_id: str, user_id: str, turn_id: str) -> list[Artifact]:
    try:
        return await list_delivered_files(store, session_id=session_id, user_id=user_id, exclude_turn_id=turn_id)
    except Exception:  # noqa: BLE001 — the refusal stays specific without the list
        return []


def refusal(reason: str, *, ids: Sequence[str], available: Sequence[Artifact] = (), latest: str | None = None) -> dict[str, Any]:
    listed = "; ".join(f"{artifact.id} ({quoted(artifact.filename, 60)})" for artifact in available) or "none"
    named = ", ".join(quoted(item, 40) for item in ids)
    messages = {
        "revise_files_invalid": f"revise_files must list 1-{MAX_REVISE_FILES} artifact IDs of files from this session.",
        "revise_files_not_found": f"These IDs are not available files of this session: {named}.",
        "revise_files_document_flow": f"These files come from the template document flow and are revised there, not as file copies: {named}.",
        "revise_files_unavailable": "The session's files could not be checked right now. Nothing was dispatched.",
        "revise_files_not_latest": f"A newer version of {named} exists: {latest}. Nothing was dispatched.",
    }
    hints = {
        "revise_files_document_flow": "Tell the user to revise the document from its draft in the document flow; do not regenerate it as a copy.",
        "revise_files_unavailable": "Retry the same dispatch once; if it fails again, tell the user the file could not be opened right now.",
        "revise_files_not_latest": f"Revise the latest version: retry with revise_files=[{latest}].",
    }
    return {
        "error": messages[reason],
        "error_kind": "unavailable" if reason == "revise_files_unavailable" else "invalid_args",
        "reason_code": reason,
        "path": "revise_files",
        **({"latest_id": latest} if latest else {}),
        # The list is the newest delivered files, not all of them. Without that sentence Lead took a file
        # outside it for one that was never made (docs/sprints/result-lookup-2026-11-15, FR19).
        "hint": hints.get(
            reason,
            f"Newest files delivered in this session: {listed}. If the file the user means is not among them, call find_results "
            "with part of its name to get its artifact_id. Retry with the exact artifact_id of the file the user means, "
            "or omit revise_files when the user means the Canvas report or only wants to read an existing file. "
            "To read or analyse an existing file, name the source file in task.",
        ),
    }


def is_revise_files_refusal(result: Any) -> bool:
    """A file-target dispatch refused before any helper ran (native spawn results only)."""
    return isinstance(result, dict) and result.get("path") == "revise_files" and result.get("reason_code") in REVISE_FILES_REASONS


def file_target_context_line(targets: Sequence[FileTarget], parts: Iterable[Any] = ()) -> str:
    """Instruction for a helper whose target is earlier files, replacing the Canvas-mode line."""
    files = "; ".join(f"《{quoted(target.name, 80)}》 (artifact_id {target.artifact_id}, {target.type})" for target in targets)
    named = [quoted(part, 80) for part in parts if str(part or "").strip()]
    scope = f" The user named these parts of the file: {'; '.join(named)}." if named else ""
    versioned = [target for target in targets if target.versionable]
    others = [target for target in targets if not target.versionable]
    versions = (
        # Regression check N10: the same file name is the same file, as a path is in coding harnesses.
        " In `python_exec`, a deliverable saved with the original's filename is recorded as its next version automatically; "
        "add `revises` only when the new file has a different name, e.g. "
        + ", ".join(f'`revises={{"<new name>": "{target.artifact_id}"}}`' for target in versioned)
        + ". In `render_deck` pass `revises` with the deck's artifact_id."
        if versioned
        else ""
    )
    new_files = (
        " " + ", ".join(f"《{quoted(target.name, 80)}》" for target in others) + " cannot be versioned (it was uploaded or attached); "
        "save the result as a new file without `revises`."
        if others
        else ""
    )
    return (
        f"- **Revision target: delivered files** — the user is changing file(s) delivered earlier, not the Canvas report: {files}.{scope} "
        "Start from the original: pass its artifact_id in the sandbox `inputs`, or rerun the code that produced it when this run "
        "already holds it. Change only what the user asked and keep everything else as it is. Write the new version with the same "
        'filename, `purpose="deliverable"`, and list it in `deliverable_outputs`; read the output checks (such as '
        "`html_static_check`) and fix reported problems before finishing." + versions + new_files + " Canvas tools are not available "
        "for this request. Return `delivery_surface='chat'` with an `exec_summary` that names the new file and what changed."
    )


def revised_files_facts(targets: Sequence[FileTarget], invocations: Iterable[Any], recorded: Sequence[Artifact] = ()) -> dict[str, Any]:
    """This dispatch's new files: recorded versions from the store, then declared outputs saved as new files."""
    target_ids = {target.artifact_id for target in targets}
    by_root = {target.lineage_root: target for target in targets}
    versions = [
        {
            "artifact_id": artifact.id,
            "name": artifact.filename,
            "version": artifact.version_no or 1,
            "version_of": by_root[artifact.lineage_root].artifact_id,
            "recorded_as_version": True,
        }
        for artifact in recorded
        if artifact.lineage_root in by_root
    ]
    new_files: dict[str, str] = {}
    for invocation in _flatten(invocations):
        result = getattr(invocation, "result", None)
        if getattr(invocation, "error", None) or not isinstance(result, dict):
            continue
        if getattr(invocation, "tool_name", "") == "render_deck":
            # A deck receipt names its registered file at the top level; it has no output_artifacts.
            artifact_id = result.get("artifact_id")
            if result.get("ok") is True and isinstance(artifact_id, str) and artifact_id and artifact_id not in target_ids:
                new_files[artifact_id] = str(result.get("filename") or "")
            continue
        for output in result.get("output_artifacts") or []:
            if not isinstance(output, dict) or output.get("data_role") == "stdout":
                continue
            artifact_id = output.get("artifact_id")
            if isinstance(artifact_id, str) and artifact_id and artifact_id not in target_ids and _declared(invocation, output.get("name")):
                new_files[artifact_id] = str(output.get("name") or "")
    recorded_ids = {item["artifact_id"] for item in versions}
    return {
        "targets": [{"artifact_id": target.artifact_id, "name": target.name} for target in targets],
        "new_files": versions
        + [
            {"artifact_id": artifact_id, "name": name, "recorded_as_version": False}
            for artifact_id, name in new_files.items()
            if artifact_id not in recorded_ids
        ],
    }


def _declared(invocation: Any, name: Any) -> bool:
    """Only files the producer declared as deliverables count as new versions (render_deck is read from its receipt)."""
    arguments = getattr(invocation, "arguments", None) or {}
    inner = arguments.get("input", arguments) if isinstance(arguments, dict) else {}
    declared = inner.get("deliverable_outputs") if isinstance(inner, dict) and inner.get("purpose") == "deliverable" else None
    if not isinstance(declared, list):
        return False
    from bowhead_skill.sandbox_deliverables import deliverable_saved_name

    # A nested file or folder entry is saved under its base name or as <folder>.zip.
    return name in {deliverable_saved_name(entry.strip()) for entry in declared if isinstance(entry, str) and entry.strip()}


def _flatten(invocations: Iterable[Any]) -> list[Any]:
    flat: list[Any] = []
    for invocation in invocations or []:
        flat.append(invocation)
        flat.extend(_flatten(getattr(invocation, "nested", None) or []))
    return flat


REVISED_FILES_DIRECTIVE = (
    " This dispatch revised delivered files, not the Canvas report. Tell the user the new file from revised_files.new_files by "
    "name: when recorded_as_version is true, as version N of the original; otherwise as a new file. If new_files is empty, say the "
    "file was not revised. Do not describe Canvas changes for it."
)
# Regression check M2: the dispatch also asked for a Canvas mode, which a file revision does not apply.
CANVAS_MODE_NOT_APPLIED_DIRECTIVE = (
    " revised_files.canvas_mode_not_applied: this dispatch changed only the file, not the Canvas report. The file is the report "
    "the user is changing; the Canvas report is a separate report and is not kept in sync with it. Do not tell the user the "
    "Canvas report is up to date or point them to it as the complete report, and do not revise the Canvas report to match "
    "unless the user asks for the report on the canvas."
)


__all__ = [
    "CANVAS_MODE_NOT_APPLIED_DIRECTIVE",
    "CANVAS_WRITE_TOOLS",
    "DELIVERED_FILES_LIMIT",
    "FILE_PRODUCER_TOOLS",
    "MAX_REVISE_FILES",
    "REVISED_FILES_DIRECTIVE",
    "FileTarget",
    "count_delivered_files",
    "file_target_context_line",
    "is_revise_files_refusal",
    "list_delivered_files",
    "refusal",
    "render_delivered_files_block",
    "resolve_revise_files",
    "revised_files_facts",
    "revision_targets_enabled",
]
