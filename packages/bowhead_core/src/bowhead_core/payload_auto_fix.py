"""Phase A — soft-gate payload validation auto-fix.

Sprint agent-redesign-1 (2026-05-15).

Converts hard-rejected payloads into accepted-with-fixes via deterministic
transforms. 21 rules soft-degrade in a single ordered pass; 8 stay hard
(enforced by `validate_payload`).

Entry: `auto_fix_payload(payload)` returns `(fixed_payload, fixes, hard_reject)`.

Design notes:
- Single-pass deterministic order (no fixed-point iteration → no infinite loops).
- Each rule is one small helper; testable in isolation.
- Mutates payload in place (caller already holds the parsed dict; saves a copy).
- Auto-fix records exposed to LLM via tool_result _auto_fixed_fields field so
  peer can learn to comply on subsequent turns (PRD r2 open Q5 resolution).

Wire site: `subagent_return_tool.py` between args-parse and validate_payload.
Flag: `BOWHEAD_PAYLOAD_SOFT_GATE` (default ON; off → behavior identical to today).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from bowhead_core.analysis_payload import (
    _EVIDENCE_MAX,
    _INSIGHTS_MAX,
    _NARR_MAX,
    _NARR_MIN,
    _SHELL_PLACEHOLDER_PATTERNS,
    _VALID_BAIL_KINDS,
    _VALID_CONFIDENCE,
    _VALID_EV_KINDS,
    _VALID_PRIORITIES,
    _VALID_SENTIMENT,
    is_deliverable_payload,
    is_short_answer_payload,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────


@dataclass
class FixRecord:
    """A single auto-fix record. Surfaced to LLM via tool_result."""

    rule: str  # e.g. "R20"
    field_path: str  # e.g. "kpis[0].sentiment"
    action: str  # "coerce" | "synthesize" | "drop" | "truncate" | "demote"
    original_excerpt: str = ""  # ≤80 chars for LLM context

    def to_llm_dict(self) -> dict[str, str]:
        return {
            "rule": self.rule,
            "field": self.field_path,
            "action": self.action,
            "before": self.original_excerpt[:80] if self.original_excerpt else "",
        }


@dataclass
class AutoFixResult:
    payload: dict[str, Any]
    fixes: list[FixRecord] = field(default_factory=list)
    hard_reject: bool = False
    hard_reject_reason: str = ""


# Coercion maps (exposed for test introspection)
SENTIMENT_COERCE_MAP = {
    "pos": "good",
    "positive": "good",
    "good": "good",
    "neg": "bad",
    "negative": "bad",
    "bad": "bad",
    "neutral": "neutral",
    "neu": "neutral",
    "": "neutral",
    # chat-v4 CHANGE-S.4 — pass-through + common LLM variants.
    "warning": "warning",
    "warn": "warning",
    "caution": "warning",
    "yellow": "warning",
}

CONFIDENCE_COERCE_MAP = {
    "high": "high",
    "h": "high",
    "med": "med",
    "medium": "med",
    "m": "med",
    "low": "low",
    "l": "low",
    "": "med",
}

PRIORITY_COERCE_MAP = {
    "p0": "P0",
    "p1": "P1",
    "p2": "P2",
    "high": "P0",
    "critical": "P0",
    "medium": "P1",
    "med": "P1",
    "low": "P2",
    "": "P1",
}


def auto_fix_payload(payload: dict[str, Any], *, revision: bool = False) -> AutoFixResult:
    """Apply 21 soft-degrade fixes in deterministic order.

    Single pass; if all insights drop during R9 cleanup, sets hard_reject=True
    (caller emits R7 hard reject).

    ``revision=True``（canvas-update-turn 批 B0，Lead 档位 revise/append）：薄修订本就可以零洞察
    （正文已 write_report 就地改了），R7 双空不硬拒——与 ``validate_payload(revision=True)`` 同口径。

    Returns AutoFixResult with mutated payload + fix records.
    """
    if not isinstance(payload, dict):
        return AutoFixResult(
            payload=payload,
            fixes=[],
            hard_reject=True,
            hard_reject_reason="payload not dict (R3 hard)",
        )

    fixes: list[FixRecord] = []

    # N1 first — every short-circuit below (bail / deliverable / short-answer) would
    # otherwise skip it, and chat answers take the short-answer exit. Idempotent, so
    # the unconditional call in `subagent_return_tool` (soft gate off) is free here.
    fix_escaped_newlines(payload, fixes)

    # Bail envelope short-circuit (R4/R5 only)
    if isinstance(payload.get("error"), dict):
        _fix_bail_kind(payload, fixes)
        _fix_bail_message(payload, fixes)
        return AutoFixResult(payload=payload, fixes=fixes)

    # pptx-from-template M3 (RD-4): deliverable short-circuit (a deliberate
    # exemption, NOT a fix to R7). A deck/render leaf returns a downloadable
    # artifact (non-empty `deliverables`) with no insights. The insight-fix
    # pipeline below targets insights, so it would no-op on a deck payload and then
    # the Step-5 R7 dual-empty check — which correctly rejects *analysis* payloads
    # with no insights — would wrongly reject this *deck* payload. Deck payloads are
    # a different type with different rules, so short-circuit straight to a clean
    # result; validate_payload's deliverable branch does the real validation.
    if is_deliverable_payload(payload):
        return AutoFixResult(payload=payload, fixes=fixes)

    # DF-04: simple scalar/small-comparison answers intentionally have no
    # insights or recommendations.  Coerce the only relevant enum, then stop
    # before the report-only R7 dual-empty gate.
    if is_short_answer_payload(payload):
        _coerce_sentiments(payload, fixes)
        return AutoFixResult(payload=payload, fixes=fixes)

    # delivery-surface C6: delivery_surface is a **suggestion**, not a verdict. A chat declaration exceeding
    # short-answer bounds (not short-circuited above) → downgrade: pop the field, record a fix, and continue through
    # the report/legacy pipeline. Previously this shape triggered validate's "requires the bounded short-answer
    # shape" plus missing title/recommendations hard rejection; rejection wording itself taught the peer to revert
    # to report mode (116 t_0f77a0a0bd3b: the peer said to remove delivery_surface and use
    # the standard report payload). Lead/terminal evidence ultimately decides the delivery surface, not this field.
    if payload.get("delivery_surface") == "chat":
        payload.pop("delivery_surface", None)
        fixes.append(
            FixRecord(
                rule="C6",
                field_path="delivery_surface",
                action="demote",
                original_excerpt="chat（超出短答边界，降级为建议；按 report 校验）",
            )
        )
        logger.info("payload_auto_fix.delivery_surface_downgraded")

    # Step 1: coerce enums (idempotent string normalization)
    _coerce_sentiments(payload, fixes)  # R20
    _coerce_priorities(payload, fixes)  # R17
    _coerce_confidences(payload, fixes)  # R15
    _coerce_evidence_kinds(payload, fixes)  # R11

    # Step 2: synthesize missing required defaults
    _synth_top_level_defaults(payload, fixes)  # R6
    _synth_evidence_captions(payload, fixes)  # R12

    # Step 3: drop bad sub-items (preserve siblings)
    _drop_invalid_insights(payload, fixes)  # R9 + R16
    _drop_invalid_evidence(payload, fixes)  # R13
    _drop_invalid_recommendations(payload, fixes)  # R18

    # Step 4: range / truncate / demote
    _truncate_insights(payload, fixes)  # R8 (>7 only; <3 was wrong in PRD r1)
    _truncate_evidence_per_insight(payload, fixes)  # R10
    _truncate_narratives(payload, fixes)  # R14
    _demote_p0_no_owner(payload, fixes)  # R19
    _drop_shell_impact_note(payload, fixes)  # R25

    # Step 5: R7 dual-empty re-check
    insight_section_ids = payload.get("insight_section_ids") or []
    is_thin = bool(insight_section_ids)
    insights_remaining = payload.get("insights") or []
    if not is_thin and not insights_remaining and not revision:
        return AutoFixResult(
            payload=payload,
            fixes=fixes,
            hard_reject=True,
            hard_reject_reason=(
                "R7: all insights dropped during auto-fix; peer produced no usable "
                "report content. Re-emit with valid insights[] (每条 ≥1 evidence, narrative ≥30 字, "
                "无空壳词); 没有真数据就改返 bail 信封 error.kind, 别硬凑."
            ),
        )

    return AutoFixResult(payload=payload, fixes=fixes)


# ─────────────────────────────────────────────────────────────
# N1 — escaped newlines in prose (called unconditionally, see docstring)
# ─────────────────────────────────────────────────────────────

# Code must survive verbatim: a fenced block or an inline span may legitimately
# contain a two-character \n (a regex, a Python string). Only prose is unescaped.
_CODE_SPAN_RE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
# A single backslash + n. The lookbehind spares `\\n` (an intentional escaped
# backslash), which is what a peer writes when it really means the two characters.
_LITERAL_NL_RE = re.compile(r"(?<!\\)\\n")

# Prose fields, by path shape. KPI values, chart specs, refs and ids are excluded
# on purpose — they are data, and a stray \n there is not a paragraph break.
_PROSE_KEYS = ("exec_summary", "narrative", "title", "report_title", "eyebrow", "fallback_markdown")
_INSIGHT_PROSE_KEYS = ("title", "thesis", "narrative")


def unescape_prose_newlines(text: str) -> tuple[str, int]:
    """`\\n` written as two characters → a real newline, outside code. Returns (text, hits)."""
    out: list[str] = []
    hits = 0
    pos = 0
    for m in _CODE_SPAN_RE.finditer(text):
        seg, n = _LITERAL_NL_RE.subn("\n", text[pos : m.start()])
        out.append(seg)
        out.append(m.group(0))
        hits += n
        pos = m.end()
    tail, n = _LITERAL_NL_RE.subn("\n", text[pos:])
    out.append(tail)
    return "".join(out), hits + n


def fix_escaped_newlines(payload: dict[str, Any], fixes: list[FixRecord]) -> None:
    """Normalize escaped prose newlines before early-return and optional autofix paths.

    Figure anchors require real line boundaries. Run this normalization even when
    soft-gated autofixes are disabled, preserving the existing anchor contract.
    """
    for key in _PROSE_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and "\\n" in value:
            fixed, hits = unescape_prose_newlines(value)
            if hits:
                payload[key] = fixed
                fixes.append(FixRecord(rule="N1", field_path=key, action="coerce", original_excerpt=value[:80]))

    err = payload.get("error")
    if isinstance(err, dict) and isinstance(err.get("message"), str) and "\\n" in err["message"]:
        fixed, hits = unescape_prose_newlines(err["message"])
        if hits:
            err["message"] = fixed
            fixes.append(FixRecord(rule="N1", field_path="error.message", action="coerce", original_excerpt=err["message"][:80]))

    for i, ins in enumerate(payload.get("insights") or []):
        if not isinstance(ins, dict):
            continue
        for key in _INSIGHT_PROSE_KEYS:
            value = ins.get(key)
            if isinstance(value, str) and "\\n" in value:
                fixed, hits = unescape_prose_newlines(value)
                if hits:
                    ins[key] = fixed
                    fixes.append(FixRecord(rule="N1", field_path=f"insights[{i}].{key}", action="coerce", original_excerpt=value[:80]))
        for j, ev in enumerate(ins.get("evidence") or []):
            if isinstance(ev, dict) and isinstance(ev.get("caption"), str) and "\\n" in ev["caption"]:
                fixed, hits = unescape_prose_newlines(ev["caption"])
                if hits:
                    ev["caption"] = fixed
                    fixes.append(
                        FixRecord(rule="N1", field_path=f"insights[{i}].evidence[{j}].caption", action="coerce", original_excerpt=ev["caption"][:80])
                    )

    for i, rec in enumerate(payload.get("recommendations") or []):
        if isinstance(rec, dict) and isinstance(rec.get("action"), str) and "\\n" in rec["action"]:
            fixed, hits = unescape_prose_newlines(rec["action"])
            if hits:
                rec["action"] = fixed
                fixes.append(FixRecord(rule="N1", field_path=f"recommendations[{i}].action", action="coerce", original_excerpt=rec["action"][:80]))

    cavs = payload.get("caveats")
    if isinstance(cavs, list):
        for i, cav in enumerate(cavs):
            if isinstance(cav, str) and "\\n" in cav:
                fixed, hits = unescape_prose_newlines(cav)
                if hits:
                    cavs[i] = fixed
                    fixes.append(FixRecord(rule="N1", field_path=f"caveats[{i}]", action="coerce", original_excerpt=cav[:80]))


# ─────────────────────────────────────────────────────────────
# Per-rule fixers
# ─────────────────────────────────────────────────────────────


def _fix_bail_kind(payload: dict, fixes: list[FixRecord]) -> None:
    """R4: error.kind not in bail set → coerce to 'tool_failure'."""
    err = payload.get("error")
    if not isinstance(err, dict):
        return
    kind = err.get("kind")
    if kind in _VALID_BAIL_KINDS:
        return
    err["kind"] = "tool_failure"
    fixes.append(
        FixRecord(
            rule="R4",
            field_path="error.kind",
            action="coerce",
            original_excerpt=f"{kind!r}→tool_failure",
        )
    )


def _fix_bail_message(payload: dict, fixes: list[FixRecord]) -> None:
    """R5: error.message missing/empty → synthesize default."""
    err = payload.get("error")
    if not isinstance(err, dict):
        return
    msg = err.get("message")
    if isinstance(msg, str) and msg.strip():
        return
    err["message"] = "peer bailed without explicit reason (auto-synthesized R5)"
    fixes.append(
        FixRecord(
            rule="R5",
            field_path="error.message",
            action="synthesize",
            original_excerpt="empty→synth default",
        )
    )


def _coerce_sentiments(payload: dict, fixes: list[FixRecord]) -> None:
    """R20: kpis[i].sentiment not in {good,bad,neutral} → coerce."""
    for i, k in enumerate(payload.get("kpis") or []):
        if not isinstance(k, dict):
            continue
        s = k.get("sentiment")
        if s is None or s in _VALID_SENTIMENT:
            continue
        s_norm = str(s).strip().lower()
        new_s = SENTIMENT_COERCE_MAP.get(s_norm, "neutral")
        k["sentiment"] = new_s
        fixes.append(
            FixRecord(
                rule="R20",
                field_path=f"kpis[{i}].sentiment",
                action="coerce",
                original_excerpt=f"{s!r}→{new_s}",
            )
        )


def _coerce_priorities(payload: dict, fixes: list[FixRecord]) -> None:
    """R17: recommendations[i].priority not in {P0,P1,P2} → coerce."""
    for i, r in enumerate(payload.get("recommendations") or []):
        if not isinstance(r, dict):
            continue
        p = r.get("priority")
        if p in _VALID_PRIORITIES:
            continue
        p_norm = str(p or "").strip().lower()
        new_p = PRIORITY_COERCE_MAP.get(p_norm, "P1")
        r["priority"] = new_p
        fixes.append(
            FixRecord(
                rule="R17",
                field_path=f"recommendations[{i}].priority",
                action="coerce",
                original_excerpt=f"{p!r}→{new_p}",
            )
        )


def _coerce_confidences(payload: dict, fixes: list[FixRecord]) -> None:
    """R15: insights[i].confidence not in {high,med,low} → coerce."""
    for i, ins in enumerate(payload.get("insights") or []):
        if not isinstance(ins, dict):
            continue
        c = ins.get("confidence")
        if c is None or c in _VALID_CONFIDENCE:
            continue
        c_norm = str(c).strip().lower()
        new_c = CONFIDENCE_COERCE_MAP.get(c_norm, "med")
        ins["confidence"] = new_c
        fixes.append(
            FixRecord(
                rule="R15",
                field_path=f"insights[{i}].confidence",
                action="coerce",
                original_excerpt=f"{c!r}→{new_c}",
            )
        )


def _coerce_evidence_kinds(payload: dict, fixes: list[FixRecord]) -> None:
    """R11: evidence[i].kind unknown → coerce to 'callout'.

    Synthesize callout_text from caption if missing. Mark as _synthetic=True
    so FE can hide from citation render (Risk P0-1 from review).
    """
    for i, ins in enumerate(payload.get("insights") or []):
        if not isinstance(ins, dict):
            continue
        for j, e in enumerate(ins.get("evidence") or []):
            if not isinstance(e, dict):
                continue
            kind = e.get("kind")
            if kind in _VALID_EV_KINDS:
                continue
            original_kind = kind
            e["kind"] = "callout"
            e["_synthetic"] = True
            if not e.get("callout_text"):
                e["callout_text"] = e.get("caption") or "(自动生成: 来源未明)"
            fixes.append(
                FixRecord(
                    rule="R11",
                    field_path=f"insights[{i}].evidence[{j}].kind",
                    action="coerce",
                    original_excerpt=f"{original_kind!r}→callout",
                )
            )


def _synth_top_level_defaults(payload: dict, fixes: list[FixRecord]) -> None:
    """R6: missing top-level keys → synth empty defaults.

    Only synth string keys (title, exec_summary); list keys left missing → caught
    by R9/R10/R7 cascade or downstream validators.
    """
    string_defaults = {"title": "", "exec_summary": ""}
    for k, default in string_defaults.items():
        if k not in payload or payload.get(k) in (None,):
            payload[k] = default
            fixes.append(
                FixRecord(
                    rule="R6",
                    field_path=k,
                    action="synthesize",
                    original_excerpt=f"missing→{default!r}",
                )
            )


def _synth_evidence_captions(payload: dict, fixes: list[FixRecord]) -> None:
    """R12: evidence[i].caption missing → synth from kind."""
    kind_to_caption = {
        "chart": "图表",
        "computation": "计算",
        "table": "数据",
        "callout": "备注",
    }
    for i, ins in enumerate(payload.get("insights") or []):
        if not isinstance(ins, dict):
            continue
        for j, e in enumerate(ins.get("evidence") or []):
            if not isinstance(e, dict):
                continue
            if e.get("caption"):
                continue
            kind = e.get("kind", "callout")
            e["caption"] = kind_to_caption.get(kind, "备注")
            fixes.append(
                FixRecord(
                    rule="R12",
                    field_path=f"insights[{i}].evidence[{j}].caption",
                    action="synthesize",
                    original_excerpt=f"empty→{e['caption']!r}",
                )
            )


def _drop_invalid_insights(payload: dict, fixes: list[FixRecord]) -> None:
    """R9 + R16: insights[i] missing core keys / not dict → drop entry."""
    insights = payload.get("insights")
    if not isinstance(insights, list):
        return
    kept: list[Any] = []
    for i, ins in enumerate(insights):
        if not isinstance(ins, dict):
            fixes.append(
                FixRecord(
                    rule="R16",
                    field_path=f"insights[{i}]",
                    action="drop",
                    original_excerpt=f"non-dict: {type(ins).__name__}",
                )
            )
            continue
        # R9: require title + thesis + narrative non-empty; evidence may be []
        # (R10 synthesizes callout from thesis when evidence is empty).
        missing = []
        for k in ("title", "thesis", "narrative"):
            if k not in ins or ins[k] in (None, ""):
                missing.append(k)
        if "evidence" not in ins:
            missing.append("evidence")
        if missing:
            fixes.append(
                FixRecord(
                    rule="R9",
                    field_path=f"insights[{i}]",
                    action="drop",
                    original_excerpt=f"missing: {','.join(missing)}",
                )
            )
            continue
        kept.append(ins)
    payload["insights"] = kept


def _drop_invalid_evidence(payload: dict, fixes: list[FixRecord]) -> None:
    """R13: evidence[j].kind-specific required field missing.

    Behavior:
      - If kind ∈ {chart, computation, table} and ID field missing, BUT the
        entry has a usable `callout_text` (or `caption`) — convert kind to
        ``callout`` in-place (rule R13-c, 0.39.7). Preserves the prose the
        peer wrote instead of dropping it (was the prod walkthrough failure:
        peer wrote a real analytical note but forgot to attach the
        computation_artifact_id → R13 dropped → R10 synth a thinner callout
        from thesis → ξ gate then rejected for missing density markers).
      - Otherwise drop (legacy behavior).
    """
    expected_field_by_kind = {
        "chart": "chart_section_id",
        "computation": "computation_artifact_id",
        "table": "table_artifact_id",
        "callout": "callout_text",
    }
    for i, ins in enumerate(payload.get("insights") or []):
        if not isinstance(ins, dict):
            continue
        ev = ins.get("evidence") or []
        if not isinstance(ev, list):
            continue
        kept_ev: list[Any] = []
        for j, e in enumerate(ev):
            if not isinstance(e, dict):
                fixes.append(
                    FixRecord(
                        rule="R13",
                        field_path=f"insights[{i}].evidence[{j}]",
                        action="drop",
                        original_excerpt="non-dict",
                    )
                )
                continue
            kind = e.get("kind")
            expected = expected_field_by_kind.get(kind)
            if expected and not e.get(expected):
                # 0.39.7 #7 (R13-c convert): for chart/computation/table whose
                # ID is missing, salvage as callout when callout_text exists
                # and is substantive (≥20 chars). Caption alone is too thin
                # to qualify — it's usually structural/short and would just
                # waste a slot. Drop otherwise (legacy behavior).
                if kind in ("chart", "computation", "table"):
                    callout_text = str(e.get("callout_text") or "").strip()
                    if len(callout_text) >= 20:
                        e["kind"] = "callout"
                        fixes.append(
                            FixRecord(
                                rule="R13-c",
                                field_path=f"insights[{i}].evidence[{j}]",
                                action="convert",
                                original_excerpt=(f"kind={kind!r} missing {expected} → callout (preserved {len(callout_text)} ch)"),
                            )
                        )
                        kept_ev.append(e)
                        continue
                fixes.append(
                    FixRecord(
                        rule="R13",
                        field_path=f"insights[{i}].evidence[{j}]",
                        action="drop",
                        original_excerpt=f"kind={kind!r} missing {expected}",
                    )
                )
                continue
            kept_ev.append(e)
        ins["evidence"] = kept_ev


def _drop_invalid_recommendations(payload: dict, fixes: list[FixRecord]) -> None:
    """R18: recommendations[i].action missing → drop entry."""
    recs = payload.get("recommendations")
    if not isinstance(recs, list):
        return
    kept: list[Any] = []
    for i, r in enumerate(recs):
        if not isinstance(r, dict):
            fixes.append(
                FixRecord(
                    rule="R18",
                    field_path=f"recommendations[{i}]",
                    action="drop",
                    original_excerpt=f"non-dict: {type(r).__name__}",
                )
            )
            continue
        if not r.get("action"):
            fixes.append(
                FixRecord(
                    rule="R18",
                    field_path=f"recommendations[{i}]",
                    action="drop",
                    original_excerpt="missing action",
                )
            )
            continue
        kept.append(r)
    payload["recommendations"] = kept


def _truncate_insights(payload: dict, fixes: list[FixRecord]) -> None:
    """R8: insights count > 7 → truncate to 7. (Range <3 no longer enforced.)"""
    insights = payload.get("insights")
    if not isinstance(insights, list):
        return
    if len(insights) <= _INSIGHTS_MAX:
        return
    original_n = len(insights)
    payload["insights"] = insights[:_INSIGHTS_MAX]
    fixes.append(
        FixRecord(
            rule="R8",
            field_path="insights",
            action="truncate",
            original_excerpt=f"{original_n}→{_INSIGHTS_MAX}",
        )
    )


def _truncate_evidence_per_insight(payload: dict, fixes: list[FixRecord]) -> None:
    """R10: evidence count >5 → truncate to 5. count=0 → synth callout from thesis.

    Mark synthesized callout with _synthetic=True (Risk P0-1).
    """
    for i, ins in enumerate(payload.get("insights") or []):
        if not isinstance(ins, dict):
            continue
        ev = ins.get("evidence")
        if not isinstance(ev, list):
            continue
        if len(ev) > _EVIDENCE_MAX:
            original_n = len(ev)
            ins["evidence"] = ev[:_EVIDENCE_MAX]
            fixes.append(
                FixRecord(
                    rule="R10",
                    field_path=f"insights[{i}].evidence",
                    action="truncate",
                    original_excerpt=f"{original_n}→{_EVIDENCE_MAX}",
                )
            )
        elif len(ev) == 0:
            thesis = ins.get("thesis") or ""
            ins["evidence"] = [
                {
                    "kind": "callout",
                    "caption": "备注",
                    "callout_text": str(thesis)[:200] if thesis else "(自动生成: 来源未明)",
                    "_synthetic": True,
                }
            ]
            fixes.append(
                FixRecord(
                    rule="R10",
                    field_path=f"insights[{i}].evidence",
                    action="synthesize",
                    original_excerpt=f"0→1 callout from thesis ({len(str(thesis))} ch)",
                )
            )


def _truncate_narratives(payload: dict, fixes: list[FixRecord]) -> None:
    """R14: narrative len not in [30,400].

    <30: pad to 30 with "...(详见图表)".
    >400: truncate at last sentence boundary before 400; fallback hard cut.
    """
    for i, ins in enumerate(payload.get("insights") or []):
        if not isinstance(ins, dict):
            continue
        narr = ins.get("narrative") or ""
        narr_str = str(narr)
        if _NARR_MIN <= len(narr_str) <= _NARR_MAX:
            continue
        if len(narr_str) < _NARR_MIN:
            # pad with deterministic suffix until length ≥ _NARR_MIN
            padding = "（详见图表与数据来源；本结论基于已展示的图表与数据汇总。）"
            new_narr = narr_str + padding
            # if still short, keep appending until min reached
            while len(new_narr) < _NARR_MIN:
                new_narr += padding
            ins["narrative"] = new_narr
            fixes.append(
                FixRecord(
                    rule="R14",
                    field_path=f"insights[{i}].narrative",
                    action="synthesize",
                    original_excerpt=f"len={len(narr_str)}<30→pad",
                )
            )
        else:  # >400
            # truncate at last 。 before _NARR_MAX, fallback to hard cut
            cut = narr_str.rfind("。", 0, _NARR_MAX)
            if cut < _NARR_MIN:
                cut = _NARR_MAX
            else:
                cut += 1  # keep the period
            new_narr = narr_str[:cut]
            ins["narrative"] = new_narr
            fixes.append(
                FixRecord(
                    rule="R14",
                    field_path=f"insights[{i}].narrative",
                    action="truncate",
                    original_excerpt=f"len={len(narr_str)}→{len(new_narr)}",
                )
            )


def _demote_p0_no_owner(payload: dict, fixes: list[FixRecord]) -> None:
    """R19: recommendations[i] is P0 but owner missing → demote to P1."""
    for i, r in enumerate(payload.get("recommendations") or []):
        if not isinstance(r, dict):
            continue
        if r.get("priority") != "P0":
            continue
        if r.get("owner"):
            continue
        r["priority"] = "P1"
        fixes.append(
            FixRecord(
                rule="R19",
                field_path=f"recommendations[{i}].priority",
                action="demote",
                original_excerpt="P0 no owner→P1",
            )
        )


def _drop_shell_impact_note(payload: dict, fixes: list[FixRecord]) -> None:
    """R25: insights[i].impact.note 含空壳词 → drop impact field."""
    for i, ins in enumerate(payload.get("insights") or []):
        if not isinstance(ins, dict):
            continue
        impact = ins.get("impact")
        if not isinstance(impact, dict):
            continue
        note = impact.get("note")
        if not _is_shell_value(note):
            continue
        ins.pop("impact", None)
        fixes.append(
            FixRecord(
                rule="R25",
                field_path=f"insights[{i}].impact",
                action="drop",
                original_excerpt=f"note shell: {str(note)[:40]!r}",
            )
        )


def _is_shell_value(s: Any) -> bool:
    """Mirror analysis_payload._is_shell_value (kept in sync via shared pattern tuple)."""
    if not s:
        return False
    s_norm = str(s).strip().lower()
    if not s_norm:
        return False
    return any(pat.lower() in s_norm for pat in _SHELL_PLACEHOLDER_PATTERNS)


# ─────────────────────────────────────────────────────────────
# Observability helpers
# ─────────────────────────────────────────────────────────────


def log_fix_records(
    fixes: list[FixRecord],
    *,
    turn_id: str = "",
    peer_id: str = "",
) -> None:
    """Emit one warning per fix + aggregate count metric."""
    for f in fixes:
        logger.warning(
            "payload_auto_fix turn=%s peer=%s rule=%s field=%s action=%s excerpt=%s",
            turn_id,
            peer_id,
            f.rule,
            f.field_path,
            f.action,
            f.original_excerpt[:80],
        )
    # Aggregate metric (best-effort)
    try:
        from bowhead_core import metrics_log as _m

        for f in fixes:
            _m.inc(
                "bowhead_payload_auto_fixed_total",
                labels={"rule": f.rule, "action": f.action},
                help="Phase A: soft-degraded payload rule fired count.",
            )
        if len(fixes) >= 8:
            _m.inc(
                "bowhead_payload_fix_drift_warn_total",
                labels={},
                help="Phase A: turn had ≥8 auto-fixes (drift signal).",
            )
    except Exception:  # noqa: S110
        # metrics best-effort; never break peer loop.
        pass


def dedup_fixes_per_rule(fixes: list[FixRecord]) -> list[FixRecord]:
    """Keep first occurrence per rule (for LLM-visible _auto_fixed_fields).

    Avoids overwhelming LLM context with N copies of the same rule firing.
    """
    seen: set[str] = set()
    out: list[FixRecord] = []
    for f in fixes:
        if f.rule in seen:
            continue
        seen.add(f.rule)
        out.append(f)
    return out
