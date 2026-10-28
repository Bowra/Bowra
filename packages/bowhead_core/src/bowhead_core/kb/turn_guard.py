"""Recheck all KB inputs before publishing an answer or report section."""

from __future__ import annotations

from typing import Any

import anyio

from bowhead_core.kb.eligibility import KbUseDecision, KbUseEligibility
from bowhead_core.kb.generations import now_iso, use_guard_enabled
from bowhead_core.kb.permission import ScopeContext
from bowhead_core.kb_citations import parse_kb_citation_identity

PAUSED_NOTICE = "知识库来源已变化或当前不可核对，本次相关结论无法核实。请核对文件后重试。"
WARNING_NOTICE = "部分知识库原件暂时无法核对。以下结果使用此前已验证的索引，请恢复原件访问后核对引用。"


_NOTICES: dict[str, tuple[str, str]] = {
    "zh-CN": (PAUSED_NOTICE, WARNING_NOTICE),
    "en-US": (
        "Knowledge sources changed or could not be verified. Related conclusions cannot be verified. Check the files and retry.",
        "Some originals are temporarily unavailable. Results use previously verified indexes; check the citations when access is restored.",
    ),
    "de-DE": (
        "Die Wissensquellen haben sich geändert oder können derzeit nicht geprüft werden. Die betreffenden Schlussfolgerungen lassen sich nicht verifizieren. Prüfen Sie die Dateien und versuchen Sie es erneut.",
        "Einige Originaldateien sind vorübergehend nicht verfügbar. Die Ergebnisse verwenden zuvor geprüfte Indizes. Prüfen Sie die Quellenverweise, sobald der Zugriff wiederhergestellt ist.",
    ),
    "fr-FR": (
        "Les sources de connaissances ont changé ou ne peuvent pas être vérifiées actuellement. Les conclusions concernées ne sont pas vérifiables. Vérifiez les fichiers et réessayez.",
        "Certains fichiers originaux sont temporairement indisponibles. Les résultats utilisent des index vérifiés auparavant ; vérifiez les citations lorsque l’accès est rétabli.",
    ),
}


async def recheck_turn_sources(ctx: Any, *, session_store: Any = None, turn_id: str = "") -> tuple[bool, str | None]:
    from bowhead_core.presentation_locale import resolve_presentation_locale
    from bowhead_core.report_chrome_text import chrome_lang

    locale = resolve_presentation_locale(chrome_lang(getattr(ctx, "language_context", None)))
    paused_notice, warning_notice = _NOTICES[locale]
    uses = getattr(ctx, "kb_source_uses", None)
    if not use_guard_enabled() or not uses:
        return True, None
    files = getattr(ctx, "kb_files_store", None)
    retriever = getattr(ctx, "kb_retriever", None)
    artifacts = getattr(ctx, "artifact_store", None)
    authorizer = getattr(ctx, "kb_file_authorizer", None)
    if files is None or authorizer is None or retriever is None:
        registry = getattr(ctx, "turn_citation_registry", None)
        if registry:
            for citation in registry.all_citations():
                if parse_kb_citation_identity(citation.artifact_id):
                    registry.bind_kb_check(citation.artifact_id, "", "source_unavailable", allowed=False)
        return False, paused_notice
    scope = ScopeContext(user_id=ctx.user_id, team_id=ctx.team_id, workspace_id=ctx.workspace_id, role="user")

    async def authorize(file):
        return await authorizer(file, ctx.user_id, ctx.team_id, ctx.workspace_id)

    checks = []
    allowed, warning = True, False
    checker = KbUseEligibility(files, artifacts, retriever._store)
    registry = getattr(ctx, "turn_citation_registry", None)
    decisions = {}
    snapshots = list(uses.values())

    async def inspect(used):
        key = (used["file_id"], used["generation_id"])
        decisions[key] = await checker.check(used["file_id"], scope, authorize, expected_generation=used["generation_id"] or "")

    with anyio.move_on_after(30):
        async with anyio.create_task_group() as group:
            for used in snapshots[:64]:
                group.start_soon(inspect, used)
    for used in snapshots:
        check = decisions.get(
            (used["file_id"], used["generation_id"]), KbUseDecision("paused", "source_check_timeout", used["generation_id"], now_iso())
        )
        check_state = check.reason if not check.allowed else check.warning or used["check_state"]
        used["check_state"] = check_state
        allowed &= check.allowed
        warning |= check_state == "source_temporarily_unavailable"
        checks.append({**used, "checked_at": check.checked_at})
        if registry is not None:
            for citation in registry.all_citations():
                identity = parse_kb_citation_identity(citation.artifact_id)
                if identity and identity[0] == used["file_id"]:
                    registry.bind_kb_check(citation.artifact_id, used["generation_id"], check_state, allowed=check.allowed)
    if session_store is not None and turn_id:
        await session_store.record_turn_kb_source_checks(turn_id, checks)
    return allowed, paused_notice if not allowed else warning_notice if warning else None


async def annotate_turn_report(ctx: Any, notice: str | None, *, paused: bool) -> None:
    """Persist and emit the same warning on this turn's narrative sections."""
    if not notice:
        return
    store = getattr(ctx, "canvas_store", None)
    turn_id = getattr(ctx, "turn_id", "")
    if store is None or not turn_id:
        return
    from bowhead_core.canvas_revise import emit_section_patched
    from bowhead_core.report_trust import apply_unverified

    sections = await store.list_sections_appended_by_turn(turn_id, include_patches=True)
    # Reports consisting solely of charts/tables still need durable visible
    # warnings. A normal narrative section reaches web, IM and every exporter.
    from bowhead_core.agents.report_tools import _event

    for canvas_id in {section.canvas_id for section in sections}:
        if not any(section.canvas_id == canvas_id and section.kind == "narrative" for section in sections):
            payload = {"markdown": f"> {notice}"}
            if paused:
                apply_unverified(payload, [notice])
            added = await store.append_section(canvas_id=canvas_id, kind="narrative", payload=payload, by_turn_id=turn_id)
            bus = getattr(ctx, "parent_bus", None)
            if bus is not None:
                await bus.emit(
                    _event(turn_id, "canvas.section_added", {"canvas_id": canvas_id, "section": added.to_report_section(), "seq": added.seq})
                )
    for section in sections:
        if section.kind != "narrative":
            continue
        payload = section.payload or {}
        markdown = str(payload.get("markdown") or "")
        if notice in markdown:
            continue
        patch = {"markdown": f"> {notice}\n\n{markdown}"}
        if paused:
            apply_unverified(patch, [*(payload.get("unverified_numbers") or []), notice])
        updated = await store.patch_section(
            section_id=section.id, patch=patch, expected_rev=section.rev, by_turn_id=turn_id, caller="kb_guard", skip_owner_check=True
        )
        if updated is None:
            raise RuntimeError("KB source warning could not be persisted")
        await emit_section_patched(getattr(ctx, "parent_bus", None), turn_id=turn_id, canvas_id=section.canvas_id, section=updated)
