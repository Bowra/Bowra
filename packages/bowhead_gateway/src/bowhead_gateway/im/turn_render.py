"""Event -> NeutralBlock state machine for the IM layer.

A turn's events are folded into a :class:`NeutralTurnState`, then :func:`render_blocks` turns that
state into an ordered ``list[NeutralBlock]`` the adapter renders to its card. This is a **net-new
canvas-section ingestion path** (tech-design §4.1): the trust data the report card needs (sources,
unverified numbers, section tree) rides ``canvas.section_*`` + ``turn.citation_registered`` events
the current Feishu renderer *drops* — so this machine subscribes to and accumulates them
server-side, then computes trust over the section tree (not over ``final_answer`` text).

Two halves, both pure & unit-testable:

* :func:`apply_event` — fold one ``OrchestratorEvent`` into the state.
* :func:`render_blocks` — fold the state into blocks (trust line + caveat + per-section blocks for a
  report turn; a single resolved ``final_answer`` TextBlock for a chat turn; a ``⏳`` progress line
  while the turn is live).

The bus subscription + ≤1/s throttle + ``adapter.update`` driver lives in the bridge loop (T7),
not here — keeping this module a pure fold.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bowhead_core.llm_failure import LLM_REASON_CODES
from bowhead_core.presentation_locale import resolve_presentation_locale
from bowhead_core.published_diagnostics import CHAT_TURN_FAILURE_CODES, clarification_failure_copy_code
from bowhead_core.report_trust import ReportTrustStats, compute_report_trust
from bowhead_core.source_linkage_presentation import (
    SourceLinkagePresentation,
    normalize_nonnegative_count,
    project_source_linkage,
)

from bowhead_gateway.im.blocks import (
    DEFAULT_LOCALE as _DEFAULT_LOCALE,
)
from bowhead_gateway.im.blocks import (
    Caveat,
    ErrorBlock,
    ImageBlock,
    KpiCell,
    KpiRow,
    NeutralBlock,
    ReportLifecycleLine,
    SourceEntry,
    SourceLinkageLine,
    SourcesBlock,
    TableBlock,
    TextBlock,
    resolve_markers,
)
from bowhead_gateway.im.error_messages import im_error_message, normalize_im_locale

if TYPE_CHECKING:
    from collections.abc import Callable

# Event types we fold (string constants from bowhead_core/events.py).
_TERMINAL_EVENTS = frozenset({"done", "error", "turn_failed", "turn_aborted", "turn_cancelled"})
_PROGRESS_EVENTS = frozenset({"headline", "step_started", "step_progress", "step_done"})
_OCCURRENCE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_TURN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CHAT_SAFE_DETAIL_RE = re.compile(r"^phase=chat; failure_class=(?:access|configuration|content|internal|limit|network|plan|timeout)$")
_LLM_SAFE_DETAIL_RE = re.compile(
    r"^phase=(?:stream|invoke); "
    r"failure_class=(?:access|rate_limit|timeout|request|response|network|upstream|interrupted); "
    r"provider_family=(?:openai_compatible|anthropic_compatible|custom|mock); "
    r"http_status=(?:none|[1-5][0-9]{2}); "
    r"stream_state=(?:not_started|pre_visible|interrupted); "
    r"fallback=(?:not_attempted|succeeded|failed); attempts=[1-9][0-9]?$"
)


@dataclass(frozen=True)
class AgentAttribution:
    agent_id: str | None = None
    agent_revision_id: str | None = None
    agent_release_id: str | None = None
    agent_execution_id: str | None = None
    display_name_snapshot: str | None = None


@dataclass
class NeutralTurnState:
    """Accumulated, channel-neutral view of a turn as its events stream in."""

    turn_id: str
    canvas_id: str | None = None
    title: str | None = None
    # section_id -> latest section payload dict; section_order preserves insertion order.
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)
    section_order: list[str] = field(default_factory=list)
    # canvas-update-turn B1-6: a revise turn patches *prior-turn* sections and re-emits
    # ``canvas.opened`` with ``revised=true``; the card then has no h1 of its own, so the title
    # is rendered as a leading "《title》updated" line. ``section_seq`` (from the top-level ``seq``
    # of section_added / section_patched) only positions sections that arrive via
    # section_patched without a prior section_added — and never after a ``canvas.reordered``
    # (``reorder_applied``), whose order is authoritative.
    revised: bool = False
    section_seq: dict[str, int] = field(default_factory=dict)
    reorder_applied: bool = False
    # citation_seq -> {artifact_id, label, kind}
    citations: dict[int, dict[str, Any]] = field(default_factory=dict)
    # peer spawn id -> exact Marketplace identity. IM does not render internal
    # IDs, but it must fold the same persisted facts as Web so reconnect and
    # future detail cards never need a second event parser.
    agent_attribution: dict[str, AgentAttribution] = field(default_factory=dict)
    # latest progress label (headline / step), shown as a ⏳ line until terminal.
    status_text: str | None = None
    # chat-mode (non-report) final answer text.
    final_text: str | None = None
    # canvas lifecycle badge (effective_status), fetched out-of-band by the bridge loop (§4.3).
    lifecycle: str | None = None
    # Authoritative report trust from canvas.done (computed at compose-time over raw sections). When
    # set, render_blocks prefers it over re-deriving trust from the stripped/lossy folded sections.
    authoritative_trust: ReportTrustStats | None = None
    # User-facing B1a projection. Each legacy count is normalized independently;
    # verified_pct is intentionally never stored here.
    authoritative_source_linkage: SourceLinkagePresentation | None = None
    terminal: bool = False
    terminal_event_type: str | None = None
    terminal_status: str | None = None
    failure_code: str | None = None
    failure_occurrence_id: str | None = None
    failure_turn_id: str | None = None
    failure_safe_detail: str | None = None
    failure_retryable: bool | None = None

    def section_list(self) -> list[dict[str, Any]]:
        """Sections in insertion order (the report tree, as dicts)."""
        return [self.sections[sid] for sid in self.section_order if sid in self.sections]


def _note_seq(state: NeutralTurnState, sid: str, seq: Any) -> None:
    if isinstance(seq, int) and not isinstance(seq, bool):
        state.section_seq[sid] = seq


def _insert_by_seq(state: NeutralTurnState, sid: str, seq: Any) -> None:
    """Insert a section that arrives first via ``section_patched``. With a usable ``seq`` and no
    ``canvas.reordered`` seen yet, it goes before the first already-placed section whose seq is
    larger; otherwise (no seq, no seq-bearing neighbours, or after a reorder) it appends."""
    if not (isinstance(seq, int) and not isinstance(seq, bool)) or state.reorder_applied:
        state.section_order.append(sid)
        return
    for idx, other in enumerate(state.section_order):
        other_seq = state.section_seq.get(other)
        if other_seq is not None and other_seq > seq:
            state.section_order.insert(idx, sid)
            return
    state.section_order.append(sid)


def apply_event(state: NeutralTurnState, event: Any) -> NeutralTurnState:
    """Fold one ``OrchestratorEvent`` (or any object with ``event_type``/``payload``) into ``state``.

    Mutates and returns ``state`` (the bridge loop reuses one state per turn). Unknown event types
    are ignored. The canvas-section events are the net-new ingestion (Feishu drops them today).
    """
    etype = getattr(event, "event_type", None) or ""
    payload = getattr(event, "payload", None)
    if not isinstance(payload, dict):
        payload = {}

    if etype in {"agent_spawned", "peer_dispatched", "lead.subagent_summary"}:
        _fold_agent_attribution(state, payload)

    if etype == "canvas.opened":
        state.canvas_id = payload.get("canvas_id") or state.canvas_id
        state.title = payload.get("title") or state.title
        if payload.get("revised") is True:
            state.revised = True
    elif etype == "canvas.section_added":
        sec = payload.get("section")
        if isinstance(sec, dict):
            sid = str(sec.get("id") or "")
            if sid:
                if sid not in state.sections:
                    state.section_order.append(sid)
                state.sections[sid] = sec
                _note_seq(state, sid, payload.get("seq"))
    elif etype == "canvas.section_done":
        # MERGE the final payload onto the section_added record — do NOT replace. The section_done
        # inner ``payload`` carries the section's fields (markdown/cells/entries) but often WITHOUT
        # id/kind (some emit paths only put kind at the event top-level, or omit it entirely —
        # canvas_streaming.py / canvas_composers/streaming.py). Replacing would drop the id+kind that
        # section_added stored (to_report_section), breaking trust classification. Verified against
        # the real emit sites.
        sid = str(payload.get("section_id") or "")
        if sid:
            merged = dict(state.sections.get(sid, {}))
            final = payload.get("payload")
            if isinstance(final, dict):
                merged.update(final)
            merged.setdefault("id", sid)
            top_kind = payload.get("kind")
            if top_kind and not merged.get("kind"):
                merged["kind"] = top_kind
            if sid not in state.sections:
                state.section_order.append(sid)
            state.sections[sid] = merged
    elif etype == "canvas.section_patched":
        # canvas-comprehensive WP-4 (B2/B3): write_report UPDATE + sources-repatch now
        # emit section_patched (not section_added). Without this branch the IM card silently
        # stops reflecting report updates / [N] source-number fixes.
        # `patch` carries the FULL section (late-join safe) → merge onto existing, insert
        # if new. Skip ok=False (failure pill — no data). Mirrors the section_done merge.
        if payload.get("ok") is not False:
            sid = str(payload.get("section_id") or "")
            if sid:
                patch = payload.get("patch")
                merged = dict(state.sections.get(sid, {}))
                if isinstance(patch, dict):
                    merged.update(patch)
                merged.setdefault("id", sid)
                top_kind = payload.get("kind")
                if top_kind and not merged.get("kind"):
                    merged["kind"] = top_kind
                if sid not in state.sections:
                    # B1-6: a prior-turn section patched by a revise turn was never section_added
                    # on this card — place it by canvas seq among the seq-bearing sections already
                    # here (arrival order is meaningless for it). Ordinary reports never take this
                    # branch (every section is section_added first), so their order is unchanged.
                    _insert_by_seq(state, sid, payload.get("seq"))
                state.sections[sid] = merged
                _note_seq(state, sid, payload.get("seq"))
    elif etype == "canvas.section_removed":
        sid = str(payload.get("section_id") or "")
        if sid in state.sections:
            del state.sections[sid]
            state.section_order = [s for s in state.section_order if s != sid]
    elif etype == "canvas.reordered":
        order = payload.get("order")
        if isinstance(order, list):
            ordered = [str(s) for s in order if str(s) in state.sections]
            # keep any sections not named in the reorder (defensive) appended in old order.
            state.section_order = ordered + [s for s in state.section_order if s not in ordered]
            state.reorder_applied = True
    elif etype == "canvas.done":
        # Keep a strict per-field user-facing projection even if one sibling is
        # malformed. The compatibility ReportTrustStats is only retained when
        # its complete four-field shape is valid; it is never a UI input.
        trust = payload.get("trust")
        if isinstance(trust, dict):
            state.authoritative_source_linkage = project_source_linkage(trust)
            state.authoritative_trust = _strict_legacy_trust(trust)
    elif etype == "turn.citation_registered":
        seq = payload.get("citation_seq")
        if isinstance(seq, int):
            # IM has no current per-source authorization proof. Keep only the
            # ordinal and a non-business kind; raw ID/label must not enter a
            # later provider payload accidentally.
            # KB-chunk exception: retain file ID in a private key never rendered, so refresh_kb_citation_labels can
            # check recipient read permission before assigning a displayable filename; never trust the registered label.
            entry: dict[str, Any] = {"kind": payload.get("kind")}
            if payload.get("kind") == "kb_chunk":
                from bowhead_core.kb_citations import parse_kb_citation_identity

                parsed = parse_kb_citation_identity(payload.get("artifact_id"))
                if parsed is not None:
                    entry["_kb_file_id"] = parsed[0]
            state.citations[seq] = entry
    elif etype == "final_answer":
        text = payload.get("text")
        if isinstance(text, str):
            state.final_text = text
    elif etype in _PROGRESS_EVENTS:
        label = payload.get("title") or payload.get("label") or payload.get("text") or payload.get("name")
        if isinstance(label, str) and label.strip():
            state.status_text = label.strip()
    elif etype in _TERMINAL_EVENTS:
        state.terminal = True
        state.terminal_event_type = etype
        state.terminal_status = str(payload.get("status") or payload.get("reason") or etype)
        if etype not in {"done", "turn_aborted", "turn_cancelled"}:
            _fold_terminal_failure(state, payload)

    return state


async def refresh_kb_citation_labels(state: NeutralTurnState, app: Any, *, private_chat: bool) -> None:
    """知识库引用的来源名：以本轮会话所属用户（一对一会话里就是接收人）为看的人判读权限。

    群聊无法按「每个接收人」判定 ⇒ 一律受限（不写 label）。任何失败同样按受限处理（fail-closed）。
    """
    pending = {seq: c for seq, c in state.citations.items() if c.get("kind") == "kb_chunk" and c.get("_kb_file_id")}
    for c in pending.values():
        c.pop("label", None)
    if not pending or not private_chat:
        return
    try:
        from bowhead_gateway.kb_citation_access import kb_file_access_for_user

        session_store = getattr(app.state, "session_store", None)
        user_store = getattr(app.state, "user_store", None)
        turn = await session_store.get_turn(state.turn_id)
        session = await session_store.get_session(turn.session_id)
        user = await user_store.get_user(session.user_id)
        check = kb_file_access_for_user(app, user)
        if check is None:
            return
        for c in pending.values():
            access = await check(str(c["_kb_file_id"]))
            if access.state == "readable" and access.file_name:
                c["label"] = access.file_name
    except Exception:
        for c in pending.values():
            c.pop("label", None)


def kb_sources_block(state: NeutralTurnState, locale: str) -> SourcesBlock | None:
    """只列终答正文里真的出现的知识库引用：登记表里可能有被弃用的文本登记过的条目
    （编造闸把散文换成 exec_summary 时，第一次登记的仍会 flush），列出来就是没被引用的来源。"""
    cited = {int(n) for n in re.findall(r"\[cite:([0-9]{1,3})\]", state.final_text or "")}
    entries = tuple(
        SourceEntry(n=seq, kb=True, label=c.get("label") if isinstance(c.get("label"), str) else None)
        for seq, c in sorted(state.citations.items())
        if c.get("kind") == "kb_chunk" and seq in cited
    )
    return SourcesBlock(entries=entries, locale=resolve_presentation_locale(locale)) if entries else None


def _fold_agent_attribution(
    state: NeutralTurnState,
    payload: dict[str, Any],
) -> None:
    peer_id = str(payload.get("spawn_id") or payload.get("subagent_id") or "").strip()
    if not peer_id:
        return

    def read(key: str) -> str | None:
        value = payload.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else None

    incoming = AgentAttribution(
        agent_id=read("agent_id"),
        agent_revision_id=read("agent_revision_id"),
        agent_release_id=read("agent_release_id"),
        agent_execution_id=read("agent_execution_id"),
        display_name_snapshot=read("display_name_snapshot"),
    )
    if not any(
        (
            incoming.agent_id,
            incoming.agent_revision_id,
            incoming.agent_release_id,
            incoming.agent_execution_id,
            incoming.display_name_snapshot,
        )
    ):
        return
    prior = state.agent_attribution.get(peer_id, AgentAttribution())
    state.agent_attribution[peer_id] = AgentAttribution(
        agent_id=prior.agent_id or incoming.agent_id,
        agent_revision_id=prior.agent_revision_id or incoming.agent_revision_id,
        agent_release_id=prior.agent_release_id or incoming.agent_release_id,
        agent_execution_id=prior.agent_execution_id or incoming.agent_execution_id,
        display_name_snapshot=(prior.display_name_snapshot or incoming.display_name_snapshot),
    )


async def refresh_report_lifecycle(state: NeutralTurnState, canvas_store: Any) -> None:
    """Best-effort finite lifecycle projection for IM; unknown stays hidden."""

    if not state.canvas_id or canvas_store is None:
        return
    try:
        canvas = await canvas_store.get_canvas(state.canvas_id)
        if canvas is None:
            return
        quarantined = await canvas_store.count_quarantined(state.canvas_id)
        if quarantined > 0:
            state.lifecycle = "quarantined_stale"
        elif canvas.status in {"active", "verified", "stale", "archived"}:
            state.lifecycle = canvas.status
        else:
            state.lifecycle = None
    except Exception:
        state.lifecycle = None


def _strict_legacy_trust(raw: dict[str, Any]) -> ReportTrustStats | None:
    verified = normalize_nonnegative_count(raw.get("verified"))
    unverified = normalize_nonnegative_count(raw.get("unverified"))
    sources = normalize_nonnegative_count(raw.get("sources"))
    pct_raw = raw.get("verified_pct")
    verified_pct = None if pct_raw is None else normalize_nonnegative_count(pct_raw)
    if verified is None or unverified is None or sources is None:
        return None
    if pct_raw is not None and (verified_pct is None or verified_pct > 100):
        return None
    return ReportTrustStats(
        verified=verified,
        unverified=unverified,
        sources=sources,
        verified_pct=verified_pct,
    )


def _fold_terminal_failure(
    state: NeutralTurnState,
    payload: dict[str, Any],
) -> None:
    """Project a terminal event onto finite IM facts; never retain event prose."""

    candidate = payload.get("reason_code") or payload.get("reason")
    allowed_failure_codes = CHAT_TURN_FAILURE_CODES | LLM_REASON_CODES
    state.failure_code = candidate if isinstance(candidate, str) and candidate in allowed_failure_codes else "lead_internal_error"

    diagnostics = payload.get("diagnostics")
    if not isinstance(diagnostics, dict):
        return
    diagnostic_code = diagnostics.get("diagnostic_code")
    if (
        diagnostics.get("schema_version") != 1
        or diagnostics.get("disclosure") != "authenticated"
        or diagnostic_code not in {"chat_turn_failed", "chat_llm_failed"}
    ):
        return
    diag_turn_id = diagnostics.get("turn_id")
    if not isinstance(diag_turn_id, str) or diag_turn_id != state.turn_id or not _TURN_ID_RE.fullmatch(diag_turn_id):
        return
    occurrence_id = diagnostics.get("occurrence_id")
    if isinstance(occurrence_id, str) and _OCCURRENCE_ID_RE.fullmatch(occurrence_id):
        state.failure_occurrence_id = occurrence_id
    state.failure_turn_id = diag_turn_id
    safe_detail = diagnostics.get("safe_detail")
    safe_detail_pattern = _LLM_SAFE_DETAIL_RE if diagnostic_code == "chat_llm_failed" else _CHAT_SAFE_DETAIL_RE
    known_clarification_stage = clarification_failure_copy_code(state.failure_code, diagnostics) != state.failure_code
    if isinstance(safe_detail, str) and (safe_detail_pattern.fullmatch(safe_detail) or known_clarification_stage):
        state.failure_safe_detail = safe_detail
    retryable = diagnostics.get("retryable")
    if isinstance(retryable, bool):
        state.failure_retryable = retryable


def _terminal_error_block(
    state: NeutralTurnState,
    *,
    locale: str,
) -> ErrorBlock | None:
    if not state.terminal or state.terminal_event_type in {
        "done",
        "turn_aborted",
        "turn_cancelled",
    }:
        return None
    code = state.failure_code or "lead_internal_error"
    return ErrorBlock(
        message=im_error_message(
            code,
            locale,
            diagnostics={
                "schema_version": 1,
                "disclosure": "authenticated",
                "diagnostic_code": "chat_turn_failed",
                "safe_detail": state.failure_safe_detail,
            },
        ),
        code=code,
        locale=normalize_im_locale(locale),
        occurrence_id=state.failure_occurrence_id,
        turn_id=state.failure_turn_id or (state.turn_id if _TURN_ID_RE.fullmatch(state.turn_id) else None),
        safe_detail=state.failure_safe_detail,
        retryable=state.failure_retryable,
    )


def _terminal_cancelled_block(
    state: NeutralTurnState,
    *,
    locale: str,
) -> TextBlock | None:
    if state.terminal_event_type not in {"turn_aborted", "turn_cancelled"}:
        return None
    return TextBlock(markdown=im_error_message("user_cancel", locale))


# ───────────────────────── state -> blocks ─────────────────────────


def render_blocks(
    state: NeutralTurnState,
    *,
    locale: str = _DEFAULT_LOCALE,
    image_url_for: Callable[[str], str | None] | None = None,
) -> list[NeutralBlock]:
    """Fold the accumulated state into the ordered blocks the adapter renders.

    * Live (non-terminal): a leading ``⏳`` progress line from ``status_text``.
    * Report turn (has sections): a :class:`SourceLinkageLine` when positive facts exist, an
      independent lifecycle line, an aggregated :class:`Caveat` of every
      ``narrative.unverified_numbers``, then one-or-more
      blocks per section in order.
    * Chat turn (no sections): a single :class:`TextBlock` of the marker-resolved ``final_text``.

    ``image_url_for`` resolves a section ``artifact_id`` to a fetchable URL (S3 presigned / ``?token``
    — §7); when absent, image/chart sections are skipped (the URL is adapter-resolved at send time).
    """
    selected_locale = resolve_presentation_locale(locale)
    blocks: list[NeutralBlock] = []

    if not state.terminal and state.status_text:
        blocks.append(TextBlock(markdown=f"⏳ {state.status_text}"))

    sections = state.section_list()
    if sections:
        # B1-6: on a revise turn the card carries no h1 of its own — name the report that changed
        # FIRST, above the trust/lifecycle lines, so the reader knows what these fragments belong to.
        revised_line = _revised_title_line(state, sections, locale=selected_locale)
        if revised_line is not None:
            blocks.append(revised_line)
        # Prefer the strict authoritative per-field projection. Older events
        # without a trust payload fall back to legacy calculation, then cross
        # the same B1a presentation boundary. A malformed authoritative payload
        # does not silently regain certainty from the lossy IM fold.
        presentation = state.authoritative_source_linkage
        if presentation is None:
            stats = compute_report_trust(sections)
            presentation = project_source_linkage({"verified": stats.verified, "unverified": stats.unverified, "sources": stats.sources})
        if presentation.visible:
            blocks.append(SourceLinkageLine(presentation=presentation, locale=selected_locale))
        if state.lifecycle in {"active", "verified", "stale", "archived", "quarantined_stale"}:
            blocks.append(
                ReportLifecycleLine(
                    effective_status=state.lifecycle,  # type: ignore[arg-type]
                    locale=selected_locale,
                )
            )
        caveat_items: list[str] = []
        for sec in sections:
            if sec.get("kind") == "narrative":
                un = sec.get("unverified_numbers")
                if isinstance(un, list):
                    caveat_items.extend(resolve_markers(str(x), locale=locale) for x in un if str(x).strip())
        if caveat_items:
            blocks.append(Caveat(items=tuple(caveat_items), locale=selected_locale))
        for sec in sections:
            blocks.extend(_section_to_blocks(sec, locale=locale, image_url_for=image_url_for))
        terminal_error = _terminal_error_block(state, locale=locale)
        if terminal_error is not None:
            blocks.append(terminal_error)
        cancelled = _terminal_cancelled_block(state, locale=locale)
        if cancelled is not None:
            blocks.append(cancelled)
        return blocks

    if state.final_text:
        blocks.append(TextBlock(markdown=resolve_markers(state.final_text, locale=locale)))
        kb_sources = kb_sources_block(state, locale)
        if kb_sources is not None:
            blocks.append(kb_sources)
    terminal_error = _terminal_error_block(state, locale=locale)
    if terminal_error is not None:
        blocks.append(terminal_error)
    cancelled = _terminal_cancelled_block(state, locale=locale)
    if cancelled is not None:
        blocks.append(cancelled)
    return blocks


def _revised_title_line(state: NeutralTurnState, sections: list[dict[str, Any]], *, locale: str) -> TextBlock | None:
    """B1-6: a revise turn's card carries no h1 of its own — say which report was updated."""
    if not state.revised or not (state.title or "").strip():
        return None
    if any(str(sec.get("kind") or "") == "h1" for sec in sections):
        return None
    title = state.title.strip()  # type: ignore[union-attr]
    text = {
        "zh-CN": f"**《{title}》已更新**",
        "en-US": f"**“{title}” updated**",
        "de-DE": f"**„{title}“ wurde aktualisiert**",
        "fr-FR": f"**« {title} » a été mis à jour**",
    }[resolve_presentation_locale(locale)]
    return TextBlock(markdown=text)


def _section_to_blocks(
    sec: dict[str, Any],
    *,
    locale: str,
    image_url_for: Callable[[str], str | None] | None,
) -> list[NeutralBlock]:
    """Map one canvas section payload to zero-or-more neutral blocks.

    Handles the text/structured kinds an IM card can carry; canvas-rich widgets (charts/iframes/
    design/code) are skipped here — charts become an :class:`ImageBlock` once server-side PNG
    rendering lands (T6/§7). A text-bearing unknown kind falls back to its ``markdown``/``text``.
    """
    kind = sec.get("kind")

    if kind == "narrative":
        md = resolve_markers(str(sec.get("markdown") or ""), locale=locale)
        return [TextBlock(markdown=md)] if md.strip() else []

    if kind in ("h1", "eyebrow"):
        text = str(sec.get("text") or "").strip()
        if not text:
            return []
        return [TextBlock(markdown=f"# {text}" if kind == "h1" else text)]

    if kind == "section":
        # The streaming narrator emits h2/h3 headings as kind="section" with the title under a
        # dynamic h1/h2/h3 key (flat, no body) — without this branch every report heading was
        # silently dropped, collapsing the card into one wall of narrative (review P1). A nested
        # `body` list (if ever present) is rendered recursively.
        heading = str(sec.get("h1") or sec.get("h2") or sec.get("h3") or sec.get("label") or "").strip()
        out: list[NeutralBlock] = []
        if heading:
            prefix = "# " if sec.get("h1") else "## "
            out.append(TextBlock(markdown=f"{prefix}{resolve_markers(heading, locale=locale)}"))
        body = sec.get("body")
        if isinstance(body, list):
            for child in body:
                if isinstance(child, dict):
                    out.extend(_section_to_blocks(child, locale=locale, image_url_for=image_url_for))
        return out

    if kind == "deck":
        md = resolve_markers(str(sec.get("markdown") or ""), locale=locale)
        return [TextBlock(markdown=md)] if md.strip() else []

    if kind == "meta":
        pills = sec.get("pills")
        if isinstance(pills, list) and pills:
            return [TextBlock(markdown=" · ".join(str(p) for p in pills))]
        return []

    if kind == "callout":
        label = str(sec.get("label") or "").strip()
        md = resolve_markers(str(sec.get("markdown") or ""), locale=locale)
        body = f"**{label}** {md}".strip() if label else md
        return [TextBlock(markdown=body)] if body.strip() else []

    if kind == "kpi_row":
        cells = sec.get("cells")
        if not isinstance(cells, list):
            return []
        kpi_cells = tuple(
            KpiCell(
                label=str(c.get("lbl") or ""),
                value=resolve_markers(str(c.get("val") or ""), locale=locale),
                delta=resolve_markers(str(c["sub"]), locale=locale) if c.get("sub") else None,
            )
            for c in cells
            if isinstance(c, dict)
        )
        return [KpiRow(cells=kpi_cells)] if kpi_cells else []

    if kind == "data_table":
        return _data_table_block(sec, locale=locale)

    if kind in ("recommendations", "next_list"):
        return _list_block(sec, kind, locale=locale)

    if kind == "insight_card":
        return _insight_block(sec, locale=locale)

    if kind == "sources":
        entries = sec.get("entries")
        if not isinstance(entries, list):
            return []
        out = tuple(SourceEntry(n=i + 1) for i, e in enumerate(entries) if isinstance(e, dict))
        return [SourcesBlock(entries=out, locale=resolve_presentation_locale(locale))] if out else []

    if kind == "image":
        return _image_block(sec, image_url_for=image_url_for)

    # text-bearing unknown kind → best-effort fallback so content isn't silently dropped.
    for key in ("markdown", "text", "summary"):
        val = sec.get(key)
        if isinstance(val, str) and val.strip():
            return [TextBlock(markdown=resolve_markers(val, locale=locale))]
    return []


def _cell_str(cell: Any, locale: str) -> str:
    """Stringify a TableCellValue (str | number | {delta,pos} | {bar,pos})."""
    if isinstance(cell, bool):  # guard: bool is an int subclass
        return str(cell)
    if isinstance(cell, (int, float)):
        return str(cell)
    if isinstance(cell, dict):
        if "delta" in cell:
            return str(cell.get("delta") or "")
        if "bar" in cell:
            return str(cell.get("bar") or "")
        return ""
    return resolve_markers(str(cell or ""), locale=locale)


def _data_table_block(sec: dict[str, Any], *, locale: str) -> list[NeutralBlock]:
    columns = sec.get("columns")
    rows = sec.get("rows")
    if not isinstance(columns, list) or not columns:
        return []
    cols = [c for c in columns if isinstance(c, dict)]
    headers = tuple(str(c.get("header") or c.get("key") or "") for c in cols)
    keys = [str(c.get("key") or "") for c in cols]
    out_rows: list[tuple[str, ...]] = []
    if isinstance(rows, list):
        for r in rows:
            if isinstance(r, dict):
                out_rows.append(tuple(_cell_str(r.get(k), locale) for k in keys))
    # The aggregate/total summary row (FE TableRow.totalRow), previously dropped (review P1).
    total_row = sec.get("totalRow")
    if isinstance(total_row, dict):
        out_rows.append(tuple(_cell_str(total_row.get(k), locale) for k in keys))
    meta = sec.get("meta")
    source = str(meta.get("source")) if isinstance(meta, dict) and meta.get("source") else None
    return [
        TableBlock(
            caption=str(sec.get("caption") or ""),
            columns=headers,
            rows=tuple(out_rows),
            source=source,
        )
    ]


def _list_block(sec: dict[str, Any], kind: str, *, locale: str) -> list[NeutralBlock]:
    items = sec.get("items")
    if not isinstance(items, list) or not items:
        return []
    lines: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if kind == "recommendations":
            prio = str(it.get("priority") or "")
            action = resolve_markers(str(it.get("action") or ""), locale=locale)
            prefix = f"**[{prio}]** " if prio else "- "
            lines.append(f"{prefix}{action}".strip())
        else:  # next_list
            title = resolve_markers(str(it.get("title") or ""), locale=locale)
            detail = resolve_markers(str(it.get("detail") or ""), locale=locale)
            lines.append(f"- **{title}**{(' — ' + detail) if detail else ''}".strip())
    text = "\n".join(line for line in lines if line)
    return [TextBlock(markdown=text)] if text else []


def _insight_block(sec: dict[str, Any], *, locale: str) -> list[NeutralBlock]:
    title = str(sec.get("title") or "").strip()
    body = resolve_markers(str(sec.get("narrative") or sec.get("thesis") or ""), locale=locale).strip()
    parts = [p for p in (f"**{title}**" if title else "", body) if p]
    text = "\n".join(parts)
    return [TextBlock(markdown=text)] if text else []


def _image_block(sec: dict[str, Any], *, image_url_for: Callable[[str], str | None] | None) -> list[NeutralBlock]:
    if image_url_for is None:
        return []
    aid = str(sec.get("artifact_id") or "")
    if not aid:
        return []
    url = image_url_for(aid)
    if not url:
        return []
    return [
        ImageBlock(
            url=url,
            caption=sec.get("caption") or None,
            alt=sec.get("alt") or None,
        )
    ]


__all__ = ["NeutralTurnState", "apply_event", "kb_sources_block", "refresh_kb_citation_labels", "refresh_report_lifecycle", "render_blocks"]
