"""StreamingDecomposer — incremental markdown → canvas event stream.

Sprint κ Phase A (r2 closes 3 P0 + 8 P1 from κ-9 review).

Per tech-design-r2 + r3 + r4 patches:

- LLM stream tokens fed via `feed(chunk)`; on H1/H2/H3 boundary emit one-shot
  `section` (with h1/h2/h3 attr; matching legacy decompose_answer schema)
  then open a new narrative section for the body. Frontend sees the same
  shape it does for non-streaming turns.
- Codeblock state ` ``` ` suppresses heading detection inside fences (r2 P0-3).
- Cumulative `_value_to_source` precomputed at construction from invocations
  (no staleness — streaming runs only on Lead's terminal post-tool call).
- Mid-marker `[num:...]` chunks buffered until `]` arrives (PRD r2 §4.2.4).
  Marker state reset on section close (r2 P1-4).
- `_full_md` capped at MAX_CONTENT_LEN to bound memory (r2 P1-10).
- `finalize()` returns `list[dict]` matching `decompose_answer` shape.

Section ownership:
- Lead's free-form markdown → StreamingDecomposer (heading `section` markers
  + `narrative` bodies + `callout` for blockquote-led paragraphs).
- tool_invocations-derived (eyebrow, kpi_row, data_table, sources) →
  `decompose_answer_invocation_only` helper, emitted via `section_added`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from bowhead_core import metrics as core_metrics

if TYPE_CHECKING:
    from bowhead_core.types import IntentType, ToolInvocation

logger = logging.getLogger(__name__)

# ─── Type aliases ────────────────────────────────────────────────────────────

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]

# Body section kinds (heading "section" sections are emitted separately).
BodyKind = Literal["narrative", "callout"]

# ─── Buffer caps ─────────────────────────────────────────────────────────────

_MARKER_MAX_LEN = 256
_MARKER_MAX_AGE_SEC = 2.0
_MAX_CONTENT_LEN = 256 * 1024  # 256 KB hard cap on cumulative markdown

# 0.27.1 hotfix: debounce token-level chunk emission. Real DeepSeek/Qwen Chinese streams (~1 character
# per token) previously emitted one `canvas.section_chunk` per token. Production trace
# t_be5558edae3c showed 167 chunks / 2 sections, stalling FE rendering. Accumulate bursts within a 50ms buffer,
# then emit once. Force-flush paths (section transition / abort /
# finalize / marker overflow) bypass buffering and emit immediately to preserve ordering.
_CHUNK_FLUSH_DELAY_MS = int(os.environ.get("BOWHEAD_CHUNK_FLUSH_MS", "50"))


# ─── Regexes ─────────────────────────────────────────────────────────────────

_OPEN_MARKER_RE = re.compile(r"\[num:")
_CALLOUT_PREFIX_RE = re.compile(r"^\s*(>|⚠️|💡|❗)")


# ─── Main class ──────────────────────────────────────────────────────────────


@dataclass
class StreamingDecomposer:
    """Incremental markdown parser → canvas streaming events.

    Invariants:
    - `feed(chunk)` is the only ingress; safe to call from async stream loop.
    - Each call MAY emit 0..N `canvas.section_*` events via ``emit``.
    - `finalize()` MUST be called once after stream end; flushes any open
      section and returns collected section dicts.
    - `tool_invocations` is the FULL invocation list at construction; streaming
      runs on Lead's terminal post-tool call so no new invocations arrive
      after construction (tech-design-r2 §4.1.1).
    """

    canvas_id: str
    turn_id: str
    emit: EmitFn
    tool_invocations: list[ToolInvocation] = field(default_factory=list)
    intent: IntentType | None = None
    question: str = ""
    execution_generation: int = 1

    # Internal state
    _full_md: str = ""  # cumulative markdown (capped)
    _content_truncated: bool = False
    _last_emit_pos: int = 0  # body chunks emitted up to here
    _last_scanned_pos: int = 0  # heading-scan boundary
    _in_codeblock: bool = False  # ``` line-start fence state
    _current_section_id: str | None = None  # current BODY (narrative) section id
    _current_section_kind: BodyKind | None = None
    _current_section_start: int = 0
    _ord: int = 0
    _collected_sections: list[dict[str, Any]] = field(default_factory=list)
    # canvas-summon: sec_lead_* IDs sent to the frontend this turn, rather than persisted IDs.
    # FE buffer-hides them pending a terminal verdict. With no persisted section at completion,
    # D6 has no DB IDs and previously emitted no verdict, so done's reveal flashed them onscreen.
    # This list is that path's sole source of which sections to hide; record separately from _collected_sections,
    # which holds only fully closed sections, excluding aborted sections that nevertheless streamed to FE.
    _emitted_section_ids: list[str] = field(default_factory=list)
    _value_to_source: dict[float, str] = field(default_factory=dict)
    _marker_open_pos: int | None = None
    _marker_opened_at: float | None = None

    # 0.27.1 hotfix: chunk debounce buffer.
    _pending_delta_section_id: str | None = None
    _pending_delta_buf: str = ""
    _pending_flush_task: asyncio.Task | None = None

    def __post_init__(self) -> None:
        from .answer_decomposer import _build_value_to_source_map

        self._value_to_source = _build_value_to_source_map(self.tool_invocations)

    # ─── Public API ──────────────────────────────────────────────────────

    def _next_section_id(self) -> str:
        self._ord += 1
        # The same Turn resumes with a new decomposer. Keep first-generation
        # IDs compatible without reusing paused preview identities.
        generation = f"g{self.execution_generation}_" if self.execution_generation > 1 else ""
        return f"sec_lead_{self.turn_id}_{generation}{self._ord:03d}"

    async def feed(self, chunk: str) -> None:
        """Ingest a stream chunk. May emit 0..N events."""
        if not chunk:
            return
        # Buffer cap (r2 P1-10): drop overflow but keep stream alive.
        if len(self._full_md) >= _MAX_CONTENT_LEN:
            if not self._content_truncated:
                logger.warning(
                    "streaming.content_truncated turn=%s cap=%d",
                    self.turn_id,
                    _MAX_CONTENT_LEN,
                )
                self._content_truncated = True
                # π H r2 (Risk P2-1): surface truncation to UI via notice event.
                await self._emit_cap_notice()
            return
        room = _MAX_CONTENT_LEN - len(self._full_md)
        if len(chunk) > room:
            chunk = chunk[:room]
            self._content_truncated = True
            logger.warning(
                "streaming.content_truncated turn=%s cap=%d",
                self.turn_id,
                _MAX_CONTENT_LEN,
            )
            # π H r2 (Risk P2-1): surface truncation to UI via notice event.
            await self._emit_cap_notice()
        self._full_md += chunk
        await self._scan_and_open_sections()
        await self._emit_safe_chunks()

    async def _emit_cap_notice(self) -> None:
        """π H r2 (Risk P2-1): emit a `notice` event so frontend can show
        "回答较长已截断" hint instead of silent truncation."""
        try:
            await self.emit(
                "notice",
                {
                    "turn_id": self.turn_id,
                    "reason": "stream_content_truncated",
                    "message": "回答较长已截断",
                },
            )
        except Exception:
            # Notice is best-effort; never let it crash the stream.
            logger.exception(
                "streaming.cap_notice_emit_failed turn=%s",
                self.turn_id,
            )

    async def finalize(self) -> list[dict[str, Any]]:
        """Stream end — flush any open section + buffered marker."""
        # Force-flush any in-progress marker (this is the actual stream end,
        # not a buffer overflow).
        if self._marker_open_pos is not None:
            self._marker_open_pos = None
            self._marker_opened_at = None
        await self._emit_safe_chunks(force=True)
        # 0.27.1 hotfix: extra safety flush — _emit_safe_chunks(force=True)
        # already calls _flush_pending_chunks if there's new content, but if
        # _last_emit_pos == safe_end (no new content but buffer pending from
        # a prior non-force call), we still need to drain.
        await self._flush_pending_chunks()
        if self._current_section_id is not None:
            await self._close_current_section()
        return self._collected_sections

    @property
    def emitted_section_ids(self) -> list[str]:
        """canvas-summon: 本轮已经发给前端的 sec_lead_* id（副本）。

        turn 收尾的 D6 裁决在「一个段都没落库」时靠它点名该藏哪些（见
        `lead_orchestrator` done-emit 的 D6 分支）。
        """
        return list(self._emitted_section_ids)

    async def abort_open_section(self, *, reason: str = "tool_call_after_stream") -> None:
        """Sprint κ Phase B r2 (κ-11 P0-CTO-1 fix): close current open section
        as `is_partial=true` so frontend cancels the partial draft.

        Called when LLM streams pre-tool prose then emits tool_call — the text
        already pushed to canvas is not the final answer; mark it cancelled so
        frontend's `cancelledSectionIds` styling kicks in.

        π H r2 fix (tech-design-r2 §H): also advance `_last_emit_pos` /
        `_last_scanned_pos` to `len(self._full_md)` so subsequent `feed()`
        calls start fresh and don't re-emit prose the aborted section already
        streamed. Without this, the same prefix bytes appear twice on canvas
        (prod symptom t_6e855d9b).

        CTO P1-5 (r2): reset `_in_codeblock` so a mid-codeblock abort doesn't
        bleed fence state into the next section. If we WERE mid-codeblock at
        abort time, append a synthetic closing fence to `_full_md` (advanced
        offsets past it) so the next scan starts clean.
        """
        if self._current_section_id is None:
            return
        # Force flush any pending chunks
        await self._emit_safe_chunks(force=True)
        section_id = self._current_section_id
        kind = self._current_section_kind or "narrative"
        await self.emit(
            "canvas.section_done",
            {
                "canvas_id": self.canvas_id,
                "section_id": section_id,
                "kind": kind,
                "payload": {"markdown": ""},
                "is_partial": True,
                "abort_reason": reason,
            },
        )
        self._reset_section_state()

        # π H r2: advance offsets so next feed() does not re-emit prose
        # already streamed by the aborted section.
        was_in_codeblock = self._in_codeblock
        # CTO P1-5: clear fence state — aborted prose's open ``` should not
        # affect parsing of the next, independent section.
        self._in_codeblock = False
        if was_in_codeblock:
            # Append synthetic close fence so next scan past this point is
            # outside codeblock from byte 0. Offsets advance past the fence.
            self._full_md += "\n```\n"
        self._last_emit_pos = len(self._full_md)
        self._last_scanned_pos = len(self._full_md)

    def update_invocations(self, invocations: list[ToolInvocation]) -> None:
        """Sprint κ Phase B r2 (κ-11 P1-ENG-1 fix): public API to refresh the
        value_to_source map post-construction. Replaces direct private-attr
        mutation.
        """
        from .answer_decomposer import _build_value_to_source_map

        self.tool_invocations = list(invocations)
        self._value_to_source = _build_value_to_source_map(self.tool_invocations)

    # ─── Internal: section detection ─────────────────────────────────────

    async def _scan_and_open_sections(self) -> None:
        """Scan for new line-start `## ` / `### ` / ` ``` ` boundaries past
        ``_last_scanned_pos``. On heading boundary: close current narrative,
        emit one-shot heading section, open a fresh narrative for body.
        On ``` fence: toggle codeblock state (suppresses heading scan).
        """
        scan_from = self._last_scanned_pos
        text = self._full_md
        i = scan_from
        while i < len(text):
            line_end = text.find("\n", i)
            if line_end == -1:
                break  # incomplete line — wait for more chunks
            line_start = i
            line = text[line_start:line_end]
            stripped = line.lstrip(" ")
            # Codeblock fence line ` ```... ` toggles the state. While inside
            # codeblock, headings are NOT split (P0-3 fix).
            if stripped.startswith("```"):
                self._in_codeblock = not self._in_codeblock
                i = line_end + 1
                self._last_scanned_pos = i
                continue
            if self._in_codeblock:
                i = line_end + 1
                self._last_scanned_pos = i
                continue
            kind: str | None = None
            title = ""
            if stripped.startswith("# "):
                kind, title = "h1", stripped[2:].strip()
            elif stripped.startswith("## "):
                kind, title = "h2", stripped[3:].strip()
            elif stripped.startswith("### "):
                kind, title = "h3", stripped[4:].strip()

            if kind is not None:
                # Close current narrative (its body ends right before this line).
                if self._current_section_id is not None:
                    await self._emit_safe_chunks(up_to=line_start, force=True)
                    await self._close_current_section()
                # Emit one-shot heading section (matching legacy
                # decompose_answer:1539-1543 shape: `kind=section, h2=title`).
                await self._emit_heading_section(kind=kind, title=title)
                # Open new narrative for the body.
                await self._open_narrative_section(body_start=line_end + 1)
                i = line_end + 1
                self._last_scanned_pos = i
                continue

            # Non-heading complete line. If no current narrative, lazily open.
            # π H r2: body_start uses the current emit floor (advanced by
            # `abort_open_section`) rather than 0, so a post-abort lazy-open
            # does not re-emit bytes the aborted section already streamed.
            if self._current_section_id is None:
                await self._open_narrative_section(
                    body_start=self._last_emit_pos,
                )
            i = line_end + 1
            self._last_scanned_pos = i

    async def _emit_heading_section(self, *, kind: str, title: str) -> None:
        """One-shot emit of an h1/h2/h3 heading section (no streaming/chunk)."""
        sid = self._next_section_id()
        self._emitted_section_ids.append(sid)
        # Match legacy decompose_answer:1539-1543 shape.
        attr_field = kind  # "h1" | "h2" | "h3"
        title = title[:120]
        section_dict = {
            "id": sid,
            "kind": "section",
            "label": "",
            attr_field: title,
        }
        self._collected_sections.append(section_dict)
        # Use section_streaming + section_done together so frontend
        # transitions match the streaming → done state machine; payload
        # carries the heading title.
        await self.emit(
            "canvas.section_streaming",
            {
                "canvas_id": self.canvas_id,
                "section_id": sid,
                "kind": "section",
                "partial_payload": {attr_field: title, "label": ""},
            },
        )
        await self.emit(
            "canvas.section_done",
            {
                "canvas_id": self.canvas_id,
                "section_id": sid,
                "kind": "section",
                "payload": {attr_field: title, "label": ""},
            },
        )

    async def _open_narrative_section(self, *, body_start: int) -> None:
        """Open a new narrative section for upcoming body content."""
        self._current_section_id = self._next_section_id()
        self._emitted_section_ids.append(self._current_section_id)
        self._current_section_kind = "narrative"
        self._current_section_start = body_start
        self._last_emit_pos = body_start
        await self.emit(
            "canvas.section_streaming",
            {
                "canvas_id": self.canvas_id,
                "section_id": self._current_section_id,
                "kind": "narrative",
                "partial_payload": {"markdown": ""},
            },
        )

    async def _close_current_section(self) -> None:
        if self._current_section_id is None:
            return
        # Body extends from section_start to last_emit_pos (P0-2 fix:
        # never include unconsumed tail past the last safe chunk).
        end = max(self._last_emit_pos, self._current_section_start)
        body = self._full_md[self._current_section_start : end]
        from bowhead_core.citation_marks import strip_legacy

        # citation-audit M1-2/§2a: streaming sections are SSE-only previews, never persisted
        # (sd.finalize() return value discarded; FE terminal reconciliation replaces them entirely). Previews emit no
        # gen2 markers; bake retired syntax to plain values, preserving units. [N] conversion depends on
        # follow-up persistence of streaming sections (tech-design §2a, out of scope).
        bound = strip_legacy(body).strip()
        if not bound:
            # Empty narrative — drop it (no events emit; we already sent
            # section_streaming above so emit a minimal section_done to
            # close the frontend state).
            await self.emit(
                "canvas.section_done",
                {
                    "canvas_id": self.canvas_id,
                    "section_id": self._current_section_id,
                    "kind": self._current_section_kind or "narrative",
                    "payload": {"markdown": ""},
                },
            )
            self._reset_section_state()
            return
        # Detect callout pattern (blockquote / ⚠️ / 💡 lead).
        first_line = bound.splitlines()[0]
        kind: BodyKind = self._current_section_kind or "narrative"
        payload: dict[str, Any] = {"markdown": bound}
        if _CALLOUT_PREFIX_RE.match(first_line):
            kind = "callout"
            payload = {"text": bound.lstrip("> ⚠️💡❗ \t").strip()}
        section_dict = {
            "id": self._current_section_id,
            "kind": kind,
            **payload,
        }
        self._collected_sections.append(section_dict)
        await self.emit(
            "canvas.section_done",
            {
                "canvas_id": self.canvas_id,
                "section_id": self._current_section_id,
                "kind": kind,
                "payload": payload,
            },
        )
        self._reset_section_state()

    def _reset_section_state(self) -> None:
        """Clear per-section state. Reset marker state too (P1-4 fix)."""
        self._current_section_id = None
        self._current_section_kind = None
        self._marker_open_pos = None
        self._marker_opened_at = None

    # ─── Internal: chunk emission ────────────────────────────────────────

    async def _emit_safe_chunks(
        self,
        *,
        up_to: int | None = None,
        force: bool = False,
    ) -> None:
        """Emit `canvas.section_chunk` events for newly-arrived markdown,
        respecting mid-marker buffering.
        """
        if self._current_section_id is None:
            # 0.27.1: even if no current section, force path must drain pending
            # buf from prior section (caller may be _close_current_section /
            # abort_open_section after section_id reset).
            if force:
                await self._flush_pending_chunks()
            return
        text = self._full_md
        end = up_to if up_to is not None else len(text)
        if end <= self._last_emit_pos:
            # 0.27.1: no new content, but force path still needs to drain pending
            # buf accumulated from prior non-force feeds.
            if force:
                await self._flush_pending_chunks()
            return
        safe_end = end
        if not force:
            scan_pos = self._last_emit_pos
            if self._marker_open_pos is None:
                m = _OPEN_MARKER_RE.search(text, scan_pos, end)
                if m:
                    close_idx = text.find("]", m.end(), end)
                    if close_idx == -1:
                        self._marker_open_pos = m.start()
                        self._marker_opened_at = time.monotonic()
                        safe_end = m.start()
            else:
                close_idx = text.find("]", self._marker_open_pos, end)
                if close_idx != -1:
                    self._marker_open_pos = None
                    self._marker_opened_at = None
                else:
                    open_pos = self._marker_open_pos
                    overflow_len = len(text) - open_pos > _MARKER_MAX_LEN
                    overflow_age = self._marker_opened_at is not None and time.monotonic() - self._marker_opened_at > _MARKER_MAX_AGE_SEC
                    if overflow_len or overflow_age:
                        logger.warning(
                            "streaming.marker_buffer_overflow turn=%s len=%d",
                            self.turn_id,
                            len(text) - open_pos,
                        )
                        core_metrics.inc(
                            "bowhead_kappa_streaming_marker_overflow_total",
                            labels={},
                        )
                        self._marker_open_pos = None
                        self._marker_opened_at = None
                    else:
                        safe_end = open_pos
        if safe_end <= self._last_emit_pos:
            return
        delta = text[self._last_emit_pos : safe_end]
        if not delta:
            return
        self._last_emit_pos = safe_end
        # 0.27.1 hotfix: route through buffer. force=True paths (section
        # transition / abort / finalize / marker overflow) flush immediately;
        # normal LLM-token feed batches at _CHUNK_FLUSH_DELAY_MS.
        if force:
            # Flush any pending from same section first to preserve ordering,
            # then emit current delta in same chunk shape.
            self._pending_delta_buf += delta
            self._pending_delta_section_id = self._current_section_id
            await self._flush_pending_chunks()
        else:
            self._pending_delta_section_id = self._current_section_id
            self._pending_delta_buf += delta
            self._schedule_flush()

    def _schedule_flush(self) -> None:
        """Cancel any in-flight flush timer + schedule a new one."""
        if self._pending_flush_task is not None and not self._pending_flush_task.done():
            self._pending_flush_task.cancel()
        self._pending_flush_task = asyncio.create_task(self._delayed_flush())

    async def _delayed_flush(self) -> None:
        """Sleep then flush. Cancelled by _schedule_flush() (next chunk arrives)
        or by _flush_pending_chunks() (force path beat the timer)."""
        try:
            await asyncio.sleep(_CHUNK_FLUSH_DELAY_MS / 1000.0)
            await self._flush_pending_chunks()
        except asyncio.CancelledError:
            return

    async def _flush_pending_chunks(self) -> None:
        """Emit any buffered delta as one chunk + clear buffer.
        Safe to call from force path even when buffer empty."""
        if not self._pending_delta_buf or self._pending_delta_section_id is None:
            return
        section_id = self._pending_delta_section_id
        delta = self._pending_delta_buf
        self._pending_delta_buf = ""
        self._pending_delta_section_id = None
        # Cancel any pending timer to avoid double-emit.
        if self._pending_flush_task is not None and not self._pending_flush_task.done():
            self._pending_flush_task.cancel()
        try:
            await self.emit(
                "canvas.section_chunk",
                {
                    "canvas_id": self.canvas_id,
                    "section_id": section_id,
                    "delta": delta,
                },
            )
        except Exception as e:
            logger.warning("canvas.section_chunk emit failed: %s", e)
