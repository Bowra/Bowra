"""subagent_return — subagent → composer 的结构化输出 tool（M137 #142）.

这是 subagent **唯一**的"我做完了，把分析结果交回去"出口（设计后续替代
free-form final_answer）。当前是双写阶段：subagent 也可以走老的"返 markdown
final_answer"路径，二者并存到 M4 (#145) 才关老路.

# 调用语义

subagent prompt 跑完分析后，**最后一个 tool 调用** 应该是：

    subagent_return(payload={...AnalysisPayload schema...})

handler 做 3 件事：
  1. 用 ``validate_payload`` 校 schema. 失败 → 返 error，subagent 看到错可以
     重试 1-2 次（自身循环负责），不退出.
  2. 通过 ``ctx._m137_returned_payload`` 把 dict 暂存到执行上下文上 ——
     subagent 主循环检测到 ``subagent_return`` 被调用时把它从 ctx 取出，
     落到 SubagentResult.payload.
  3. 返成功 + payload 摘要（让 LLM 看到自己交出去的 insights 数等）

# 终态语义

handler 返回 result 里附 ``__terminal: True`` 标志，subagent 主循环看到这个
flag 不再继续询问 LLM，立刻收尾返 SubagentResult. 即"调完此 tool 即 done"
的契约语义.

# 不在 Lead 的白名单

Lead 不应该自己 pack payload —— 那是 subagent 的活. Lead 拿到 SubagentResult
之后由 composer 直接消费 payload 不再二次加工. 所以本工具只在 compute /
compute 等 subagent profile 的 ``tool_whitelist`` 里出现.

# 测试

见 ``tests/test_subagent_return_tool.py``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from typing import Any

from bowhead_router.types import ToolSchema

from bowhead_core.agents._lead_context import run_invocations, run_ref_allowlist
from bowhead_core.analysis_payload import (
    is_short_answer_payload,
    payload_from_dict,
    validate_payload,
    validate_thin_canvas_sections,
)
from bowhead_core.terminal_effect_contract import TOOL_EFFECT_NONE
from bowhead_core.tool_registry import ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)


def _xi_track_metric(ctx: Any, name: str, **str_attrs: str) -> None:
    """0.39.7 #6 observability — fire-and-forget metric track.

    Sends `name` to the configured MetricsLog (when ctx has one) so the
    `/admin/metrics/rollup?name=<name>` endpoint can aggregate citation /
    ξ-gate event rates over time. Best-effort: any exception swallowed —
    instrumentation must NEVER break a peer return path.

    Pattern mirrors lead_orchestrator.py:1551 (create_task off hot path so
    SQLite INSERT doesn't block the subagent_return tool reply).
    """
    metrics_log = getattr(ctx, "metrics_log", None)
    if metrics_log is None:
        return
    try:
        from bowhead_core.terminal_llm_invocation import (
            create_detached_auxiliary_task,
        )

        attrs = {"turn_id": getattr(ctx, "turn_id", "") or "", **str_attrs}
        create_detached_auxiliary_task(
            metrics_log.track(name, attrs=attrs),
            reason="subagent_return_metric",
        )
    except Exception:  # noqa: S110
        pass


# Attribute holding payload on ctx; the subagent main loop reads this name.
CTX_PAYLOAD_ATTR = "_m137_returned_payload"
# Tool-result terminal-on-return flag; subagent exits immediately when seen.
TERMINAL_FLAG = "__terminal"
# M157: attribute used by turn_runner to inject outline into ctx.
CTX_OUTLINE_ATTR = "_m157_outline"
# cca P3 (S4 §5): team-subagent output_schema gate — per-tc ctx attrs + retry cap.
# The runner stamps the active schema on `_running_output_schema`; the gate caps retries
# then DEGRADES (accepts the AnalysisPayload-valid payload), never loops to the timeout.
CTX_OUTPUT_SCHEMA_ATTR = "_running_output_schema"
# review fix: the retry counter is a DICT keyed by subagent_id (was a flat int), seeded on the
# SHARED parent_ctx alongside the schema (subagent.py / subagent_graph.py stamp sites). Keyed-
# by-sid so a sibling subagent in the same turn can't inherit another's exhausted count
# (langgraph shares parent_ctx directly); a dict (mutated in place) so the count ACCUMULATES
# across a peer's retries even on the legacy path, where each subagent_return runs on a per-call
# copy.copy(ctx) that shares the same dict object by reference. Keep this literal in sync with
# the stamp-site seeds in subagent.py / subagent_graph.py.
CTX_OUTPUT_SCHEMA_RETRY_ATTR = "_p3_output_schema_retry_counts"
_OSCHEMA_MAX_RETRIES = 2
# Skip the (additive) output_schema gate when the payload serializes larger than this —
# bounds the synchronous match-time CPU on the shared event loop (S4 review P2).
_OSCHEMA_PAYLOAD_MAX_BYTES = 262144  # 256 KiB

# ─── Sprint π Phase D Item B — ν1-ref-unresolvable bail ───
# r2 §B: counter promoted to (turn_id, subagent_id)-keyed dict on parent_ctx
# so it survives peer re-spawn within the same turn. Strip-pass still wins
# when applicable; if strip leaves payload empty of evidence, synthesize a
# user-grade bail envelope and break the peer loop with __terminal=True.
CTX_NU_RETRY_COUNTS_ATTR = "_ν_retry_counts"
CTX_CURRENT_SUBAGENT_ID_ATTR = "_current_subagent_id"
PI_B_BAIL_KIND = "ν1_ref_unresolvable"


def _ledger_error_text(result: Any) -> str:
    """Describe reconciliation failures with corrections that do not require a particular Tool."""
    lines = [
        "Numeric ledger reconciliation failed (reconcile_failed): register claimed numbers in ground_facts using verified Tool outputs or a verifiable formula."
    ]
    for rf in (getattr(result, "rejected_facts", None) or [])[:6]:
        lines.append(
            f"  Rejected ground_fact {rf.get('label') or rf.get('key')!r}: value={rf.get('value')} "
            f"formula={rf.get('formula', '')!r}, reason={rf.get('reason')}. Recompute the formula or correct the declared value."
        )
    for u in (getattr(result, "unauthorized", None) or [])[:6]:
        lines.append(
            f"  Unverified number {u.get('raw') or u.get('value')!r} in {u['field']}: "
            "use a verified ledger value, or register a verifiable formula in ground_facts. "
            "If more evidence or computation is needed, use available Tools; otherwise omit the unsupported claim and state the gap."
        )
    lines.append("After correcting the payload, call subagent_return again.")
    return "\n".join(lines)


# ─── Sprint chat-style-fix Phase A3 — header_insight haiku micro-LLM ───
# Pattern mirrors tool_finding.py (1.5s timeout, fire-and-forget, narrative
# agent_type). Adds ≤200 char aggregate prose to payload so FE agsec-head can
# render `.insight` row above the agsec body.

_HEADER_INSIGHT_TIMEOUT_S = 1.5
_HEADER_INSIGHT_MAX_CHARS = 200
_HEADER_INSIGHT_SYSTEM_PROMPT = (
    "你将收到一个数据分析的 insight 列表. 用 ≤200 字的散文综合 1 句 aggregate (含数字 + 主因), 不要 bullet. 直接输出散文, 不带前缀."
)


async def _compose_header_insight(payload: dict[str, Any], ctx: Any) -> None:
    """Compose ≤200 char header_insight via haiku, mutate payload in-place.

    Skip rules:
      - payload is bail envelope (error set) → "" + metric inc.
      - insights list empty → "" + metric inc.
      - router unavailable or timeout → "" + metric inc.

    Latency tracked via observe_header_insight_haiku_latency_ms.
    """
    import time as _hi_time

    # Always set the field (forward-compat with old turn JSON dicts).
    payload.setdefault("header_insight", "")

    # Bail envelope or no insights — no haiku call.
    if payload.get("error") is not None:
        _bump_header_insight_empty("bail_path")
        return
    insights = payload.get("insights") or []
    if not isinstance(insights, list) or len(insights) == 0:
        _bump_header_insight_empty("no_insights")
        return

    # Compose prompt from top-3 insights (thesis + first 80 char narrative).
    parts = []
    for ins in insights[:3]:
        if not isinstance(ins, dict):
            continue
        thesis = str(ins.get("thesis") or "").strip()
        narrative = str(ins.get("narrative") or "").strip()
        if thesis or narrative:
            parts.append(f"- {thesis}: {narrative[:80]}")
    if not parts:
        _bump_header_insight_empty("no_insights")
        return
    user_prompt = "以下是数据分析的 insight 列表. 用 ≤200 字的散文综合成 1 句 aggregate (含数字 + 主因), 不要 bullet:\n" + "\n".join(parts)

    # Look up router on ctx — try direct attrs first, fall back to
    # subagent_runner.router (which is where SubagentRunner stashes it for
    # tool_finding.py reuse).
    router = getattr(ctx, "router", None) or getattr(ctx, "_router", None)
    if router is None:
        _runner = getattr(ctx, "subagent_runner", None)
        if _runner is not None:
            router = getattr(_runner, "router", None)
    if router is None:
        _bump_header_insight_empty("no_router")
        return

    try:
        from bowhead_router.types import LLMRequest, Message
    except Exception:
        _bump_header_insight_empty("router_import_fail")
        return

    import asyncio

    llm_req = LLMRequest(
        model="",  # router resolves via agent_type
        messages=[Message(role="user", content=user_prompt)],
        system=_HEADER_INSIGHT_SYSTEM_PROMPT,
        max_output_tokens=200,
        temperature=0.3,
        stream=False,
    )

    t0 = _hi_time.monotonic()
    try:
        resp = await asyncio.wait_for(
            router.invoke("narrative", llm_req),
            timeout=_HEADER_INSIGHT_TIMEOUT_S,
        )
    except TimeoutError:
        _bump_header_insight_empty("timeout")
        return
    except Exception:
        _bump_header_insight_empty("llm_fail")
        return
    finally:
        try:
            from bowhead_core.agents.chat_style_fix_metrics import (
                observe_header_insight_haiku_latency_ms,
            )

            observe_header_insight_haiku_latency_ms(
                (_hi_time.monotonic() - t0) * 1000.0,
            )
        except Exception:  # noqa: S110
            pass

    text = (getattr(resp, "content", None) or "").strip()
    if not text:
        _bump_header_insight_empty("empty_response")
        return

    truncated = False
    if len(text) > _HEADER_INSIGHT_MAX_CHARS:
        text = text[:_HEADER_INSIGHT_MAX_CHARS]
        truncated = True

    payload["header_insight"] = text
    if truncated:
        try:
            from bowhead_core.agents.chat_style_fix_metrics import (
                inc_header_insight_truncated,
            )

            inc_header_insight_truncated()
        except Exception:  # noqa: S110
            pass


def _bump_header_insight_empty(reason: str) -> None:
    """Best-effort metric — header_insight bail / empty path counter."""
    try:
        from bowhead_core.agents.chat_style_fix_metrics import (
            inc_header_insight_empty,
        )

        inc_header_insight_empty(reason)
    except Exception:  # noqa: S110
        pass


def _build_fallback_narrative() -> str:
    """π Phase D Item B — user-grade prose for ν1 bail envelope.

    r2 §B explicit: do NOT leak internal terms ("注：peer 引用了 N 个未确认 ID")
    to chat. User sees a plain "重述需求" prompt.
    """
    return "我已经基于查到的数据整理了一段文字结论。如果需要更精确的数字, 请告诉我具体想看的指标 / 时间范围 / 维度, 我重新拉一次."


def _payload_has_any_evidence(payload: dict[str, Any]) -> bool:
    """π B — after strip, decide whether payload still carries something user
    can consume.

    Strip nullifies invalid chart_section_id / data_artifact_id and removes
    invalid `[ref:X]` / `[num:V:U:src:S]` markers. Insights with no remaining
    evidence chunk + no narrative are effectively empty. We consider payload
    non-empty if any of:
      - insights[] with ≥1 entry retaining a non-empty narrative or thesis
      - insight_section_ids[] (thin path)
      - kpis[] non-empty
      - recommendations[] non-empty
      - exec_summary ≥ 30 chars
    """
    insight_section_ids = payload.get("insight_section_ids") or []
    if insight_section_ids:
        return True
    kpis = payload.get("kpis") or []
    if kpis:
        return True
    recs = payload.get("recommendations") or []
    if recs:
        return True
    exec_summary = str(payload.get("exec_summary") or "").strip()
    if len(exec_summary) >= 30:
        return True
    insights = payload.get("insights") or []
    for ins in insights:
        if not isinstance(ins, dict):
            continue
        narrative = str(ins.get("narrative") or "").strip()
        thesis = str(ins.get("thesis") or "").strip()
        if narrative or thesis:
            return True
    return False


def _collected_refs_so_far(parent_ctx: Any) -> list[dict[str, Any]]:
    """π B — best-effort accumulate refs from peer invocations for bail
    envelope's evidence_refs[]. Pull from _current_subagent_invocations.
    """
    refs: list[dict[str, Any]] = []
    invocations = run_invocations(parent_ctx)
    for inv in invocations:
        try:
            tn = getattr(inv, "tool_name", "") or ""
            res = getattr(inv, "result", None) or {}
            if not isinstance(res, dict):
                continue
            # bi_fetch_rows / bi_query / kb_search etc — collect their artifact_id / chunk_ids
            # χ-3.1 P0-C2: bi_query batch shape {results: [{artifact_id, ...}, ...]} —
            # collect EACH sub-result's _ref_artifact_id (artifact_id mirror).
            sub_results = res.get("results")
            if isinstance(sub_results, list):
                for sub in sub_results:
                    if not isinstance(sub, dict):
                        continue
                    sub_aid = sub.get("_ref_artifact_id") or sub.get("artifact_id")
                    if sub_aid:
                        refs.append({"kind": "data_artifact", "id": str(sub_aid), "source_tool": tn})
            # Legacy flat shape — N=1 backward-compat path (or fetch_rows / kb_search).
            art_id = res.get("artifact_id") or res.get("data_artifact_id")
            if art_id:
                refs.append({"kind": "data_artifact", "id": str(art_id), "source_tool": tn})
            chunk_ids = res.get("chunk_ids") or []
            for cid in chunk_ids:
                if cid:
                    refs.append({"kind": "chunk", "id": str(cid), "source_tool": tn})
        except Exception:  # noqa: S112
            continue
    return refs


def _get_nu_retry_key(
    parent_ctx: Any,
    *,
    captured_subagent_id: str | None = None,
) -> tuple[str, str]:
    """π B — counter key on parent_ctx _ν_retry_counts dict.

    Use (turn_id, subagent_id) per r2 §B + Eng P1-3. If `_current_subagent_id`
    is unset (legacy tests / direct handler calls), fall back to a default
    bucket so old test paths still keep a counter.

    CTO P0-1 fix: accept `captured_subagent_id` snapshot taken sync at handler
    entry. Re-reading `ctx._current_subagent_id` AFTER an `await` is unsafe
    under concurrent peer dispatch on the same parent_ctx — coroutine A may
    observe coroutine B's overwrite. When caller passes a captured value, use
    it verbatim; only fall back to ctx attr (and "default") when None passed
    (legacy / non-peer paths).
    """
    turn_id = getattr(parent_ctx, "turn_id", "") or ""
    sid: str | None = captured_subagent_id
    if sid is None:
        sid = getattr(parent_ctx, CTX_CURRENT_SUBAGENT_ID_ATTR, None)
    return (str(turn_id), str(sid or "default"))


def _ensure_nu_retry_counts(parent_ctx: Any) -> dict[tuple[str, str], int]:
    """π B — lazy init of `_ν_retry_counts` dict on parent_ctx."""
    counts = getattr(parent_ctx, CTX_NU_RETRY_COUNTS_ATTR, None)
    if not isinstance(counts, dict):
        counts = {}
        with contextlib.suppress(AttributeError, TypeError):
            setattr(parent_ctx, CTX_NU_RETRY_COUNTS_ATTR, counts)
    return counts


# ─── ξ B7: backend gate for citation density ──
# B5 viz gate (add_chart + bi_fetch_rows hard requirement) was deleted in
# chat-truth sprint (2026-05-14): it contradicted the prompt prohibiting canvas writing and
# forced strict viz requirement that even strongest LLMs couldn't follow reliably.
# composer mini fallback (B-5.5) handles chart_section_id missing case server-side.
# Citation density (B7) remains: a universal contract consistent with the prompt.
# The φ Phase F narrative gate was bound to the canvas-writer profile only and was
# removed with that agent (2026-09-30).

# Match `[num:5.37:次]` / `[ref:1]` / `[ref:chunk_xxx]` patterns
_CITATION_MARKER_RE = re.compile(r"\[(num|ref|cite):[^\]]+\]")
_CITE_MARKER_RE = re.compile(r"\[cite:\d+\]")


def _payload_has_analytical_content(payload: dict[str, Any]) -> bool:
    """Decide whether payload represents a "report" — needs citations.

    Returns True if peer claims insights / kpis / recommendations / non-trivial
    narrative — gates that say "this is a report not a chat answer".
    """
    insights = payload.get("insights") or []
    insight_section_ids = payload.get("insight_section_ids") or []
    kpis = payload.get("kpis") or []
    recs = payload.get("recommendations") or []
    exec_summary = str(payload.get("exec_summary") or "").strip()
    return bool(insights or insight_section_ids or kpis or recs or len(exec_summary) > 100)


def _insights_are_callout_only(payload: dict[str, Any]) -> bool:
    """0.39.5 (prod walkthrough): true iff payload has ≥1 insight AND ALL
    insights are callout-only (no chart/computation/table artifact in evidence).

    Real prod (turn t_af5d8dbe): peer compute LLM emits insights w/ kind=
    'computation' but forgets the artifact_id; payload_auto_fix R13 drops the
    bad evidence and R10 synthesizes callout-only replacement. The reform's
    auto-append decision table needs chart/computation/table evidence to find
    a citation target — callout-only insights have nothing to register.

    Narrow scope (vs `_payload_has_any_citable_evidence`): this returns True
    ONLY when there ARE insights but they're all callout-only. Payloads with
    NO insights (kpis-only / recs-only / exec_summary-only) keep the legacy
    citation density gate, since those are the actively-cared cases the gate
    was designed for and don't have the "stripped evidence" failure mode.
    """
    insights = payload.get("insights") or []
    if not isinstance(insights, list) or not insights:
        return False
    for ins in insights:
        if not isinstance(ins, dict):
            continue
        if _pick_evidence_artifact_id(ins) is not None:
            return False  # at least one insight has real evidence
    return True  # all insights are callout-only


_HAS_DIGIT_RE = re.compile(r"\d")


def _validate_grounding_density(payload: dict[str, Any]) -> tuple[bool, list[str]]:
    """citation-audit M1-0 — ξ B7 换锚：闸的**意图**不变（>80 字的分析文本必须
    有数据锚点，反无出处结论），**语法要求**消失——纯散文数字（backend 按 tool
    真值绑定 [N]）或显式引用 marker 均算 grounded。gen2 kill-switch 模式仍走旧
    `_validate_citation_density`（marker 强制）。默认 shadow（只记不拒），
    `BOWHEAD_B7_GROUNDING=enforce` 翻转（tech-design §3.3 M1-0）。"""
    if not _payload_has_analytical_content(payload):
        return True, []
    if (payload.get("error") or {}).get("kind"):
        return True, []
    if _insights_are_callout_only(payload):
        return True, []

    def _grounded(text: str) -> bool:
        return bool(_HAS_DIGIT_RE.search(text) or _CITATION_MARKER_RE.search(text))

    errors: list[str] = []
    exec_summary = str(payload.get("exec_summary") or "")
    if len(exec_summary) > 80 and not _grounded(exec_summary):
        errors.append(f"exec_summary ({len(exec_summary)} 字) 0 数字 0 引用 — 数据结论必须含具体数字（写纯散文数字即可, backend 自动绑定溯源）。")
    for i, ins in enumerate(payload.get("insights") or []):
        if not isinstance(ins, dict):
            continue
        title = str(ins.get("title") or "")
        for j, fact in enumerate(ins.get("facts") or []):
            if not isinstance(fact, dict):
                continue
            text = str(fact.get("text") or "")
            if len(text) > 50 and not _grounded(text):
                errors.append(f"insight[{i}].facts[{j}] '{title[:20]}...' ({len(text)} 字) 0 数字 — 必须含具体数字或 [ref:N].")
    # FU-1 (writer-payload-debt, 2026-07-19): do not ground recommendations.
    # Recommendations are action instructions and can naturally lack numbers (e.g. marketing devises traffic acquisition before Q2); grounding protects data
    # assertions (exec_summary / insights.facts) from fabrication. Spike measured 668 real recommendations: only one exceeded 80 characters
    # without numbers, still a legitimate English recommendation (false positive). A recommendation gate only harms valid content, missing actual fabrication.
    # Previously this read rec.get("text"), but the real field is action (text always empty), a dead no-op;
    # fix by explicitly excluding it, rather than reading action to activate a useless, falsely rejecting gate.
    return (not errors), errors[:5]


def _pick_evidence_artifact_id(insight: dict[str, Any]) -> tuple[str, str] | None:
    """Pick best citable artifact_id from an insight's evidence list.

    Priority order (per design BE-P1-1, Risk D applied — `EvidenceBlock` has
    only chart/computation/table id slots; no `data_artifact_id` field):
        chart_section_id  →  computation_artifact_id  →  table_artifact_id

    The priority is applied ACROSS the whole evidence list (priority outer,
    evidence inner) — a `chart_section_id` in evidence[1] beats a
    `computation_artifact_id` in evidence[0]. This matters because:
      - `chart_section_id` resolves to a Canvas section the FE can scroll to.
      - `computation_artifact_id` is a transient tool-output with no DOM
        target → click → "数据引用未在画布中找到" toast.
    CTO BE-Review #1 P0-1 (2026-05-27).

    Returns (artifact_id, kind) or None if no evidence has any of those.
    `kind` is the citation kind ("chart_section" | "tool_output" | "data_ref")
    used when registering in TurnCitationRegistry.
    """
    evidence = insight.get("evidence") or []
    if not isinstance(evidence, list):
        return None
    # Priority OUTER (chart > computation > table), evidence list INNER —
    # find the highest-priority field across ALL evidence blocks.
    for field_name, kind in (
        ("chart_section_id", "chart_section"),
        ("computation_artifact_id", "tool_output"),
        ("table_artifact_id", "data_ref"),
    ):
        for ev in evidence:
            if not isinstance(ev, dict):
                continue
            val = ev.get(field_name)
            if val and isinstance(val, str):
                return (val, kind)
    return None


def _pick_first_insight_artifact(
    insights: list[Any],
) -> tuple[str, str, str | None] | None:
    """Walk insights[] looking for the first one with a citable evidence
    artifact. Returns (artifact_id, kind, label_hint) or None.

    Used by the exec_summary auto-append path where there's no per-field
    evidence — borrow from the strongest available insight. (FU-1 removed the
    recommendations auto-append caller; exec_summary is now the only user.)
    """
    for ins in insights:
        if not isinstance(ins, dict):
            continue
        picked = _pick_evidence_artifact_id(ins)
        if picked is not None:
            aid, kind = picked
            label = ins.get("title")
            return (aid, kind, str(label) if label else None)
    return None


_TRAILING_ANCHOR_RUN_RE = re.compile(r"(?:[ \t]*\[figure:[A-Za-z0-9_]+\])+[ \t]*$")


def _append_citation_snippet(text: str, snippet: str) -> str:
    """把 ` [cite:N]` 贴到 text 的**散文**末尾，而不是字符串末尾。

    chat 行内图互作（chat-inline-figure-2026-08）：peer 按
    `model_view._with_figure_anchor_hint` 的指引在文本尾部写 `[figure:sec_x]`
    锚。FE 判据自 74d0e81c3 起是**行尾锚**（`md.ts::_FIGURE_ANCHOR_LINE`：
    前可有正文、后只能有空白；report 路仍是严格独占行 `_ANCHOR_ONLY_LINE`）。
    marker 粘到锚点**后面**即失配 ⇒ 图静默掉回末尾堆。所以：
    从后往前找最后一个非空、非纯锚点串、非围栏区的行——行以锚点串收尾时
    marker 插到锚点串**之前**，否则贴行尾；找不到散文行保持旧行为直接追加。
    ⚠ 围栏区（```/~~~ 定界与其内容）不落 marker——贴上闭栏会把整段变成代码。
    """
    if "[figure:" not in text:
        return text + snippet
    lines = text.split("\n")
    # Review F5: mark fenced regions forward first (```/~~~ delimiters and contents), then skip them throughout the walk.
    _fence = []
    _in = False
    for ln in lines:
        _delim = ln.lstrip().startswith("```") or ln.lstrip().startswith("~~~")
        _fence.append(_in or _delim)
        if _delim:
            _in = not _in
    for i in range(len(lines) - 1, -1, -1):
        # Skip empty lines, anchor-only runs (single standalone anchors are a special case; review C5:
        # _ANCHOR_ONLY_LINE misses two adjacent anchors, treating their line as prose and inserting the marker
        # at its start), and fenced regions.
        if not lines[i].strip() or _fence[i] or _TRAILING_ANCHOR_RUN_RE.fullmatch(lines[i]):
            continue
        # Since 74d0e81c3, FE requires line-ending anchors (observed peer text `…反弹。[figure:sec_a]`);
        # appending a marker after the anchor breaks matching, sending the chart back to the end.
        # When a line ends in an anchor run, insert the marker before that run (FE's lead capture group
        # merges cite into the preceding paragraph); otherwise append at line end as before.
        m = _TRAILING_ANCHOR_RUN_RE.search(lines[i])
        if m:
            lines[i] = lines[i][: m.start()] + snippet + lines[i][m.start() :]
        else:
            lines[i] += snippet
        return "\n".join(lines)
    return text + snippet


def _auto_append_citations(payload: dict[str, Any], ctx: Any) -> list[dict[str, str]]:
    """prod-bug-bash-039 Phase 4 (0.39.3) — auto-append `[cite:N]` to citation-
    poor texts before ξ B7 density gate.

    Replaces the reject→retry→strip ladder for the citation gate. Instead of
    bouncing the LLM to add a marker (which it does unreliably per
    `[[feedback_llm_internal_syntax_unreliable]]`), backend deterministically
    registers an artifact in the turn-scoped registry and appends a `[cite:N]`
    marker to the text. The density gate then sees a citation and passes.

    Decision table (per design BE-P1-1, Risk D applied — no data_artifact_id):
    | text field                                  | candidate artifact source                                       | action                |
    |---------------------------------------------|-----------------------------------------------------------------|-----------------------|
    | `insights[i].facts[j].text` (>50, no marker)| `insights[i].evidence[0]` (chart > computation > table)         | append `[cite:N]`     |
    | `exec_summary` (>80, no marker)             | first insight with picked evidence                              | append `[cite:N]`     |
    | `recommendations` (any)                     | n/a — 不自动补引用（FU-1: 挂第一 insight = 弱关联/假溯源）       | (no op)               |
    | no insights / no evidence anywhere          | n/a — accept; gate fires → strip-and-pass ladder takes over     | (no op)               |
    | payload not analytical / bail envelope      | n/a — gate skips entirely                                       | (no op)               |

    Idempotent: text already containing any `[cite:N]` / `[num:V:U]` /
    `[ref:N]` marker is left alone (matches gate's own pass criterion).

    Returns list of injection records: ``[{"field": "exec_summary", "seq": "3",
    "artifact_id": "sec_xxx"}, ...]`` for audit/log. Empty list = no injection.

    Defensive: catches any TypeError from malformed payload shapes (peer can
    emit ragged dicts) — never raises (caller wraps in try/except too).
    """
    if not _payload_has_analytical_content(payload):
        return []
    # CTO BE-Review #1 P2-1: handle explicit `{"error": None}` — `dict.get("error", {})`
    # returns `None` (default only applies when KEY MISSING, not when value is None),
    # then `None.get("kind")` raises AttributeError. The outer try/except at the call
    # site catches it, but the auto-append is silently skipped → the gate then triggers
    # the retry/strip ladder we explicitly eliminated. Be explicit with `or {}` here
    # AND in _validate_citation_density (same bug pattern).
    if (payload.get("error") or {}).get("kind"):
        return []

    insights = payload.get("insights") or []
    if not isinstance(insights, list):
        return []

    # ⚠ feedback-triage-2026-08-26 correction: the former claim that ctx here IS
    # parent_ctx for peers no longer holds. Peer dispatch uses dispatch_ctx = copy.copy(ctx)
    # (subagent.py when running_profile != "lead"), so this receives a shallow copy.
    # ensure_registry sets an attribute only when absent; on a copy it attaches the registry to that copy,
    # while Lead's _flush_citations reads runtime.parent_ctx and always sees nothing. Observed on 116:
    # no citations registered in report turns since 08-20, despite [cite:N] remaining in body prose.
    # Eager initialization on real parent_ctx at turn start (in lead_orchestrator) now ensures
    # copies share the same registry by reference; keep ensure_registry(ctx) here unchanged.
    # Lesson: shallow copies share existing mutable objects, not attributes created afterward.
    try:
        from bowhead_core.citations import ensure_registry

        registry = ensure_registry(ctx)
    except Exception:
        return []  # registry unavailable → gate falls through to legacy strip

    records: list[dict[str, str]] = []

    def _register_and_format(artifact_id: str, kind: str, label: str | None) -> str:
        """Register artifact in registry, return ` [cite:N]` snippet to append.

        Leading space ensures the marker doesn't visually glue to the prior
        word/punctuation. Registry dedups by artifact_id, so same artifact
        across multiple fields gets same N.
        """
        from bowhead_core.citations import CitationKind  # noqa: F401

        seq = registry.register(artifact_id, label=label, kind=kind)  # type: ignore[arg-type]
        return f" [cite:{seq}]"

    # 1) insights[i].facts[j].text — per-insight evidence
    for i, ins in enumerate(insights):
        if not isinstance(ins, dict):
            continue
        picked = _pick_evidence_artifact_id(ins)
        if picked is None:
            continue
        aid, kind = picked
        label = ins.get("title")
        label_str = str(label) if label else None
        facts = ins.get("facts") or []
        if not isinstance(facts, list):
            continue
        for j, fact in enumerate(facts):
            if not isinstance(fact, dict):
                continue
            text = str(fact.get("text") or "")
            if len(text) > 50 and not _CITATION_MARKER_RE.search(text):
                snippet = _register_and_format(aid, kind, label_str)
                fact["text"] = _append_citation_snippet(text, snippet)
                # extract the seq from the snippet we just produced for record
                m = _CITE_MARKER_RE.search(snippet)
                records.append(
                    {
                        "field": f"insights[{i}].facts[{j}]",
                        "seq": m.group(0) if m else snippet.strip(),
                        "artifact_id": aid,
                    }
                )

    # 2) exec_summary — borrow from first insight with evidence
    exec_summary = str(payload.get("exec_summary") or "")
    if len(exec_summary) > 80 and not _CITATION_MARKER_RE.search(exec_summary):
        picked_first = _pick_first_insight_artifact(insights)
        if picked_first is not None:
            aid, kind, label = picked_first
            snippet = _register_and_format(aid, kind, label)
            payload["exec_summary"] = _append_citation_snippet(exec_summary, snippet)
            m = _CITE_MARKER_RE.search(snippet)
            records.append(
                {
                    "field": "exec_summary",
                    "seq": m.group(0) if m else snippet.strip(),
                    "artifact_id": aid,
                }
            )

    # 3) recommendations: do not automatically add citations. FU-1 (writer-payload-debt): attaching the first
    # insight's artifact is weak linkage / false provenance (recommendations may not derive from that chart). Previously rec.get("text")
    # never fired (always empty; actual field is action). Explicitly exclude, rather than reading action to activate false provenance.

    return records


def _apply_output_schema_gate(
    ctx: Any,
    payload: dict[str, Any],
    subagent_id: str | None = None,
) -> dict[str, Any] | None:
    """cca P3 (S4 §5): gate a returning payload against the per-tc team-subagent
    output_schema (stamped on ctx by the runner). ADDITIVE on top of validate_payload
    (AnalysisPayload shape) — extra constraints, never a bypass.

    Returns a retry-error dict (``ok:False`` + ``_schema_retry``) while under the retry
    cap, else None to PROCEED (no schema / schema passes / retries exhausted → degrade-
    accept the AnalysisPayload-valid payload; never loop to the 900s timeout).

    ``format_checker=None`` + try/except is belt-and-suspenders: the create-time
    ``$ref``/``pattern``/``format`` reject (``routes/team_subagents._validate_output_schema``)
    is the LOAD-BEARING guard, so even a malformed/hostile stored schema degrades to a
    retry here, never crashes or hangs the turn. ``_schema_retry`` is a non-tool-failure
    flag (mirror ``ν_ref_retry``/``ξ_report_retry``) → the subagent loop won't circuit-break.
    """
    osc = getattr(ctx, CTX_OUTPUT_SCHEMA_ATTR, None)
    if not osc:
        return None
    # Payload size pre-cap (S4 review P2, belt-and-suspenders): the schema is bounded at
    # create, but the PAYLOAD is the subagent's tool arg and is only loosely bounded by
    # validate_payload (unknown extra keys uncapped). iter_errors runs SYNCHRONOUSLY on the
    # event loop, so an oversized payload could burn CPU across tenants. Above the cap, skip
    # the (additive, degrade-accept) gate rather than validate. RecursionError-safe.
    try:
        if len(json.dumps(payload, ensure_ascii=False, default=str)) > _OSCHEMA_PAYLOAD_MAX_BYTES:
            logger.warning("p3.output_schema_skipped_oversize_payload turn=%s", getattr(ctx, "turn_id", ""))
            return None
    except (TypeError, ValueError, RecursionError):
        return None  # unserializable / pathological payload → skip the gate (degrade-accept)
    try:
        # import INSIDE the try: jsonschema is a transitive dep (via openapi-core); if the
        # tree ever drops it, degrade rather than crash the return path. Build + iter_errors
        # also inside (RecursionError / referencing → degrade).
        import jsonschema as _jsonschema  # type: ignore[import-untyped]

        errs = [e.message for e in _jsonschema.Draft202012Validator(osc, format_checker=None).iter_errors(payload)][:10]
    except Exception as exc:
        logger.warning("p3.output_schema_validate_error turn=%s err=%s", getattr(ctx, "turn_id", ""), exc)
        errs = ["output_schema_validation_error"]
    if not errs:
        return None
    # Per-subagent retry count on the SHARED ctx (review fix): keyed by subagent_id so a
    # sibling can't inherit another's exhausted count, and a dict (mutated in place) so the
    # count survives the legacy per-call copy.copy(ctx) and accumulates across this peer's
    # retries. Seeded at the schema-stamp sites on parent_ctx; lazily created here as a fallback
    # (on the legacy copy that fallback wouldn't persist, but the stamp seed makes it present).
    counts = getattr(ctx, CTX_OUTPUT_SCHEMA_RETRY_ATTR, None)
    if not isinstance(counts, dict):
        counts = {}
        setattr(ctx, CTX_OUTPUT_SCHEMA_RETRY_ATTR, counts)
    _key = subagent_id or ""
    retry = int(counts.get(_key, 0) or 0)
    if retry < _OSCHEMA_MAX_RETRIES:
        counts[_key] = retry + 1
        return {
            "ok": False,
            "error": "payload failed the subagent's output_schema:\n" + "\n".join(f"  - {m}" for m in errs),
            "validation_errors": errs,
            "_schema_retry": True,
        }
    # retries exhausted → DEGRADE: the payload already passed the AnalysisPayload shape;
    # accept it rather than fail the whole turn or loop to the timeout.
    logger.warning("p3.output_schema_degrade turn=%s after %d retries errs=%s", getattr(ctx, "turn_id", ""), retry, errs)
    return None


def _make_handler() -> Any:
    async def _handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        # CTO P0-1 fix — capture `_current_subagent_id` sync BEFORE any await.
        # Two concurrent peer coroutines on shared parent_ctx can race: A sets
        # ctx._current_subagent_id="P1", yields at ensure_canvas, B sets it to
        # "P2", A resumes and reads "P2" → cross-attribution on counter key.
        # Snapshot here while we're still in a single sync section.
        _handler_subagent_id: str | None = getattr(
            ctx,
            CTX_CURRENT_SUBAGENT_ID_ATTR,
            None,
        )
        # Upstream ToolRegistry should fully unwrap raw_arguments; defensively decode double encoding once more.
        if isinstance(args, dict) and "_args_parse_error" in args:
            # error is a model-facing repair instruction: args_parse_hint branches on the actual JSONDecodeError,
            # including error position and nearby original text for a successful corrected resend. Preserve it.
            # error_message is user-facing; _extract_result_error prefers it, so the step card no longer displays
            # protocol prose such as 「请修正后原样重发；先检查转义…（错误位置 offset=1083，
            # 附近原文：'…"confidence": "high"}]}]}'）. 重发标准 JSON: subagent_return(
            # payload={...AnalysisPayload})」 to customers (photographed in 116 dogfood on
            # 2026-09-07). Same as pbi: normalize before emitting events; keep internal prose out of SSE.
            # ⚠ Deliberately omit error_kind="invalid_args": subagent.py gives it a different meaning,
            # rendering the entire step as success (agent self-corrects arguments and retries, not counted as failure).
            # This did fail and retry once; making it green is a separate decision outside this change.
            return {
                "ok": False,
                "error_message": "这一步返回的内容格式不合法，正在重新整理。",
                "error": (f"args 解析失败：{args['_args_parse_error']}. 重发标准 JSON: subagent_return(payload={{...AnalysisPayload}})."),
            }
        payload = args.get("payload")
        # Fallback: payload is a JSON string (LLM double encoding); decode again.
        if isinstance(payload, str):
            try:
                import json as _json

                payload = _json.loads(payload)
            except (ValueError, TypeError) as e:
                return {
                    "ok": False,
                    "error": f"payload was a string but not valid JSON: {e}",
                }
        if not isinstance(payload, dict):
            return {
                "ok": False,
                "error": (
                    f"payload must be a JSON object matching AnalysisPayload schema, got {type(payload).__name__}. 见 analyst-sop-output-contract."
                ),
            }
        # Existing producers use both top-level and nested question envelopes.
        # Normalize before validation/terminal storage: Lead reads error, and a
        # successful terminal return must never silently lose the user's question.
        error = payload.get("error")
        if isinstance(error, dict) and error.get("kind") == "needs_user_input":
            from bowhead_core.agents.clarification_tool import normalize_clarification_questions

            nested = error.get("clarification_payload")
            outer = payload.get("clarification_payload")
            if nested is not None and outer is not None and nested != outer:
                return {"ok": False, "error": "Conflicting clarification_payload values; return one question envelope inside payload.error."}
            clarification = nested if nested is not None else outer
            questions = clarification.get("questions") if isinstance(clarification, dict) else None
            normalized = normalize_clarification_questions(questions)
            if not normalized or len(normalized) != len(questions):
                return {
                    "ok": False,
                    "error_message": "澄清问题未写完整，正在重新整理。",
                    "error": (
                        "needs_user_input requires 1-3 valid questions inside "
                        "payload.error.clarification_payload.questions. Each question needs a nonempty "
                        '"question" string, "type", and "options". For a typed numeric answer, use '
                        '{"question":"请提供缺少的数值","type":"free_text","options":[]} '
                        "and name the actual missing value in the user's language."
                    ),
                }
            payload = {**payload, "error": {**error, "clarification_payload": {"questions": normalized}}}
            payload.pop("clarification_payload", None)
        # M137 #150 Bug B: subagent_return is exclusively a subagent contract; Lead calls have no effect
        # (Lead's ctx does not accumulate _m137_subagent_payloads). Defensive detection here:
        # ctx with no subagent markers → suggest subagent_spawn instead of calling directly.
        # When subagents run, ctx is parent_ctx but the LLM calls tools in the subagent loop;
        # this would match only Lead-side ctx, which lacks a subagent call-stack marker.
        # Simple distinction: subagent calls execute _execute_tool within their loop; Lead calls lack
        # last_nested_invocations marking the active subagent_id. Precise classification across ctx is difficult,
        # so accept and store payload first (before M3, composer only consumed _m137_subagent_payloads;
        # Lead's _m137_returned_payload never rendered, so Lead calls were harmless no-ops).

        # ─── Sprint agent-redesign-1 Phase A — soft-gate auto-fix ───
        # 21 rules soft-degrade in deterministic single pass before validate.
        # Hard-keep 8 rules (R1/R2/R3 done above + R21/R23/R24/R26/R30 in validate).
        # Flag-gated: BOWHEAD_PAYLOAD_SOFT_GATE=0 → identical to today's behavior.
        # P1-1 defensive wrap (CTO review f8ed5ba): any helper exception (import
        # failure / type bug in _fix_*) must NOT crash peer return — fall
        # through to legacy validate_payload so worst case = pre-ar1 behavior.
        # N1 escaped newlines — **outside the soft gate on purpose**. It is not a
        # validation degrade, it is rendering correctness: a payload whose prose
        # carries `\n` as two characters loses every chart it drew (chat anchors
        # must end a line, `md.ts:167`) and shows the litter to the user. Coupling
        # that to BOWHEAD_PAYLOAD_SOFT_GATE would mean a deployment that turned the
        # gate off silently loses charts. Wrapped: a bug here must never fail a return.
        with contextlib.suppress(Exception):
            from bowhead_core.payload_auto_fix import fix_escaped_newlines as _fix_esc_nl

            _esc_fixes: list[Any] = []
            _fix_esc_nl(payload, _esc_fixes)
            if _esc_fixes:
                logger.info(
                    "subagent_return.escaped_newlines_unescaped n=%d fields=%s",
                    len(_esc_fixes),
                    [f.field_path for f in _esc_fixes][:6],
                )

        import os as _os_a

        _soft_gate = _os_a.environ.get("BOWHEAD_PAYLOAD_SOFT_GATE", "1") == "1"
        _auto_fix_records: list[dict[str, str]] = []
        # canvas-update-turn batch B0 (review fix): Lead revise/append use revision-turn validation; document-level
        # fields are optional and zero insights do not hard-fail (the finisher does not render them; revision instructions also request thin revisions). Excludes plan execution.
        _revision_mode = False
        with contextlib.suppress(Exception):
            from bowhead_core.canvas_revise import no_document_chrome as _revision_no_chrome

            _revision_mode = _revision_no_chrome(getattr(ctx, "parent_bus", None) or getattr(ctx, "bus", None))
        if _soft_gate:
            try:
                from bowhead_core.payload_auto_fix import (
                    auto_fix_payload,
                    dedup_fixes_per_rule,
                    log_fix_records,
                )

                _fix_res = auto_fix_payload(payload, revision=_revision_mode)
                if _fix_res.fixes:
                    log_fix_records(
                        _fix_res.fixes,
                        turn_id=getattr(ctx, "turn_id", "") or "",
                        peer_id=str(_handler_subagent_id or ""),
                    )
                    _auto_fix_records = [f.to_llm_dict() for f in dedup_fixes_per_rule(_fix_res.fixes)]
                if _fix_res.hard_reject:
                    from bowhead_core.ordinary_lead_runtime import note_ordinary_return_rejection

                    return note_ordinary_return_rejection(
                        ctx,
                        {
                            "ok": False,
                            "error": f"payload schema invalid (R7 after auto-fix): {_fix_res.hard_reject_reason}",
                            "validation_errors": [_fix_res.hard_reject_reason],
                        },
                    )
                # auto-fixed payload now feeds into validate_payload below.
                # Only hard-keep rules (R1-3/R21/R23/R24/R26/R30) can still fire.
            except Exception as _af_exc:
                logger.exception(
                    "ar1.soft_gate.unhandled_err err=%s peer_id=%s — falling through to legacy validate",
                    _af_exc,
                    _handler_subagent_id,
                )
                _auto_fix_records = []
                # Don't return; legacy validate_payload runs as normal.

        ok, errs = validate_payload(payload, revision=_revision_mode)
        if not ok:
            # Give the LLM readable errors so it can repair and retry.
            from bowhead_core.ordinary_lead_runtime import note_ordinary_return_rejection

            err_lines = "\n".join(f"  - {e}" for e in errs[:10])
            return note_ordinary_return_rejection(
                ctx,
                {"ok": False, "error": "payload schema invalid:\n" + err_lines, "validation_errors": errs[:10]},
            )

        # ─── report-consistency-gate v3: ground_facts ledger self-reconciliation (flag-gated) ───
        # Report quantities with units must belong to the authorized ledger (actual tool output ∪ formula-verified derivations).
        # off=skip | shadow=metrics only, no rejection | enforce=reject + self-correct (own retry budget;
        # fail soft after exhaustion, accepting with a caveat). Measure false positives on newly generated ledger-prompt reports, not old payloads lacking gf.
        import os as _osl

        # W1-A: default off→shadow, first activation step. Shadow logs metrics only, without rejecting or modifying
        # payload (whole block wrapped in try), with no user-visible changes. Enforce activation is a separate decision after shadow metrics and live-LLM
        # dogfood. Kill switch: BOWHEAD_ANALYST_LEDGER=off.
        _ledger_mode = _osl.environ.get("BOWHEAD_ANALYST_LEDGER", "shadow").strip().lower()
        if _ledger_mode in ("shadow", "enforce"):
            try:
                from bowhead_core.metrics import inc as _ledger_inc
                from bowhead_core.report_ledger import reconcile_payload

                _lg = reconcile_payload(payload, run_invocations(ctx))
                # has_gf distinguishes actual ledger violations from legacy/no-ground_facts noise;
                # shadow metrics inform enforce decisions, which would be invalid without this distinction.
                _has_gf = "1" if (isinstance(payload, dict) and payload.get("ground_facts")) else "0"
                if _lg.clean:
                    _ledger_inc("ledger_reconcile_total", labels={"mode": _ledger_mode, "outcome": "clean", "has_gf": _has_gf})
                else:
                    _ledger_inc("ledger_rejected_facts_total", value=float(len(_lg.rejected_facts)))
                    _ledger_inc("ledger_unauthorized_numbers_total", value=float(len(_lg.unauthorized)))
                    logger.info(
                        "ledger.reconcile turn=%s sub=%s mode=%s rejected=%d unauthorized=%d",
                        getattr(ctx, "turn_id", ""),
                        _handler_subagent_id,
                        _ledger_mode,
                        len(_lg.rejected_facts),
                        len(_lg.unauthorized),
                    )
                    if _ledger_mode == "enforce":
                        _lk = (getattr(ctx, "turn_id", ""), _handler_subagent_id or "default")
                        _lc = getattr(ctx, "_ledger_retry_counts", None)
                        if _lc is None:
                            _lc = {}
                            ctx._ledger_retry_counts = _lc
                        if _lc.get(_lk, 0) < 2:
                            _lc[_lk] = _lc.get(_lk, 0) + 1
                            _ledger_inc("ledger_reconcile_total", labels={"mode": _ledger_mode, "outcome": "retry_issued", "has_gf": _has_gf})
                            return {
                                "ok": False,
                                # _ledger_retry marker exempts this from the subagent loop's tool_failure budget
                                # (see subagent.py's retry-signal exemption), avoiding competition for circuit-breaker allowance.
                                "_ledger_retry": True,
                                "error": _ledger_error_text(_lg),
                                "ledger_rejected": _lg.rejected_facts[:10],
                                "ledger_unauthorized": _lg.unauthorized[:10],
                            }
                        # Retry exhaustion → fail soft: stop blocking, attach a caveat as a record.
                        _ledger_inc("ledger_reconcile_total", labels={"mode": _ledger_mode, "outcome": "retry_exhausted", "has_gf": _has_gf})
                        logger.warning("ledger.reconcile_retry_exhausted turn=%s sub=%s", getattr(ctx, "turn_id", ""), _handler_subagent_id)
                        if isinstance(payload, dict):
                            from bowhead_core.analysis_payload import append_system_caveat as _append_sys_cav

                            _append_sys_cav(payload, "⚠ 部分数字未能与计算台账对齐，请以原始数据为准。")  # Review item 3
                    else:
                        # Shadow: observe only, take no action.
                        _ledger_inc("ledger_reconcile_total", labels={"mode": _ledger_mode, "outcome": "mismatch", "has_gf": _has_gf})
            except Exception as _lg_exc:
                logger.warning("ledger.reconcile_failed turn=%s err=%s", getattr(ctx, "turn_id", ""), _lg_exc)

        # ─── canvas-update-turn batch B2-5: refresh KPI cards after changing calculation criteria (decision ④) ───
        # Only this peer can supply new values; the server lacks data under the new criteria. It has exited by finish time, allowing only a warning, no repair.
        # A gate after state consumption is a dead end; check before return: old revised values remain in the KPI band and payload supplies no kpis
        # ⇒ reject once, naming stale values. One rejection per peer, exempt from tool-failure budget (_schema_retry); after exhaustion, allow as usual,
        # sending remaining values to Lead as stale_kpi in the canvas ledger for truthful disclosure.
        try:
            from bowhead_core.canvas_revise import lead_canvas_mode as _kpi_mode
            from bowhead_core.canvas_revise import stale_kpi_after_revision as _stale_kpi

            _kbus = getattr(ctx, "parent_bus", None) or getattr(ctx, "bus", None)

            def _revised_prior_text(invs: Any) -> bool:
                # Review P2: stale values are calculated for the whole turn, but request KPIs only from the peer that revised prior-turn prose itself;
                # sibling peers adding sections or merely analyzing know nothing of new criteria; asking wastes a retry.
                for _i in invs or []:
                    _r = getattr(_i, "result", None)
                    _a = getattr(_i, "arguments", None) or {}
                    if getattr(_i, "tool_name", "") == "write_report" and isinstance(_r, dict) and _r.get("ok") and _a.get("section_id"):
                        return True
                return False

            if (
                isinstance(payload, dict)
                and not payload.get("kpis")
                and _kpi_mode(_kbus) == "revise"
                and not getattr(_kbus, "_d6_is_plan_execution", False)
                and _revised_prior_text(run_invocations(ctx) or None)
            ):
                _kkey = (getattr(ctx, "turn_id", ""), _handler_subagent_id or "default")
                _kcounts = getattr(ctx, "_revise_kpi_retry_counts", None)
                if _kcounts is None:
                    _kcounts = {}
                    ctx._revise_kpi_retry_counts = _kcounts
                if _kcounts.get(_kkey, 0) < 1:
                    _kstore = getattr(ctx, "canvas_store", None)
                    _kcv = await _kstore.get_canvas_by_session(getattr(ctx, "session_id", None)) if _kstore is not None else None
                    _kstale = await _stale_kpi(
                        _kstore, canvas_id=getattr(_kcv, "id", None), turn_id=getattr(ctx, "turn_id", None), payload=payload, bus=_kbus
                    )
                    if _kstale:
                        _kcounts[_kkey] = 1
                        logger.info("revise_kpi.retry turn=%s sub=%s stale=%s", _kkey[0], _kkey[1], _kstale[:8])
                        return {
                            "ok": False,
                            "_schema_retry": True,
                            "error_kind": "invalid_args",
                            "error": (
                                f"正文已按新口径改了，但 KPI 卡里还是旧口径的数：{'、'.join(_kstale[:8])}。"
                                "请在 payload.kpis 里按新口径给出**整条带的全部指标**（不是只给变了的那一个），再调一次 subagent_return。"
                                "确实算不出某项就在 exec_summary 里说明哪项没更新。"
                            ),
                            "stale_kpi_values": _kstale[:8],
                        }
        except Exception as _kpi_exc:  # Best effort: a broken gate must not block delivery.
            logger.warning("revise_kpi.gate_failed turn=%s err=%s", getattr(ctx, "turn_id", ""), _kpi_exc)

        # ─── cca P3 (S4 §5) — optional team-subagent output_schema gate ───
        _osc_gate = _apply_output_schema_gate(ctx, payload, _handler_subagent_id)
        if _osc_gate is not None:
            return _osc_gate

        # ─── Sprint ν1.5 — translate simple-int IDs (1/2/3) → real IDs ───
        # Peer uses simple IDs from allowlist XML; backend translates, then ν1 validates
        # against real IDs. Flag-gated with BOWHEAD_REF_ALLOWLIST_INJECT.
        try:
            _ν15_al = run_ref_allowlist(ctx)
            if _ν15_al is not None:
                _ν15_al.translate_payload(payload)
        except Exception as _ν15_exc:
            logger.warning(
                "ν1.5.translate_payload_failed turn=%s err=%s",
                getattr(ctx, "turn_id", ""),
                _ν15_exc,
            )

        # ─── Inline-chart batch 4: deterministic chat figure-anchor gate (same family as report-path _strip_invalid_figure_anchors).
        #     Still-invalid translated anchors are repaired positionally only if their count exactly matches
        #     this turn's unreferenced charts one-to-one; otherwise strip them and orphaned figure-caption lines.
        #     Without repair/stripping, invalid anchors reach FE and silently disappear; charts neither error nor stay beside arguments,
        #     all moving to the answer's end without any logs. A 100% production failure becomes invisible.
        try:
            from bowhead_core._ref_allowlist import RefAllowlist as _RefAllowlist

            # Use the same invocation source/order as report_writer_loop: ContextVar first
            # (parallel sibling isolation B-P2-9), shared attributes only as legacy harness fallback. Check
            # is not None: an explicit empty list must not disappear through an or-chain.
            from bowhead_core.agents._lead_context import (
                get_current_subagent_invocations as _get_cur_invs,
            )
            from bowhead_core.chat_figure_anchors import PayloadAnchorResolver as _AnchorResolver
            from bowhead_core.chat_figure_anchors import (
                figures_from_invocations as _figs_from_invs,
            )

            _invs_for_figs = _get_cur_invs()
            if _invs_for_figs is None:
                _invs_for_figs = getattr(ctx, "_current_subagent_invocations", None)
            _fig_order = _figs_from_invs(_invs_for_figs)
            _known_figs = set(_fig_order)
            _al_for_figs = run_ref_allowlist(ctx)
            if _al_for_figs is not None:
                # Previous-turn charts can also be cited in chat; all allowlist-known IDs form the known set,
                # rather than stripping old charts as hallucinations.
                _known_figs |= _al_for_figs.real_ids_of_kinds(("chart", "image"))
            # Two passes: collect anchors across the entire payload before counting. Per-field checks compare all this turn's charts
            # against only that field's anchors, never matching and degenerating into stripping the entire batch.
            _fig_resolver = _AnchorResolver(figures=_fig_order, known_ids=_known_figs)
            _RefAllowlist.map_payload_texts(payload, _fig_resolver.observe)
            _fig_resolver.plan()
            _RefAllowlist.map_payload_texts(payload, _fig_resolver.apply)
            _fig_st = _fig_resolver.stats
            if _fig_st.total:
                logger.info(
                    "chat_figure_anchor.gate turn=%s total=%d kept=%d repaired=%d stripped=%d figures=%d",
                    getattr(ctx, "turn_id", ""),
                    _fig_st.total,
                    _fig_st.kept,
                    _fig_st.repaired,
                    _fig_st.stripped,
                    len(_fig_order),
                )
        except Exception as _fig_exc:  # noqa: BLE001 — Gate failure must not interrupt return.
            logger.warning(
                "chat_figure_anchor.gate_failed turn=%s err=%s",
                getattr(ctx, "turn_id", ""),
                _fig_exc,
            )

        # ─── Sprint ν1: single reference-validation entry ───
        # Verify all payload refs (chart_section_id / table_artifact_id /
        # computation_artifact_id / [ref:S] / [num:V:U:src:S]) actually exist.
        # Maximum two retries; then strip + audit + continue the original flow.
        # per-turn flag `BOWHEAD_REF_VALIDATE_STRICT=1` (default ON, hot read).
        import os as _os

        if _os.environ.get("BOWHEAD_REF_VALIDATE_STRICT", "1") == "1":
            try:
                _ν_canvas_store = getattr(ctx, "canvas_store", None)
                _ν_artifact_store = getattr(ctx, "artifact_store", None)
                _ν_session_id = getattr(ctx, "session_id", "") or ""
                _ν_user_id = getattr(ctx, "user_id", "") or ""
                _ν_workspace_id = getattr(ctx, "workspace_id", "") or ""
                _ν_turn_id = getattr(ctx, "turn_id", "") or ""
                _ν_invocations = run_invocations(ctx)
                # empty-canvas mitigation: read-only, reading canvas solely to validate references.
                # No canvas means no real references, equivalent to an empty real-ref set; do not create a row.
                _ν_canvas = await _ν_canvas_store.get_canvas_by_session(_ν_session_id) if _ν_canvas_store is not None and _ν_session_id else None
                if _ν_canvas is not None and _ν_artifact_store is not None:
                    from bowhead_core._ref_audit import (
                        emit_ref_audit_event,
                    )
                    from bowhead_core._ref_validation import (
                        _build_real_refs,
                        _strip_invalid_refs,
                        _validate_all_refs,
                    )

                    _ν_real = await _build_real_refs(
                        canvas_id=_ν_canvas.id,
                        session_id=_ν_session_id,
                        user_id=_ν_user_id,
                        canvas_store=_ν_canvas_store,
                        artifact_store=_ν_artifact_store,
                        tool_invocations=_ν_invocations,
                    )
                    _ν_errors = _validate_all_refs(payload, _ν_real)
                    _ν_first_pass_strip = False
                    # 2026-09-06: first-pass remapping + direct stripping (default flag on; 0 restores two rejections then stripping):
                    # Of 1244 spawns over 90 days on 116, 19.5% were rejected at least once, costing an extra iteration each. Compared 417 rejected refs to storage:
                    # most used wrong namespaces (call IDs as section IDs, code sections as artifacts, artifacts as KB refs), with one real target ⇒ remap;
                    # others ([ref:1], manual_calc, 22) have no repair target. Retrying forces a valid but possibly wrong allowlist_sample ID ⇒ strip directly.
                    if _ν_errors and _os.environ.get("BOWHEAD_REF_REMAP_FIRST_PASS", "1") == "1":
                        from bowhead_core._ref_validation import build_ref_remap_index, remap_invalid_refs

                        # Review fix: remapping catches its own exceptions; partially remapped payloads reaching the outer fail-open would be accepted unchanged.
                        # Failure here means no successful remapping; follow existing strip/retry paths below.
                        _ν_remapped = []
                        try:
                            try:
                                _ν_sections = await _ν_canvas_store.list_sections(_ν_canvas.id)
                            except Exception:  # noqa: BLE001
                                _ν_sections = []
                            _ν_index = build_ref_remap_index(tool_invocations=_ν_invocations, sections=_ν_sections)
                            payload, _ν_remapped = remap_invalid_refs(payload, _ν_errors, _ν_index)
                        except Exception as _ν_remap_exc:  # noqa: BLE001
                            logger.warning("ν1.remap_first_pass_failed turn=%s err=%s — 按未映射处理", _ν_turn_id, _ν_remap_exc)
                        # Revalidate outside try: remapping mutates payload itself, so validate whether it succeeds or fails; audit remapped entries are those actually applied.
                        _ν_errors = _validate_all_refs(payload, _ν_real)
                        if _ν_remapped or _ν_errors:
                            await emit_ref_audit_event(
                                _ν_turn_id,
                                "remap_first_pass",
                                {"remapped": _ν_remapped[:20], "stripped": _ν_errors[:20]},
                                user_id=_ν_user_id,
                                workspace_id=_ν_workspace_id,
                            )
                            logger.info(
                                "ν1.remap_first_pass turn=%s remapped=%d stripped=%d",
                                _ν_turn_id,
                                len(_ν_remapped),
                                len(_ν_errors),
                            )
                        # Remaining errors directly enter the existing strip-and-bail-if-no-evidence branch (same code as _ν_retry >= 2 below), without retry.
                        _ν_first_pass_strip = bool(_ν_errors)
                    if _ν_errors:
                        # π Phase D Item B — counter on parent_ctx
                        # `_ν_retry_counts` dict, keyed by (turn_id,
                        # subagent_id), survives peer re-spawn. Old
                        # `_ν_retry_count` scalar attr still mirrored on
                        # write for back-compat (test introspection in
                        # nu15_integration).
                        _ν_counts = _ensure_nu_retry_counts(ctx)
                        # CTO P0-1: pass captured sid so post-await read uses
                        # the sync snapshot, not a possibly-swapped ctx attr.
                        _ν_key = _get_nu_retry_key(
                            ctx,
                            captured_subagent_id=_handler_subagent_id,
                        )
                        _ν_retry = _ν_counts.get(_ν_key, 0)
                        # Back-compat: if dict has no entry but legacy
                        # scalar was set directly (existing nu15 tests),
                        # honor the scalar for the 'default' bucket only.
                        # Sibling subagent_id buckets MUST NOT inherit the
                        # scalar (would contaminate cross-peer counts).
                        if _ν_key not in _ν_counts and _ν_key[1] == "default":
                            _legacy = getattr(ctx, "_ν_retry_count", None)
                            if isinstance(_legacy, int):
                                _ν_retry = _legacy
                                _ν_counts[_ν_key] = _legacy
                        if _ν_retry >= 2 or _ν_first_pass_strip:
                            payload = _strip_invalid_refs(payload, _ν_errors)
                            await emit_ref_audit_event(
                                _ν_turn_id,
                                "strip_degrade",
                                {
                                    "errors": _ν_errors[:20],
                                    "retry_count": _ν_retry,
                                },
                                user_id=_ν_user_id,
                                workspace_id=_ν_workspace_id,
                            )
                            # π B — if strip leaves no evidence, synthesize
                            # user-grade bail envelope and terminate peer.
                            # Without this, peer keeps subagent_return-ing
                            # an empty payload schema-invalid → reject loop
                            # until lead's 900 s hard timeout.
                            if not _payload_has_any_evidence(payload):
                                await emit_ref_audit_event(
                                    _ν_turn_id,
                                    "bail_envelope",
                                    {
                                        "bail_kind": PI_B_BAIL_KIND,
                                        "retry_count": _ν_retry,
                                        "errors": _ν_errors[:20],
                                    },
                                    user_id=_ν_user_id,
                                    workspace_id=_ν_workspace_id,
                                )
                                _bail_narrative = _build_fallback_narrative()
                                # CTO P1-1 fix: omit `title` from bail
                                # envelope so Lead's _resolve_final_answer
                                # chain (exec_summary → title → narrative →
                                # fallback_markdown) doesn't surface a
                                # placeholder title like "(无法生成结构化报告)"
                                # ahead of the user-grade prose. Letting title
                                # fall through means the chain picks
                                # narrative / fallback_markdown — exactly the
                                # user-actionable reframe we want.
                                _bail_payload: dict[str, Any] = {
                                    "narrative": _bail_narrative,
                                    "fallback_markdown": _bail_narrative,
                                    "evidence_refs": _collected_refs_so_far(ctx),
                                }
                                # Store bail payload on ctx (composer / aggregator
                                # use _m137_returned_payload; SubagentRunner
                                # consumes it at terminal break).
                                with contextlib.suppress(AttributeError, TypeError):
                                    setattr(
                                        ctx,
                                        CTX_PAYLOAD_ATTR,
                                        _bail_payload,
                                    )
                                return {
                                    "ok": True,
                                    TERMINAL_FLAG: True,
                                    "bail_kind": PI_B_BAIL_KIND,
                                    "payload": _bail_payload,
                                    "summary": {
                                        "bail": True,
                                        "reason": PI_B_BAIL_KIND,
                                    },
                                }
                            # Continue original flow; do not count as tool_failure.
                        else:
                            # Bump atomic single-statement increment.
                            # CPython get+set on a single dict has no `await`
                            # window — safe under GIL within this coroutine.
                            _ν_counts[_ν_key] = _ν_retry + 1
                            # CTO P1-2 fix: scalar `_ν_retry_count` mirror
                            # write removed — it diverges from dict on peer
                            # respawn (dict is per-(turn,sid) authoritative).
                            # Read fallback for the "default" bucket kept
                            # above (lines marked Back-compat) so legacy
                            # nu15 tests that pre-arm the scalar still work.
                            await emit_ref_audit_event(
                                _ν_turn_id,
                                "validate_reject",
                                {
                                    "errors": _ν_errors[:20],
                                    "retry_count": _ν_retry,
                                },
                                user_id=_ν_user_id,
                                workspace_id=_ν_workspace_id,
                            )
                            err_lines = "\n".join(f"  - {e['kind']} field={e['field']} ref={e['ref_id']}" for e in _ν_errors[:5])
                            # feedback-0713 #3: copy must not assert hallucination; missing from the ref
                            # allowlist does not imply it (image sections historically missing structurally;
                            # compliant models were accused of hallucination, misleading diagnosis and user reports).
                            return {
                                "ok": False,
                                "error": (
                                    "ν1 ref validation failed（下列引用不在本轮可引用清单中）:\n" + err_lines + "\nFix: 仅引用 tool 调用真返回的 ID, "
                                    "或不引用 (caption-only OK)."
                                ),
                                "ν_ref_retry": True,  # Do not count toward tool_failure_counts.
                                "ref_errors": _ν_errors[:20],
                            }
            except Exception as _ν_exc:
                # ν1 validation failure must not block a turn; fall back to acceptance + logging.
                logger.warning(
                    "ν1.ref_validate_failed turn=%s err=%s — falling open",
                    getattr(ctx, "turn_id", ""),
                    _ν_exc,
                )

        # ─── M157 thin-path validation (dual-mode priority + canvas section anti-shell + tier one-to-one correspondence) ───
        # chat-trace-fix-3 (2026-05-15): graceful auto-fix; peers often inserted
        # add_chart sec_id into insight_section_ids, rejecting the entire payload,
        # unrecovered through all 13 iterations, leaving no canvas report (observed t_a9524866).
        # Fix: strip IDs with kind != insight_card before validation; if none remain, use
        # inline insights[] (peers usually write both, so dropping one loses no content).
        _raw_insight_ids = payload.get("insight_section_ids") or []
        if _raw_insight_ids:
            canvas_store_for_filter = getattr(ctx, "canvas_store", None)
            session_id_for_filter = getattr(ctx, "session_id", "")
            if canvas_store_for_filter is not None and session_id_for_filter:
                try:
                    # empty-canvas mitigation: read-only, reading canvas solely to filter by kind.
                    # No canvas ⇒ empty _kind_by_id ⇒ preserve all IDs unchanged for downstream validation
                    # (existing missing-ID-in-canvas path); create no row.
                    _cv = await canvas_store_for_filter.get_canvas_by_session(session_id_for_filter)
                    _secs = await canvas_store_for_filter.list_sections(_cv.id) if _cv is not None else []
                    _kind_by_id = {s.id: s.kind for s in _secs}
                    # Only auto-strip when ID IS in canvas BUT kind is wrong.
                    # Unknown IDs (not in canvas at all — typo / hallucination)
                    # are kept and downstream validator hard-fails with
                    # "unknown id" — distinguishes peer's confusion (auto-fix)
                    # vs peer's fabrication (hard reject).
                    _filtered = [sid for sid in _raw_insight_ids if str(sid) not in _kind_by_id or _kind_by_id[str(sid)] == "insight_card"]
                    if len(_filtered) != len(_raw_insight_ids):
                        _wrong = [f"{sid}(kind={_kind_by_id.get(str(sid), 'unknown')})" for sid in _raw_insight_ids if sid not in _filtered]
                        logger.warning(
                            "subagent_return.auto_strip_non_insight_ids turn=%s "
                            "dropped=%d kept=%d wrong_kinds=%s — peer mixed chart/"
                            "narrative sec_ids into insight_section_ids; falling "
                            "to inline insights[] if available.",
                            getattr(ctx, "turn_id", ""),
                            len(_raw_insight_ids) - len(_filtered),
                            len(_filtered),
                            _wrong[:5],
                        )
                        try:
                            from bowhead_core import metrics as _m

                            _m.inc(
                                "bowhead_insight_section_ids_auto_stripped_total",
                                labels={"reason": "wrong_kind"},
                                help="auto-stripped non-insight_card IDs from insight_section_ids (chat-trace-fix-3).",
                            )
                        except Exception:  # noqa: S110
                            pass
                        payload["insight_section_ids"] = _filtered
                except Exception as _strip_exc:
                    logger.debug(
                        "subagent_return.auto_strip_check_failed turn=%s err=%s",
                        getattr(ctx, "turn_id", ""),
                        _strip_exc,
                    )
        insight_section_ids = payload.get("insight_section_ids") or []
        try:
            from bowhead_core import metrics as _m

            _m.inc(
                "bowhead_insight_emit_total",
                labels={"mode": "chunk" if insight_section_ids else "inline"},
                help="subagent_return insight emission mode (M157).",
            )
        except Exception:  # noqa: S110
            pass
        if insight_section_ids:
            # P0 #5: thin + inline both present → log warning and ignore inline.
            if payload.get("insights"):
                logger.warning(
                    "subagent_return: both insight_section_ids (%d) AND insights[] "
                    "(%d) provided; using thin path (insight_section_ids), "
                    "insights[] ignored to avoid double-render.",
                    len(insight_section_ids),
                    len(payload["insights"]),
                )
                payload["insights"] = []  # Enforce thin path to prevent double rendering.

            # Fetch canvas sections for validation.
            canvas_store = getattr(ctx, "canvas_store", None)
            session_id = getattr(ctx, "session_id", "")
            if canvas_store is None or not session_id:
                logger.warning("subagent_return thin-path: canvas_store/session_id 缺失, 跳过 canvas section 校验 (这不应在生产路径发生).")
            else:
                try:
                    # empty-canvas mitigation: read-only, reading canvas solely to validate sections.
                    # No canvas ⇒ empty sections; use existing section-missing branch, without creating a row.
                    canvas = await canvas_store.get_canvas_by_session(session_id)
                    sections = await canvas_store.list_sections(canvas.id) if canvas is not None else []
                except Exception as e:
                    logger.warning(
                        "subagent_return thin-path: list_sections failed: %s",
                        e,
                    )
                    return {
                        "ok": False,
                        "error": (f"thin-path 校验失败 (canvas_store 不可用): {e}. 改用 insights[] inline 路径或重试."),
                    }
                # Section → dict (kind + flattened payload)
                sections_by_id = {s.id: {"kind": s.kind, **s.payload} for s in sections}

                # Read outline injected by turn_runner.
                outline = getattr(ctx, CTX_OUTLINE_ATTR, None)
                tier_titles = list(outline.tier_titles) if outline else None

                thin_errs = validate_thin_canvas_sections(
                    insight_section_ids=insight_section_ids,
                    sections_by_id=sections_by_id,
                    outline_tier_titles=tier_titles,
                )
                if thin_errs:
                    try:
                        from bowhead_core import metrics as _m

                        # Classify reason for retry metrics.
                        reason = (
                            "tier_uncovered"
                            if any("未覆盖的 tier" in e for e in thin_errs)
                            else "anti_shell"
                            if any("placeholder" in e for e in thin_errs)
                            else "missing_id"
                            if any("unknown id" in e for e in thin_errs)
                            else "wrong_kind"
                            if any("expected 'insight_card'" in e for e in thin_errs)
                            else "other"
                        )
                        _m.inc(
                            "bowhead_thin_payload_retry_total",
                            labels={"reason": reason},
                            help="subagent_return thin-path validation failures by reason (M157).",
                        )
                        if reason == "tier_uncovered":
                            _m.inc(
                                "bowhead_outline_coverage_validation_failed_total",
                                help="thin-path tier outline coverage check failures (M157).",
                            )
                    except Exception:  # noqa: S110
                        pass
                    err_lines = "\n".join(f"  - {e}" for e in thin_errs[:10])
                    return {
                        "ok": False,
                        "error": "thin-path canvas section 校验失败:\n" + err_lines,
                        "validation_errors": thin_errs[:10],
                    }

        # ─── ξ B7: report-quality gate (citation) ───
        # ξ B5 visualization gate removed in chat-truth sprint (contradicting the prompt). Citation gate encodes a
        # universal contract consistent with the prompt; retained.
        # 0.39.3 Phase 4: citation density path now auto-appends [cite:N] (BE deterministic
        # registry) instead of reject→retry→strip ladder. LLM emit unreliability bypassed
        # entirely (per [[feedback_llm_internal_syntax_unreliable]]).
        _ξ_cite_gate = os.environ.get("BOWHEAD_CITATION_DENSITY_GATE", "1") == "1"
        if _ξ_cite_gate:
            # Skip ALL gates when this is a unit-test fixture path
            # (`_current_subagent_invocations` attr absent on stub ctx). Prod path
            # always sets it via subagent.py:946.
            _ξ_skip_gates = getattr(ctx, "_current_subagent_invocations", None) is None
            _ξ_retry = getattr(ctx, "_ξ_report_retry_count", 0)
            _ξ_errors: list[str] = []
            if not _ξ_skip_gates and _ξ_cite_gate:
                # Phase 4: auto-append [cite:N] in-place before validation.
                # Mutates payload texts; gate sees `[cite:N]` markers after this
                # and only fires when no evidence artifacts available at all
                # (in which case strip-and-pass ladder still cleans up).
                try:
                    _autocite_records = _auto_append_citations(payload, ctx)
                    if _autocite_records:
                        logger.info(
                            "ξ.citation_auto_append turn=%s injected=%d sample=%s",
                            getattr(ctx, "turn_id", ""),
                            len(_autocite_records),
                            _autocite_records[:3],
                        )
                except Exception as _ac_exc:
                    logger.warning(
                        "ξ.citation_auto_append.failed turn=%s err=%s",
                        getattr(ctx, "turn_id", ""),
                        _ac_exc,
                    )
                # citation-audit M1-0: B7 changes grounding anchors (numbers or citations count as grounded);
                # default shadow logs without rejection. Enforce activation is an independent decision after
                # enough shadow data (BOWHEAD_B7_GROUNDING=enforce). M3-2: the old
                # mandatory-marker _validate_citation_density kill-switch branch was removed.
                _cite_ok, _cite_errs = _validate_grounding_density(payload)
                if not _cite_ok and os.environ.get("BOWHEAD_B7_GROUNDING", "shadow").strip() != "enforce":
                    logger.warning(
                        "xi.b7.grounding_shadow_fail turn=%s errors=%s",
                        getattr(ctx, "turn_id", ""),
                        _cite_errs[:5],
                    )
                    # Shadow: log without rejection; clear errors to avoid the rejection ladder.
                    _cite_ok, _cite_errs = True, []
                _ξ_errors.extend(_cite_errs)
                # 0.39.7 #6 observability — distinguish callout-only-skip from
                # real-pass and real-reject so dashboards can chart 0.39.6 fix
                # impact over time. Skip-callout-only fires when payload has
                # insights but ALL are callout-only (no chart/comp/table id);
                # see _insights_are_callout_only docstring.
                if _cite_ok and not _cite_errs and _insights_are_callout_only(payload):
                    _xi_track_metric(ctx, "xi.gate.skip", reason="callout_only")
            if _ξ_errors and _ξ_retry < 1:
                ctx._ξ_report_retry_count = _ξ_retry + 1
                logger.info(
                    "ξ.report_gate_reject turn=%s retry=%d errors=%d",
                    getattr(ctx, "turn_id", ""),
                    _ξ_retry,
                    len(_ξ_errors),
                )
                _xi_track_metric(
                    ctx,
                    "xi.gate.reject",
                    retry=str(_ξ_retry),
                    error_count=str(len(_ξ_errors)),
                )
                err_lines = "\n".join(f"  - {e}" for e in _ξ_errors[:6])
                return {
                    "ok": False,
                    "error": (
                        "ξ report-quality gate rejected — peer 必须修后再 subagent_return:\n"
                        + err_lines
                        + "\n\n本次 retry 后续仍 reject 会 strip-and-pass (max 1 retry)."
                    ),
                    "ξ_report_retry": True,  # Do not count toward tool_failure_counts (same pattern as ν1).
                    "ξ_errors": _ξ_errors[:10],
                }
            if _ξ_errors:
                # >=2 retries: strip-and-pass + audit (same as ν1 strip_degrade).
                logger.warning(
                    "ξ.report_gate_strip_degrade turn=%s retry=%d errors=%s",
                    getattr(ctx, "turn_id", ""),
                    _ξ_retry,
                    _ξ_errors[:3],
                )
                _xi_track_metric(
                    ctx,
                    "xi.gate.strip_degrade",
                    retry=str(_ξ_retry),
                    error_count=str(len(_ξ_errors)),
                )
                try:
                    from bowhead_core._ref_audit import emit_ref_audit_event

                    await emit_ref_audit_event(
                        getattr(ctx, "turn_id", ""),
                        "ξ_report_strip_degrade",
                        {"errors": _ξ_errors[:10], "retry_count": _ξ_retry},
                        user_id=getattr(ctx, "user_id", "") or "",
                        workspace_id=getattr(ctx, "workspace_id", "") or "",
                    )
                except Exception:  # noqa: S110
                    pass
                # let through — degraded but don't block user

        # Sprint chat-style-fix Phase A3 — compose ≤200 char header_insight
        # via haiku micro-LLM (reuse tool_finding.py Router.invoke pattern).
        # Skip when no insights (bail/thin path) — header_insight stays "" and
        # FE B2 force-expands the agsec body so user still sees full content.
        # Best-effort: any failure leaves header_insight="" (silent).
        try:
            await _compose_header_insight(payload, ctx)
        except Exception as _hi_exc:
            logger.warning(
                "chat_style_fix.header_insight.compose_failed: %s",
                _hi_exc,
            )
            # Ensure field is at least set to "" so downstream serializers
            # don't crash on missing key.
            if isinstance(payload, dict):
                payload.setdefault("header_insight", "")

        # Store on ctx for the subagent main loop to consume later (do not directly modify SubagentResult here,
        # keeping tool handlers from knowing too much about subagent internals).
        try:
            setattr(ctx, CTX_PAYLOAD_ATTR, payload)
        except Exception as e:
            logger.warning("subagent_return: failed to attach payload to ctx: %s", e)
            return {"ok": False, "error": f"internal: ctx attach failed: {e}"}

        # Sprint ψ-E: eager compose. As soon as peer returns a valid payload,
        # write sections to canvas_store via compose_canvas_from_payload with
        # emit_lifecycle=False (Lead end-of-turn call still emits lifecycle once,
        # and dedupes by (turn_id, peer_id, kind_ordinal) — see canvas_composer.py).
        # This is the Bug 1a fix: if Lead cancels/times out after peer return,
        # canvas_store still has narrative/insight/kpi sections.
        try:
            from bowhead_core import psi_flags as _pf

            _eager_on = _pf.get("PSI_EAGER_COMPOSE")
        except Exception:
            _eager_on = True

        # DF-04: the bounded Chat contract must never enter either report
        # composer.  This eager path runs inside subagent_return, before Lead's
        # end-of-turn aggregate filter, so filtering only in Lead still causes a
        # mid-turn Canvas flash.
        if is_short_answer_payload(payload) or isinstance(payload.get("error"), dict) and payload["error"].get("kind") == "needs_user_input":
            _eager_on = False

        _cs = getattr(ctx, "canvas_store", None)
        _bus = getattr(ctx, "parent_bus", None) or getattr(ctx, "bus", None)
        # Decide before the first automatic report write, using the same
        # user/Plan/Lead precedence as final composition.
        if _eager_on:
            try:
                from bowhead_core.agents.delivery_decision import decide_early_surface
                from bowhead_core.agents.lead_orchestrator import (
                    parse_user_delivery_directive as _pudd,
                )

                if (
                    decide_early_surface(
                        user_directive=_pudd(getattr(ctx, "_psi_turn_user_text", "") or ""),
                        plan_deliverable_kind=getattr(_bus, "_d6_plan_deliverable_kind", None),
                        lead_delivery=getattr(_bus, "_lead_delivery_spawn", None),
                    )
                    == "chat"
                ):
                    _eager_on = False
            except Exception:  # noqa: S110
                pass
        # With cards as default ghostwriting, eager is the main rendering path without direct authorship. If this peer used the pen,
        # body prose is already on canvas (M-C direct authorship) and payload serves structured evidence (KPI band/aggregation);
        # eager cards would duplicate the body, contradicting compute's promise that payload will not duplicate its
        # prose. Use actual write_report calls from this invocation (ContextVar isolates each spawn,
        # B-P2-9), which the LLM cannot forge.
        if _eager_on:
            try:
                from bowhead_core.agents._lead_context import (
                    get_current_subagent_invocations,
                )
                from bowhead_core.agents.report_tools import _resolve_writer_id
                from bowhead_core.canvas_revise import write_report_succeeded

                _pen_invs = get_current_subagent_invocations()
                if _pen_invs is None:
                    _pen_invs = getattr(ctx, "_current_subagent_invocations", None) or []
                # Only a successful write_report may suppress eager composition. Reuse the
                # writer-identity resolver on both ledger sides and record success on the bus,
                # independent of parallel invocation-snapshot timing.
                if write_report_succeeded(_bus, _resolve_writer_id(ctx), _pen_invs):
                    _eager_on = False
            except Exception:  # noqa: S110
                pass
        if (
            _eager_on
            and _cs is not None
            and _bus is not None
            and isinstance(payload, dict)
            and (payload.get("insights") or payload.get("kpis") or payload.get("exec_summary"))
        ):
            try:
                # C2 (plan-report-assembly discovery §2): peer_id must come from
                # ContextVar, never parent_ctx._current_subagent_id.
                # The latter is explicitly SHARED across parallel sibling tasks → last-wins
                # (_lead_context.py:44), overwritten by later sibling legs during spawn;
                # even the handler-entry synchronous snapshot cannot rescue an already-overwritten value.
                # In production t_a06cfbe66ec8, compute legs 0/1 got the same peer_id, colliding
                # on ψ-E idempotency key (turn_id, peer_id, kind#ordinal). The later-returning leg's
                # nine sections were discarded as duplicates: no competitor-report prose reached canvas, yet its plan card still said ✓Verified.
                # SubagentRunner sets ContextVar once per spawn with per-task isolation,
                # exactly why report-section owners always resolve the correct sub_ps_*.
                from bowhead_core.agents._lead_context import (
                    get_current_subagent_id as _get_cur_sub_id,
                )
                from bowhead_core.canvas_composer import compose_canvas_from_payload as _psi_compose
                from bowhead_core.types import TurnRequest as _PsiTR

                _peer_id = _get_cur_sub_id() or _handler_subagent_id or "peer"
                _req = _PsiTR(
                    session_id=getattr(ctx, "session_id", "") or "",
                    workspace_id=getattr(ctx, "workspace_id", "") or "",
                    user_id=getattr(ctx, "user_id", "") or "",
                    member_uid=getattr(ctx, "member_uid", "") or "",
                    text=getattr(ctx, "_psi_turn_user_text", "") or "",
                    turn_id=getattr(ctx, "turn_id", "") or "",
                    language_context=getattr(ctx, "language_context", None),
                )
                _intent = getattr(ctx, "_inferred_intent", None)
                _invs = list(run_invocations(ctx))
                # canvas-update-turn batch B0 (review fix): revise/append eager must not create a new document.
                # Remove title (no h1) + chapter_mode (no eyebrow/meta/recommendations/limitations); write only cards and charts.
                # Otherwise a failed compute write_report renders full chrome here, creating a new document and collapsing the old report.
                from bowhead_core.canvas_revise import no_document_chrome as _no_chrome
                from bowhead_core.canvas_revise import strip_document_chrome_payload as _strip_chrome

                _mode_no_chrome = _no_chrome(_bus)
                if _mode_no_chrome:
                    logger.info("psi.eager_compose_mode_no_chrome peer=%s turn=%s", _peer_id, _req.turn_id)
                # canvas-elements: render document chrome once per turn (tech-design-mixed-turn-chrome §2.1). Another peer succeeded
                # in direct authorship in this turn's ledger (116 A3/C5: peer 1 wrote nine chapters then crashed; respawned peer 2 returns payload).
                # This payload is merely a document chapter: strip title / exec_summary / kpis and use chapter_mode; render chrome
                # (h1 / byline / KPI band / recommendations / limitations) once from the aggregated payload at finish. Suppression for this peer's own
                # pen use is decided above; this checks only other peers' direct authorship.
                _mixed_turn = False
                if not _mode_no_chrome:
                    try:
                        from bowhead_core.agents.report_tools import _resolve_writer_id as _rwid_mixed
                        from bowhead_core.canvas_revise import other_writer_succeeded as _other_pen

                        _mixed_turn = _other_pen(_bus, _rwid_mixed(ctx))
                    except Exception:  # noqa: S110
                        _mixed_turn = False
                if _mixed_turn:
                    from bowhead_core.canvas_revise import strip_mixed_turn_payload as _strip_mixed

                    logger.info(
                        "psi.eager_compose_mode_mixed_turn peer=%s turn=%s writers=%s",
                        _peer_id,
                        _req.turn_id,
                        sorted(getattr(_bus, "_write_report_ok_writers", None) or []),
                    )
                _eager_payload = _strip_chrome(payload, _bus) if _mode_no_chrome else (_strip_mixed(payload) if _mixed_turn else payload)
                _kinds = await _psi_compose(
                    canvas_store=_cs,
                    request=_req,
                    bus=_bus,
                    payload=_eager_payload,
                    intent=_intent,
                    tool_invocations=_invs,
                    peer_id=_peer_id,
                    emit_lifecycle=False,  # Lead end-of-turn emits lifecycle
                    # Batch 2-a (plan-report-assembly §2.3): each leg in plan execution submits a chapter,
                    # not an independent report. Do not render leg-level chrome (eyebrow/recommendations/
                    # data limitations) locally; Lead renders each once for the whole aggregated payload at turn end.
                    # Use turn-level _d6_is_plan_execution (= sctx.plan_execution_id),
                    # not leg count, unknown when the first leg submits its draft.
                    # Leave non-plan multi-leg turns unchanged for now (only three of 533 production turns split, §2.7).
                    chapter_mode=bool(getattr(_bus, "_d6_is_plan_execution", False)) or _mode_no_chrome or _mixed_turn,
                    # chat-truth B-5.5: chart auto-build fallback (peer ctx artifact_store)
                    artifact_store=getattr(ctx, "artifact_store", None),
                )
                logger.info(
                    "psi.eager_compose peer=%s kinds=%s",
                    _peer_id,
                    _kinds,
                )
                # chat-trace-fix-4 (2026-05-15): set flag on BUS (turn-scope,
                # shared with Lead's runtime.parent_bus) so Lead end-of-turn
                # compose can detect ψ-E already ran and skip — was previously
                # set on `ctx` (peer ctx) which Lead never reads → double
                # compose. Real bug from prod t_e6a617842126 (18 sections,
                # 9-section report duplicated).
                # canvas-update-turn batch B0 (116 follow-up s_f2c1700cd1d3): revise/append strips payload
                # title/kpis/exec_summary, so eager often writes no sections (kinds=[]); do not mark composed then,
                # or final Lead compose_skip_eager_done prevents the revision finisher from running (sources/provenance/B1 chrome rely on it).
                if _kinds:
                    try:  # noqa: SIM105
                        _bus._psi_compose_done = True  # type: ignore[attr-defined]
                    except Exception:  # noqa: S110
                        pass
                else:
                    logger.info("psi.eager_compose_landed_nothing peer=%s turn=%s — 不标 _psi_compose_done", _peer_id, _req.turn_id)
                # Keep legacy ctx attr for backwards compat / debugging.
                ctx._psi_compose_attempted = True
            except Exception as _e:
                # Likewise, logs must not read shared attributes (exceptions can precede _peer_id assignment,
                # so reread ContextVar here rather than referencing _peer_id).
                from bowhead_core.agents._lead_context import (
                    get_current_subagent_id as _get_cur_sub_id_log,
                )

                logger.warning("psi.eager_compose_failed peer=%s err=%s", _get_cur_sub_id_log() or _handler_subagent_id or "?", _e)
                # Do NOT raise — Lead end-of-turn compose is the safety net
        # Return a summary to the LLM explaining its submission + terminal signal.
        try:
            obj = payload_from_dict(payload)
            summary = {
                "title": obj.title,
                "insights_count": len(obj.insights),
                "evidence_total": sum(len(i.evidence) for i in obj.insights),
                "recommendations_count": len(obj.recommendations),
                "kpis_count": len(obj.kpis),
            }
        except Exception as e:
            logger.warning("subagent_return: from_dict failed (payload已存): %s", e)
            summary = {"insights_count": len(payload.get("insights") or [])}

        _result: dict[str, Any] = {
            "ok": True,
            "summary": summary,
            "message": ("Payload accepted. Subagent loop will now exit and hand off to composer. Do NOT call more tools."),
            TERMINAL_FLAG: True,
        }
        # Sprint agent-redesign-1 Phase A: expose auto-fix records to LLM.
        # Review P1-3 (2026-05-15): default OFF for ship to avoid 600+ token
        # nag per peer return × N peers. Structured log records still emitted
        # via log_fix_records (above). Single-line note when fix count high.
        # Flip BOWHEAD_AUTOFIX_NOTIFY_LLM=1 after offline analysis shows peer
        # actually adapts (i.e. follow-up turns emit fewer auto-fix triggers).
        if _auto_fix_records:
            _notify_llm = _os_a.environ.get("BOWHEAD_AUTOFIX_NOTIFY_LLM", "0") == "1"
            if _notify_llm:
                _result["_auto_fixed_fields"] = _auto_fix_records
                _result["message"] = (
                    _result["message"] + f" Note: {len(_auto_fix_records)} field(s) auto-fixed by soft-gate "
                    f"(see `_auto_fixed_fields` for details — please use compliant form on subsequent turns)."
                )
            elif len(_auto_fix_records) >= 3:
                # Quiet but visible signal when fix count high → peer awareness
                _result["message"] = _result["message"] + f" (soft-gate auto-fixed {len(_auto_fix_records)} fields silently)"
        return _result

    return _handler


def register_subagent_return_tool(tool_registry: ToolRegistry) -> None:
    """把 ``subagent_return`` 注册进 ToolRegistry.

    必须在 gateway 启动时调用一次. 注册后还需要把 ``"subagent_return"`` 写入
    需要使用此 tool 的 SubagentProfile.tool_whitelist 才能让 LLM 看到它.
    """
    schema = ToolSchema(
        name="subagent_return",
        description=(
            "**Final tool — call this LAST**, after analysis is complete. Pack the "
            "result into a structured AnalysisPayload (JSON). **delivery_surface is YOUR decision** "
            "('chat' or 'report') — always write it; the backend honors it and does not second-guess "
            "whether the content 'looks like a report'. (Omitting it is not rejected, but then the "
            "surface falls back to whatever evidence the turn produced — decide, don't drift.) "
            "If the Lead passed a delivery hint in the Sub-task context (user's words or remembered "
            "preference), follow it. "
            # Explain where each payload field renders. Repeating complete answers in both
            # exec_summary and the appended KPI/insight arrays duplicates evidence; authors
            # need the rendering order, not an arbitrary prose-length restriction.
            "chat: exec_summary is the only required field. Anything else you add "
            "(kpis / insights / recommendations / caveats) is appended VERBATIM below it, in that "
            "order — so a figure you already stated in exec_summary gets read twice. Put each fact "
            "in exactly one of the two places. A chart named in an insight's evidence (chart_section_id) is shown right after that insight; charts no insight names go at the end. "
            "report: title + KPIs + insights + recommendations. "
            "Long text reads poorly in chat — if it needs sections, multiple dimensions, or will be "
            "re-read/exported, choose 'report'. "
            "After this tool succeeds, the subagent loop ends — DO NOT call more tools.\n"
            "Short-answer required field: exec_summary. Report required fields: "
            "title, exec_summary, kpis, insights, recommendations.\n"
            "Each insight requires: title, thesis, evidence (1-5 blocks), narrative (30-1200 chars).\n"
            "Each evidence requires: kind ∈ {chart, computation, table, callout} + caption + matching ref field.\n"
            "P0 recommendations require owner. See analyst-sop-output-contract for schema doc."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "payload": {
                    "type": "object",
                    "description": "AnalysisPayload — see schema doc",
                    "properties": {
                        "delivery_surface": {
                            "type": "string",
                            "enum": ["chat", "report"],
                            "description": (
                                "你的交付面决定，**每次都写**。chat=答案在聊天里（kpis/insights/建议/caveats 会**按序原样拼在 exec_summary 之后**——同一个数别在两处都写）；report=画布报告。后端照办、不替你判断算不算报告；省略不会被拒，但交付面会退回按本轮产出推断。"
                            ),
                        },
                        "title": {"type": "string"},
                        "eyebrow": {"type": ["string", "null"]},
                        "exec_summary": {"type": "string"},
                        "ground_facts": {
                            "type": "array",
                            "description": (
                                "数字台账：报告里要引用的每个数都先登记于此（先算→记账→照账写）。"
                                "一条 = {key, label, value(数字), unit, formula, source}。"
                                '派生量(户均/占比/同比…) formula 必填(如 "3.57亿/8438" 或 "70.17/136.38*100")；'
                                '给定/工具读出的基数留空 formula、source="given"。'
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "key": {"type": "string"},
                                    "label": {"type": "string"},
                                    "value": {"type": "number"},
                                    "unit": {"type": "string"},
                                    "formula": {"type": "string"},
                                    "source": {"type": "string"},
                                },
                                "required": ["value"],
                            },
                        },
                        "kpis": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "value": {"type": "string"},
                                    "delta": {"type": ["string", "null"]},
                                    "sentiment": {
                                        "type": "string",
                                        "enum": ["good", "bad", "neutral"],
                                    },
                                },
                                "required": ["label", "value"],
                            },
                        },
                        "insights": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 7,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "thesis": {"type": "string"},
                                    "evidence": {
                                        "type": "array",
                                        "minItems": 1,
                                        "maxItems": 5,
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "kind": {
                                                    "type": "string",
                                                    "enum": ["chart", "computation", "table", "callout"],
                                                },
                                                "caption": {"type": "string"},
                                                "chart_section_id": {"type": ["string", "null"]},
                                                "computation_artifact_id": {"type": ["string", "null"]},
                                                "table_artifact_id": {"type": ["string", "null"]},
                                                "callout_text": {"type": ["string", "null"]},
                                                "subcaption": {"type": ["string", "null"]},
                                                "display_hint": {
                                                    "type": "string",
                                                    "enum": ["full_width", "half", "third", "collapsed"],
                                                },
                                            },
                                            "required": ["kind", "caption"],
                                        },
                                    },
                                    "narrative": {"type": "string"},
                                    "impact": {"type": ["object", "null"]},
                                    "confidence": {
                                        "type": "string",
                                        "enum": ["high", "med", "low"],
                                    },
                                    "source_refs": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                                "required": ["title", "thesis", "evidence", "narrative"],
                            },
                        },
                        "recommendations": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "priority": {
                                        "type": "string",
                                        "enum": ["P0", "P1", "P2"],
                                    },
                                    "action": {"type": "string"},
                                    "owner": {"type": ["string", "null"]},
                                    "timeline": {"type": ["string", "null"]},
                                    "expected_impact": {"type": ["string", "null"]},
                                    "rationale": {"type": ["string", "null"]},
                                },
                                "required": ["priority", "action"],
                            },
                        },
                        "caveats": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "sources": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": (
                                "数据/信息来源清单, 每条 {label, tool, query?}. "
                                "label=来源名(网页标题/机构/数据集/查询口径), tool=取数工具"
                                "(web_search/bi_query/kb_search/python_sandbox…), query=检索词或查询(可选). "
                                "★**只要你调用过任何取数工具就必填**, 每次实际取数一条 —— "
                                "**不限于联网检索**: bi_query / kb_search / python_sandbox 同样要填. "
                                "只在 narrative 里 inline 提及而不填这里, 下游核验、引用归属与溯源都看不见."
                            ),
                        },
                        "insight_section_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "**内置 peer 一律留空**, insights inline 写进 payload.insights[]. "
                                "只有工具面里真有 chunk 式洞察写入工具、且你调过它时才填(它返的 sec_xxx 列表; 那时 insights 留空防双渲染)."
                            ),
                        },
                    },
                    "required": ["exec_summary"],
                },
            },
            "required": ["payload"],
        },
    )

    tool_registry.register(
        ToolSpec(
            schema=schema,
            handler=_make_handler(),
            risk_level="low",
            effect_spec=TOOL_EFFECT_NONE,
        )
    )


def consume_returned_payload(ctx: Any) -> dict[str, Any] | None:
    """subagent 主循环用 —— 调一次取走暂存在 ctx 上的 payload.

    会清掉 ctx 上的属性，避免被下一次 turn 捡到（虽然 ctx 本就是 turn-scoped）.
    """
    payload = getattr(ctx, CTX_PAYLOAD_ATTR, None)
    if payload is not None:
        with contextlib.suppress(AttributeError):
            delattr(ctx, CTX_PAYLOAD_ATTR)
    return payload
