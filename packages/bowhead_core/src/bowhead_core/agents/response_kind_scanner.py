"""Sprint ω Phase 2 — stateful streaming scanner for `<response_kind>` marker.

Per spike Q3 (130 LLM call, 60 streamed):
- **0 / 65 markers fit in one stream chunk** — always split across 3-11 chunks.
- DeepSeek mean delta = 2.18 chars; `<response_kind>chat</response_kind>` = 32
  chars → physically cannot fit one chunk.
- **DeepSeek 47%** of streams **drop the opening `<response` deltas entirely**
  — stream starts with literal `_kind>chat</response_kind>...`.
- **DeepSeek 20%** (chat-kind queries) emit NO marker at all.
- Doubao 30/30 well-formed at offset 0; Qwen 5/5 well-formed.
- Pre-marker prose: avg 6 chars / max 61 chars — LLM does not strictly emit
  marker as the absolute first byte.

This module is a stateful state machine that:
- Buffers across `feed()` calls until a marker is recognized OR buffer exceeds
  `_MAX_PRE_PROSE` (decision="absent" fallback).
- Never emits any byte that COULD be a marker fragment to the UI.
- Strict enum match `(chat|canvas)` — Unicode adversarial input (e.g.
  `<response_kind>对话</response_kind>`) returns decision="absent" instead
  of crashing the caller with KeyError.
- After decision lock, keeps a light filter that drops repeated complete
  markers (one scanner state spans every LLM call of a Lead turn).

The module is **flag-agnostic**. The Lead-orchestrator wire decides whether to
call `feed()` at all (gated on `BOWHEAD_OMEGA_RESPONSE_KIND_V3`); this module
just implements the protocol.

Sprint chat-v4 W3W4-rewrite (Option A, 2026-05-18): the `<plan>` /
`<phase_update/>` / `<final_payload>` in-band block capture introduced in
0a1d8cb is REMOVED. Real-LLM dogfood (this sprint) showed Lead emits 0 v4
tokens in prod despite the 4744-tok directive; replacement is pure backend
post-hoc deterministic emit. Sprint phase-plan-removal (0.27.0) then
deleted turn.plan + phase.update events; only lead.final.payload survives
as a backend emit (see `lead_orchestrator.py`). The headline +
response_kind capture stays (verified working in prod).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# Tag literals — used both for direct match and for buffer-safe-cut.
_OPEN_TAG = "<response_kind>"  # 15 chars
_CLOSE_TAG = "</response_kind>"  # 16 chars

# Sprint chat-style-fix Phase A2 — headline marker emitted immediately after
# response_kind decision. Body cap 200 chars (FE display further caps to 30
# chars; backend 200 is forgiving safety margin so we still capture truncated
# value rather than dropping on overflow).
_HEADLINE_OPEN = "<headline>"
_HEADLINE_CLOSE = "</headline>"
_MAX_HEADLINE_BUFFER = 200
# Clean full-form match — `<response_kind>chat</response_kind>` (case-insensitive,
# whitespace tolerated inside tags). Strict (chat|canvas) enum — Unicode
# alternatives (e.g. `对话`) won't match → fall to "absent" branch.
_FULL_MARKER_RE = re.compile(
    r"<response_kind>\s*(chat|canvas)\s*</response_kind>",
    re.IGNORECASE,
)

# DeepSeek-missing-open tolerance — observed in 47% of DS streams (Q3).
# r3.1 P0 fix (CTO catch): `\b_kind>` fails when `_` follows another word
# character (e.g. `the_kind>`) because `\b` is a transition between `\w` and
# `\W`, and `_` IS `\w`. Use `(?<![A-Za-z0-9])` non-letter-digit lookbehind
# so the literal `_kind>` only matches at safe boundaries. This still allows
# Chinese prose prefix (since `,` `\n` `。` are non-word), and is robust to
# English-letter prefix too.
_MISSING_OPEN_HINT_RE = re.compile(
    r"(?<![A-Za-z0-9])_kind>\s*(chat|canvas)\s*</response_kind>",
    re.IGNORECASE,
)

# Buffer caps.
_MAX_LOOKAHEAD = 64
# Q3 max pre-marker prose = 61 chars. 80-char cap gives small safety margin
# without sacrificing TTFT on chat-kind turns (PM P0-1 latency budget).
_MAX_PRE_PROSE = 80
# Hard cap for absent-decision: pre-prose + tag widths + slack.
_ABSENT_THRESHOLD = _MAX_PRE_PROSE + len(_OPEN_TAG) + len(_CLOSE_TAG) + 16


@dataclass
class ScannerState:
    """Per-Lead-turn state. Reset at start of each turn via
    `init_per_turn_scanner_state()` (CTO P0-2: frozen-corpus replay 同 turn_id
    重放 if state stale, locked=True → pass-through → safe_text 不同 → false
    negative)."""

    buffer: str = ""
    seen_offset: int = 0
    decision: str | None = None  # "chat" | "canvas" | "absent" | None
    locked: bool = False  # True after decision, pass-through mode
    match_mode: str | None = None  # "clean" | "missing_open" | "absent"
    # Sprint ω Phase 2 P0-2 fix (2026-05-12): last word-char emitted as part
    # of safe_text. Used as a virtual lookbehind prefix when running
    # `_MISSING_OPEN_HINT_RE` so a split feed of `"the"` then `"_kind>..."`
    # does NOT false-match (the word-char predecessor `e` correctly disables
    # the missing-open hint). Cleared whenever a non-word char is emitted.
    last_word_char: str = ""
    # Sprint chat-style-fix Phase A2 — headline scan substate. After
    # response_kind decision is locked, scanner buffers chunks looking for
    # `<headline>...</headline>` and emits the captured text via the
    # `headline` SSE event. See `_feed_headline` for the 4 disorder rules
    # (R-1..R-4) per tech-design §I-1.
    headline_buffer: str = ""  # accumulator since response_kind locked
    headline_decision: str | None = None  # captured | absent | truncated | missing-close | duplicate | disorder
    headline_text: str | None = None  # body extracted from `<headline>BODY</headline>` (≤200 char)
    headline_locked: bool = False  # True after headline phase done — pass-through after
    headline_seen_open: bool = False  # latch — once a `<headline>` open tag was observed
    # Characters of captured/held headlines seen before the response_kind lock;
    # they do not count toward the absent-decision window.
    headline_chars: int = 0
    # Last character emitted by the post-lock filter — lookbehind context for a
    # missing-open `_kind>` that starts the next delta.
    post_lock_prev_char: str = ""


# A headline is one short line. A `<headline>` whose body runs past the cap or
# a newline before `</headline>` is prose (for example an XML sample in a code
# block); the streaming filter and the post-hoc sanitizer share this rule.
_HEADLINE_FULL_RE = re.compile(
    r"<headline>([^\n]{0,200}?)</headline>",
    re.IGNORECASE,
)

# Post-hoc sanitizer — strips `<response_kind>chat|canvas</response_kind>` from
# any text. Phase 2 P0 fix (Sprint ω 2026-05-12 dogfood reveal): the streaming
# scanner only filters live deltas (`sd.feed`), but `result.final_answer` is
# the LLM-assembled raw text that bypasses the stream wire and lands in done
# event payload (lead_orchestrator.py:1034/1374/1407/1536/1586). Real LLM
# dogfood showed 12.5% leak rate. Apply this regex sanitizer before any
# final_answer/text payload emit as defense-in-depth.
#
# Matches BOTH well-formed `<response_kind>chat</response_kind>` AND
# missing-open `_kind>chat</response_kind>` (DeepSeek 47% drop opening tags).
# Only enum bodies and single-line headlines count as markers; orphan close
# tags and non-enum bodies stay as text (same rule as the streaming filter).
_POST_HOC_SANITIZE_RE = re.compile(
    r"(?:<\s*response_kind\s*>|_kind\s*>)\s*(?:chat|canvas)\s*</\s*response_kind\s*>"
    r"|<\s*headline\s*>[^\n]{0,200}?</\s*headline\s*>",
    re.IGNORECASE,
)
# Blank lines directly after a marker that sat on its own line.
_MARKER_LINE_TAIL_RE = re.compile(r"(?:[ \t]*\n)+|[ \t]*\Z")
# The text before a marker line is empty or ends with a blank line.
_BLANK_BEFORE_RE = re.compile(r"(?:\A|\n)[ \t]*\n\Z|\A\Z")


def _remove_markers(text: str) -> tuple[str, bool]:
    """Remove markers without gluing or splitting paragraphs.

    A marker on its own line is removed with its line. When nothing but blank
    lines (or the start of the text) precedes it, the blank lines after it go
    too; when prose sits on the line above, only the marker's own line is
    removed, so a following blank line still separates the paragraphs.
    """
    parts: list[str] = []
    end = 0
    removed = False
    # Blank-before status of the marker line removed just before `end`, so a run of
    # marker lines after a blank line is treated as one block.
    chained_blank: bool | None = None
    for match in _POST_HOC_SANITIZE_RE.finditer(text):
        if match.start() < end:
            continue
        removed = True
        parts.append(text[end : match.start()])
        follows_marker_line = chained_blank is not None and match.start() == end
        end = match.end()
        own_line = match.start() == 0 or text[match.start() - 1] == "\n"
        tail = _MARKER_LINE_TAIL_RE.match(text, end) if own_line else None
        if tail is None or not (tail.end() == len(text) or text[tail.end() - 1] == "\n"):
            chained_blank = None
            continue
        blank_before = bool(chained_blank) if follows_marker_line else _BLANK_BEFORE_RE.search(text, 0, match.start()) is not None
        if blank_before:
            end = tail.end()
        else:
            line_end = text.find("\n", end, tail.end())
            end = line_end + 1 if line_end != -1 else tail.end()
        chained_blank = blank_before
    parts.append(text[end:])
    return "".join(parts), removed


def strip_response_kind_markers(text: str) -> str:
    """Remove `<response_kind>...</response_kind>` AND `<headline>...</headline>`.

    Idempotent + safe to call on already-clean text (returns unchanged).
    Used at done-event / final_answer payload assembly so neither marker NEVER
    survives to user-visible DOM regardless of which code path emitted.

    Sprint chat-style-fix Phase A2: regex extended to cover headline tags so
    archived-turn replay + final_answer leak paths handle the new marker too.

    Markers sit on their own lines at the top of an answer. Removing only the
    tag used to leave "\\n\\n\\n\\n" before the first paragraph (final_answer,
    exec_summary, turns.final_answer), so a marker line takes the blank lines
    after it and leading newlines are trimmed. Other whitespace (code blocks,
    first-line indentation) is untouched.
    """
    if not text:
        return text
    lower = text.lower()
    if "_kind>" not in text and "response_kind" not in lower and "<headline" not in lower:
        return text
    cleaned, removed = _remove_markers(text)
    if not removed:
        return text
    return cleaned.lstrip("\n")


# Models can emit a clarification tool call as body text, including malformed
# attributes. The terminal fallback must render readable questions and options:
# stripping the only content would leave an empty answer.
# Lead rejects uninvoked protocol text before finalization. Only a native Tool
# call can create a card and pause. The legacy rendering fallback below neither
# executes arguments nor reconstructs a missing card.
_STRAY_CLARIFICATION_RE = re.compile(
    r"<\s*clarification_ask\b[^>]*>(.*?)</\s*clarification_ask\s*>",
    re.IGNORECASE | re.DOTALL,
)
# Detection must also cover self-closing and truncated calls. Requiring a
# matching closing tag lets malformed protocol text bypass both final gates.
# Only the legacy rendering fallback needs a complete body to humanize.
_UNINVOKED_CLARIFICATION_RE = re.compile(r"<\s*clarification_ask(?=[\s/>]|$)", re.IGNORECASE)


def _clarification_prose(text: str) -> str:
    """Mask quoted Markdown without changing offsets used for prose cleanup."""
    lines: list[str] = []
    fence = ""
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ""
            lines.append(re.sub(r"[^\r\n]", " ", line))
        elif fence or line.lstrip().startswith(">") or line.startswith(("    ", "\t")):
            lines.append(re.sub(r"[^\r\n]", " ", line))
        else:
            lines.append(re.sub(r"(`+).*?\1", lambda match: " " * len(match.group()), line))
    return "".join(lines)


def has_uninvoked_clarification(text: str) -> bool:
    """Recognize protocol leakage, never infer a question or execute its arguments."""
    visible = _clarification_prose(text)
    return bool(
        _UNINVOKED_CLARIFICATION_RE.search(visible)
        or (re.search(r"^\s*questions\s*=\s*\[", visible, re.MULTILINE) and re.search(r"^\s*fallback_text\s*=", visible, re.MULTILINE))
    )


def _clarification_block_to_prose(payload: str) -> str:
    """把 clarification_ask 的 JSON 参数翻成可读文本；翻不动就退回原文（绝不返空）。"""
    import json as _json

    inner = payload.strip()
    if not inner:
        return ""
    try:
        data = _json.loads(inner)
    except Exception:
        # Arguments are invalid JSON (the model mangled the entire call); at least remove angle brackets
        # while preserving text, rather than showing users markup.
        return inner
    if not isinstance(data, dict):
        return inner
    lines: list[str] = []
    questions = data.get("questions")
    if isinstance(questions, list):
        for item in questions:
            if not isinstance(item, dict):
                continue
            text = str(item.get("question") or "").strip()
            if text:
                lines.append(text)
            options = item.get("options")
            if isinstance(options, list):
                lines.extend(f"- {option}" for option in options if str(option).strip())
    if not lines:
        fallback = data.get("fallback_text")
        if isinstance(fallback, str) and fallback.strip():
            lines.append(fallback.strip())
    return "\n".join(lines) if lines else inner


def humanize_stray_clarification_ask(text: str) -> str:
    """Render stray protocol prose while preserving quoted examples verbatim."""
    if not text or "clarification_ask" not in text.lower():
        return text
    parts: list[str] = []
    end = 0
    for match in _STRAY_CLARIFICATION_RE.finditer(_clarification_prose(text)):
        parts.extend((text[end : match.start()], _clarification_block_to_prose(text[match.start(1) : match.end(1)])))
        end = match.end()
    return "".join(parts) + text[end:]


def init_per_turn_scanner_state(
    *,
    capture_v4_tokens: bool = False,
    on_turn_plan: Callable[[dict], None] | None = None,
    on_phase_update: Callable[[dict], None] | None = None,
    on_final_payload: Callable[[dict], None] | None = None,
) -> ScannerState:
    """Factory — fresh state per Lead turn. Use in `_lead_loop` setup.

    Sprint chat-v4 W3W4-rewrite (Option A, 2026-05-18): v4 token capture
    parameters (`capture_v4_tokens`, `on_turn_plan`, `on_phase_update`,
    `on_final_payload`) are INERT — accepted for backward-compat with
    existing call-sites but ignored. Sprint phase-plan-removal (0.27.0)
    deleted turn.plan + phase.update events; only lead.final.payload remains
    as a backend deterministic emit (see lead_orchestrator).
    """
    return ScannerState()


def feed(state: ScannerState, chunk: str) -> tuple[str, ScannerState]:
    """Feed one streaming delta. Returns (safe_text_to_emit, new_state).

    Invariants:
    - `<response_kind` / `</response_kind` substring NEVER appears in safe_text.
    - A complete `<headline>...</headline>` is captured wherever it appears,
      before or after the response_kind marker and whether or not that marker
      exists. The first headline wins; later ones are dropped.
    - After `state.decision` is set, a light marker filter (`_feed_headline`)
      stays active for the rest of the Lead turn. One scanner state spans every
      LLM call of the turn and models repeat the markers on later calls, so
      complete `<response_kind>…</response_kind>` / `<headline>…</headline>`
      spans are dropped there too. Only text that could still begin a marker
      is buffered.
    - When buffer exceeds `_ABSENT_THRESHOLD` with no match, decision="absent"
      and buffer is flushed (Lead falls back to post-hoc ν2 v2 classifier).

    Headline rules:
      R-1: `<headline>` before response_kind → still captured (it used to leak
           as prose; 116 dogfood 2026-10).
      R-2: body > _MAX_HEADLINE_BUFFER without close tag → truncated.
      R-3: stream ends without `</headline>` → missing-close (see
           `finalize_headline`).
      R-4: second `<headline>` after one already captured → dropped.
    """
    # Phase 1: response_kind scan (pre-decision-lock).
    if not state.locked:
        safe, state = _feed_response_kind(state, chunk)
        # Residual text after the lock goes through the marker filter in the
        # same call so a headline (or a repeated marker) in it is handled.
        if state.locked and safe:
            return _feed_headline(state, safe)
        return safe, state

    # Phase 2: marker filter for the rest of the turn.
    return _feed_headline(state, chunk)


def _locked_state(state: ScannerState, kind: str, match_mode: str) -> ScannerState:
    """Lock the response_kind decision, keeping headline progress."""
    return ScannerState(
        buffer="",
        seen_offset=state.seen_offset,
        decision=kind,
        locked=True,
        match_mode=match_mode,
        last_word_char="",
        headline_decision=state.headline_decision,
        headline_text=state.headline_text,
        headline_locked=state.headline_locked,
        headline_seen_open=state.headline_seen_open,
        headline_chars=state.headline_chars,
    )


def _capture_headlines_in_rk_buffer(state: ScannerState) -> None:
    """R-1: capture complete headlines that arrive before the response_kind lock."""
    while True:
        m = _HEADLINE_FULL_RE.search(state.buffer)
        if not m:
            return
        if state.headline_text is None:
            state.headline_text = m.group(1)[:_MAX_HEADLINE_BUFFER]
            state.headline_decision = "captured"
        elif state.headline_decision == "captured":
            state.headline_decision = "duplicate"
        state.headline_seen_open = True
        state.headline_locked = True
        state.headline_chars += m.end() - m.start()
        state.buffer = state.buffer[: m.start()] + state.buffer[m.end() :]


def _headline_open_status(state: ScannerState, tail: str) -> str:
    """Status of a `<headline>` at the start of `tail` (lowercase not required).

    "complete" — a closed single-line headline (handled by the full regex);
    "hold"     — unclosed, single-line and within the cap: may still close;
    "prose"    — a newline or the cap came first: never a headline. A single-line
                 overflow still records the first chars as a truncated headline (R-2).
    """
    body = tail[len(_HEADLINE_OPEN) :]
    close = body.lower().find(_HEADLINE_CLOSE)
    newline = body.find("\n")
    if close != -1 and (newline == -1 or close < newline) and close <= _MAX_HEADLINE_BUFFER:
        return "complete"
    if newline != -1 and newline <= _MAX_HEADLINE_BUFFER:
        return "prose"
    if len(body) <= _MAX_HEADLINE_BUFFER:
        return "hold"
    # A body of exactly the cap may still be followed by a close tag that is
    # arriving in pieces: keep holding while the overflow is a prefix of it.
    overflow = body[_MAX_HEADLINE_BUFFER:].lower()
    if len(overflow) < len(_HEADLINE_CLOSE) and newline == -1:
        for split in range(max(0, len(body) - len(_HEADLINE_CLOSE) + 1), _MAX_HEADLINE_BUFFER + 1):
            if _HEADLINE_CLOSE.startswith(body[split:].lower()):
                return "hold"
    if state.headline_text is None:
        state.headline_text = body[:_MAX_HEADLINE_BUFFER]
        state.headline_decision = "truncated"
        state.headline_locked = True
    return "prose"


def _held_headline_index(state: ScannerState, buf: str) -> int:
    """Index of the first `<headline>` in `buf` that must stay buffered, or -1."""
    lower = buf.lower()
    start = lower.find(_HEADLINE_OPEN)
    while start != -1:
        state.headline_seen_open = True
        if _headline_open_status(state, buf[start:]) == "hold":
            return start
        start = lower.find(_HEADLINE_OPEN, start + 1)
    return -1


def _feed_response_kind(
    state: ScannerState,
    chunk: str,
) -> tuple[str, ScannerState]:
    """Inner — response_kind scan phase (extracted from `feed` for clarity)."""
    state.buffer += chunk
    state.seen_offset += len(chunk)
    _capture_headlines_in_rk_buffer(state)

    # 1. Clean full-form match
    m = _FULL_MARKER_RE.search(state.buffer)
    if m:
        kind = m.group(1).lower()
        safe = state.buffer[: m.start()] + state.buffer[m.end() :]
        return safe, _locked_state(state, kind, "clean")

    # 2. DeepSeek missing-open fallback. Phase 2 P0-2 fix (2026-05-12):
    # prepend `last_word_char` as a virtual lookbehind prefix so a split feed
    # `"the"` then `"_kind>chat..."` correctly rejects (previous emit's `"e"`
    # is a word-char, lookbehind fails). The fragment is search-only — never
    # emitted; we offset match positions by `prefix_len` when slicing buffer.
    prefix = state.last_word_char
    prefix_len = len(prefix)
    haystack = prefix + state.buffer
    m2 = _MISSING_OPEN_HINT_RE.search(haystack)
    if m2:
        kind = m2.group(1).lower()
        # Translate match positions back into buffer space.
        b_start = max(0, m2.start() - prefix_len)
        b_end = max(0, m2.end() - prefix_len)
        safe = state.buffer[:b_start] + state.buffer[b_end:]
        return safe, _locked_state(state, kind, "missing_open")

    # An unclosed single-line `<headline>` within the cap stays buffered so it
    # cannot leak as prose; its characters (and captured headlines) do not count
    # toward the absent window. Past the cap or a newline it is prose.
    open_idx = _held_headline_index(state, state.buffer)
    held_headline = len(state.buffer) - open_idx if open_idx >= 0 else 0

    # 3. Absent fallback — buffer exceeded tolerance window with no match.
    if state.seen_offset - state.headline_chars - held_headline > _ABSENT_THRESHOLD:
        return state.buffer, _locked_state(state, "absent", "absent")

    # 4. Still scanning — emit prefix that CANNOT begin a marker, retain suffix.
    safe_cut = _find_safe_cut(state.buffer)
    if open_idx >= 0:
        safe_cut = min(safe_cut, open_idx)
    # A missing-open `_kind>chat</response_kind>` arriving in pieces: hold from a
    # word-boundary `_kind>` while its body can still become a marker.
    kind_idx = _held_missing_open_index(state, state.buffer[:safe_cut])
    if kind_idx >= 0:
        safe_cut = kind_idx
    safe_text = state.buffer[:safe_cut]
    state.buffer = state.buffer[safe_cut:]
    # Update last_word_char for next chunk's missing-open lookbehind context.
    # Reset to "" when most recent emitted char is non-word so genuine
    # word-boundary missing-open at next chunk start can still fire.
    if safe_text:
        tail = safe_text[-1]
        state.last_word_char = tail if (tail.isascii() and tail.isalnum()) else ""
    return safe_text, state


_OPEN_PREFIXES = (_OPEN_TAG, _HEADLINE_OPEN)
_MISSING_OPEN_PREFIX = "_kind>"
_KIND_BODY_RE = re.compile(r"\s*(?:chat|canvas)\s*</response_kind>", re.IGNORECASE)
_KIND_WORDS = ("chat", "canvas")
_MAX_KIND_SPAN = 64
_MARKER_CANDIDATE_RE = re.compile(r"[<_]")


def _is_ascii_word(char: str) -> bool:
    return bool(char) and char.isascii() and char.isalnum()


def _kind_body_status(rest: str) -> tuple[str, int]:
    """After `<response_kind>` / `_kind>`: ("drop", length) | ("hold", 0) | ("text", 0).

    Only enum bodies (`chat` / `canvas`) are markers, matching the sanitizer.
    "hold" means the text so far can still become such a marker.
    """
    m = _KIND_BODY_RE.match(rest)
    if m:
        return "drop", m.end()
    if len(rest) > _MAX_KIND_SPAN:
        return "text", 0
    stripped = rest.lstrip().lower()
    for word in _KIND_WORDS:
        if word.startswith(stripped):
            return "hold", 0
        if stripped.startswith(word):
            close = stripped[len(word) :].lstrip()
            if _CLOSE_TAG.startswith(close):
                return "hold", 0
    return "text", 0


def _classify_angle(state: ScannerState, buf: str, i: int) -> tuple[str, int]:
    """Classify a `<` at `buf[i]`: ("drop", end) | ("hold", i) | ("text", i)."""
    tail = buf[i:]
    low = tail[: len(_OPEN_TAG)].lower()
    if low == _OPEN_TAG:
        action, length = _kind_body_status(tail[len(_OPEN_TAG) :])
        return action, i + len(_OPEN_TAG) + length
    if tail[: len(_HEADLINE_OPEN)].lower() == _HEADLINE_OPEN:
        state.headline_seen_open = True
        m = _HEADLINE_FULL_RE.match(tail)
        if m:
            if state.headline_text is None:
                state.headline_text = m.group(1)
                state.headline_decision = "captured"
            elif state.headline_decision == "captured":
                state.headline_decision = "duplicate"
            state.headline_locked = True
            return "drop", i + m.end()
        status = _headline_open_status(state, tail)
        return ("hold", i) if status == "hold" else ("text", i)
    if any(len(tail) < len(tag) and tag.startswith(tail.lower()) for tag in _OPEN_PREFIXES):
        return "hold", i
    return "text", i


def _classify_missing_open(buf: str, i: int) -> tuple[str, int]:
    """Classify a word-boundary `_` at `buf[i]` (DeepSeek drops `<response`)."""
    tail = buf[i:]
    if tail[: len(_MISSING_OPEN_PREFIX)].lower() == _MISSING_OPEN_PREFIX:
        action, length = _kind_body_status(tail[len(_MISSING_OPEN_PREFIX) :])
        return action, i + len(_MISSING_OPEN_PREFIX) + length
    if len(tail) < len(_MISSING_OPEN_PREFIX) and _MISSING_OPEN_PREFIX.startswith(tail.lower()):
        return "hold", i
    return "text", i


def _held_missing_open_index(state: ScannerState, buf: str) -> int:
    """Index of the first word-boundary `_kind>` in `buf` that may still complete, or -1."""
    lower = buf.lower()
    start = lower.find(_MISSING_OPEN_PREFIX)
    while start != -1:
        prev = buf[start - 1] if start > 0 else state.last_word_char
        if not _is_ascii_word(prev) and _classify_missing_open(buf, start)[0] == "hold":
            return start
        start = lower.find(_MISSING_OPEN_PREFIX, start + 1)
    return -1


def _feed_headline(
    state: ScannerState,
    chunk: str,
) -> tuple[str, ScannerState]:
    """Post-lock marker filter: headline capture plus repeated-marker drop.

    Runs for every delta after the response_kind decision is locked, for the
    rest of the Lead turn. Buffers only text that can still become a marker
    (a `<response_kind>` / `_kind>` with an enum body, or an unclosed
    single-line `<headline>` within the cap), drops complete markers and
    captures the first headline (state.headline_text; the caller dispatches the
    SSE `headline` event). Orphan close tags and non-enum bodies are text, as
    in the sanitizer. `flush` releases whatever is still buffered at the end
    of an LLM call.

      R-2: single-line body > _MAX_HEADLINE_BUFFER without close tag → truncated.
      R-3: handled by `finalize_headline` at stream-end.
      R-4: later `<headline>` spans → dropped (FIRST-WINS).
    """
    buf = state.headline_buffer + chunk
    state.headline_buffer = ""
    out: list[str] = []
    start = 0
    pos = 0
    while True:
        m = _MARKER_CANDIDATE_RE.search(buf, pos)
        if not m:
            break
        i = m.start()
        if buf[i] == "<":
            action, end = _classify_angle(state, buf, i)
        else:
            prev = buf[i - 1] if i > 0 else state.post_lock_prev_char
            action, end = ("text", i) if _is_ascii_word(prev) else _classify_missing_open(buf, i)
        if action == "text":
            pos = i + 1
            continue
        out.append(buf[start:i])
        if action == "hold":
            state.headline_buffer = buf[i:]
            start = len(buf)
            break
        start = pos = end
    out.append(buf[start:])
    safe = "".join(out)
    if safe:
        state.post_lock_prev_char = safe[-1]
    return safe, state


def flush(state: ScannerState) -> tuple[str, ScannerState]:
    """Release text still buffered at the end of an LLM call (or the turn).

    Held text is a marker that never completed, so it is prose: return it with
    any complete markers removed. Call at each LLM-call boundary (before tool
    events, so narrative order is preserved) and before `finalize_headline`.
    """
    held = state.buffer + state.headline_buffer
    state.buffer = ""
    state.headline_buffer = ""
    if not held:
        return "", state
    text, _removed = _remove_markers_inline(held)
    if text:
        state.post_lock_prev_char = text[-1]
        state.last_word_char = text[-1] if _is_ascii_word(text[-1]) else ""
    return text, state


def _remove_markers_inline(text: str) -> tuple[str, bool]:
    cleaned = _POST_HOC_SANITIZE_RE.sub("", text)
    return cleaned, cleaned != text


def finalize_headline(state: ScannerState) -> ScannerState:
    """Stream-end finalizer — applies R-3 if headline phase still open.

    Called by the orchestrator after the last delta is fed. If we entered the
    headline phase but never observed `</headline>`, mark state as
    "missing-close" (open was seen but body not closed) or "absent" (no open
    tag observed).
    """
    if state.headline_locked or not state.locked:
        return state
    # If response_kind decision locked but headline never closed:
    if state.headline_seen_open:
        # R-3: open was emitted but close never arrived → missing-close.
        # Best-effort: surface any body that was captured between open and
        # buffer end (capped at _MAX_HEADLINE_BUFFER).
        lower = state.headline_buffer.lower()
        open_idx = lower.find(_HEADLINE_OPEN)
        if open_idx >= 0:
            body_start = open_idx + len(_HEADLINE_OPEN)
            body = state.headline_buffer[body_start : body_start + _MAX_HEADLINE_BUFFER]
            state.headline_text = body if body else None
        state.headline_decision = "missing-close"
    else:
        state.headline_decision = "absent"
    state.headline_locked = True
    state.headline_buffer = ""
    return state


def _find_safe_cut(buf: str) -> int:
    """Largest prefix len whose remaining suffix cannot start a marker token.

    A suffix is "unsafe" (cannot cut) if it might be the start of `<` or
    `_kind>` literal — we need to keep it buffered for next chunk.
    """
    n = len(buf)
    # Search backwards from end for the leftmost position whose suffix is
    # potentially marker-leading.
    cut = n
    lookahead_start = max(0, n - _MAX_LOOKAHEAD)
    suffix = buf[lookahead_start:]
    lower = suffix.lower()
    # If suffix contains `<` or `_kind`, must keep from that position onward.
    # Find earliest unsafe char in the lookahead window.
    angle = lower.find("<")
    if angle != -1:
        cut = min(cut, lookahead_start + angle)
    # `_kind` — only worry if seen in stream; partial `_ki`, `_kin` etc. also
    # need buffering. Detect any prefix of `_kind` at end of buffer.
    for tail_len in range(1, min(5, len(suffix)) + 1):
        tail = suffix[-tail_len:].lower()
        if "_kind>"[:tail_len] == tail:
            # End of buffer could be start of `_kind>` — keep from this point.
            cut = min(cut, n - tail_len)
            break
    return cut
