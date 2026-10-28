"""AnalysisPayload — subagent → composer 的结构化输出契约（M137 expert-report）.

设计文档：``docs/M137-expert-report-architecture.md``
Spike 验证：``reports/spike-M137/decision.md`` — Qwen 5/5 通过结构 + 内容质量

# 本文件做什么

定义 subagent 用 `subagent_return(payload=...)` 回 Lead 的 schema. 老的"自由 narrative
+ 散落 add_chart"输出路径仍然保留（M1 双写，M4 才关）；这层 schema 是新增的可选路径.

# Schema 决策点

- ``Insight.confidence`` 是 **required**（默认 "med"）—— spike 实测 Qwen 不主动填，
  设 required + 默认值后强制 prompt 引导填.
- ``Insight.caveats`` 故意 **没有** —— spike 发现 Qwen 把 caveats 全部丢顶层
  ``AnalysisPayload.caveats``，不要双写字段制造 confusion.
- ``EvidenceBlock`` 的 4 种 kind 通过对应字段区分（chart_section_id / computation_artifact_id /
  table_artifact_id / callout_text），同时只能填一个，由 ``validate_payload`` 校验.

# 不变量（与 design doc / SOP 一致）

- ``len(insights) ∈ [1, 7]`` —— 太少不算报告，太多违反 MECE
- 每个 ``insight.evidence`` 至少 1 个 block，最多 5 个
- ``insight.narrative`` 长度 ∈ [30, 1200] 字（缺省代笔=卡片后放宽，2026-08-13）
- 每个 ``Recommendation`` 至少 ``priority + action``；P0 必须有 ``owner``
- ``EvidenceBlock`` 同时只能有一个 ``*_id`` / ``callout_text`` 字段非空
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable

# ─────────────────────────────────────────────────────────────
# Schema dataclasses
# ─────────────────────────────────────────────────────────────

# delivery-surface review #3: a 2-KPI/300-character window misses this sprint's anchor case:
# three to five lookups should not require a report. A three-KPI peer chat answer loses the exemption and is forced into
# a report (contract cliff observed on 116 t_0f77a0a0bd3b). Increase to 6 KPIs/600 characters, covering
# three to five lookups with margin; report downgrade prevention relies on title/insights/recommendations
# shell-field classification (checks above in is_short_answer_payload), not these two numeric limits.


@dataclass
class EvidenceBlock:
    """Insight 的证据单元 —— 1 个 insight 含 1-N 个 evidence.

    feedback-0713 #3: ``chart_section_id`` 语义 = **视觉 section 引用**——
    canvas 的 chart section 或 image section（compute peer 的 matplotlib PNG 经
    add_image 落 kind="image"）都用它引用, kind 仍写 "chart"。ν1/ν1.5
    allowlist 与 FE insight-card 嵌入两侧均按此语义收编（chart/image 同桶）。
    """

    kind: Literal["chart", "computation", "table", "callout"]
    caption: str
    chart_section_id: str | None = None
    computation_artifact_id: str | None = None
    table_artifact_id: str | None = None
    callout_text: str | None = None
    subcaption: str | None = None
    display_hint: Literal["full_width", "half", "third", "collapsed"] = "full_width"


@dataclass
class Impact:
    """量化业务影响（全部 optional —— 真填不出别编）."""

    quantity: float | None = None
    unit: str | None = None
    direction: Literal["+", "-", "=", None] = None
    timeframe: str | None = None
    note: str | None = None


@dataclass
class Insight:
    """报告的最小结构化分析单元.

    chat-v4 CHANGE-S.7: optional ``module_num`` (M1..M7 framework module) +
    ``signal`` (ok/warn/crit) for FE 7-module dashboard rendering.
    """

    title: str
    thesis: str
    evidence: list[EvidenceBlock]
    narrative: str
    impact: Impact | None = None
    confidence: Literal["high", "med", "low"] = "med"
    source_refs: list[str] = field(default_factory=list)
    # chat-v4 CHANGE-S.7 — optional 7-module classification fields.
    module_num: Literal["M1", "M2", "M3", "M4", "M5", "M6", "M7"] | None = None
    signal: Literal["ok", "warn", "crit"] | None = None


@dataclass
class Recommendation:
    """行动建议 —— P0 必须有 owner."""

    priority: Literal["P0", "P1", "P2"]
    action: str
    owner: str | None = None
    timeline: str | None = None
    expected_impact: str | None = None
    rationale: str | None = None


@dataclass
class Kpi:
    """顶部 KPI 卡.

    chat-v4 CHANGE-S.1: ``sentiment`` 新增 ``"warning"`` (中性偏负, FE 显黄色).
    """

    label: str
    value: str
    delta: str | None = None
    sentiment: Literal["good", "bad", "neutral", "warning"] = "neutral"


@dataclass
class Source:
    """数据源条目."""

    label: str
    tool: str
    artifact_id: str | None = None
    query: str | None = None


@dataclass
class GroundFact:
    """授权数字台账条目 —— 报告里写出的每个数字的单一真值来源。

    见 report-consistency-gate tech-design v3 §1–2。两类：
      - **给定/工具读出的基数**：``source`` 记来源（"given" / tool 名 / artifact_id /
        call_id），``formula`` 留空。
      - **派生量**（户均/占比/同比…）：``formula`` 必填（如 ``"3.57亿/8438"`` 或
        ``"3.57/8.26*100"``），后端按 **unit-aware** 重算校验（pre-test 实测模型
        分数/百分数两种写法都会出现 → 比对前两边归一）。

    ``value`` 是归一后的数值真值；``unit`` 保留原始单位 token（``% / 百分点 / 亿 /
    万 / 元 / 户`` …，**pp≠%** 须分类保留）。向后兼容：老 payload 无此字段 → ``[]``。
    """

    key: str
    label: str
    value: float
    unit: str = ""
    formula: str = ""  # Only derived quantities fill this; given bases leave it empty.
    source: str = ""  # "given" / tool / artifact_id / call_id


# chat-v4 W4-fix CTO P0-4 — partial_failure schema + RetryAction.
# Surfaces from peer subagent_return when one of N sub-fetches failed but
# others succeeded. FE renders .v4-ob-actions retry banner with subtask_id
# from retry_action so user can POST /retry_subtask.
@dataclass
class RetryAction:
    """Banner CTA for a partial failure row."""

    kind: Literal["retry_subtask", "ignore"]
    cta_label: str
    subtask_id: str | None = None


@dataclass
class PartialFailure:
    """Peer 主动暴露: 哪部分丢了 / 替代来源 / 影响 / 重试 CTA.

    与 ``error`` (整盘 bail) 互补: partial_failure 表示 turn 仍能 ship 部分结果,
    只是某 subtask 没成功 (e.g. multi-fetch 4/5 通过).
    """

    what_missed: str
    what_used_instead: str
    impact: str
    retry_action: RetryAction | None = None


@dataclass
class AnalysisPayload:
    """subagent_return 必传顶层结构 —— composer 按这个铺 canvas.

    M157 双兼容:
      - 老路径: insights[] inline 填 (M137 原始 schema)
      - 新路径: insights[] 留空, insight_section_ids 填 canvas_add_insight 返过的 ID
                composer 按 IDs 顺序拉 canvas section 渲染
      - 优先级: insight_section_ids 非空时优先用它, insights[] 被忽略 (双 fill 时 log warn)

    Sprint µ — `error` 字段 (optional bail envelope):
      payload={"error":{"kind":"domain_mismatch","message":"..."}} → peer 显式
      bail. Lead 看到结构化 kind 直接走 prose final answer 路径, 不走 clarification_ask.
      kind 枚举: domain_mismatch / ambiguity / transient / schema_invalid /
                tool_failure / needs_user_input.
      bail 时 title/exec_summary/kpis/insights/recommendations 都可省 (validator 跳过).

    bi-hitl-cleanup B7.1 (2026-05-21): added `needs_user_input` bail kind for
    peer→Lead HITL handoff. Distinct from `domain_mismatch` (ws 没该 domain →
    Lead prose tell user); `needs_user_input` means a required value or choice
    is missing, so peer fills `clarification_payload` (questions/options/type,
    B5 schema) into `error` dict. Lead emits the existing card (B7.2 path).

    `error` typed as `dict[str, Any]` (not `dict[str, str]`) — peer may carry
    nested `clarification_payload: dict` sub-key with the B5 questions list.
    A missing required value uses a `free_text` question with no options.
    """

    title: str
    exec_summary: str
    kpis: list[Kpi]
    insights: list[Insight]
    recommendations: list[Recommendation]
    # DF-04: explicit delivery discriminator for bounded scalar/small-comparison
    # answers. ``None`` retains the legacy/report contract.
    delivery_surface: Literal["chat", "report"] | None = None
    eyebrow: str | None = None
    caveats: list[str] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    # M157: chunk-emitted insight section IDs (returned by canvas_add_insight)
    insight_section_ids: list[str] = field(default_factory=list)
    # Sprint chat-style-fix Phase A3: ≤200 char aggregate insight composed via
    # haiku micro-LLM (see subagent_return_tool._compose_header_insight). FE
    # B2 force-expands agsec when this is empty so user always sees full body.
    # Forward+backward compat: AnalysisPayload is stored as JSON dict in
    # turn_log_store / session_store — old readers `.get('header_insight', '')`
    # safe; new readers on old turn return "". No DB migration needed.
    header_insight: str = ""
    # Sprint µ: structured bail envelope. Non-None → peer voluntarily bails; Lead branches by kind.
    # bi-hitl-cleanup B7.1 (2026-05-21): widened `dict[str, str]` → `dict[str, Any]`
    # so peer can carry nested `clarification_payload: dict` (B5 schema:
    # questions/options/type) for the `needs_user_input` bail kind path.
    error: dict[str, Any] | None = None
    # chat-v4 W5 (merged into W4): multi-source comparison sub-payload.
    # substructure: {sources: [{name, kind}], gap?: str,
    #                rows: [{label, values: {<src>: <val>}, agreement?: str}],
    #                verdict: {summary?: str, rows: [{label, verdict, confidence}]}}.
    # Length caps enforced via validate_payload: rows ≤12, findings ≤20,
    # quote_excerpt ≤280 char.
    multi_source_compare: dict[str, Any] | None = None
    # chat-v4 W4 / G-scenario: free-form findings list (len ≤20).
    findings: list[dict[str, Any]] | None = None
    # chat-v4 W4-fix CTO P0-4: peer-emitted partial failure block. FE reads
    # retry_action.subtask_id to POST /api/v1/turns/{turn_id}/retry_subtask.
    partial_failure: PartialFailure | None = None
    # report-consistency-gate v3: authorized-number ledger. Analyst declares the true sources of report numbers
    # (derived quantities include formulas for backend unit-aware recalculation). Optional/backward-compatible: old payloads → [].
    ground_facts: list[GroundFact] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────


_VALID_EV_KINDS = {"chart", "computation", "table", "callout"}
_VALID_PRIORITIES = {"P0", "P1", "P2"}
_VALID_CONFIDENCE = {"high", "med", "low"}
_VALID_SENTIMENT = {"good", "bad", "neutral", "warning"}  # CHANGE-S.2 +warning
_VALID_MODULE_NUMS = {"M1", "M2", "M3", "M4", "M5", "M6", "M7"}  # CHANGE-S.7
_VALID_SIGNALS = {"ok", "warn", "crit"}  # CHANGE-S.7

# chat-v4 W5 (merged into W4) — multi_source_compare length caps.
_MSC_ROWS_MAX = 12
_MSC_QUOTE_EXCERPT_MAX = 280
_FINDINGS_MAX = 20

# Ambiguous units following a numeric value, including compact suffixes.
# Ordinary text such as "Operations" or "Corporate" is not a unit.
_BANNED_NUMERIC_UNIT = re.compile(r"(?<![\w.])[+-]?(?:\d+(?:[.,]\d+)*|\.\d+)(?:e[+-]?\d+)?\s*(ratio|rate)\b", re.IGNORECASE)

# Sprint µ + bi-hitl-cleanup B7.1: structured bail kinds. When a peer explicitly bails,
# payload={"error":{"kind":...}}. Lead branches by kind:
#   - domain_mismatch: workspace lacks this domain (e.g. no '万达店'
#                      dim_value) → Lead explains no data in prose (no clarification card)
#   - needs_user_input: workspace has data but the user's phrasing is ambiguous; peer fills
#                       `clarification_payload` (B5 schema: questions/options/type)
#                       into `error` dict; Lead routes to clarification_ask emit
#                       (B7.2 path). bi-hitl-cleanup 2026-05-21 added.
#   - ambiguity: inadequate query phrasing → clarify with user-keyword candidates (legacy)
#   - transient: tool failed but workspace actually has data → retry or show error
#   - schema_invalid: peer's own schema error (fallback after anti-shell trigger)
#   - tool_failure: persistent failures in foundational bi/sandbox tools
_VALID_BAIL_KINDS = {
    "domain_mismatch",
    "needs_user_input",
    "ambiguity",
    "transient",
    "schema_invalid",
    "tool_failure",
}
# pptx-from-template M3 (RD-4): a render/deck leaf returns a *deliverable* payload
# (a downloadable artifact), not an analysis payload — no insights/exec_summary/
# kpis/recommendations. Such a payload carries a non-empty `deliverables` list;
# each entry names a produced artifact. `validate_payload` + `auto_fix_payload`
# treat this as a distinct mode (alongside bail / thin / inline) so the deck-only
# return validates. `pptx` is the M3 deliverable; the set is intentionally open
# to future sandbox deliverable kinds (pdf/xlsx/docx) — a soft set, not a hard cap.
_VALID_DELIVERABLE_KINDS = {"pptx", "pdf", "xlsx", "docx", "image"}  # image: multimodal-v1


def is_deliverable_payload(p: dict[str, Any]) -> bool:
    """True iff `p` is a deck/render *deliverable* return (non-empty
    `deliverables` list) rather than an analysis or bail payload. A bail
    envelope (`error.kind` set) takes priority and is NOT a deliverable.
    Shared by `validate_payload` and `auto_fix_payload` so both agree.
    """
    if not isinstance(p, dict):
        return False
    if isinstance(p.get("error"), dict) and p["error"].get("kind") is not None:
        return False
    dv = p.get("deliverables")
    return isinstance(dv, list) and len(dv) > 0


_INSIGHTS_MIN, _INSIGHTS_MAX = 1, 7
_EVIDENCE_MIN, _EVIDENCE_MAX = 1, 5
# Default-ghostwriting decision (2026-08-13): cards are the default report form; insight.narrative is its main
# prose. The 400-character cap dates from summary-only cards; raise to 1200 to develop arguments
# (FE insight-card uses full NarrativeSection Markdown rendering without clamping; verified).
# Keep the minimum 30: shorter is not interpretation.
_NARR_MIN, _NARR_MAX = 30, 1200

# M137 follow-up: anti-shell — LLM lacks data yet packs a placeholder payload to deceive users.
# Matching these terms in any KPI value / impact note / narrative / thesis rejects the schema.
_SHELL_PLACEHOLDER_PATTERNS = (
    "待计算",
    "待补充",
    "待数据",
    "待加载",
    "待提供",
    "待查询",
    "暂无数据",
    "暂无",
    "暂未",
    "无数据",
    "TBD",
    "tbd",
    # Sprint agent-redesign-1 r2 (2026-05-15): removed "NA" / "N/A" / "n/a"
    # — prod trace t_06b59114 showed legit "-NA%" prose (no comparable period) hard-rejected.
    # Real hallucination signals are 暂无 / 待计算 / TBD; NA is too overloaded.
    "数据加载后",
    "数据校验后",
    "等待数据",
    "pending",
    "Pending",
    "待定",
    "未知",
    "<TODO>",
    "[计算中]",
    "[待补充]",
)


def _is_shell_value(s: str | None) -> bool:
    if not s:
        return False
    s_norm = str(s).strip().lower()
    if not s_norm:
        return False
    return any(pat.lower() in s_norm for pat in _SHELL_PLACEHOLDER_PATTERNS)


def _shell_token(s: str | None) -> str | None:
    """返触发 shell 的具体 token, 用于给 LLM 具体反馈."""
    if not s:
        return None
    s_norm = str(s).strip().lower()
    for pat in _SHELL_PLACEHOLDER_PATTERNS:
        if pat.lower() in s_norm:
            return pat
    return None


def is_short_answer_payload(p: Any) -> bool:
    """Whether a payload is a bounded Chat answer rather than a report draft.

    Compute's live prompt has long advertised a natural-size short form for a
    scalar lookup or small comparison.  The old validator nevertheless required
    report-only insights and recommendations, so peers padded trivial arithmetic
    into a report.  The exemption stays bounded — one short summary, a handful
    of KPI values (covers the「3-5 个查数」anchor case, see the constants above),
    and no report-bearing fields (title/insights/recommendations are the real
    discriminators against downgrading an incomplete report).
    """
    if not isinstance(p, dict):
        return False
    if p.get("delivery_surface") != "chat":
        return False
    if isinstance(p.get("error"), dict) or is_deliverable_payload(p):
        return False
    # compute-agent-optimize batch 5ⓐ, delivery-surface autonomy: a peer's chat declaration is authoritative; no longer use
    # short-enough shape (no title / no insights / ≤600 chars / ≤6 KPIs) to decide whether it is a report.
    # Extra insights/recommendations supply Lead's chat prose, not grounds to turn it into a report.
    # Prompts guide the judgment that long text is hard to read in chat; do not reject it here.

    summary = p.get("exec_summary")
    return isinstance(summary, str) and bool(summary.strip())


def validate_payload(p: dict[str, Any] | AnalysisPayload, *, revision: bool = False) -> tuple[bool, list[str]]:
    """校 dict 或 AnalysisPayload —— 返 (ok, errors).

    用于：
      - subagent_return tool runtime 验入参（Qwen 出 dict）
      - 单测验构造的实例
      - composer 入口 defensive check

    M157 双模式 priority (P0 #5):
      - ``insight_section_ids`` 非空 → thin path; ``insights[]`` 应空, 非空时 warn 忽略 (handler 端 log)
      - ``insight_section_ids`` 空 + ``insights[]`` 非空 → 老 inline path
      - 都空 → 拒 (subagent 没产 insight, 是 bug)

    ``revision=True``（canvas-update-turn 批 B0，Lead 档位 revise/append）：这轮交的不是一份新报告，
    文档级字段（title/kpis/insights 条数/recommendations）不再必填——收尾器在这两档本就不渲它们，
    硬要就是让 compute 编 KPI（review 修：修订说明叫「薄修订别填」，校验却按新报告拒收）。
    形状检查照留：给了就得是对的。
    """
    if isinstance(p, AnalysisPayload):
        d = asdict(p)
    elif isinstance(p, dict):
        d = p
    else:
        return False, [f"payload must be dict or AnalysisPayload, got {type(p).__name__}"]

    errs: list[str] = []
    delivery_surface = d.get("delivery_surface")
    if delivery_surface not in (None, "chat", "report"):
        errs.append("delivery_surface must be 'chat' or 'report'")

    # Validation-mode priority (each is an early-return; order matters):
    #   1. bail envelope (`error.kind` set) — the escape hatch, checked first.
    #   2. deliverable (non-empty `deliverables`) — deck/render return (RD-4).
    #   3. thin / inline analytical (insights[] / insight_section_ids) — default.
    # A new early-return branch MUST preserve this order (e.g. is_deliverable_payload
    # already excludes bail envelopes so #1 wins over #2).
    #
    # Sprint µ: structured bail envelope (`error.kind` set) → skip rest of validation.
    # Per discovery.md design fix: peers explicitly mark bail_kind for Lead's code to branch, without relying on LLM
    # free-text parsing. kind must be in _VALID_BAIL_KINDS; message is required.
    err_envelope = d.get("error")
    if isinstance(err_envelope, dict):
        kind = err_envelope.get("kind")
        message = err_envelope.get("message")
        if kind not in _VALID_BAIL_KINDS:
            errs.append(
                f"error.kind={kind!r} not in {sorted(_VALID_BAIL_KINDS)}. "
                f"Use 'domain_mismatch' (ws 无对应 metric/dim), "
                f"'needs_user_input' (required input is missing — fill "
                f"`clarification_payload` for HITL), "
                f"'ambiguity' (query 写法不到位), 'transient' (tool 暂失败), "
                f"'schema_invalid' (anti-shell), 'tool_failure'."
            )
        if not isinstance(message, str) or not message.strip():
            errs.append("error.message missing/empty (need 1-2 sentence reason)")
        return (len(errs) == 0, errs)

    # report-consistency-gate v3: lightweight structural validation of optional ground_facts; presence is not required.
    # When present, each entry needs a dict + nonempty key. Formula recalculation / units / cross-surface reconciliation belong to phased reconcile-layer work.
    gf = d.get("ground_facts")
    if gf is not None:
        if not isinstance(gf, list):
            errs.append("ground_facts must be a list")
        else:
            if len(gf) > 64:
                errs.append(f"ground_facts too many ({len(gf)} > 64)")
            for i, g in enumerate(gf[:64]):
                if not isinstance(g, dict):
                    errs.append(f"ground_facts[{i}] must be an object")
            # Missing key is nonfatal (payload_from_dict falls back to label); a convenience ledger must not hard-fail
            # the entire report schema (code-review F5).

    # pptx-from-template M3 (RD-4): deliverable mode — a deck/render leaf returns a
    # downloadable artifact, not an analysis payload. Validate `title` + each
    # `deliverables[]` entry; SKIP the analytical insights/exec_summary/kpis/
    # recommendations requirements (a render turn has no analysis content). This
    # branch is additive: payloads without a non-empty `deliverables` list are
    # unaffected and fall through to the existing thin/inline validation below.
    # INTENTIONAL deck-only semantics: a deck return is deck-only by contract. If a
    # payload carries BOTH `deliverables` and `insights`, it validates as a
    # deliverable and the insights are NOT validated/rendered (ignored). That's
    # accepted — the deliverable artifact is what surfaces; ignored insights are
    # harmless (not a data path). To get insight validation, omit `deliverables`.
    if is_deliverable_payload(d):
        if not (isinstance(d.get("title"), str) and d["title"].strip()):
            errs.append("missing/empty top-level: title")
        deliverables = d.get("deliverables") or []
        for i, dv in enumerate(deliverables):
            if not isinstance(dv, dict):
                errs.append(f"deliverables[{i}] not dict")
                continue
            aid = dv.get("artifact_id")
            if not (isinstance(aid, str) and aid.strip()):
                errs.append(f"deliverables[{i}].artifact_id missing/empty")
            # Distinguish missing from invalid (mirror the artifact_id pattern).
            if "kind" not in dv:
                errs.append(f"deliverables[{i}].kind missing")
            elif dv["kind"] not in _VALID_DELIVERABLE_KINDS:
                errs.append(f"deliverables[{i}].kind={dv['kind']!r} not in {sorted(_VALID_DELIVERABLE_KINDS)}")
        return (len(errs) == 0, errs)

    # B1e/DF-04: a bounded scalar lookup or small comparison is a Chat answer,
    # not a malformed report.  It still runs KPI/ground-fact validation below.
    is_short_answer = is_short_answer_payload(d)

    _chat_autonomy = delivery_surface == "chat"

    # M157: determine thin vs inline. Thin skips insights[] validation.
    insight_section_ids = d.get("insight_section_ids") or []
    is_thin = bool(insight_section_ids)

    # Required top-level fields; thin path permits empty insights.
    # Delivery-surface autonomy: chat requires only exec_summary (extra/out-of-scope fields no longer trigger report requirements; this coupling caused
    # 21 rejections in 30 days). The agent decides whether a report is appropriate; do not decide here.
    # B0 revision turns: document-level fields are optional (see docstring); short-answer/chat autonomy remains unchanged.
    # canvas-elements: the system guarantees only identity and traceability (tech-design-document-minimalism §2.1; user decision 2026-09-07).
    # recommendations and kpis are no longer required: content rather than identity, included at the author's discretion,
    # guided by skills/prompts, not hard validation rejection. On 116, of 419 subagent_return rejections over 180 days,
    # 100 were solely for omitted recommendations and 32 for omitted KPIs; every case rejected the entire payload for resubmission.
    # Hard rejection also duplicated content: forced fields always exist and render, while prompts teach authors to end prose with ## 建议;
    # in 312 authored-report turns, 50% did so themselves; 40 turns (12.8%) had both sets of recommendations.
    # Keep title / exec_summary / insights: titles identify documents; summaries and insights evidence what this turn analyzed,
    # consistent with this criterion. Preserve every shape check (recommendations/kpis fields below).
    _relaxed_doc = bool(revision) and not is_short_answer and not _chat_autonomy
    if is_short_answer or _chat_autonomy or _relaxed_doc:
        required_keys = ("exec_summary",)
    elif is_thin:
        required_keys = ("title", "exec_summary")
    else:
        required_keys = ("title", "exec_summary", "insights")
    for k in required_keys:
        if k not in d or d[k] in (None, "", []):
            errs.append(f"missing/empty top-level: {k}")

    # Dual modes: reject when both are empty.
    inline_insights = d.get("insights") or []
    if not is_short_answer and not is_thin and not _relaxed_doc and not inline_insights:
        errs.append(
            "neither insight_section_ids nor insights[] provided. 报告 payload 必须 inline 写 insights[]（≥1 条, 每条带 evidence）; "
            "insight_section_ids 只给工具面里有 chunk 式洞察写入工具的 peer 用, 内置 peer 留空."
        )

    # Validate insight counts only on the inline path (insights should be empty on the thin path).
    insights = inline_insights
    if not is_short_answer and not is_thin and not _relaxed_doc and not (_INSIGHTS_MIN <= len(insights) <= _INSIGHTS_MAX):
        errs.append(f"insights count={len(insights)} not in [{_INSIGHTS_MIN},{_INSIGHTS_MAX}]")
    # Delivery-surface autonomy: chat insights are just material for Lead's prose; do not impose report evidence/length validation.
    for i, ins in enumerate([] if _chat_autonomy else insights):
        if not isinstance(ins, dict):
            errs.append(f"insights[{i}] not dict")
            continue
        for k in ("title", "thesis", "evidence", "narrative"):
            if k not in ins or ins[k] in (None, "", []):
                errs.append(f"insights[{i}].{k} missing/empty")
        ev = ins.get("evidence") or []
        if not (_EVIDENCE_MIN <= len(ev) <= _EVIDENCE_MAX):
            errs.append(f"insights[{i}].evidence count={len(ev)} not in [{_EVIDENCE_MIN},{_EVIDENCE_MAX}]")
        for j, e in enumerate(ev):
            errs.extend(_validate_evidence(e, f"insights[{i}].evidence[{j}]"))
        narr = ins.get("narrative") or ""
        if not (_NARR_MIN <= len(narr) <= _NARR_MAX):
            errs.append(f"insights[{i}].narrative len={len(narr)} not in [{_NARR_MIN},{_NARR_MAX}]")
        conf = ins.get("confidence", "med")
        if conf not in _VALID_CONFIDENCE:
            errs.append(f"insights[{i}].confidence={conf!r} not in {_VALID_CONFIDENCE}")

    # recommendations: empty lists no longer fail (content belongs to authors). Included content must have the right shape; retain shape checks:
    # string lists such as recommendations: ["加投华东"] can pass validation but silently disappear during rendering,
    # leaving peers unaware of the mistake (original rationale retained).
    recs = d.get("recommendations") or []
    # review-R5 / review2-R5: chat declarations remove only report-specific priority/owner requirements
    # (the rejection-loop source); retain shape checks, otherwise string lists such as recommendations: ["加投华东"]
    # pass validation but silently disappear during rendering, leaving peers unaware of the mistake.
    for i, r in enumerate(recs):
        if not isinstance(r, dict):
            errs.append(f"recommendations[{i}] not dict")
            continue
        if not r.get("action"):
            errs.append(f"recommendations[{i}].action missing")
        if _chat_autonomy:
            continue
        prio = r.get("priority")
        if prio not in _VALID_PRIORITIES:
            errs.append(f"recommendations[{i}].priority={prio!r} not in {_VALID_PRIORITIES}")
        if prio == "P0" and not r.get("owner"):
            errs.append(f"recommendations[{i}] is P0 but owner missing")

    # kpis sentiment + anti-shell + Sprint µ unit whitelist
    raw_kpis = d.get("kpis")
    if raw_kpis is not None and not isinstance(raw_kpis, list):
        errs.append("kpis must be a list")
    for i, k in enumerate(raw_kpis if isinstance(raw_kpis, list) else []):
        if not isinstance(k, dict):
            errs.append(f"kpis[{i}] not dict")
            continue
        sent = k.get("sentiment", "neutral")
        if sent not in _VALID_SENTIMENT:
            errs.append(f"kpis[{i}].sentiment={sent!r} not in {_VALID_SENTIMENT}")
        if _is_shell_value(k.get("value")):
            errs.append(f"kpis[{i}].value={k.get('value')!r} is a placeholder. 无真数据 → 不要 pack payload，改返 insufficient_data.")
        # Reject numeric unit suffixes without scanning inside ordinary words.
        v_str = str(k.get("value", ""))
        for match in _BANNED_NUMERIC_UNIT.finditer(v_str):
            errs.append(
                f"kpis[{i}].value={v_str!r} contains banned unit {match[1].lower()!r}; "
                "use an explicit unit such as percent, decimal, currency, count or duration."
            )

    # M137 follow-up: check insight + impact for placeholders too.
    # review-R9 / review2-R5: chat declarations remove placeholder-word scanning (report discipline),
    # retaining shape checks: non-dict insights must still fail, or rendering silently loses content.
    for i, ins in enumerate(insights):
        if not isinstance(ins, dict):
            errs.append(f"insights[{i}] not dict")
            continue
        if _chat_autonomy:
            continue
        if _is_shell_value(ins.get("thesis")):
            tok = _shell_token(ins.get("thesis"))
            errs.append(
                f"insights[{i}].thesis 含空壳词 {tok!r}: '{ins.get('thesis')[:30]}'. "
                f"改写: 删 {tok!r}, 用真实数字 + dim_value (e.g. '体系合规率 51.6%' 而非 '体系合规率待计算')."
            )
        if _is_shell_value(ins.get("narrative")):
            tok = _shell_token(ins.get("narrative"))
            # Find the matched narrative position (20 context characters on either side).
            narr = str(ins.get("narrative") or "")
            idx = narr.lower().find(tok.lower()) if tok else -1
            ctx = narr[max(0, idx - 20) : idx + len(tok) + 20] if idx >= 0 else narr[:60]
            errs.append(
                f"insights[{i}].narrative 含空壳词 {tok!r} ('...{ctx}...'). 改写: narrative 必须含具体数字/dim_value, 不能用 {tok!r} 类占位词."
            )
        impact = ins.get("impact") or {}
        if isinstance(impact, dict) and _is_shell_value(impact.get("note")):
            tok = _shell_token(impact.get("note"))
            errs.append(f"insights[{i}].impact.note 含空壳词 {tok!r}. 无真量化数据 → 整个 impact 字段省略, 不要硬填 {tok!r}.")
        # CHANGE-S.7: validate optional module_num / signal
        mn = ins.get("module_num")
        if mn is not None and mn not in _VALID_MODULE_NUMS:
            errs.append(f"insights[{i}].module_num={mn!r} not in {sorted(_VALID_MODULE_NUMS)}")
        sig = ins.get("signal")
        if sig is not None and sig not in _VALID_SIGNALS:
            errs.append(f"insights[{i}].signal={sig!r} not in {sorted(_VALID_SIGNALS)}")

    # chat-v4 W5: multi_source_compare length caps + findings cap.
    msc = d.get("multi_source_compare")
    if isinstance(msc, dict):
        rows = msc.get("rows") or []
        if isinstance(rows, list) and len(rows) > _MSC_ROWS_MAX:
            errs.append(f"multi_source_compare.rows count={len(rows)} > cap {_MSC_ROWS_MAX}")
        for ri, row in enumerate(rows if isinstance(rows, list) else []):
            if not isinstance(row, dict):
                continue
            qe = row.get("quote_excerpt")
            if isinstance(qe, str) and len(qe) > _MSC_QUOTE_EXCERPT_MAX:
                errs.append(f"multi_source_compare.rows[{ri}].quote_excerpt len={len(qe)} > cap {_MSC_QUOTE_EXCERPT_MAX}")
        verdict = msc.get("verdict")
        if isinstance(verdict, dict):
            vrows = verdict.get("rows") or []
            for vi, vr in enumerate(vrows if isinstance(vrows, list) else []):
                if not isinstance(vr, dict):
                    continue
                vc = vr.get("confidence")
                if vc is not None and vc not in _VALID_CONFIDENCE:
                    errs.append(f"multi_source_compare.verdict.rows[{vi}].confidence={vc!r} not in {_VALID_CONFIDENCE}")
    findings = d.get("findings")
    if isinstance(findings, list) and len(findings) > _FINDINGS_MAX:
        errs.append(f"findings count={len(findings)} > cap {_FINDINGS_MAX}")

    return (len(errs) == 0, errs)


def _validate_evidence(e: Any, path: str) -> list[str]:
    if not isinstance(e, dict):
        return [f"{path} not dict"]
    errs: list[str] = []
    kind = e.get("kind")
    if kind not in _VALID_EV_KINDS:
        errs.append(f"{path}.kind={kind!r} not in {_VALID_EV_KINDS}")
    if not e.get("caption"):
        errs.append(f"{path}.caption missing")
    # Only one *_id / callout_text may be nonempty, and it must match kind.
    expected_field_by_kind = {
        "chart": "chart_section_id",
        "computation": "computation_artifact_id",
        "table": "table_artifact_id",
        "callout": "callout_text",
    }
    expected = expected_field_by_kind.get(kind)
    if expected and not e.get(expected):
        errs.append(f"{path}.{expected} required when kind={kind!r}")
    # Prevent Qwen from filling two fields simultaneously.
    set_fields = [f for f in expected_field_by_kind.values() if e.get(f) not in (None, "")]
    if len(set_fields) > 1:
        errs.append(f"{path} has multiple ref fields filled: {set_fields}")
    return errs


# ─────────────────────────────────────────────────────────────
# from_dict: tolerate extra / omitted Qwen fields.
# ─────────────────────────────────────────────────────────────


def payload_from_dict(d: dict[str, Any]) -> AnalysisPayload:
    """dict → AnalysisPayload（不验证；先 validate 再调）.

    防御性地接 LLM 输出：未识别字段忽略；缺 optional 用默认.
    要求调用方先 ``validate_payload(d)`` 通过.
    """
    msc = d.get("multi_source_compare")
    findings = d.get("findings")
    pf_raw = d.get("partial_failure")
    pf: PartialFailure | None = None
    if isinstance(pf_raw, dict):
        ra_raw = pf_raw.get("retry_action")
        ra: RetryAction | None = None
        if isinstance(ra_raw, dict):
            kind = ra_raw.get("kind") if ra_raw.get("kind") in ("retry_subtask", "ignore") else "retry_subtask"
            ra = RetryAction(
                kind=kind,  # type: ignore[arg-type]
                cta_label=str(ra_raw.get("cta_label") or ""),
                subtask_id=ra_raw.get("subtask_id"),
            )
        pf = PartialFailure(
            what_missed=str(pf_raw.get("what_missed") or ""),
            what_used_instead=str(pf_raw.get("what_used_instead") or ""),
            impact=str(pf_raw.get("impact") or ""),
            retry_action=ra,
        )
    return AnalysisPayload(
        title=str(d.get("title") or ""),
        delivery_surface=(d.get("delivery_surface") if d.get("delivery_surface") in ("chat", "report") else None),
        eyebrow=d.get("eyebrow"),
        exec_summary=str(d.get("exec_summary") or ""),
        kpis=[_kpi_from_dict(k) for k in (d.get("kpis") or [])],
        insights=[_insight_from_dict(x) for x in (d.get("insights") or [])],
        recommendations=[_rec_from_dict(r) for r in (d.get("recommendations") or [])],
        caveats=[str(c) for c in (d.get("caveats") or [])],
        sources=[_source_from_dict(s) for s in (d.get("sources") or [])],
        insight_section_ids=[str(x) for x in (d.get("insight_section_ids") or [])],
        # chat-v4 W5 round-trip
        multi_source_compare=msc if isinstance(msc, dict) else None,
        findings=list(findings) if isinstance(findings, list) else None,
        partial_failure=pf,
        ground_facts=[_groundfact_from_dict(g) for g in (d.get("ground_facts") or []) if isinstance(g, dict)],
    )


# Bare positive percentages: 12% / 0.40 % / 1.26％ (fullwidth). No sign or directional word.
_BARE_POSITIVE_PCT = re.compile(r"^\s*\d+(?:\.\d+)?\s*[%％]\s*$")


def _normalize_delta_sign(raw: Any) -> Any:
    """Add a plus sign to an otherwise bare positive percentage delta.

    Preserve explicit signs, direction words, and non-percentage values. This applies
    only to KPI.delta; prose guidance is enforced separately.
    """
    if not isinstance(raw, str):
        return raw
    if _BARE_POSITIVE_PCT.match(raw):
        return f"+{raw.strip()}"
    return raw


def _kpi_from_dict(d: dict[str, Any]) -> Kpi:
    return Kpi(
        label=str(d.get("label") or ""),
        value=str(d.get("value") or ""),
        delta=_normalize_delta_sign(d.get("delta")),
        sentiment=d.get("sentiment") if d.get("sentiment") in _VALID_SENTIMENT else "neutral",
    )


def _insight_from_dict(d: dict[str, Any]) -> Insight:
    impact_raw = d.get("impact")
    impact = None
    if isinstance(impact_raw, dict):
        impact = Impact(
            quantity=impact_raw.get("quantity"),
            unit=impact_raw.get("unit"),
            direction=impact_raw.get("direction"),
            timeframe=impact_raw.get("timeframe"),
            note=impact_raw.get("note"),
        )
    conf = d.get("confidence")
    mn = d.get("module_num")
    sig = d.get("signal")
    return Insight(
        title=str(d.get("title") or ""),
        thesis=str(d.get("thesis") or ""),
        evidence=[_evidence_from_dict(e) for e in (d.get("evidence") or [])],
        narrative=str(d.get("narrative") or ""),
        impact=impact,
        confidence=conf if conf in _VALID_CONFIDENCE else "med",
        source_refs=[str(x) for x in (d.get("source_refs") or [])],
        module_num=mn if mn in _VALID_MODULE_NUMS else None,
        signal=sig if sig in _VALID_SIGNALS else None,
    )


def _evidence_from_dict(d: dict[str, Any]) -> EvidenceBlock:
    hint = d.get("display_hint")
    return EvidenceBlock(
        kind=d.get("kind") or "callout",
        caption=str(d.get("caption") or ""),
        chart_section_id=d.get("chart_section_id"),
        computation_artifact_id=d.get("computation_artifact_id"),
        table_artifact_id=d.get("table_artifact_id"),
        callout_text=d.get("callout_text"),
        subcaption=d.get("subcaption"),
        display_hint=hint if hint in {"full_width", "half", "third", "collapsed"} else "full_width",
    )


def _rec_from_dict(d: dict[str, Any]) -> Recommendation:
    prio = d.get("priority")
    return Recommendation(
        priority=prio if prio in _VALID_PRIORITIES else "P2",
        action=str(d.get("action") or ""),
        owner=d.get("owner"),
        timeline=d.get("timeline"),
        expected_impact=d.get("expected_impact"),
        rationale=d.get("rationale"),
    )


def _source_from_dict(d: dict[str, Any]) -> Source:
    return Source(
        label=str(d.get("label") or ""),
        tool=str(d.get("tool") or ""),
        artifact_id=d.get("artifact_id"),
        query=d.get("query"),
    )


def _groundfact_from_dict(d: dict[str, Any]) -> GroundFact:
    """dict → GroundFact，defensive 接 LLM 输出。

    ``value`` best-effort coerce 成 float（模型多按 schema 出 number，但也可能给
    "4.23" / "4.23万"〔单位另在 unit〕这类字符串）。无法解析 → 0.0（结构保留，
    数值不可信 → 由 reconcile/formula 校验抓，非这里硬失败）。
    """
    raw = d.get("value")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        fv = float(raw)
    else:
        m = re.search(r"-?\d+(?:\.\d+)?", str(raw or "").replace(",", ""))
        fv = float(m.group()) if m else 0.0
    return GroundFact(
        key=str(d.get("key") or d.get("label") or "fact"),  # Missing key is nonfatal: fall back to label.
        label=str(d.get("label") or ""),
        value=fv,
        unit=str(d.get("unit") or ""),
        formula=str(d.get("formula") or ""),
        source=str(d.get("source") or ""),
    )


# ─────────────────────────────────────────────────────────────
# M157 thin-path canvas section validation
# ─────────────────────────────────────────────────────────────


def validate_thin_canvas_sections(
    insight_section_ids: list[str],
    sections_by_id: dict[str, dict[str, Any]],
    outline_tier_titles: list[str] | None = None,
) -> list[str]:
    """thin path 的 canvas section 校验 (M157 P0 #3 + #4 + P1 #6).

    传入:
      insight_section_ids: payload.insight_section_ids
      sections_by_id: {sid → section payload dict (含 title/thesis/narrative/kind)}
                      kind 字段表示 canvas section kind, 期望 "insight_card".
      outline_tier_titles: meta_intent 抽出的 tier_titles (None / [] = 跳过 tier 校验).

    返:
      errors: 字符串 list, 空 = 通过.
    """
    errs: list[str] = []

    # P0 #4: every ID must exist in canvas and have kind insight_card.
    insight_titles_in_canvas: list[str] = []
    for sid in insight_section_ids:
        s = sections_by_id.get(sid)
        if s is None:
            errs.append(f"insight_section_ids contains unknown id {sid!r}; 先 canvas_add_insight 才能引用它.")
            continue
        if s.get("kind") and s.get("kind") != "insight_card":
            errs.append(f"section {sid!r} kind={s.get('kind')!r}, expected 'insight_card'. insight_section_ids 只能引 canvas_add_insight 返过的 ID.")
            continue
        # Collect titles for tier one-to-one validation.
        title = s.get("title") or ""
        if isinstance(title, str) and title.strip():
            insight_titles_in_canvas.append(title.strip())

        # P1 #6: extend anti-shell to canvas section title/thesis/narrative.
        for field_name in ("title", "thesis", "narrative"):
            val = s.get(field_name)
            if isinstance(val, str) and _is_shell_value(val):
                errs.append(f"section {sid!r}.{field_name} is placeholder ('{val[:30]}'); thin path canvas section 含空壳词.")

    # P0 #3: unique outline.tier_titles correspondence (greedy match + remove from pool).
    if outline_tier_titles:
        remaining = list(outline_tier_titles)
        for ins_title in insight_titles_in_canvas:
            matched = next(
                (t for t in remaining if t in ins_title or ins_title in t),
                None,
            )
            if matched:
                remaining.remove(matched)
        if remaining:
            errs.append(
                f"outline 未覆盖的 tier: {remaining}. "
                f"已产 insight 标题: {insight_titles_in_canvas}. "
                f"每个 tier 必须有对应 insight (title 包含 tier 关键词或反包含)."
            )

    return errs


# ─────────────────────────────────────────────────────────────
# chat-v4 CHANGE-W4.3 — fabrication back-reference helpers.
#
# Goal: detect KPI / insight numbers in the final payload that have NO
# back-reference to any observed tool_result. Treat as fabrication: add to
# caveats + downgrade verdict confidence rather than hard-reject (LLM still
# emits human-readable prose; user sees the caveat instead of silent lie).
# ─────────────────────────────────────────────────────────────


# Numeric token regex: integer / decimal / percent / 万 / 亿 / ‰ suffix.
# Captures the number group (g1) + optional unit group (g2).
_NUMBER_RE = re.compile(
    r"(-?\d+(?:[,，]\d{3})*(?:\.\d+)?)\s*([%‰]|百分点|万|亿|千)?",
)


def _norm_unit_value(num_str: str, unit: str | None) -> float | None:
    """Normalize a number+unit pair to a comparable float.

    - "1,234.5" → 1234.5
    - "12%" → 0.12 (percent → decimal)
    - "5 万" → 50000.0
    - "3 亿" → 300000000.0
    - "8 ‰" → 0.008
    - "0.5 百分点" → 0.005 (percent point)
    """
    if not num_str:
        return None
    cleaned = num_str.replace(",", "").replace("，", "")
    try:
        v = float(cleaned)
    except (TypeError, ValueError):
        return None
    if not unit:
        return v
    u = str(unit).strip()
    if u == "%":
        return v / 100.0
    if u == "‰":
        return v / 1000.0
    if u in ("万",):
        return v * 10_000.0
    if u in ("亿",):
        return v * 100_000_000.0
    if u == "千":
        return v * 1_000.0
    if u == "百分点":
        return v / 100.0
    return v


_RATIO_UNITS = ("%", "‰", "百分点")
# r8: absolute-quantity units, excluding ratios. Shares / growth can be computed immediately from two conversational figures
# (_extract_numbers_with_kind's docstring already protects derived ratios on this basis); absolute quantities cannot:
# either you have them or invent them. Deliberately exclude 元 / 个 (Eng-r3 P0-3: too common in everyday prose).
# ⚠ Exclude bare 千: everyday prose such as 「3 千米 / 5 千瓦 / 5 千个网点」 would count as business numbers.
# 千万 is different: since r9, the multi-character unit table below parses it correctly as 1e7.
# r9 (116 live round 7, t_7b1b7bc0fac8): Lead fabricated seventh place as 「银凤阁，约 **118 百万元**」;
# 百万 was absent from this table, so the gate missed it. 百万元 / ¥M are this product's own canonical notations
# (KPI cards and peers' exec_summary use them), not edge cases.
# ⇒ r8 uses its own longest-first multi-character unit scan; leave shared _NUMBER_RE unchanged, since payload guards and
# chat-number guards still use it. Changing it alters normalization in both (_NUMBER_RE consumes one unit character at a time,
# parsing 百万 as bare numbers, 5 千万 as 5000, and 47 万亿 as 47 万: all the same defect).
_R8_MAGNITUDE_SCALE = {"万亿": 1e12, "百万": 1e6, "千万": 1e7, "亿": 1e8, "万": 1e4}
_R8_MAGNITUDE_RE = re.compile(r"(-?\d+(?:[,，]\d{3})*(?:\.\d+)?)\s*(万亿|百万|千万|亿|万)")
# r10 (116 live round 8): ¥379.55M is the default canvas KPI-card notation; after a request 「金额用百万元（¥M）展示」,
# Lead used it throughout. This both missed detection (t_efde3ac984b3 invented 「银凤阁 ¥85.49M」 without rejection)
# and falsely rejected a faithful restatement (t_9160f02cec89: prior report ¥9,116.78M restated as 「91.17 亿元」);
# two sides of the same gap as 百万: unrecognized notation errs in both directions.
# ⚠ Require a currency-symbol prefix: bare 3M is a brand (3M company); 1.2M may be a version, causing many false positives.
_R8_CURRENCY_SUFFIX_SCALE = {"K": 1e3, "M": 1e6, "B": 1e9}
_R8_CURRENCY_SUFFIX_RE = re.compile(r"[¥￥]\s*(\d+(?:[,，]\d{3})*(?:\.\d+)?)\s*([KMB])(?![A-Za-z0-9])")
# ⚠ Pool side uses a separate regex without requiring currency: Lead writes ¥ once in lists, leaving later entries bare
# (observed text: 「金辉福以 148.3 百万元居首…；六福珠宝（**56.7M**）、阿迪达斯（**54.3M**）」;
# review measured 6% of K/M/B amounts without currency symbols in this sprint's dogfood corpus).
# Broader pools can only miss detection, not falsely accuse: this sourced-evidence set favors inclusion. Detection still requires currency,
# or bare 3M (company) / 1.2M (version) becomes money. Deliberate asymmetry; do not unify the two.
_R8_BARE_SUFFIX_RE = re.compile(r"(?<![A-Za-z0-9])(\d+(?:[,，]\d{3})*(?:\.\d+)?)\s*([KMB])(?![A-Za-z0-9])")
# Restatements round and use approximation (about / nearly / over): 91.17 亿 restated as 约 91 亿 is the same fact.
# Use the larger tolerance: ① 2% relative (decimal drift); ② written precision — 90 亿 is written to the billion place,
# claiming 9×10⁹ ± 5×10⁸; the pool's 91.17 亿 within that range is the same fact.
# ② Is strictly better than skipping insufficient significant digits: 华中 5000 万元 also has one significant digit but is far
# from every pooled number (nothing within ±500 万), so is still detected. The first version's fewer-than-two-digits skip wrongly exempted integer fabrication.
_UNSOURCED_REL_TOL = 0.02
# The character immediately after the unit determines money: 万 in 12 万行 / 25 万条 / 1.2 万字 is a counting unit.
# Check only the next character after whitespace: 元 ⇒ money; another Han character or English letter (12 万行 / 100 万 token)
# ⇒ counting something else, excluded; punctuation / end ⇒ treated as money.
# (Observed sentence 「营业额约 5010 万。」 lacks 元; do not require 元.)
# This is not a word list: allow only 元; all other letters/Han characters mean not money and are skipped, always reducing detection.
_CURRENCY_TAIL_CHAR = "元"


def _has_currency_prefix(text: str, start: int) -> bool:
    """``text[start]`` 这个数字前面（跳过空白）是不是货币符号。"""
    i = start - 1
    while i >= 0 and text[i].isspace():
        i -= 1
    return i >= 0 and text[i] in "¥￥"


def _r8_magnitude_figures(text: str, *, pool_side: bool = False) -> list[tuple[str, float, float, bool]]:
    """文本里所有「数字 + 量级单位」：返回 (写出来的数字串, 归一后的值, 单位倍率, 是不是钱)。

    与 `_extract_numbers_with_units` 分开的理由见 `_R8_MAGNITUDE_SCALE` 上方注释。
    ``pool_side=True`` 时额外收裸 `N[KMB]`（见 `_R8_BARE_SUFFIX_RE` 上方注释）——**只有证据池能这么放**。
    """
    out: list[tuple[str, float, float, bool]] = []
    for m in _R8_MAGNITUDE_RE.finditer(text):
        try:
            raw = float(m.group(1).replace(",", "").replace("，", ""))
        except ValueError:
            continue
        tail = text[m.end() :].lstrip()[:1]
        # Currency symbols override the trailing-character rule: 居 in ¥8.55 亿居首 otherwise means counting something else,
        # but the preceding ¥ unambiguously indicates money (observed text included ¥3.80 亿居首).
        is_money = _has_currency_prefix(text, m.start()) or not (
            tail and tail != _CURRENCY_TAIL_CHAR and (("\u4e00" <= tail <= "\u9fff") or (tail.isascii() and tail.isalpha()))
        )
        scale = _R8_MAGNITUDE_SCALE[m.group(2)]
        out.append((m.group(1), raw * scale, scale, is_money))
    # r10: ¥379.55M already establishes money via currency symbol; no need to check the next character.
    seen: set[tuple[int, int]] = set()
    for m in _R8_CURRENCY_SUFFIX_RE.finditer(text):
        try:
            raw = float(m.group(1).replace(",", "").replace("，", ""))
        except ValueError:
            continue
        seen.add(m.span(1))
        scale = _R8_CURRENCY_SUFFIX_SCALE[m.group(2)]
        out.append((m.group(1), raw * scale, scale, True))
    if pool_side:
        for m in _R8_BARE_SUFFIX_RE.finditer(text):
            if m.span(1) in seen:
                continue  # Already collected by the currency-prefixed regex above.
            try:
                raw = float(m.group(1).replace(",", "").replace("，", ""))
            except ValueError:
                continue
            out.append((m.group(1), raw * _R8_CURRENCY_SUFFIX_SCALE[m.group(2)], _R8_CURRENCY_SUFFIX_SCALE[m.group(2)], True))
    return out


def _extract_numbers_with_units(text: str) -> list[float]:
    """Regex-extract numeric tokens from arbitrary text + normalize.

    Returns deduped sorted list of normalized floats (for stable comparison
    in tests). Bad parses skipped silently.
    """
    return [v for v, _ in _extract_numbers_with_kind(text)]


def _extract_numbers_with_raw(text: str) -> list[tuple[float, str]]:
    """同 `_extract_numbers_with_units`，但保留**正文里的原始字面**（含单位）。

    为什么需要它：caveat 此前印的是归一化值（`13.8 亿` → `1,380,000,000`、`1%` → `0.01`），
    而用户在正文里看到的是原始写法。116 全库实测：122 个被点名的数字里 **90 个（73%）
    在正文里根本搜不到**——一条「这个数没有来源」的指控，指向的是用户看不见的内部表示，
    没法核对。判据不变，只把展示换成原文字面。
    """

    if not text or not isinstance(text, str):
        return []
    out: list[tuple[float, str]] = []
    for m in _NUMBER_RE.finditer(text):
        v = _norm_unit_value(m.group(1), m.group(2))
        if v is None:
            continue
        out.append((v, m.group(0).strip()))
    return out


def _extract_numbers_with_kind(text: str) -> list[tuple[float, bool]]:
    """Same extraction as ``_extract_numbers_with_units`` but keeps whether the
    token carried a **ratio** unit (``%`` / ``‰`` / 百分点).

    Why the distinction matters: a free-text figure the Lead *derived* in prose
    (a growth rate, a share) is overwhelmingly ratio-typed, while an absolute
    magnitude presented as the answer ("有 28,000 star") should be traceable to a
    tool result. ``is_chat_text_fabricated`` uses this to close the single-number
    blind spot without weakening the derived-ratio protection.
    """
    if not text or not isinstance(text, str):
        return []
    out: list[tuple[float, bool]] = []
    for m in _NUMBER_RE.finditer(text):
        v = _norm_unit_value(m.group(1), m.group(2))
        if v is None:
            continue
        out.append((v, (m.group(2) or "").strip() in _RATIO_UNITS))
    return out


def _walk_dict_strings(obj: Any) -> Iterable[str]:
    """Yield every string leaf encountered when walking obj (dict / list / scalar)."""
    if obj is None:
        return
    if isinstance(obj, str):
        yield obj
        return
    if isinstance(obj, (int, float)):
        yield str(obj)
        return
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_dict_strings(v)
        return
    if isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_dict_strings(v)
        return


def _written_precision(num_str: str) -> float:
    """写出来的这个数，最后一位有效数字落在哪个数位上（`90` → 10、`5010` → 10、`91.17` → 0.01、`1` → 1）。

    整数末尾的零按惯例当「没写到那一位」（`5000` → 1000），小数按写出来的位数算。
    r8 拿它定容差：一个数声称的精度就写在它自己脸上，`约 90 亿` 和 `91.17 亿` 是同一个事实，
    而 `5000 万` 虽然同样粗，离池子里任何数都远，仍然判得到。
    """
    s = num_str.replace(",", "").replace("，", "").lstrip("-").strip()
    if not s:
        return 1.0
    if "." in s:
        frac = s.split(".", 1)[1]
        return 10.0 ** (-len(frac)) if frac else 1.0
    stripped = s.rstrip("0")
    return 10.0 ** (len(s) - len(stripped))


def unsourced_absolute_figures(text: str, observed: set[float]) -> list[float]:
    """``text`` 里**带绝对量单位**（万 / 亿 / 百万 / 千万 / 万亿）却在 ``observed`` 里找不到出处的数字。

    「有出处」= 与池子里某个数的差 ≤ 容差，容差取 ``_UNSOURCED_REL_TOL`` 与**写出来的精度**两者较大者
    ——复述会四舍五入、会带「约 / 近 / 超过」，`约 90 亿` 与 `91.17 亿` 是同一个事实；
    而同样粗的 `5000 万` 若离池子里任何数都远，仍然判得到。
    r8 用它判「零工具轮报了对话里没有出处的业务数字」。为什么判据是「带单位的绝对量」而不是
    「所有数字」：真机 `t_8b05d38e92c7` 的答案是「银凤阁，约 1.63 亿元（据 2026 年…）」——
    年份 2026 在历史里匹配得上，按「全部未匹配」判就漏了；而按「所有数字都要有出处」判，
    日期、序号、条数会把正常回答全打成造假。带单位的绝对量恰好圈出「业务数字」这一类，
    与 `pipeline_invariants._INV8_NUMBER_RE`（`亿|万|%`）选的是同一类东西——那条判据对，只是从没接到任何动作上。
    ⚠ 仍判不到：英文散文（"about 163 million yuan"）与**裸**元数（`50,103,803`，没有量级单位也没有货币符号）。
    这是**已知缺口**、不是疏忽：`_INV8_NUMBER_RE` 同样如此，再扩要连带重估假阳，另开。
    （带货币符号的 `¥379.55M` r10 起认得——那是画布 KPI 卡的默认渲染，不认它两个方向都会错。）
    ⚠ 另一个已知假阳：常识数字（「中国人口约 14 亿」）没有出处也会被判。代价是多走一圈，
    模型第二遍会说明那是常识而非本工作区数据；BI 产品里这类句子少，暂不为它加语义判断。
    """
    if not text or not observed:
        return []
    out: list[float] = []
    _emitted: set[float] = set()
    for num_str, v, scale, is_money in _r8_magnitude_figures(text):
        if not is_money:
            continue  # Counts such as 12 万行 / 100 万 token are not monetary amounts.
        # Written precision uses normalized magnitude: 118 百万 has precision 1 百万; 3.80 亿 has precision 0.01 亿.
        tol = max(abs(v) * _UNSOURCED_REL_TOL, 0.5 * _written_precision(num_str) * scale)
        if any(abs(w - v) <= tol for w in observed):
            continue
        if v in _emitted:
            continue  # 2.11 亿元（¥211M） is one number in two notations; do not log it twice.
        _emitted.add(v)
        out.append(v)
    return out


def build_observed_numbers_from_texts(texts: Iterable[str]) -> set[float]:
    """从纯文本（turn 级历史的消息、本轮用户原话）收数字，单位归一同 `build_observed_numbers`。

    r8：**零工具轮**里终答数字唯一的合法来源就是这段对话本身——用户已经看见过的数字可以复述，
    没见过的就是无中生有（116 第六轮：零派活轮答「金辉福 5010 万」而真值 3.80 亿）。
    与判据侧 `unsourced_absolute_figures` **共用两套抽取**（`_extract_numbers_with_units` + r9 的
    `_r8_magnitude_figures`），两侧归一口径必须一致，否则「历史里的 379.6 百万」与「答案里的 3.80 亿」会对不上。
    ⚠ 因此本函数比 `build_observed_numbers`（工具结果侧，只走 `_extract_numbers_with_units`）多收一套多字单位值——
    那侧至今把「379.6 百万元」读成裸 379.6，是 `is_chat_text_fabricated` 的**已知缺口**（同一个根因的另一半，记为后续）。
    """
    observed: set[float] = set()
    for t in texts or []:
        if not isinstance(t, str) or not t:
            continue
        try:
            for v in _extract_numbers_with_units(t):
                observed.add(round(v, 6))
            # r9: _extract_numbers_with_units parses multi-character units (百万 / 千万 / 万亿) incorrectly;
            # normalization must match on both sides, so historical 379.55 百万元 matches answer 3.80 亿元.
            for _num, v, _scale, _money in _r8_magnitude_figures(t, pool_side=True):
                observed.add(round(v, 6))
        except Exception:  # noqa: S112
            continue
    return observed


def build_observed_numbers(tool_results: Iterable[Any]) -> set[float]:
    """Walk tool_results (heterogeneous shape: dicts, results, _ref_table, ...)
    and build a set of observed numeric values (unit-normalized).

    Caller passes the list of inv.result for the turn's tool_invocations.
    Failure modes (None / non-dict) silently skipped.
    """
    observed: set[float] = set()
    for r in tool_results or []:
        try:
            for s in _walk_dict_strings(r):
                # Plain numeric strings (e.g. "0.1234") + suffix-tagged.
                # Also: a bare float scalar would have been str()'d above.
                for v in _extract_numbers_with_units(s):
                    observed.add(round(v, 6))
        except Exception:  # noqa: S112
            continue
    return observed


def _payload_number_sources(
    payload: dict[str, Any],
) -> list[tuple[str, float, str]]:
    """Extract (location, normalized_value) pairs for KPI values + insight
    thesis/narrative numbers.

    Used by ``check_fabrication_back_reference`` to compare against observed.

    ⚠️ 位置标签用 ``#`` 而非 ``[i]`` —— 这些串会**逐字进入用户可见的 caveat 正文**，而前端
    markdown 路把 ``[N]`` 渲成可点的引用 chip（`lib/md.ts` 的 rehypeCardCitations；那里只给
    code/pre 做了豁免，普通散文照渲）。116 真机实测：``insights[1].thesis=-0.029`` 里的
    ``[1]`` 变成了指向「来源1」的 chip —— **一条「这些数字没有来源」的警告自己挂着假引用**。
    `validate_payload` 的报错串仍用 ``[i]``：那是给 LLM 看的 schema 错误，不经 markdown。
    """
    out: list[tuple[str, float, str]] = []
    if not isinstance(payload, dict):
        return out
    for i, k in enumerate(payload.get("kpis") or []):
        if not isinstance(k, dict):
            continue
        val = k.get("value")
        if not isinstance(val, str):
            continue
        for v, raw in _extract_numbers_with_raw(val):
            out.append((f"kpis#{i}.value", round(v, 6), raw))
    for i, ins in enumerate(payload.get("insights") or []):
        if not isinstance(ins, dict):
            continue
        for fld in ("thesis", "narrative"):
            txt = ins.get(fld)
            if not isinstance(txt, str):
                continue
            for v, raw in _extract_numbers_with_raw(txt):
                out.append((f"insights#{i}.{fld}", round(v, 6), raw))
    return out


#: Stable prefix for guard-generated caveats about numbers lacking tool support. Downstream uses it to distinguish machine judgments from the model's
#: own modeling limitations (see provenance_caveats). Changing the string silently breaks deterministic rendering,
#: so use a constant rather than scattered f-string literals.
FABRICATION_CAVEAT_PREFIX = "以下数值在工具结果中未找到对应来源, 可能为生成型估算: "
# chrome-i18n: guard accusations in English turns must also be English (dogfood t_ea634e084407: the only Chinese block
# left in an otherwise English report). The prefix is both a provenance_caveats recognition marker and persisted with payloads;
# filters must recognize both languages (old Chinese caveats persist indefinitely).
FABRICATION_CAVEAT_PREFIX_EN = "The following values could not be matched to any tool result and may be model-estimated: "
FABRICATION_CAVEAT_PREFIX_DE = (
    "Für die folgenden Werte wurde keine passende Quelle in den Tool-Ergebnissen gefunden; sie könnten vom Modell geschätzt worden sein: "
)
FABRICATION_CAVEAT_PREFIX_FR = "Les valeurs suivantes n’ont pu être rattachées à aucun résultat d’outil et peuvent être des estimations du modèle : "

# canvas-elements tech-design-document-minimalism §2.3 gap fix (2026-09-07): missing-plan-leg warnings also provide
# system-generated honesty information (lead_orchestrator._plan_report_gap_caveats, FIX-2 / 2026-06-25 production incident:
# a report appeared complete despite a failed leg). Previously mixed into payload["caveats"] without a marker,
# these vanished when returning model-authored limitations to body prose. Add a prefix so rendering distinguishes them.
# ⚠ Prefixes persist with payloads; recognition must tolerate old data without prefixes (historical sections only, not rerendered).
#
# ★ Machine-only prefix, removed by strip_caveat_marker during rendering. The missing-leg sentence itself
# (「「compute」这步执行失败，本报告未包含其分析结果。」) already explains the issue;
# prefixing 「报告缺口:」 repeats it. FABRICATION_CAVEAT_PREFIX differs:
# that prefix is itself an explanatory sentence (「以下数值在工具结果中未找到对应来源」), followed by numbers,
# so render it normally.
PLAN_GAP_CAVEAT_PREFIX = "[plan_gap] "


def check_fabrication_back_reference(
    payload: dict[str, Any],
    observed: set[float],
    tolerance: float = 0.02,
    lang: str = "en-US",
) -> list[str]:
    """Fuzzy-match payload numbers against observed; return caveat strings for
    likely-fabricated numbers.

    Tolerance is relative (default 2% — survives rounding in narrative).
    A number is "matched" if any observed value satisfies::

        abs(payload_v - observed_v) <= max(tolerance * abs(payload_v), 1e-6)

    Edge cases: payload_v == 0 → require exact 0 match.
    """
    if not isinstance(payload, dict):
        return []
    if not observed:
        # No tool_results → cannot diagnose fabrication. Return [] (caller
        # gets to decide whether to caveat the whole payload).
        return []
    caveats: list[str] = []
    fabricated_locs: list[str] = []
    for loc, pv, raw in _payload_number_sources(payload):
        # Always display the original body literal (raw): readers must be able to find the accused number in the body.
        # _render_caveat_number is only a fallback when raw is absent. Judge normalized values as before.
        shown = raw or _render_caveat_number(pv)
        if pv == 0.0:
            if 0.0 in observed:
                continue
            fabricated_locs.append(_humanize_fabrication_loc(loc, shown, lang))
            continue
        ok = False
        for ov in observed:
            tol = max(tolerance * abs(pv), 1e-6)
            if abs(pv - ov) <= tol:
                ok = True
                break
        if not ok:
            fabricated_locs.append(_humanize_fabrication_loc(loc, shown, lang))
    if fabricated_locs:
        # Cap to first 5 — caveat string for FE display, not full forensic.
        head = fabricated_locs[:5]
        if lang == "fr-FR":
            suffix = "" if len(fabricated_locs) <= 5 else f" (et {len(fabricated_locs) - 5} autres)"
            caveats.append(f"{FABRICATION_CAVEAT_PREFIX_FR}{', '.join(head)}{suffix}")
        elif lang == "en-US":
            suffix = "" if len(fabricated_locs) <= 5 else f" (and {len(fabricated_locs) - 5} more)"
            caveats.append(f"{FABRICATION_CAVEAT_PREFIX_EN}{', '.join(head)}{suffix}")
        elif lang == "de-DE":
            suffix = "" if len(fabricated_locs) <= 5 else f" (und {len(fabricated_locs) - 5} weitere)"
            caveats.append(f"{FABRICATION_CAVEAT_PREFIX_DE}{', '.join(head)}{suffix}")
        else:
            suffix = "" if len(fabricated_locs) <= 5 else f"（另有 {len(fabricated_locs) - 5} 处）"
            caveats.append(f"{FABRICATION_CAVEAT_PREFIX}{', '.join(head)}{suffix}")
    return caveats


def _render_caveat_number(value: float) -> str:
    """把 caveat 里要展示的数字渲成人能读的形式（**一律不用科学计数法**）。

    ⚠ feedback-triage-2026-08-26：116 真机报告底部的「数据可信度提示」原样印出
    ``4.28e+09``，而正文同一个数写的是「4.28 亿元」。病根是这里原来用 ``f"{pv:g}"``
    —— ``%g`` 一到 1e6 就切指数形式。这段 caveat **随 payload 落库、并原样印进导出的
    PDF**（见 `_humanize_fabrication_loc` 里那条同源教训），是客户可见文案不是日志。

    ⚠ 这只治「不可读」，**没治「对不上」**：``pv`` 是 `_extract_numbers_with_units`
    归一化后的值（「4.28 亿元」→ 4.28e9），所以渲出来仍不是正文里那个字面。要让用户
    照着找得到，得让抽取器把**原始匹配文本**一并带出来——那要改它的返回形状与全部
    调用点，属另一件事，已在飞书条目里记明。
    """
    if value != value or value in (float("inf"), float("-inf")):  # NaN / ±inf fallback
        return str(value)
    from decimal import Decimal

    rounded = round(value, 6)
    if rounded == 0 and value != 0:
        # ⚠ Must precede the integer branch: round(1e-8, 6) == 0.0 also satisfies
        # rounded == int(rounded); after it, this branch is unreachable and tiny values render as "0",
        # falsely reporting a different number. (Decimal(str(x)) with f format never uses exponents at any
        # magnitude, unlike guessing decimal places in an f-string.)
        text = format(Decimal(str(value)), "f")
    elif rounded == int(rounded) and abs(rounded) < 1e15:
        return f"{int(rounded):,}"
    else:
        text = f"{rounded:.6f}".rstrip("0").rstrip(".")
    head, _, tail = text.partition(".")
    neg = head.startswith("-")
    digits = head[1:] if neg else head
    head_fmt = f"{int(digits):,}" if digits else "0"
    return ("-" if neg else "") + head_fmt + (f".{tail}" if tail else "")


def _humanize_fabrication_loc(loc: str, rendered_value: str, lang: str = "en-US") -> str:
    """内部字段路径 → 用户可读位置（批0 0-C，chart-table-ux tech-design §1.3）。

    此前 caveat 逐字下发 ``insights#0.thesis=0.02`` 这类内部路径，真 prod 的
    报告正文和导出 PDF 都原样印给了客户（cv_55d680eda3f7「数据可信度提示」段
    实测）。位置改说人话；无法识别的 loc 兜底只显数值。
    """
    m = re.fullmatch(r"kpis#(\d+)\.value", loc)
    if m:
        if lang == "fr-FR":
            return f"{rendered_value} dans le KPI {int(m.group(1)) + 1}"
        if lang == "en-US":
            return f"{rendered_value} in KPI {int(m.group(1)) + 1}"
        if lang == "de-DE":
            return f"{rendered_value} im KPI {int(m.group(1)) + 1}"
        return f"KPI {int(m.group(1)) + 1} 的 {rendered_value}"
    m = re.fullmatch(r"insights#(\d+)\.(thesis|narrative)", loc)
    if m:
        if lang == "fr-FR":
            part = "conclusion" if m.group(2) == "thesis" else "texte"
            return f"{rendered_value} dans l’analyse {int(m.group(1)) + 1} ({part})"
        if lang == "en-US":
            part = "thesis" if m.group(2) == "thesis" else "narrative"
            return f"{rendered_value} in insight {int(m.group(1)) + 1} {part}"
        if lang == "de-DE":
            part = "These" if m.group(2) == "thesis" else "Fließtext"
            return f"{rendered_value} in Insight {int(m.group(1)) + 1} ({part})"
        part = "论点" if m.group(2) == "thesis" else "正文"
        return f"洞察 {int(m.group(1)) + 1} {part}中的 {rendered_value}"
    return rendered_value


def is_faithful_rounding(stated: float, authoritative: float) -> bool:
    """`stated` 是不是 `authoritative` 的一个**合法四舍五入形式**。

    为什么不用相对容差：容差是一刀切的窗口，而人/模型复述数字用的是**有效数字**或
    **小数位**。82.937 复述成 "83"(2sf) / "82.9"(3sf) / "82.94"(4sf) 都忠实；
    复述成 "83.885" 不是任何一档 —— 它是**改了数**，不是圆了数。一刀切容差分不开这两者：
    2% 会把 83.885 放行（差 1.14%），而收紧到 0.5% 又会把 "约2200"（2165.758 的 2sf）误伤。
    """
    if authoritative == 0.0:
        return stated == 0.0
    if math.isclose(stated, authoritative, rel_tol=1e-9):
        return True
    for sig in range(1, 7):
        try:
            candidate = float(f"%.{sig}g" % authoritative)
        except (ValueError, OverflowError):  # pragma: no cover - Extreme values
            break
        if math.isclose(stated, candidate, rel_tol=1e-9):
            return True
    return any(math.isclose(stated, round(authoritative, nd), rel_tol=1e-9) for nd in range(7))


# Private alias: existing callers/tests import the private name; promoted to a public primitive shared by number_verifier.
_is_faithful_rounding = is_faithful_rounding


def payload_authoritative_numbers(payload: Any) -> set[float]:
    """payload 里**报告实际展示**的那组数：KPI 值 + exec_summary。

    这是「Lead 复述时本该照抄的东西」。刻意不含 insights 正文——那本身也是散文，
    拿散文当基准会把两处笔误当成互相印证。
    """
    out: set[float] = set()
    if not isinstance(payload, dict):
        return out
    for k in payload.get("kpis") or []:
        if isinstance(k, dict) and isinstance(k.get("value"), str):
            out.update(_extract_numbers_with_units(k["value"]))
    es = payload.get("exec_summary")
    if isinstance(es, str):
        out.update(_extract_numbers_with_units(es))
    return out


def narrative_restatement_drift(
    text: str,
    authoritative: set[float],
    *,
    referent_window: float = 0.10,
) -> list[tuple[float, float]]:
    """Return stated and authoritative values for clear restatement drift.

    Require both a nearby authoritative referent within referent_window and a value
    that is not faithful rounding. Distant numbers belong to the separate fabrication
    guard. Prefer missing an uncertain case over suppressing valid user-visible prose.
    """
    if not text or not authoritative:
        return []
    drifted: list[tuple[float, float]] = []
    raw = re.sub(r"\[(?:cite|ref):[^\]]*\]", "", text)
    for v, _is_ratio in _extract_numbers_with_kind(raw):
        # Skip year-like integers (1900–2100): in this domain they almost always label periods rather than assert data
        # (same criterion as text_fabrication_detail).
        if 1900.0 <= v <= 2100.0 and float(v).is_integer():
            continue
        if v == 0.0:
            continue
        nearest = min(authoritative, key=lambda a: abs(v - a) / max(abs(a), 1e-9))
        if abs(v - nearest) / max(abs(nearest), 1e-9) > referent_window:
            continue
        if not is_faithful_rounding(v, nearest):
            drifted.append((v, nearest))
    return drifted


def narrative_restates_payload_incorrectly(
    text: str,
    payload: Any,
    *,
    min_drift: int = 2,
) -> bool:
    """Lead 的 narrative 是否把 peer payload 的数字复述错了。

    `min_drift=2`：单个数字落在窗口内又不是四舍五入，可能只是巧合撞上了别的量；
    两个以上才当作系统性复述失真。

    与 `is_chat_text_fabricated` 是**互补**关系，不是替代：
      - 那条管「凭空造数」（narrative 的数在工具结果里根本找不到来源，多数未匹配）
      - 这条管「照着抄却抄错」（有明确指涉对象，但抄歪了）—— 116 实测那一档，
        偏差 1.14%，一刀切 2% 容差放行、多数阈值又够不着，两道旧闸都拦不住。
    """
    return len(narrative_restatement_drift(text, payload_authoritative_numbers(payload))) >= min_drift


def provenance_caveats(payload: Any) -> list[str]:
    """Return machine-generated provenance caveats separately from model caveats.

    Render these findings deterministically rather than relying on a writer to repeat
    warnings about unsupported numbers in its own answer.
    """
    if not isinstance(payload, dict):
        return []
    return [
        c
        for c in (payload.get("caveats") or [])
        if isinstance(c, str)
        and c.startswith((FABRICATION_CAVEAT_PREFIX, FABRICATION_CAVEAT_PREFIX_EN, FABRICATION_CAVEAT_PREFIX_DE, FABRICATION_CAVEAT_PREFIX_FR))
    ]


def plan_gap_caveats(payload: Any) -> list[str]:
    """本 payload 里**系统生成的 plan 缺腿提示**（区别于模型自己写的建模局限）。

    与 `provenance_caveats` 同一类：机器判定、作者写不出来、关乎报告是否诚实。
    作者写正文时这些腿还没结束，它看不见——所以不能像普通局限那样「交回作者写进正文」。
    """
    if not isinstance(payload, dict):
        return []
    return [c for c in (payload.get("caveats") or []) if isinstance(c, str) and c.startswith(PLAN_GAP_CAVEAT_PREFIX)]


def append_system_caveat(payload: dict[str, Any], text: str) -> None:
    """**所有**由代码（而非模型）追加的 caveat 都走这里——它负责盖上机器标记。

    ★为什么必须收成一个入口（review 第 7 条）：渲染侧按「有没有标记」区分
    「作者写的（交回正文，不渲）」和「系统生成的（照渲）」。任何一处忘了盖标记，
    那条诚实性信息就静默掉进丢弃桶——peer 失败、台账对不齐、计划回退这三类
    都是这么丢的，而测试和类型都拦不住。守卫测试
    `test_no_bare_caveat_append.py` 扫源码钉住「不许绕过本函数」。

    幂等：已带标记的不重复盖；重复文案不重复追加（原地去重与调用点旧行为一致）。
    """
    if not isinstance(payload, dict) or not str(text or "").strip():
        return
    marked = text if text.startswith(PLAN_GAP_CAVEAT_PREFIX) else PLAN_GAP_CAVEAT_PREFIX + text
    bucket = payload.setdefault("caveats", [])
    if isinstance(bucket, list) and marked not in bucket:
        bucket.append(marked)


def strip_caveat_marker(caveat: str) -> str:
    """剥掉只给机器看的标记，返回给人读的那句话。非标记开头的原样返回。"""
    return caveat[len(PLAN_GAP_CAVEAT_PREFIX) :] if caveat.startswith(PLAN_GAP_CAVEAT_PREFIX) else caveat


def system_caveats(payload: Any) -> list[str]:
    """所有**系统生成**的 caveat = 守卫溯源指控 + plan 缺腿提示。渲染侧要保留的就是这些。"""
    return provenance_caveats(payload) + plan_gap_caveats(payload)


def author_caveats(payload: Any) -> list[str]:
    """作者自己写的建模局限——`system_caveats` 的补集。系统不再渲它，交回正文（§2.3）。"""
    if not isinstance(payload, dict):
        return []
    _sys = set(system_caveats(payload))
    return [c for c in (payload.get("caveats") or []) if isinstance(c, str) and c not in _sys]


def non_provenance_caveats(payload: Any) -> list[str]:
    """`provenance_caveats` 的补集 —— **模型自己写的**建模局限，通用 caveats 段该渲的就是这些。

    为什么要有这个函数（2026-08-25，116 `t_0dd71032d772` 复现）：守卫的判定被追加进
    `payload["caveats"]`，而亲笔路收尾器有**两个互不排斥的消费者**——通用 caveats 段把
    整份 caveats 渲一遍，`provenance_caveats()` 又按前缀把同一条挑出来渲成独立的
    「数据可信度提示」段。用户于是在一份报告里读到同一句话两遍。

    ⚠ **不要在调用方手写前缀匹配**：前缀同时是落库识别标记（见 `FABRICATION_CAVEAT_PREFIX`
    上方注释），分叉一份就会让识别静默失效。正反两个判据共用这里同一个元组，改前缀只改一处。

    ⚠ **也不要用「整块丢弃 caveats」去重**：通用段里还有写手自己写的真·局限性
    （「本报告基于 X 月单月数据，未包含环比/同比趋势」这类），必须一条不少地留着。
    """
    if not isinstance(payload, dict):
        return []
    _prov = set(provenance_caveats(payload))
    return [c for c in (payload.get("caveats") or []) if isinstance(c, str) and c not in _prov]


def text_fabrication_ratio(
    text: str,
    observed: set[float],
    tolerance: float = 0.02,
) -> tuple[int, int]:
    """Return (unmatched_count, total_count) of numbers in FREE TEXT vs observed.

    Sprint followup-context-deck (P1) — the chat-side analogue of
    ``check_fabrication_back_reference`` (which only inspects structured payload
    kpis/insights). Used to detect a Lead closing-narrative that invented numbers
    contradicting the peer's actual tool results. Same fuzzy match (2% relative).
    """
    # Strip citation/ref markers first. _resolve_citations keeps `[cite:N]`
    # literally (lead_orchestrator `_validate_cite` → "keep [cite:N] as-is"), so
    # the bare index N would be extracted as a number, never match `observed`,
    # and false-trip the guard on a legitimately-cited answer (e.g. two cites →
    # ratio 0.5). Drop [cite:N] / [ref:...] before number extraction.
    unmatched, total, _ = text_fabrication_detail(text, observed, tolerance)
    return (unmatched, total)


def text_fabrication_detail(
    text: str,
    observed: set[float],
    tolerance: float = 0.02,
) -> tuple[int, int, int]:
    """``text_fabrication_ratio`` + how many of the unmatched were **absolute**
    (non-ratio) magnitudes.

    Returns ``(unmatched, total, unmatched_absolute)``. See
    ``_extract_numbers_with_kind`` for why the ratio/absolute split matters.
    """
    raw = re.sub(r"\[(?:cite|ref):[^\]]*\]", "", text or "")
    nums = [
        (v, is_ratio)
        for v, is_ratio in _extract_numbers_with_kind(raw)
        # Skip year-like integers (1900–2100): in this BI domain they're almost
        # always periods/labels ("2025 vs 2026 同比"), not data claims, and would
        # otherwise inflate the unmatched count → false-positive fabrication.
        if not (1900.0 <= v <= 2100.0 and float(v).is_integer())
    ]
    if not nums:
        return (0, 0, 0)
    unmatched = 0
    unmatched_absolute = 0
    for v, is_ratio in nums:
        if v == 0.0:
            if 0.0 not in observed:
                unmatched += 1
                unmatched_absolute += 0 if is_ratio else 1
            continue
        tol = max(tolerance * abs(v), 1e-6)
        if not any(abs(v - ov) <= tol for ov in observed):
            unmatched += 1
            unmatched_absolute += 0 if is_ratio else 1
    return (unmatched, len(nums), unmatched_absolute)


def is_chat_text_fabricated(
    text: str,
    observed: set[float],
    *,
    min_unmatched: int = 2,
    min_ratio: float = 0.5,
) -> bool:
    """True when free text is likely fabricating numbers vs observed tool results.

    Conservative to avoid false positives on legitimately-derived figures (a sum
    the Lead computed in-prose): requires BOTH an absolute floor (``min_unmatched``)
    AND a majority (``min_ratio``) of the text's numbers to be unmatched. Returns
    False when there are no observed numbers (cannot diagnose) — never punish a
    turn we can't ground. In trace t_0e818cb5 all 6 chat numbers were unmatched
    (8.2/94.3/35/42/12.5/78 vs real 5.1/96.11/…) → True.

    **Single-absolute-number case** (2026-07-21, 116 E2E turn ``t_02d904aaaa38``):
    the ``min_unmatched=2`` floor made the guard structurally unable to fire on a
    one-number answer — exactly the shape of a simple factual question. That turn
    called its team tool successfully, got ``stargazers_count=138521``, wrote the
    correct figure into the canvas KPIs, and still told the user "约 28,000+ star"
    in chat; the guard stayed silent because the lie was a single number (1/1).
    So: also fire when **every** number in the text is unmatched AND at least one
    of them is an absolute magnitude (non-``%``/``‰``/百分点). A derived ratio
    stays protected by the original floor — only ungrounded absolutes trip this.
    """
    if not observed:
        return False
    unmatched, total, unmatched_absolute = text_fabrication_detail(text, observed)
    if total == 0:
        return False
    if unmatched >= min_unmatched and (unmatched / total) >= min_ratio:
        return True
    return unmatched == total and unmatched_absolute >= 1


def coerce_verdict_confidence(
    payload: dict[str, Any],
    observed: set[float],
) -> None:
    """Backend coerce ``payload.multi_source_compare.verdict.rows[].confidence``
    in-place based on source agreement / evidence count / caveats / observed
    membership.

    Rules:
      - row has explicit ``values: {src: v}`` mapping → compute max relative
        spread Δ across numeric values:
          * Δ ≤ 5%  → "high"
          * 5% < Δ ≤ 20% → "med"
          * Δ > 20%  → "low"
      - row has only one observed value or no numeric values → keep existing
        (no coerce).
      - If payload has ≥1 caveat entry → cap confidence at "med" (downgrade
        "high" → "med").
      - chat-v4 W4-fix CTO P0-2 — observed-membership cross-check:
          numeric_vals do NOT match any observed (±2%) → cap to "low".
          Defends against verdict rows where the LLM cited a number that
          never appeared in any tool_result.
      - chat-v4 W4-fix CTO P0-2 — evidence-count boost/cap:
          row.evidence_refs ≥ 3 → boost (med → high) — strong corroboration.
          row.evidence_refs == 1 → cap at "med" (single source isn't high).
    """
    if not isinstance(payload, dict):
        return
    msc = payload.get("multi_source_compare")
    if not isinstance(msc, dict):
        return
    verdict = msc.get("verdict")
    if not isinstance(verdict, dict):
        return
    rows = verdict.get("rows")
    if not isinstance(rows, list):
        return
    caveats_present = bool(payload.get("caveats"))
    for row in rows:
        if not isinstance(row, dict):
            continue
        # Find the corresponding compare row (by label) to inspect values.
        label = row.get("label")
        compare_row = None
        for r in msc.get("rows") or []:
            if isinstance(r, dict) and r.get("label") == label:
                compare_row = r
                break
        if compare_row is None:
            continue
        values = compare_row.get("values")
        if not isinstance(values, dict):
            continue
        numeric_vals: list[float] = []
        for v in values.values():
            if isinstance(v, (int, float)):
                numeric_vals.append(float(v))
            elif isinstance(v, str):
                for nv in _extract_numbers_with_units(v):
                    numeric_vals.append(nv)
                    break  # first per value-cell
        if len(numeric_vals) < 2:
            continue  # cannot compute agreement
        vmax = max(numeric_vals)
        vmin = min(numeric_vals)
        base = max(abs(vmax), abs(vmin), 1e-9)
        spread = abs(vmax - vmin) / base
        if spread <= 0.05:
            new_conf = "high"
        elif spread <= 0.20:
            new_conf = "med"
        else:
            new_conf = "low"
        # P0-2 cross-check (tightened r3 per CTO follow-up review): if observed
        # has values AND ANY of the row's numeric_vals is NOT within ±2% of any
        # observed → cap to "low". Tech-design semantic: "ANY mismatch → cap".
        # Prior "NONE match → cap" was permissive; one fabricated number among
        # several correct ones used to slip past.
        if observed and numeric_vals:

            def _matches_observed(n: float) -> bool:
                for o in observed:
                    denom = max(abs(o), abs(n), 1.0)
                    if abs(o - n) / denom <= 0.02:
                        return True
                return False

            if not all(_matches_observed(n) for n in numeric_vals):
                new_conf = "low"
        # P0-2 evidence-count boost/cap. row.evidence_refs is a list of
        # source/citation ids supporting this verdict row.
        ev_refs = row.get("evidence_refs")
        if isinstance(ev_refs, list):
            ev_count = len(ev_refs)
            if ev_count >= 3 and new_conf == "med":
                new_conf = "high"
            elif ev_count == 1 and new_conf == "high":
                new_conf = "med"
        # Cap at med when caveats present.
        if caveats_present and new_conf == "high":
            new_conf = "med"
        row["confidence"] = new_conf
