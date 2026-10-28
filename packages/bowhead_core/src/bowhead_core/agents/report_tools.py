"""report-engine Phase-2 WP-B — `read_report` / `write_report` tools.

⚠️  写报告链路（tool + Lead compose 编排 + done-branch 收尾）的架构总览、两条必守
    契约（canvas 事件 emit / final_answer 持久化）和"每次改必过的 checklist"见同目录
    ``REPORT_WRITING.md``。改本文件或 lead_orchestrator 的 compose / done-branch 报告
    逻辑，**必须同步更新那个文档**（它是这条链路的权威文档）。

The document-model-agnostic write surface a report-writer agent uses to put
content into the shared report document (canvas adapter today; external Google
Docs/Notion later). Replaces canvas-writer's `canvas_add_section` +
`canvas_compose_final` (the F12 double-write path).

**Enforcement lives HERE, in handler Python — NOT in the JSON schema.** This
stack (`tool_registry.execute`) does not validate tool-call args against
`input_schema`, and the LLM providers treat `if/then/default` as hints. So the
schema below is an LLM-facing *hint*; every invariant (expected_rev required on
update, kind default, figure-id validity, owner enforcement) is enforced by the
handler + the store. See phase2-tech-design §13.4.

Ownership: the writing peer's identity is stamped server-side from
`get_current_peer_name()` (a framework-set ContextVar the LLM cannot forge).
`_resolve_writer_id` is the single seam — WP-D's multi-writer dispatch refines
it to a unique-per-spawn id when two same-profile writers can run concurrently.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from typing import Any
from weakref import WeakValueDictionary

from bowhead_router.types import ToolSchema

from bowhead_core.agents._lead_context import (
    get_current_peer_name,
    get_current_subagent_id,
)
from bowhead_core.agents._write_guard import WRITE_TRUST_CLASSES, peer_trust_class
from bowhead_core.canvas_composer import _event
from bowhead_core.canvas_store import MACHINE_REORDER_TURN_ID, CanvasNotOwnerError, CanvasRevConflictError, CanvasSectionSupersededError
from bowhead_core.figure_anchor_layout import FIGURE_KINDS
from bowhead_core.figure_anchor_layout import figure_title as _fig_title
from bowhead_core.report_trust import apply_unverified
from bowhead_core.terminal_effect_contract import TOOL_EFFECT_READ_ONLY
from bowhead_core.terminal_llm_invocation import reject_unclassified_direct_mutation
from bowhead_core.tool_registry import ToolSpec

logger = logging.getLogger(__name__)


async def _emit_section_added(ctx: Any, canvas_id: str, stored: Any) -> None:
    """Emit a just-persisted section on the live bus so the workspace canvas
    renders it DURING the turn — not only after a manual refresh.

    M1 (report-pipeline-reliability): write_report was the ONE canvas writer that
    persisted (append_section / patch_section) without emitting
    canvas.section_added, so the report NARRATIVE body never reached the live bus
    (prod t_feae6f38: narrative persisted, 0 turn_events → blank until F5, while
    charts/kpi/sources — which DO emit — rendered live). Mirrors
    canvas_tools._add_image's emit. Best-effort — never breaks the write.
    """
    bus = getattr(ctx, "parent_bus", None)
    if bus is None or stored is None:
        return
    try:
        await bus.emit(
            _event(
                getattr(ctx, "turn_id", None),
                "canvas.section_added",
                {
                    "canvas_id": canvas_id,
                    "section": stored.to_report_section(),
                    "seq": stored.seq,
                },
            )
        )
    except Exception as e:
        logger.warning("write_report: canvas.section_added emit failed: %s", e)


async def _promote_chat_hidden_figures(ctx: Any, canvas_store: Any, canvas_id: str, section_ids: list[str]) -> None:
    """Make chat-hidden figures that this report anchors visible again, same id.

    The live client removed them from its canvas when the earlier turn emitted
    `canvas.sections_hidden`; re-emitting `canvas.section_added` puts them back
    through the existing upsert-by-id path, so live, persisted and replayed
    state agree without a new event type. Best-effort: a failure here leaves
    the anchors in place and terminal reconcile still converges on the server.
    """
    turn_id = getattr(ctx, "turn_id", None)
    try:
        lifted = await canvas_store.unhide_sections_for_report(section_ids, f"report_reference:{turn_id or ''}")
    except Exception:
        logger.exception("write_report.promote_hidden_figures_failed turn=%s ids=%s", turn_id, section_ids)
        return
    if not lifted:
        return
    lifted_set = set(lifted)
    for stored in await canvas_store.list_sections(canvas_id):
        if stored.id in lifted_set:
            await _emit_section_added(ctx, canvas_id, stored)
    logger.info("write_report.promoted_hidden_figures turn=%s n=%d ids=%s", turn_id, len(lifted), lifted)


async def _emit_section_patched(ctx: Any, canvas_id: str, stored: Any, *, open_turn: bool = False) -> None:
    """canvas-comprehensive WP-4 (B3): the write_report UPDATE path must emit
    canvas.section_patched (REPORT_WRITING.md 契约一: update→patched), NOT
    section_added. 批 B1-0：本体下沉到 `canvas_revise.emit_section_patched(bus, …)`（收尾器的 chrome
    patch 共用），这里只从 ctx 取 bus/turn_id。``open_turn``：跨轮改段的轮可能只有 patch 事件，先开轮一帧。"""
    from bowhead_core.canvas_revise import emit_section_patched as _emit_patched

    await _emit_patched(
        getattr(ctx, "parent_bus", None), turn_id=getattr(ctx, "turn_id", None), canvas_id=canvas_id, section=stored, open_turn=open_turn
    )


# per-canvas asyncio lock for the continuation-guard check-and-set (append +
# record) so two concurrent writes by one writer can't both miss the guard and
# create two sections. WeakValueDict → GC'd when no writer holds it.
_GUARD_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def _guard_lock(canvas_id: str) -> asyncio.Lock:
    lock = _GUARD_LOCKS.get(canvas_id)
    if lock is None:
        lock = asyncio.Lock()
        _GUARD_LOCKS[canvas_id] = lock
    return lock


# figure-like section kinds an [figure:id] anchor may reference inline.
# Single authority: core figure_anchor_layout. Write-position normalization must use same rule
# for whether anchor points to a figure, or drift makes one path strip what other retains.
_FIGURE_KINDS = FIGURE_KINDS

# Note shown to the writer next to a chat-hidden figure. It says the figure
# exists and what referencing it does, so the writer reuses it instead of
# redrawing an identical chart.
CHAT_HIDDEN_FIGURE_NOTE = "历史图:之前一轮在对话里展示过、当前未在画布显示;用 [figure:id] 引用后会按原样放回画布,无需重画"


def chat_hidden_figures(sections_including_hidden: list[Any], exclude_ids: set[str] | frozenset[str] = frozenset()) -> list[Any]:
    """Figures D6 hid because their turn was delivered in chat.

    They are still real sections of the canvas and stay referenceable: a report
    anchor brings them back under the same id (see `write_report`). This is the
    single definition used by `write_report`, `read_report`, and the writer
    loop's figure pool, so the pool the writer is shown and the pool it is
    validated against cannot drift. Superseded figures are never included.
    """
    return [
        s
        for s in sections_including_hidden
        if s.kind in _FIGURE_KINDS and getattr(s, "hidden_for_chat_mode", False) and not getattr(s, "superseded", False) and s.id not in exclude_ids
    ]


_FIGURE_REF_RE = re.compile(r"\[figure:([a-zA-Z0-9_]+)\]")
# 2b: narrative only (re-review: listing structured kinds while content is a
# markdown string invites the card anti-pattern; structured kinds return with
# their object schemas in a later WP). The handler also defaults to narrative.
_WRITE_KINDS = ["narrative"]


def write_report_enabled() -> bool:
    """Flag `BOWHEAD_WRITE_REPORT` — report-engine Phase-2 WP-E: now **default ON**
    so read_report/write_report are registered by default and the writer-loop is
    the default compose path. Set `BOWHEAD_WRITE_REPORT=0` to roll back (tools not
    registered → legacy deterministic layout, fully reversible per design §8)."""
    return os.environ.get("BOWHEAD_WRITE_REPORT", "1").strip() != "0"


def register_report_tools(registry: Any) -> None:
    """Register read_report + write_report. Called (flag-gated) at startup."""
    registry.register(
        ToolSpec(
            schema=_read_report_schema(),
            handler=_read_report,
            effect_spec=TOOL_EFFECT_READ_ONLY,
        )
    )
    registry.register(ToolSpec(schema=_write_report_schema(), handler=_write_report))


def _resolve_writer_id(ctx: Any) -> str:
    """The owner id stamped on sections this caller writes. Framework-set,
    LLM-unforgeable. Prefer the UNIQUE per-spawn id (so two same-profile
    concurrent writers don't collide on owner — re-review 安全P1); fall back to
    the peer/profile name for the Lead (no spawn)."""
    return get_current_subagent_id() or get_current_peer_name() or "lead"


def _text_content(payload: Any) -> str | None:
    """The narrative text of a section, or None for non-text (figure/structured)
    sections. Returns ONLY the `markdown` string — never the raw payload dict, so
    a chart/image section's internal/structural fields can't leak through
    `read_report(include_full=True)` (re-review read-P1)."""
    if isinstance(payload, dict):
        md = payload.get("markdown")
        return md if isinstance(md, str) else None
    if isinstance(payload, str):
        return payload
    return None


def _outline_title(s: Any) -> str:
    """outline 的 title：payload.title；narrative 取章标题（切章落库时写进 _meta.chapter_title，
    老段就地取首个 `## ` 行）——让写手按**内容**挑段（tech-design-chapter-split §2.5）。"""
    pl = getattr(s, "payload", None)
    if not isinstance(pl, dict):
        return ""
    if pl.get("title"):
        return str(pl["title"])
    if getattr(s, "kind", "") == "h1":  # Chapter title (in-place editable since B2-7); writer identifies by title.
        return str(pl.get("text") or "")
    if getattr(s, "kind", "") == "narrative":
        meta = pl.get("_meta") or {}
        if isinstance(meta, dict) and meta.get("chapter_title"):
            return str(meta["chapter_title"])
        from bowhead_core.report_chapters import chapter_title

        for line in str(pl.get("markdown") or "").splitlines():
            if line.startswith("## "):
                return chapter_title(line)
    return ""


# ─────────────────────────────────────────────────────────────
# read_report
# ─────────────────────────────────────────────────────────────
def _read_report_schema() -> ToolSchema:
    return ToolSchema(
        name="read_report",
        description=(
            "读取当前报告文档:顺序段落 + 已有图表(含 id)+ 每段 owner/rev。"
            "**更新已有段、引用不是你本轮刚画的图、或报告可能已有别人写的段时, 写之前必须先调用它** —— "
            "知道已有什么、确认你负责写哪段、用真实图 id 引图、拿到更新所需的 rev。"
            "本轮首次写整篇、且只引你自己 add_chart 刚返回的图 id 时可以不调。报告是共享文档(可能多 agent 同编)。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "section_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "只读这几段;不传=返 outline + figures(默认,最省 context)",
                },
                "include_full": {
                    "type": "boolean",
                    "description": "true=连每段正文一起返(大报告慎用)。默认 false",
                },
            },
        },
    )


async def _read_report(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    canvas_store = getattr(ctx, "canvas_store", None)
    session_id = getattr(ctx, "session_id", None)
    if canvas_store is None or not session_id:
        return {"ok": True, "stub": True, "outline": [], "figures": []}
    # Scoping: the report is the CALLER's own session canvas (session_id comes
    # from the turn ctx). list_sections is per-canvas, and section_ids only
    # filter within it → no cross-canvas/user leak (re-review read-scoping).
    canvas = await canvas_store.get_canvas_by_session(session_id)
    if canvas is None:
        return {"ok": True, "outline": [], "sections": [], "figures": []}
    sections = await canvas_store.list_sections(canvas.id)
    want = set(args.get("section_ids") or [])
    include_full = bool(args.get("include_full")) or bool(want)

    # canvas-update-turn batch A4: model lacks own ID; owner alone cannot tell whether editable.
    # prior_turn = section from previous turn; editable = can this writer patch it now
    # (superseded never editable). Batch B0 (FEATURE_CANVAS_SECTION_REVISE) opens previous-turn sections
    # to this-turn writer, same flag/rule as _do_update cross-turn admission.
    from bowhead_core.canvas_revise import section_revise_enabled

    _writer_now = _resolve_writer_id(ctx)
    _turn_now = getattr(ctx, "turn_id", None)
    _revise_on = section_revise_enabled()

    from bowhead_core.canvas_revise import is_cross_turn_editable, is_prior_turn_section, out_of_revise_scope

    def _payload_keys(s: Any) -> list[str]:
        p = getattr(s, "payload", None)
        return sorted(k for k in p if not str(k).startswith("_")) if isinstance(p, dict) else []

    _trusted_now = peer_trust_class(get_current_peer_name()) in WRITE_TRUST_CLASSES

    def _editable(s: Any) -> bool:
        if getattr(s, "superseded", False):
            return False
        own = s.owner in (None, _writer_now)
        # Review fix: previous-turn **body only** editable (write_report writes Markdown; h1/KPI/figure
        # patches invisible, revision finalizer only recognizes narrative); same rule as _do_update.
        if _revise_on and is_prior_turn_section(s, _turn_now) and out_of_revise_scope(getattr(ctx, "parent_bus", None), s.id):
            return False  # Batch B2-5: outside revision scope (same rejection as _do_update).
        if _revise_on and _trusted_now and is_prior_turn_section(s, _turn_now):
            # Batch B2-7: previous-turn sections use cross-turn rule only; unowned document h1/KPI previously own=true/editable,
            # but writes got kind_not_editable. Editable chapter titles make false true steer writer to document title.
            return is_cross_turn_editable(s, _turn_now)
        return (is_cross_turn_editable(s, _turn_now) or own) if _revise_on else own

    outline = [
        {
            "section_id": s.id,
            "kind": s.kind,
            "seq": s.seq,
            "title": _outline_title(s),
            "owner": s.owner,
            "rev": s.rev,
            "prior_turn": is_prior_turn_section(s, _turn_now),
            "editable": _editable(s),
            # Review fix 2026-09-06: multiturn canvases have dozens of folded same-title copies; editable alone cannot distinguish other owner from old section;
            # explicitly expose superseded: with revise off, patch **actually updates folded row** (AtomicCanvasOps bare UPDATE, no superseded check), invisible in body.
            "superseded": bool(getattr(s, "superseded", False)),
            # Batch B1-7: expose nonbody payload keys; without them, model guessing kpi_row / recommendations keys
            # wrote KPI into unrendered spec.kpis on 116: rev advanced, UI unchanged.
            **({"payload_keys": _payload_keys(s)} if s.kind != "narrative" else {}),
        }
        for s in sections
    ]
    # RC-8: mark figure freshness; writer previously could not distinguish this-turn vs historical figures
    # (a root cause of stale references). fresh = appended/patched this turn (canvas_revisions).
    turn_id = getattr(ctx, "turn_id", None)
    touched: set[str] = set()
    if turn_id:
        try:
            touched = await canvas_store.section_ids_touched_by_turn(turn_id)
        except Exception:  # Best effort: missing lookup omits note, never breaks read_report.
            touched = set()
    figures = []
    for s in sections:
        if s.kind not in _FIGURE_KINDS:
            continue
        if _revise_on and getattr(s, "superseded", False):
            # Exclude folded figures from the reference pool because the frontend hides them.
            # Preserve the existing path when the same rollout flag is disabled.
            continue
        fig: dict[str, Any] = {"id": s.id, "kind": s.kind, "title": _fig_title(s.payload, s.kind)}
        if turn_id:
            fig["fresh"] = s.id in touched
            if not fig["fresh"]:
                fig["note"] = "历史图:生成于之前轮次,数据未在本轮重算"
        figures.append(fig)
    # Without these the writer sees `figures: []` after a chat turn and redraws
    # the same charts (116 `s_0571e73c586e`, 2026-10-01).
    try:
        _listed = {f["id"] for f in figures}
        for s in chat_hidden_figures(await canvas_store.list_sections(canvas.id, include_hidden=True), _listed):
            figures.append({"id": s.id, "kind": s.kind, "title": _fig_title(s.payload, s.kind), "fresh": False, "note": CHAT_HIDDEN_FIGURE_NOTE})
    except Exception:  # best effort: the visible pool alone is still a valid answer
        logger.exception("read_report.chat_hidden_figures_failed canvas=%s", canvas.id)
    out: dict[str, Any] = {"ok": True, "outline": outline, "figures": figures}
    if include_full:
        out["sections"] = [
            {
                "section_id": s.id,
                "kind": s.kind,
                "seq": s.seq,
                "owner": s.owner,
                "rev": s.rev,
                "superseded": bool(getattr(s, "superseded", False)),
                "content": _text_content(s.payload),
                "source_refs": (s.payload.get("source_refs") if isinstance(s.payload, dict) else None) or [],
            }
            for s in sections
            if (not want or s.id in want)
        ]
    return out


# ─────────────────────────────────────────────────────────────
# write_report
# ─────────────────────────────────────────────────────────────
def _write_report_schema() -> ToolSchema:
    from bowhead_core.canvas_revise import section_revise_enabled

    # canvas-update-turn batch B0: flag adds revision-rule line (schema built at registration; env flag).
    _revise_on = section_revise_enabled()
    _revise_line = (
        "- 修订轮(canvas_mode=revise):上一轮的**正文段**在 outline 里 editable=true,传 section_id+expected_rev 就地改;"
        "只改受影响的段,未受影响的段不动;**不要**不传 section_id 另写一篇(那会在旧正文后面再挂一份)。\n"
        if _revise_on
        else ""
    )
    # Review fix: rule② contradicted above (flag permits previous-turn body); wording follows flag.
    _rule_2 = "②只能改你 own 的段,以及 outline 里 editable=true 的上一轮正文段。" if _revise_on else "②只能改你 own 的段。"
    return ToolSchema(
        name="write_report",
        description=(
            "写/更新报告里**你负责的一段**。写叙事正文的唯一方式。\n"
            "- 更新你 own 的段:传 section_id + 它的 rev(read_report 拿)。就地改写,不新增重复段。\n"
            "- 新增一段:不传 section_id(owner=你)。**不确定 id 就别填,绝不拼造**。\n"
            "- 新章要落在某段之后(「在第一章和第二章之间插一章」):传 after_section_id=第一章的 id;不传就落在最后一段正文之后。\n"
            "- read_report outline 里每段带 `editable`:false 的段别传 section_id(改不了,会被拒)。\n"
            + _revise_line
            + "铁律:①一段只写一次,没有「先加再组装」步骤,写完即生效;续写用同 section_id 更新。"
            # report-streaming batch R: remove old read_report-before-write rule②; combined with WRITER_SYSTEM
            # duplicate rule burned 1.5-2.3 extra pure-probe round trips per document (production: 54.5%
            # writer input tokens), though user message already supplies all figures/evidence.
            # **Only updating existing sections truly requires read_report**, to obtain expected_rev,
            # already stated in bullet above and expected_rev field description.
            + _rule_2
            + "③引图 `[figure:id]` 的 id 必须是证据「可用图表」里列出的真实 id;**没有可用图表就不写任何 [figure:]**。"
            "标 fresh=false 的历史图优先不引;引了就不得把本轮新数字说成图中内容。"
            "④数字只用证据里给的,别人的数字引用不重述。"
        ),
        input_schema={
            "type": "object",
            "required": ["kind", "content"],
            "properties": {
                "section_id": {"type": "string", "description": "更新时传(read_report 拿);新增不传。绝不拼造。"},
                "expected_rev": {"type": "integer", "description": "更新必填:你 read 到的该段 rev。不符→拒,重读再写。"},
                "kind": {"type": "string", "enum": _WRITE_KINDS, "description": "默认 narrative(连续 markdown 行文,不是卡片)。创建定,更新不可改。"},
                "content": {"type": "string", "description": "段正文(markdown);图用 [figure:id] 内联。"},
                "source_refs": {"type": "array", "items": {"type": "string"}, "description": "本段引用的来源 id(渲染 Sources 段)。"},
                "after_section_id": {
                    "type": "string",
                    "description": "可选:新增时把新段/新章插在这段之后(read_report 拿 id;「在第一章和第二章之间插一章」就传第一章的 id)。不传:新章插在最后一段正文之后。更新时忽略。",
                },
            },
        },
    )


def _consume_t7_pending(ctx: Any, writer: str, kind: str, result: Any) -> None:
    """T7: 写手成功写下 narrative 即注销其 pending 槽位标记 → off-enum kind 归一
    (见 _write_report 顶部)自动失效,后续 deliberate different-kind append 回归
    handler-P0 宽容语义。"""
    if kind != "narrative" or not (isinstance(result, dict) and result.get("ok")):
        return
    pending = getattr(getattr(ctx, "parent_bus", None), "_t7_slot_pending", None)
    if isinstance(pending, set):
        pending.discard((writer, "narrative"))


def _note_write_ok(ctx: Any, writer: str, result: Any) -> None:
    """canvas-update-turn 批 A3：write_report **成功**才在 turn 级 bus 记账。
    ψ-E eager 抑制按这本账判（不按「调过 write_report」判），失败的调用不再压掉 eager。"""
    if isinstance(result, dict) and result.get("ok"):
        from bowhead_core.canvas_revise import note_authored_title, note_write_report_ok

        note_write_report_ok(getattr(ctx, "parent_bus", None), writer)
        # Same title fallback gate: only successful append pins first-line # title via note_authored_title
        # (still first-wins internally: first body section title is document title, later chapters must not replace it).
        with contextlib.suppress(Exception):
            _pending = getattr(ctx, "_pending_authored_title", None)
            if _pending:
                note_authored_title(getattr(ctx, "parent_bus", None), _pending)
            ctx._pending_authored_title = None


def _my_sections_map(ctx: Any) -> dict[tuple[str, str], str] | None:
    """Turn-scoped {(writer_id, kind): section_id} on parent_bus — the
    continuation guard: a writer that already wrote a section OF THIS KIND this
    turn routes a no-section_id call to UPDATE it (not a 2nd append) even if the
    LLM drops section_id — kills the forgot-id→re-append F12. Keyed by
    (writer, kind) so a deliberate NEW section of a DIFFERENT kind is NOT
    hijacked into the existing one (re-review handler-P0). None if no bus."""
    bus = getattr(ctx, "parent_bus", None)
    if bus is None:
        return None
    m = getattr(bus, "_report_writer_sections", None)
    if m is None:
        m = {}
        try:
            bus._report_writer_sections = m
        except Exception:
            return None
    return m


def _coerce_rev(raw: Any) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def _do_update(
    canvas_store: Any,
    section_id: str,
    payload: dict[str, Any],
    raw_rev: Any,
    writer: str,
    turn_id: str | None,
    *,
    ctx: Any = None,
    canvas_id: str | None = None,
    old_sec: Any = None,
) -> dict[str, Any]:
    """Update path with handler-enforced rev + error-dict translation.

    ``old_sec``：调用方已取到的目标段（``_write_report`` 为判跨轮 bake 先取了一次）；没给就自己取。
    """
    rev = _coerce_rev(raw_rev)
    if rev is None:
        return {"error": "rev_required", "hint": "更新必须带整数 expected_rev;先 read_report 拿当前 rev"}
    from bowhead_core.canvas_revise import (
        REVISABLE_KINDS,
        is_cross_turn_editable,
        is_prior_turn_section,
        section_revise_enabled,
    )

    _revise_on = section_revise_enabled()
    # canvas-update-turn batch B0: always fetch target once (previously only under _pprov_rt());
    # cross-turn/superseded checks need it. Unavailable (stub store) treated same-turn.
    _old_sec: Any = old_sec
    if _old_sec is None and canvas_id:
        with contextlib.suppress(Exception):
            _secs = await canvas_store.list_sections(canvas_id, include_hidden=True)
            _old_sec = next((s for s in _secs if s.id == section_id), None)
    # Flag on: folded History sections not editable (review P1-1: flag-gated, off write path byte-identical to batch A).
    # Snapshot check only gives early hint; actual gate in patch_section(reject_superseded=…) transaction (review TOCTOU fix).
    _superseded_err = {
        "error": "section_superseded",
        "section_id": section_id,
        "hint": "这段已被折叠进 History，不可改；留空 section_id 新写，或改当前可见的段",
    }
    if _revise_on and _old_sec is not None and getattr(_old_sec, "superseded", False):
        return _superseded_err
    # Cross-turn = target created previously (same outline prior_turn rule: canvas_revise.is_prior_turn_section).
    # owner is **same-turn** concurrent-writer mutex; previous writer exited, no concurrency, flag permits current writer;
    # ledger still records current writer (patch_section caller).
    # Review fix: only **body** admitted (write_report Markdown only; h1/KPI/figure patches invisible, revision
    # finalizer narrative only); explicitly reject prior nonbody, rather than misleading not_owner current-other-writer reason.
    # 2026-09-09 (tech-design: docs/sprints/custom-agent-report-pen-2026-09-09/): cross-turn admission
    # **system manifests only**. Previous-writer-exited/no-concurrency rationale is a system-writer
    # agreement; custom agents are creatable by **members** (team-subagent creation route requires no admin).
    # Letting any member-authored prompt rewrite another previous-turn writer's body is exactly the privilege
    # that must not accompany expanded pen access. Reuse _write_guard criterion: CCA absent from system registry → no trust class
    # → automatic fail-closed; team agents cannot get system peer names (creation rejects), so no impersonation.
    # Custom agents still edit **own** sections and append normally (owner path).
    _cross_turn_trusted = peer_trust_class(get_current_peer_name()) in WRITE_TRUST_CLASSES
    _prior = bool(_revise_on and _old_sec is not None and is_prior_turn_section(_old_sec, turn_id) and _cross_turn_trusted)
    if _prior and not is_cross_turn_editable(_old_sec, turn_id):
        return {
            "error": "kind_not_editable",
            "section_id": section_id,
            "kind": getattr(_old_sec, "kind", None),
            "hint": f"上一轮的段只有 {'/'.join(sorted(REVISABLE_KINDS))} 与章标题可就地改；图用 add_chart 重出、正文引新 id；报告大标题/KPI 带由 payload.title/kpis 换",
        }
    # canvas-update-turn batch B2-5: user-selected chapters only; no changes to other prior sections, including aggregate conclusion.
    from bowhead_core.canvas_revise import out_of_revise_scope

    if _prior and out_of_revise_scope(getattr(ctx, "parent_bus", None), section_id):
        return {
            "error": "out_of_revise_scope",
            "section_id": section_id,
            "hint": "用户这轮只让改点名的章节，这段在范围外，不改；若它因此和新口径对不上，在 exec_summary 里如实说明。",
        }
    _cross_turn = _prior
    _old_pl = getattr(_old_sec, "payload", None)
    _is_chapter = isinstance(_old_pl, dict) and bool((_old_pl.get("_meta") or {}).get("chapter_title"))
    if _is_chapter:
        from bowhead_core.report_chapters import refresh_chapter_title

        payload = refresh_chapter_title(payload, _old_pl)
    if (_cross_turn or _is_chapter) and "style" in payload and isinstance(_old_pl, dict):
        # Review fix: prior-body edit preserves style (changing caveats to executive changes rendering).
        # Same-turn chapter update after split likewise preserves insight, never overridden by whole-document executive.
        _old_style = _old_pl.get("style")
        if isinstance(_old_style, str) and _old_style:
            payload["style"] = _old_style
    # graph-mode batch 4 (review P2-6): source_refs union; shallow patch replaces entire key,
    # so LLM key would overwrite deterministic T7 provenance seeds (unreliable seeds
    # undermine D-C premise). Ordered union: existing first, LLM additions after.
    if "source_refs" in payload and _old_sec is not None:
        # Narrow (implementation review P2-1): union only multiwriter slots (owner=writer-*, sole seed home)
        # with flag ON; chat sections retain pre-batch-4 semantics (LLM may replace key wholesale,
        # removing wrong citations on revision). Failed union falls back to LLM value, never blocks writing.
        from bowhead_core.planning_v2 import planning_provenance_enabled as _pprov_rt

        with contextlib.suppress(Exception):
            if _pprov_rt():
                _owner_str = str(getattr(_old_sec, "owner", "") or "")
                if _owner_str.startswith("writer-"):
                    _old_refs = (_old_sec.payload.get("source_refs") if isinstance(_old_sec.payload, dict) else None) or []
                    _incoming = payload.get("source_refs") or []
                    payload["source_refs"] = list(dict.fromkeys([*(str(r) for r in _old_refs), *(str(r) for r in _incoming)]))[:24]
    try:
        # Review fix: canvas_id scope; previous bare-ID patch allowed tool-supplied other-session
        # section_id changes (NULL-owner figure/chrome); outside this canvas → None → section_not_found.
        sec = await canvas_store.patch_section(
            section_id=section_id,
            patch=payload,
            expected_rev=rev,
            by_turn_id=turn_id,
            caller=writer,
            skip_owner_check=_cross_turn,
            canvas_id=canvas_id,
            reject_superseded=_revise_on,
        )
    except CanvasNotOwnerError as e:
        # Return the caller's writer identity and a usable fallback action.
        # Do not promise folding: latest-wins is default-off and folding requires a title.
        # With cross-turn admission enabled, not_owner represents a same-turn mutex.
        _hint = (
            "这段是本轮另一位写手的，不可改；写你自己的段（不传 section_id）。"
            if _revise_on
            else "这段是别的 agent 写的，本版本不能就地改：留空 section_id 另写一段承载你的正文，不要再试同一个 id。"
        )
        return {"error": "not_owner", "owner": e.owner, "hint": _hint}
    except CanvasRevConflictError as e:
        return {"error": "rev_conflict", "current_rev": e.current_rev}
    except CanvasSectionSupersededError:
        return _superseded_err
    if sec is None:
        return {"error": "section_not_found", "section_id": section_id, "hint": "这个 id 不存在(多半你猜的)→ 留空当新增,或 read_report 拿真实 id"}
    if ctx is not None and canvas_id:
        # WP-4/B3: update path emits section_patched (was section_added — off-contract).
        # Cross-turn patch opens turn first (may have no section_added).
        await _emit_section_patched(ctx, canvas_id, sec, open_turn=_cross_turn)
    if _cross_turn:
        logger.info(
            "write_report.cross_turn_patch turn=%s section=%s from_turn=%s writer=%s rev=%s",
            turn_id,
            section_id,
            getattr(_old_sec, "turn_id", None),
            writer,
            sec.rev,
        )
    # Review fix: return actual owner column (cross-turn keeps owner; ledger records current writer), never falsely writer.
    return {"ok": True, "section_id": sec.id, "seq": sec.seq, "rev": sec.rev, "owner": getattr(sec, "owner", None) or writer}


_CHAPTER_TITLE_MAX = 120


async def _update_chapter_title(
    canvas_store: Any, ctx: Any, *, canvas_id: str, old_sec: Any, content: Any, expected_rev: Any, writer: str, turn_id: str | None
) -> dict[str, Any]:
    """canvas-update-turn 批 B2-7：就地改**上一轮 plan 章的标题**（h1 + chapter meta）。

    116 dogfood（1.3.49，`s_18c8b86f3003`）：整份改成 1-6 月后封面标题换了、章标题还是「2026年1-9月…」——写手对 h1 只拿到
    `kind_not_editable`，收尾器只管封面，时间窗又不是数字 token（残留扫描看不见），没有任何一环会动它。
    h1 渲染读 `payload.text`：这里只写这一个键，**不进正文的 bake**（bake 会把首行 `# 标题` 当文档标题剥掉、
    还会铸 [N]）。闸与 `_do_update` 的跨轮放行同一套：系统写手、修订范围、未折叠、rev CAS。"""
    rev = _coerce_rev(expected_rev)
    if rev is None:
        return {"error": "rev_required", "hint": "更新必须带整数 expected_rev;先 read_report 拿当前 rev"}
    section_id = str(old_sec.id)
    if peer_trust_class(get_current_peer_name()) not in WRITE_TRUST_CLASSES:
        return {"error": "kind_not_editable", "section_id": section_id, "kind": "h1", "hint": "上一轮的章标题只有系统写手可改"}
    from bowhead_core.canvas_revise import out_of_revise_scope

    if out_of_revise_scope(getattr(ctx, "parent_bus", None), section_id):
        return {
            "error": "out_of_revise_scope",
            "section_id": section_id,
            "hint": "用户这轮只让改点名的章节，这个章标题在范围外，不改。",
        }
    lines = [ln.strip() for ln in str(content or "").splitlines() if ln.strip()]
    if len(lines) > 1:
        # Review P1: entire chapter body sent to heading ID; saving first line and returning ok falsely implies body changed.
        return {
            "error": "chapter_title_only",
            "section_id": section_id,
            "hint": "这个 id 是章标题，content 只能写新标题这一行；这一章的正文请按 outline 里该章的 narrative 段 id 分别改。",
        }
    title = (lines[0] if lines else "").lstrip("#").strip().strip("*").strip()[:_CHAPTER_TITLE_MAX]
    if not title:
        return {"error": "empty_title", "section_id": section_id, "hint": "章标题的 content 只写新标题这一行，不能为空"}
    if "[" in title:
        # Title bypasses bake: [cite:N] / [figure:…] / links render unchanged in heading.
        return {"error": "title_has_markup", "section_id": section_id, "hint": "章标题只写纯文字，不带 [N]、[figure:…] 或链接"}
    try:
        sec = await canvas_store.patch_section(
            section_id=section_id,
            patch={"text": title},
            expected_rev=rev,
            by_turn_id=turn_id,
            caller=writer,
            skip_owner_check=True,
            canvas_id=canvas_id,
            reject_superseded=True,
        )
    except CanvasRevConflictError as e:
        return {"error": "rev_conflict", "current_rev": e.current_rev}
    except CanvasSectionSupersededError:
        return {"error": "section_superseded", "section_id": section_id, "hint": "这段已被折叠进 History，不可改"}
    if sec is None:
        return {"error": "section_not_found", "section_id": section_id, "hint": "这个 id 不存在(多半你猜的)→ read_report 拿真实 id"}
    if ctx is not None:
        await _emit_section_patched(ctx, canvas_id, sec, open_turn=True)
    logger.info("write_report.chapter_title turn=%s section=%s from_turn=%s rev=%s", turn_id, section_id, getattr(old_sec, "turn_id", None), sec.rev)
    res = {"ok": True, "section_id": sec.id, "seq": sec.seq, "rev": sec.rev, "kind": "h1", "title": title}
    _note_write_ok(ctx, writer, res)  # Review P2: same this-turn successful-write ledger as body.
    return res


# ─────────────────────────────────────────────────────────────
# canvas-elements step 2: persist handwritten body by chapter (tech-design-chapter-split.md).
# ─────────────────────────────────────────────────────────────


def _split_for_write(kind: str, content: str) -> list[Any] | None:
    """开关开且是 narrative 时按 `##` 切章；否则 None（= 今天：整篇一段）。
    切完只有一个导语（没有章标题）也返回 None——与不切逐字节同形，走老路径。"""
    if kind != "narrative":
        return None
    from bowhead_core.report_chapters import split_chapters

    chapters = split_chapters(content)
    if len(chapters) == 1 and chapters[0].is_lead:
        return None
    return chapters


def _last_body_section_id(existing: list[Any]) -> str | None:
    """文档最后一段**正文**（导语 / 章）的 id：新一轮「加一章」要插在它后面，而不是排在上一轮的
    sources / 建议 / caveats 外壳之后（dogfood §5.3：第五章落在了「建议」和两段 caveats 后面）。"""
    last: str | None = None
    for s in existing:
        if getattr(s, "superseded", False) or getattr(s, "kind", "") != "narrative":
            continue
        pl = getattr(s, "payload", None) or {}
        if isinstance(pl, dict) and pl.get("style") in ("executive", "insight"):
            last = s.id
    return last


def _chapter_payloads(chapters: list[Any], base: dict[str, Any], baked_unv: list[str] | None) -> list[dict[str, Any]]:
    """各章的 section payload：导语 style=executive（报告判定信号不变）、章 style=insight
    （与 composer 路「一洞察一段」同一 style）；未验证数字按「值在哪章」分配；source_refs 整份继承。"""
    from bowhead_core.report_chapters import assign_unverified

    per_unv = assign_unverified(chapters, baked_unv) if baked_unv is not None else None
    out: list[dict[str, Any]] = []
    for i, ch in enumerate(chapters):
        # ⚠ Strip **both** keys, or each chapter carries full-document denominator, counted N times.
        pl = {k: v for k, v in base.items() if k not in ("markdown", "style", "unverified_numbers", "unverified_total")}
        pl["markdown"] = ch.markdown
        pl["style"] = "executive" if ch.is_lead else "insight"
        if ch.title:
            pl["_meta"] = {**(pl.get("_meta") or {}), "chapter_title": ch.title}
        if per_unv is not None:
            pl["unverified_numbers"] = [str(x)[:60] for x in per_unv[i] if isinstance(x, str) and x.strip()][:20]
        out.append(pl)
    return out


async def _append_chapters_after(
    canvas_store: Any,
    ctx: Any,
    canvas_id: str,
    turn_id: str | None,
    writer: str,
    kind: str,
    payloads: list[dict[str, Any]],
    *,
    after_section_id: str,
) -> list[str]:
    """把几章 append 到 `after_section_id` 之后（append 再 reorder：store 没有 insert_at）。"""
    ids: list[str] = []
    for pl in payloads:
        sec = await canvas_store.append_section(canvas_id=canvas_id, kind=kind, payload=pl, by_turn_id=turn_id, owner=writer)
        ids.append(sec.id)
        await _emit_section_added(ctx, canvas_id, sec)
    if not ids:
        return ids
    try:
        sections = await canvas_store.list_sections(canvas_id)
        order = [s.id for s in sections if s.id not in set(ids)]
        if after_section_id in order:
            at = order.index(after_section_id) + 1
            order = order[:at] + ids + order[at:]
            await canvas_store.reorder_sections(canvas_id=canvas_id, ordered_ids=order, by_turn_id=turn_id or MACHINE_REORDER_TURN_ID)
    except Exception:
        logger.warning("write_report.chapter_reorder_failed canvas=%s", canvas_id, exc_info=True)
    return ids


async def _reconcile_chapters(
    canvas_store: Any,
    ctx: Any,
    canvas_id: str,
    turn_id: str | None,
    writer: str,
    kind: str,
    existing: list[Any],
    chapters: list[Any],
    base_payload: dict[str, Any],
    baked_unv: list[str] | None,
) -> dict[str, Any]:
    """续写守卫的「整篇重投」（§2.3）：新 body 也切章，按**标题归一化**匹配已有章 → patch；
    没匹配上的新章 append 在本写手最后一章之后；旧章在新 body 里不见了**不删**（M-D：
    宁可少写不冒丢内容的险）；新 body 没有 `##` 而旧的有 → 只重写导语。"""
    from bowhead_core.report_chapters import normalize_title

    payloads = _chapter_payloads(chapters, base_payload, baked_unv)
    ex_by_title: dict[str, Any] = {}
    ex_lead = None
    for s in existing:
        pl = getattr(s, "payload", None) or {}
        meta = pl.get("_meta") if isinstance(pl, dict) else None
        t = normalize_title(str((meta or {}).get("chapter_title") or ""))
        if isinstance(pl, dict) and pl.get("style") == "executive" and ex_lead is None:
            ex_lead = s
        elif t:
            ex_by_title.setdefault(t, s)
    touched: list[str] = []
    new_chapters: list[dict[str, Any]] = []
    for ch, pl in zip(chapters, payloads, strict=False):
        target = ex_lead or existing[0] if ch.is_lead else ex_by_title.get(normalize_title(ch.title))
        if target is None:
            new_chapters.append(pl)
            continue
        res = await _do_update(canvas_store, target.id, pl, target.rev, writer, turn_id, ctx=ctx, canvas_id=canvas_id)
        if isinstance(res, dict) and not res.get("ok"):
            return res
        touched.append(target.id)
    appended = (
        await _append_chapters_after(canvas_store, ctx, canvas_id, turn_id, writer, kind, new_chapters, after_section_id=existing[-1].id)
        if new_chapters
        else []
    )
    all_ids = [s.id for s in existing] + appended
    head = touched[0] if touched else (appended[0] if appended else existing[0].id)
    return {
        "ok": True,
        "section_id": head,
        "section_ids": all_ids,
        "updated": touched,
        "appended": appended,
        "chapters": len(payloads),
        "owner": writer,
        # B2-2: old text recommended canvas_delete_section, unavailable to compute (canvas-writer only): unreachable tool.
        "hint": "整篇重投已按章合并：同标题的章就地更新，新章追加在末尾，旧章保留、不会自动删除。",
    }


async def _write_report(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    reject_unclassified_direct_mutation("report.write_report")
    # The owned Plan publishes this decision before dispatch. Direct report
    # authorship must obey it just as eager and final composition already do.
    # This guard is specific to report prose; requested charts/files retain
    # their existing Tools and delivery contracts.
    if getattr(getattr(ctx, "parent_bus", None), "_d6_plan_deliverable_kind", None) == "answer":
        return {
            "ok": False,
            "error": "plan_answer_only",
            "hint": "This Plan delivers a chat answer. Return findings through subagent_return instead of creating or editing report sections.",
        }
    content = args.get("content")
    if not content or not isinstance(content, str):
        return {"error": "content 必填(markdown 字符串)"}
    kind = args.get("kind") or "narrative"  # handler-applied default (schema default is inert)
    section_id = args.get("section_id") or None
    expected_rev = args.get("expected_rev")
    source_refs = args.get("source_refs") or []
    after_section_id = str(args.get("after_section_id") or "").strip() or None

    canvas_store = getattr(ctx, "canvas_store", None)
    session_id = getattr(ctx, "session_id", None)
    turn_id = getattr(ctx, "turn_id", None)
    if canvas_store is None or not session_id:
        return {"ok": True, "stub": True, "section_id": "stub_sec", "kind": kind}

    writer = _resolve_writer_id(ctx)
    if kind not in _WRITE_KINDS:
        # T7 review P2: off-enum kind misses continuation guard (writer,kind),
        # leaving premade draft slot unpatched + separate off-kind body (two chapters, same owner);
        # wrote_section=True also disables direct fallback. **Normalize only writers holding unconsumed T7 slots**;
        # ordinary writers deliberately choosing another kind retain permissive old behavior (handler-P0
        # pins no hijacking deliberate different-kind append). Unregister slot after first successful
        # narrative write (below); normalization automatically ceases.
        _pending = getattr(getattr(ctx, "parent_bus", None), "_t7_slot_pending", None)
        if isinstance(_pending, set) and (writer, "narrative") in _pending:
            logger.info("write_report: off-enum kind %r → narrative (T7 pending slot)", kind)
            kind = "narrative"
    canvas = await canvas_store.ensure_canvas(
        session_id=session_id,
        workspace_id=getattr(ctx, "workspace_id", None) or "ws_unknown",
        user_id=getattr(ctx, "user_id", None) or "u_unknown",
    )

    # figure-id validation against the real figure pool (re-review F9 redux guard).
    existing = await canvas_store.list_sections(canvas.id)
    # Batch B1-2: folded figures cannot be referenced (same flag; off unchanged).
    from bowhead_core.canvas_revise import section_revise_enabled as _fig_revise_on

    _fold_hides_figs = _fig_revise_on()
    fig_ids = {s.id for s in existing if s.kind in _FIGURE_KINDS and not (_fold_hides_figs and getattr(s, "superseded", False))}
    # D6 hides every section of a chat-delivered turn, but those figures are
    # still real sections of this canvas. A later report that anchors them by
    # id ("turn the analysis above into a report, with charts") must get them
    # back under the same id. 116 `t_6846eb7b274a` rejected four such anchors
    # as "unknown" three times, then redrew the same charts. Superseded figures
    # stay unreferenceable; they are collected only to explain the rejection.
    _all_sections = await canvas_store.list_sections(canvas.id, include_hidden=True)
    promotable_fig_ids = {s.id for s in chat_hidden_figures(_all_sections, fig_ids)}
    superseded_fig_ids = {s.id for s in _all_sections if s.kind in _FIGURE_KINDS and getattr(s, "superseded", False)}
    referenceable_fig_ids = fig_ids | promotable_fig_ids
    _last_body = _last_body_section_id(existing)
    # Dogfood §5.5: schema already had after_section_id, handler never read it (dead parameter); inserting chapter between 1 and 2
    # only appended at end while Lead claimed inserted. Now read it: existing ID inserts after; absent uses default plus hint.
    _after_ok = bool(after_section_id) and any(s.id == after_section_id and not getattr(s, "superseded", False) for s in existing)
    _after_hint = (
        None if (_after_ok or not after_section_id) else "after_section_id 不存在(多半你猜的),已按默认位置落库;要挪位置先 read_report 拿真实 id。"
    )
    if after_section_id:
        logger.info("write_report.after_section_id canvas=%s after=%s found=%s", canvas.id, after_section_id, _after_ok)

    # canvas-update-turn batch B0 (review fix): explicit update fetches target to detect cross-turn; bake must
    # avoid minting current-turn [N] (turn-scoped; reader uses section turn / first document source section, so prior-turn
    # insertion cites wrong sources; same reason canvas_patch_section strips cross-turn); pass fetched target to _do_update, no refetch.
    _old_sec: Any = None
    _cross_turn_bake = False
    if section_id is not None:
        from bowhead_core.canvas_revise import is_cross_turn_editable as _xt_editable
        from bowhead_core.canvas_revise import section_revise_enabled as _revise_on_fn

        with contextlib.suppress(Exception):
            _all = await canvas_store.list_sections(canvas.id, include_hidden=True)
            _old_sec = next((s for s in _all if s.id == section_id), None)
        _cross_turn_bake = bool(_revise_on_fn() and _old_sec is not None and _xt_editable(_old_sec, turn_id))
        from bowhead_core.canvas_revise import is_chapter_title as _is_ch_title

        if _cross_turn_bake and _is_ch_title(_old_sec):
            # Batch B2-7: prior-turn chapter title writes payload.text only, no body bake (see _update_chapter_title).
            with contextlib.suppress(Exception):
                ctx._pending_authored_title = None
            return await _update_chapter_title(
                canvas_store, ctx, canvas_id=canvas.id, old_sec=_old_sec, content=content, expected_rev=expected_rev, writer=writer, turn_id=turn_id
            )

    # delivery-surface M-B: guards move into pen; provenance/antiforgery pipeline (strip / [cite:N] resolution /
    # [N] minting + numeric checks / bare-ID rewrite / figure-anchor stripping) runs uniformly on write path for
    # every holder (writer-loop / peer / direct). Previously post-LLM writer-loop seam held pipeline,
    # so passing pen to another agent lost all guards (tech-design §13.4). Internal best effort;
    # extra outer defense: bake crash saves original (fail-open + loud logs), never loses write.
    _baked_unv: list[str] | None = None
    _bake_stats: dict[str, Any] = {}
    # Title fallback (canvas-elements tech-design-mixed-turn-chrome §2.3): bake strips first-line # title;
    # attach candidate to ctx first, **successful write only** moves it to turn bus via _note_write_ok (review item 5:
    # previously first-wins before bake, rejected write pinned false title that later successful write could not replace).
    # Reset each call (append sets candidate, update clears); no stale previous candidate.
    with contextlib.suppress(Exception):
        from bowhead_core.report_writer_loop import leading_h1_title

        ctx._pending_authored_title = leading_h1_title(content) if section_id is None else None
    # canvas-update-turn batch B2-6 (116 dogfood 1.3.46: write_report.figure_anchors written=2 stripped=2 invalid_ids=['15','17'],
    # ['26','27']): body uses this-run ν1.5 simple integer IDs, not sec_*; bake strips all as invalid figure IDs,
    # leaving figures unattached to body. Chat already translates using **this run's own table** at close (resolve_run_figure_anchors); report missed it.
    # Same translation entry: figure kinds only (data/doc unchanged for downstream stripping); unresolved stay for bake.
    # Read **this run's own** ContextVar; ctx._ν15_allowlist is parallel-sibling shared last-wins attribute,
    # using it binds A's IDs via B's table to B's figures. If unavailable, no translation (bake strips), no shared-attr fallback.
    from bowhead_core.agents._lead_context import get_current_ref_allowlist

    _nu_al = get_current_ref_allowlist()
    if _nu_al is not None and "[figure:" in (content or ""):
        with contextlib.suppress(Exception):
            content = _nu_al.translate_figure_markers(content)
    # KB answer citations batch 3: doc-kind chunk IDs likewise translate through own run table to [ref:<chunk_id>], registered by bake
    # as [N]. Previously untranslated here, then strip_legacy removed every [ref:…]; peer instructed KB citations
    # all disappeared from report body (chat translates at run exit, kb_citations.translate_doc_refs).
    if _nu_al is not None and "[ref:" in (content or ""):
        with contextlib.suppress(Exception):
            from bowhead_core.kb_citations import translate_doc_refs

            content = translate_doc_refs(content, _nu_al)
    try:
        from bowhead_core.report_writer_loop import bake_report_markdown

        content, _baked_unv = await bake_report_markdown(
            content,
            ctx=ctx,
            figure_ids=referenceable_fig_ids,
            is_update=section_id is not None,
            trace=str(turn_id or ""),
            cross_turn=_cross_turn_bake,
            stats_out=_bake_stats,
            prior_markdown=(_old_sec.payload or {}).get("markdown") if _old_sec is not None else None,
        )
    except Exception:
        logger.exception("write_report.bake_failed — landing raw content (guards degraded)")
    from bowhead_core.kb.turn_guard import recheck_turn_sources

    sources_allowed, source_notice = await recheck_turn_sources(ctx)
    if not sources_allowed:
        return {"ok": False, "error": "kb_source_unavailable", "message": source_notice}
    if source_notice:
        content = f"> {source_notice}\n\n{content}"
    if _cross_turn_bake and _baked_unv is not None:
        if not _bake_stats.get("authorized"):
            # Wording-only revision (zero this-turn tool results) lacks truth values: never erase prior warnings with empty list; preserve key.
            # (Tool-backed all-verified [] still writes: this turn actually verified away old warnings.)
            _baked_unv = None
        else:
            # Review fix: cross-turn unverified numbers use only current tool results, so unchanged
            # previously verified numbers get falsely marked. Treat numbers already in old body as retained, never remark.
            # Unverified items normalize value+unit without whitespace (9.9亿); body may use 9.9 亿: strip both before compare.
            _old_md = str((_old_sec.payload or {}).get("markdown") or "") if isinstance(getattr(_old_sec, "payload", None), dict) else ""
            _old_flat = re.sub(r"\s+", "", _old_md)
            _baked_unv = [x for x in _baked_unv if not (isinstance(x, str) and x.strip() and re.sub(r"\s+", "", x) in _old_flat)]

    # Bake already strips invalid figure anchors; normally unreachable, retained for non-bake caller defense.
    # The bake-side stripper deliberately leaves fenced blocks alone (brackets
    # inside code are not references). This gate must use the same rule, or a
    # report that merely quotes `[figure:...]` in a code block is rejected
    # wholesale -- two implementations of one concept, disagreeing.
    from bowhead_core.report_writer_loop import _FENCED_BLOCK_RE

    anchored_ids = list(dict.fromkeys(_FIGURE_REF_RE.findall(_FENCED_BLOCK_RE.sub("", content))))
    bad = [fid for fid in anchored_ids if fid not in referenceable_fig_ids]
    if bad:
        # A rejection has to carry the right answer, otherwise the model can
        # only guess: the old hint named `read_report` -- a tool the writer
        # prompt tells it not to call -- and never said which ids were allowed,
        # so 116 `t_6846eb7b274a` resent the same bytes three times.
        valid = sorted(referenceable_fig_ids)
        replaced = [fid for fid in bad if fid in superseded_fig_ids]
        parts: list[str] = []
        if replaced:
            parts.append(f"{','.join(replaced)} 已被新版本替换,不能再引用。")
        if valid:
            shown = ",".join(valid[:20])
            more = f"(另有 {len(valid) - 20} 个)" if len(valid) > 20 else ""
            parts.append(f"可引用的真实图 id 只有:{shown}{more}。只能用这些 id;其余 [figure:...] 请删除后重试。")
        else:
            parts.append("本画布没有任何可引用的图表。请删除正文里所有 [figure:...] 锚后重试,不要编造 id。")
        return {"error": "unknown_figure_ref", "bad_ids": bad, "valid_ids": valid, "replaced_ids": replaced, "hint": "".join(parts)}

    promoted = [fid for fid in anchored_ids if fid in promotable_fig_ids]
    if promoted:
        await _promote_chat_hidden_figures(ctx, canvas_store, canvas.id, promoted)

    payload: dict[str, Any] = {"markdown": content}
    if kind == "narrative":
        payload["style"] = "executive"  # report-worthiness signal (Phase-1 hide-gate)
    if source_refs:
        payload["source_refs"] = list(source_refs)
    # citation-audit M1-3: server-computed unverified numbers (the structured
    # replacement for the amber `[num:V:U]` inline wrap)。
    # After M-B, bake computes truth inline above (no writer-loop preset ctx side channel);
    # identically named args key forgeable by LLM, always discard (SEC/A-P2-6 unchanged).
    # List (including []) writes key; empty clears stale warnings (C-P1-5/B-P2-5 unchanged).
    args.pop("_server_unverified_numbers", None)
    if _baked_unv is not None:
        # Write list/count together (apply_unverified); previously list only here
        # counted 30 unverified items as 20, falsely inflating trust percentage.
        apply_unverified(payload, list(_baked_unv))

    # explicit update path (the LLM passed a section_id) — outside the guard lock;
    # patch_section has its own owner + rev CAS.
    if section_id is not None:
        # canvas-elements step 2 (tech-design-chapter-split §2.4): target introduction/chapter; multiple ## headings in
        # content patches first chapter into target, appends remaining after target.
        _chapters_upd = _split_for_write(kind, content)
        if _chapters_upd is not None and len(_chapters_upd) > 1:
            _payloads_upd = _chapter_payloads(_chapters_upd, payload, _baked_unv)
            _res = await _do_update(
                canvas_store, section_id, _payloads_upd[0], expected_rev, writer, turn_id, ctx=ctx, canvas_id=canvas.id, old_sec=_old_sec
            )
            if isinstance(_res, dict) and _res.get("ok"):
                _extra = await _append_chapters_after(
                    canvas_store, ctx, canvas.id, turn_id, writer, kind, _payloads_upd[1:], after_section_id=section_id
                )
                _res = {**_res, "section_ids": [section_id, *_extra], "chapters": len(_payloads_upd)}
            _consume_t7_pending(ctx, writer, kind, _res)
            _note_write_ok(ctx, writer, _res)
            return _res
        _res = await _do_update(
            canvas_store,
            section_id,
            payload,
            expected_rev,
            writer,
            turn_id,
            ctx=ctx,
            canvas_id=canvas.id,
            old_sec=_old_sec,
        )
        _consume_t7_pending(ctx, writer, kind, _res)
        _note_write_ok(ctx, writer, _res)
        return _res

    # no section_id: continuation guard + append, serialized per-canvas so two
    # concurrent same-writer calls can't both miss the check-and-set → 2 sections.
    my = _my_sections_map(ctx)
    async with _guard_lock(canvas.id):
        key = (writer, kind)
        recorded_raw = my.get(key) if my is not None else None
        # Continuation value: old section_id or post-split ordered list (intro + chapters); accept both.
        recorded_ids: list[str] = [recorded_raw] if isinstance(recorded_raw, str) else list(recorded_raw or [])
        _chapters = _split_for_write(kind, content)
        if recorded_ids:
            # continuation: re-fetch the recorded sections' CURRENT rev under the
            # lock (don't trust the pre-lock `existing` snapshot).
            fresh = await canvas_store.list_sections(canvas.id)
            by_id = {s.id: s for s in fresh}
            alive = [sid for sid in recorded_ids if sid in by_id]
            if not alive:
                # recorded sections were deleted → drop stale mapping, append fresh.
                if my is not None:
                    my.pop(key, None)
            elif _chapters is not None and (len(alive) > 1 or len(_chapters) > 1):
                # canvas-elements step 2 (§2.3): whole-document resubmit reconciles chapters: same title patch, new append, old retained.
                _res = await _reconcile_chapters(
                    canvas_store, ctx, canvas.id, turn_id, writer, kind, [by_id[sid] for sid in alive], _chapters, payload, _baked_unv
                )
                if my is not None and isinstance(_res, dict) and _res.get("ok"):
                    my[key] = list(_res.get("section_ids") or alive)
                _consume_t7_pending(ctx, writer, kind, _res)
                _note_write_ok(ctx, writer, _res)
                return _res
            else:
                cur = by_id[alive[0]]
                _res = await _do_update(
                    canvas_store,
                    cur.id,
                    payload,
                    cur.rev,
                    writer,
                    turn_id,
                    ctx=ctx,
                    canvas_id=canvas.id,
                )
                _consume_t7_pending(ctx, writer, kind, _res)
                _note_write_ok(ctx, writer, _res)
                return _res
        if _chapters is not None:
            # canvas-elements step 2 (§2.2): introduction + one section per chapter, all same writer owner; return section_ids.
            # No introduction (new-turn add-chapter) with existing body ⇒ insert after last body, not behind previous shell.
            _payloads = _chapter_payloads(_chapters, payload, _baked_unv)
            _ids: list[str] = []
            _anchor = after_section_id if _after_ok else (_last_body if not _chapters[0].is_lead else None)
            if _anchor is not None:
                _ids = await _append_chapters_after(canvas_store, ctx, canvas.id, turn_id, writer, kind, _payloads, after_section_id=_anchor)
            else:
                for _pl in _payloads:
                    _sec = await canvas_store.append_section(canvas_id=canvas.id, kind=kind, payload=_pl, by_turn_id=turn_id, owner=writer)
                    _ids.append(_sec.id)
                    await _emit_section_added(ctx, canvas.id, _sec)
            _fresh = {s.id: s for s in await canvas_store.list_sections(canvas.id)}
            _first = _fresh.get(_ids[0])
            if my is not None:
                my[key] = list(_ids)
            _result = {
                "ok": True,
                "section_id": _ids[0],
                "section_ids": _ids,
                "chapters": len(_ids),
                "seq": getattr(_first, "seq", None),
                "rev": getattr(_first, "rev", 1),
                "owner": writer,
                "hint": _after_hint
                or (
                    f"正文已按 ## 分成 {len(_ids)} 段落库；后续要改某一章，read_report 里按标题找到它的 section_id 再 update。"
                    if len(_ids) > 1
                    else "这一章已落库（带章标题）；要改它就 update 这个 section_id。"
                ),
            }
            _consume_t7_pending(ctx, writer, kind, _result)
            _note_write_ok(ctx, writer, _result)
            return _result
        if _after_ok and after_section_id is not None:
            # Untitled sections also honor after_section_id (append + reorder, same helper).
            _pid = (await _append_chapters_after(canvas_store, ctx, canvas.id, turn_id, writer, kind, [payload], after_section_id=after_section_id))[
                0
            ]
            sec = next(s for s in await canvas_store.list_sections(canvas.id) if s.id == _pid)
        else:
            sec = await canvas_store.append_section(
                canvas_id=canvas.id,
                kind=kind,
                payload=payload,
                by_turn_id=turn_id,
                owner=writer,
            )
            # M1 (report-pipeline-reliability): emit the persisted section live so the
            # workspace canvas renders the narrative body DURING the turn, not only
            # after F5. write_report was the sole canvas writer that persisted silently
            # (prod t_feae6f38: narrative in DB, 0 turn_events → blank until refresh).
            await _emit_section_added(ctx, canvas.id, sec)
        if my is not None:
            my[key] = sec.id
        _result = {"ok": True, "section_id": sec.id, "seq": sec.seq, "rev": sec.rev, "owner": writer}
        if _after_hint:
            _result["hint"] = _after_hint
        _consume_t7_pending(ctx, writer, kind, _result)
        _note_write_ok(ctx, writer, _result)
        return _result
