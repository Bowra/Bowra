"""Prepare authorized style references and existing report content for a new deck.

The ordinary spawn path calls this only for the dedicated deck peer. It does
not infer an in-place template update from file presence or request keywords.
Native private-draft editing and confirmation belong to the document service.
Best-effort input preparation returns None on failure.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
# artifacts we PRODUCE — never valid as the refresh template (else period-2 picks its
# own period-1 output and drifts). Excluded from the template candidate list.
_OWN_OUTPUT_SOURCES = frozenset({"python_sandbox", "sandbox_output", "canvas_export", "canvas_export_clone", "consultant_deck"})
_OWN_OUTPUT_KINDS = frozenset({"deliverable", "deck_preview"})


def _is_pptx(art: Any) -> bool:
    fn = (getattr(art, "filename", "") or "").lower()
    return fn.endswith(".pptx") or getattr(art, "mime", "") == _PPTX_MIME


def _is_user_template(art: Any) -> bool:
    """A .pptx that the USER uploaded — not a deck we generated (template-refresh
    self-poisoning fix). A generated deck (kind deliverable/deck_preview or a
    produced source) must never be picked as the template."""
    if not _is_pptx(art):
        return False
    if getattr(art, "kind", "") in _OWN_OUTPUT_KINDS:
        return False
    return getattr(art, "source", "") not in _OWN_OUTPUT_SOURCES


def _materialize_default_template() -> str | None:
    """Copy the fixed packaged template to an isolated work directory.

    The legacy document guidance requires a temporary local template path.
    Community guidance returns before this helper; explicit callers still use
    the selected edition asset rather than an unavailable captured template.
    Return None if the packaged manifest or asset is invalid.
    """
    try:
        import shutil
        import tempfile
        from pathlib import Path as _P

        from bowhead_skill.catalog import builtin_catalog_dir

        root = builtin_catalog_dir() / "canvas-pptx" / "templates"
        manifest = json.loads((root / "default.json").read_text(encoding="utf-8"))
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"schema_version", "template"}
            or type(manifest["schema_version"]) is not int
            or manifest["schema_version"] != 1
        ):
            raise ValueError("Invalid packaged template manifest")
        name = manifest["template"]
        if name not in {"canvas-default.pptx", "community-neutral.pptx"}:
            raise ValueError("Unknown packaged template")
        src = root / name
        if not src.is_file() or src.resolve().parent != root.resolve():
            logger.warning("default deck template missing at %s", src)
            return None
        tmp_dir = _P(tempfile.mkdtemp(prefix="bow-consultant-tpl-"))
        dst = tmp_dir / name
        shutil.copyfile(src, dst)
        return str(dst.resolve())
    except Exception as e:
        logger.warning("default deck template materialize failed: %s", e)
        return None


def deck_overclaim_caveat_needed(
    *,
    tool_invocations: Any,
    turn_artifacts: Any,
    turn_id: str,
) -> bool:
    """True when a consultant deck spawn produced NO downloadable .pptx for this
    turn — so the chat must not claim "可下载 PPT". Pure (no I/O).

    Sprint followup-context-deck (P0-B over-claim guard). The backend registers
    the real deck as a kind=deliverable artifact ONLY on render_deck success
    (subagent.py:2172), so the absence of a .pptx/deliverable artifact for this
    turn — when a consultant (the deck profile) was spawned — means the deck
    failed and any chat claim of a downloadable PPT is a lie.
    """
    consultant_spawned = any(
        getattr(i, "tool_name", "") == "subagent_spawn"
        and isinstance(getattr(i, "arguments", None), dict)
        and i.arguments.get("profile") == "consultant"
        for i in (tool_invocations or [])
    )
    if not consultant_spawned:
        return False
    has_deck = any(
        getattr(a, "created_by_turn", None) == turn_id
        and ((getattr(a, "filename", "") or "").lower().endswith(".pptx") or getattr(a, "kind", "") == "deliverable")
        for a in (turn_artifacts or [])
    )
    return not has_deck


async def prepare_deck_render_inputs(
    *,
    canvas_store: Any,
    artifact_store: Any,
    session_id: str,
    user_id: str | None,
    mode: str = "compute",
    context: Any | None = None,
    require_delivery: bool = False,
) -> str | None:
    """Return scoped new-deck guidance with an authorized style reference.

    Existing Canvas content becomes a session-pinned intermediate input. The
    consultant uses the packaged default when no uploaded reference is available;
    the legacy compute mode requires an uploaded reference. Preparation errors
    return None. With `context`, a consultant assignment receives the render
    binding; a file is mandatory when `require_delivery` is set or no uploaded
    deck exists (the assignment cannot be an inspection).
    """
    from bowhead_core.document_runtime import community_deck_guidance, read_document_profile

    if context is not None and (context.user_id != user_id or context.session_id != session_id):
        return "Presentation inputs do not match the original user and session. No rendering is admitted."

    if read_document_profile() == "bowra-v1":
        # No template materialization or intermediate Artifact creation in this profile.
        if not artifact_store or not session_id or not user_id:
            return "Presentation inputs cannot be authorized. Ask for the missing session context; do not generate a file."
        try:
            arts = await artifact_store.list_for_session(session_id=session_id, user_id=user_id)
        except Exception:
            logger.warning("community_deck.input_lookup_failed", exc_info=True)
            return "Presentation inputs could not be checked. Report the failure; do not generate a file."
        return community_deck_guidance(template_present=any(_is_user_template(art) for art in arts))

    if not (artifact_store and session_id) or (mode != "consultant" and not canvas_store):
        return None
    try:
        # 1. .pptx in session scope? (the deck-render signal — the upload IS the
        #    trigger; list_for_session surfaces session_attachments + uploads.)
        arts = await artifact_store.list_for_session(
            session_id=session_id,
            user_id=user_id or "",
        )
        # template-refresh: exclude decks WE produced (a generated deck is never a
        # valid template — period-2 would pick period-1's output and drift, review P1-4).
        templates = [(a.id, a.filename or "deck.pptx") for a in arts if _is_user_template(a)]
        # Sprint followup-context-deck (P0-B no-template deck path) — an uploaded
        # .pptx is the deck signal for a COMPUTE spawn, so no template there = not
        # a deck request → bail (unchanged). But for a CONSULTANT spawn the Lead
        # already routed here BECAUSE the user asked for a PPT/deck (lead.py:210);
        # the absence of an upload just means "no custom template" — fall back to
        # the bundled default so "做个ppt" still yields a downloadable deck instead
        # of silently degrading to a report (the prod trace t_0e818cb5 failure).
        _use_default_template = False
        if not templates:
            if mode != "consultant":
                return None
            _use_default_template = True

        # 2. serialize the current canvas content (full shape, no truncation).
        sections_json_id: str | None = None
        n_sections = 0
        canvas = await canvas_store.get_canvas_by_session(session_id) if canvas_store else None
        if canvas is not None:
            sections = await canvas_store.list_sections(canvas.id, include_hidden=True)
            n_sections = len(sections)
            if n_sections:
                doc = {
                    "canvas_id": canvas.id,
                    "session_id": session_id,
                    "sections": [{"id": s.id, "seq": s.seq, "kind": s.kind, "payload": s.payload} for s in sections],
                }
                content = json.dumps(doc, ensure_ascii=False).encode("utf-8")
                art = await artifact_store.put_bytes(
                    content=content,
                    filename="sections.json",
                    mime="application/json",
                    # canvas_export → inject=stub: pinned + fetchable but NOT
                    # auto-injected into prompts (no flood).
                    source="canvas_export",
                    # file-management-v2 P1 (PRD §4.2.4): explicit tool_data. This is the deck renderer's
                    # **intermediate input**, not the user's exported deliverable. Inheriting the canvas_export kind inferred
                    # from source would mix it into the Assets tab as an exported file.
                    # artifact_store's user_version=2 backfill corrects historical rows.
                    kind="tool_data",
                    session_id=session_id,
                    created_by=user_id,
                    canvas_id=canvas.id,
                )
                sections_json_id = art.id

        # 3a. CONSULTANT branch (design r3 hybrid): hand the consultant a LOCAL
        # template path + tell it to CALL the tested render_deck (NOT hand-author
        # OOXML — the dogfood failure mode). render_deck runs as a trusted pptx-skill
        # script via SkillRuntime; the consultant authors `sections` (its analysis),
        # render_deck does the mechanical styling. Optional python-pptx polish after.
        if mode == "consultant":
            tpl_local: str | None = None
            if _use_default_template:
                tpl_local = _materialize_default_template()
            else:
                tpl_aid, tpl_fn = templates[0]
                try:
                    import tempfile
                    from pathlib import Path as _P

                    art = await artifact_store.get(tpl_aid)
                    if art is not None:
                        tmp_dir = _P(tempfile.mkdtemp(prefix="bow-consultant-tpl-"))
                        tpl_local = str((tmp_dir / (tpl_fn or "template.pptx").replace("/", "_")).resolve())
                        _P(tpl_local).write_bytes(await artifact_store.open_read(art))
                except Exception as _te:
                    logger.warning("consultant deck template materialize failed: %s", _te)
            if not tpl_local:
                return None  # can't render without the template file
            if context is not None:
                from bowhead_core.deck_render_tool import DeckRenderBinding, deck_target

                context.deck_render_binding = DeckRenderBinding(deck_target(context), tpl_local, required=require_delivery or _use_default_template)
            return (
                "## New editable PPTX delivery\n"
                "Use the native render_deck Tool. Its server-bound template is a style reference, not an in-place edit. "
                "Do not read renderer source files or pass local paths to python_sandbox/skill_invoke. "
                "Read-only inspection and existing-template edits retain their original workflows.\n"
                "For confirmed content, preserve the requested slide count, facts and order; do not load analytical methods or query BI. "
                "Existing Canvas material is optional supporting input, not a reason to enlarge the task.\n"
                "Call render_deck(sections=[{kind:'eyebrow',payload:{text:'subtitle'}},"
                "{kind:'h1',payload:{text:'title'}},"
                "{kind:'insight_card',payload:{title:'slide title',bullets:['first point','second point','third point']}}], "
                "expected_slides=<requested count>, style={accent:'2F6FCE',ink:'16324F',background:'FFFFFF'}) "
                "using the requested colors (omit style to inherit the reference). Cover uses eyebrow+h1 together; "
                "each insight_card is one additional slide. For user-supplied lists use title+bullets, one string per point in the original order; never move the first point into thesis or combine points into narrative. For analytical prose use title+thesis+narrative instead. "
                "Analysis may also use kpi_row, chart and recommendations; read the compact deck schema only when needed.\n"
                "Success requires ok=true and a real artifact_id. Then call subagent_return with delivery_surface='chat' "
                "and a concise exec_summary. The backend attaches the actual file; never invent deliverable IDs. "
                "If rendering fails, report that failure. If registration fails, retry identical arguments to register "
                "retained bytes without rendering again; do not claim a download exists before verification.\n"
            )

        # 3b. Legacy compute callers retain their new-deck rendering method.
        tmpl_lines = "\n".join(f"  - 模板 .pptx: artifact_id={aid} (filename {fn!r})" for aid, fn in templates)
        content_line = (
            f"  - canvas 结构化内容: artifact_id={sections_json_id} (sections.json, {n_sections} 段)"
            if sections_json_id
            else "  - (本 session 暂无 canvas 内容 → 先 bi_fetch_rows 取数, 或先澄清要做什么)"
        )
        return (
            "## New presentation rendering from a style reference\n"
            "This method creates new slides, not an in-place template update or a private document draft. "
            "Use it only when a new presentation is requested; read-only inspection must not generate files.\n"
            "生成一份可下载 .pptx, 板式继承用户模板, 内容来自下面的 canvas truth。\n"
            "**可用输入** (作为 python_sandbox 的 inputs):\n"
            f"{tmpl_lines}\n{content_line}\n"
            "**怎么做** (你自己写 python-pptx, 像人用 pptx skill 那样, 不要找现成渲染库):\n"
            '  1. `skill_read(name="pptx")` 学 python-pptx (开模板/读 layout/加 slide/原生图表)。\n'
            '  2. `skill_invoke(name="python_sandbox", input={code:<你写的 python-pptx>, '
            "inputs:{template:{artifact_id:<模板>}, content:{artifact_id:<sections.json>}}, "
            "timeout_sec:90})`。\n"
            "     **输入路径铁律 (最易错!)**: runner 自动注入全局 `INPUTS` dict; 文件在 "
            "`INPUTS['<key>']['path']` (key = 你在上面 inputs 里起的名, 如 'template'/'content')。"
            "code **第一行先 `print(INPUTS)`** 看清 key 和 path; **只用 `INPUTS[key]['path']` 打开文件, "
            "绝不要硬编码或猜路径** (例: `/home/user/INPUTS/...`、`/workdir/sections.json` 都是错的, 会 "
            "FileNotFoundError)。\n"
            "     code 逻辑: `Presentation(INPUTS['template']['path'])` 开模板 → "
            "`json.load(open(INPUTS['content']['path']))` 读 sections → 把 KPI/图表/发现/建议组合到模板"
            "板式 → `prs.save('/workdir/output.pptx')` (输出写 /workdir 才会被收集); 任何失败 "
            "`sys.exit(非零)` (别静默吞)。\n"
            "  3. 缺数据 → 先在本 leaf `bi_fetch_rows` 补 (CSV 也当 input 传); 补不出 → "
            "`subagent_return(error={kind:'needs_user_input', message:'...'})` 澄清, 别渲空 deck。\n"
            "  4. 自验 `result.json.outputs[]` 有 `output.pptx` → `subagent_return(payload="
            "{title, deliverables:[{kind:'pptx', artifact_id:<output.pptx>, slide_count:N}]})`; "
            "没有就当失败, 别报成功。\n"
        )
    except Exception as e:
        logger.warning(
            "prepare_deck_render_inputs failed session=%s err=%s",
            session_id,
            e,
        )
        return None
