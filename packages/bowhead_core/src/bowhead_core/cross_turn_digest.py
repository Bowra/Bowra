"""Cross-turn digest — compact per-turn summary for the NEXT turn's Lead context.

Build a small, modern summary from the two clean sources available
at the Lead done-emit (`lead_orchestrator._lead_loop`): the user's text + the
merged peer `aggregated_payload` (the same structured AnalysisPayload that feeds
`lead.final.payload` / the canvas report). The dict this produces is persisted
to `turns.digest_json` via `session_store.save_digest`, and read back next turn
by `list_recent_digests` → rendered into the Lead system prompt (read side).

Shape is a plain JSON dict (list_recent_digests json.loads it). The read-side
renderer (`render_recent_turns_block`) is the only consumer and agrees on this
shape — there is no `mode`/`explanations_struct` legacy field, by design.
"""

from __future__ import annotations

import re
from typing import Any

# Caps — keep each digest small (read side packs ~3 of these into the Lead
# system prompt under a 6500-tok budget). See spike-p0a.md facet 1.
_MAX_USER_TEXT = 240
_MAX_CONCLUSION = 280
_MAX_FINAL_ANSWER = 600
_MAX_KEY_FACTS = 6
_MAX_INSIGHTS = 4


# Internal LLM markers that are turn-scoped and must NOT leak into the digest
# (which is injected into the NEXT turn's prompt): `[cite:N]` / `[ref:...]`
# resolve against THIS turn's registry → dangling next turn; `[num:val:unit]`
# is a verify/format tag. Strip the tag, keep any surrounding plain text.
_MARKER_RE = re.compile(r"\[(?:cite|ref|num):[^\]]*\]")


def _strip_markers(s: Any) -> Any:
    if not isinstance(s, str):
        return s
    s = _MARKER_RE.sub("", s)
    s = re.sub(r"[ \t]{2,}", " ", s)  # collapse gaps left by removed tags
    s = re.sub(r"\s+([，。；、）)】」])", r"\1", s)  # drop space before CJK/closing punct
    return s


def _clip(s: Any, n: int) -> str:
    # NOT `s or ""` — a literal 0 / 0.0 KPI value is falsy but meaningful
    # ("缺货次数=0"); only None means absent.
    t = "" if s is None else s
    if not isinstance(t, str):
        t = str(t)
    t = t.strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _first_sentence(text: str) -> str:
    """First sentence-ish chunk of free prose, for a one-line conclusion."""
    t = (text or "").strip()
    if not t:
        return ""
    for sep in ("。", "\n", ". ", "！", "!"):  # noqa: RUF001 — matching real CJK prose
        idx = t.find(sep)
        if 0 <= idx <= 160:
            return t[: idx + (0 if sep in ("\n",) else len(sep))].strip()
    return t[:160].strip()


def build_cross_turn_summary(
    *,
    turn_id: str,
    user_text: str,
    intent: str | None,
    final_answer: str,
    aggregated_payload: dict[str, Any] | None = None,
    canvas_id: str | None = None,
    tool_invocations: list[Any] | None = None,
) -> dict[str, Any]:
    """Build the compact cross-turn summary dict persisted to digest_json.

    Pure function (no I/O, no raise on dirty input). Caller wraps the
    save in try/except; this never throws on missing/odd fields.
    """
    payload = aggregated_payload if isinstance(aggregated_payload, dict) else {}
    # Strip turn-scoped LLM markers up front so none reach the next turn's prompt.
    final_answer = _strip_markers(final_answer or "")

    # conclusion: prefer the structured exec_summary (the real analytical
    # thesis); fall back to the first sentence of the chat answer.
    exec_summary = _strip_markers(payload.get("exec_summary") or payload.get("title") or "")
    conclusion = _clip(exec_summary or _first_sentence(final_answer), _MAX_CONCLUSION)

    key_facts: list[dict[str, Any]] = []
    for kpi in (payload.get("kpis") or [])[:_MAX_KEY_FACTS]:
        if not isinstance(kpi, dict):
            continue
        lbl = kpi.get("label")
        if not lbl:
            continue
        key_facts.append(
            {
                "label": _clip(_strip_markers(lbl), 60),
                "value": _clip(_strip_markers(kpi.get("value")), 40),
                "delta": _clip(_strip_markers(kpi.get("delta")), 24) if kpi.get("delta") else None,
            }
        )

    insights: list[str] = []
    for ins in (payload.get("insights") or [])[:_MAX_INSIGHTS]:
        if isinstance(ins, dict) and ins.get("title"):
            insights.append(_clip(_strip_markers(ins.get("title")), 80))

    summary: dict[str, Any] = {
        "turn_id": turn_id,
        "user_text": _clip(user_text, _MAX_USER_TEXT),
        "intent": intent or None,
        "conclusion": conclusion,
        "final_answer_full": _clip(final_answer, _MAX_FINAL_ANSWER),
    }
    if key_facts:
        summary["key_facts"] = key_facts
    if insights:
        summary["insights"] = insights
    if canvas_id:
        summary["canvas_id"] = canvas_id
    srcs = payload.get("sources") or []
    if srcs:
        summary["produced"] = {
            "kind": "report" if payload.get("kpis") or payload.get("insights") else "answer",
            "sources_count": len(srcs),
        }
    # D1: how this turn was answered — open world (web/external) vs closed (the connected
    # dataset/KB) — so a topical follow-up next turn continues the same world instead of
    # defaulting to bi_query just because a BI dataset is connected.
    if tool_invocations is not None:
        from bowhead_core.tool_category import classify_turn_source

        summary["answered_via"] = classify_turn_source(tool_invocations)
    return summary


# ── read side (P0-A line 2): render prior-turn digests into Lead prompt ──────

# Hard backstop on the rendered block size (chars). The per-digest fields are
# already tightly capped on write, so ~3 digests land well under this; the cap
# only guards against an unexpectedly fat payload. CJK-heavy, so ~3 chars/token
# → this is roughly ≤1k tokens, acceptable on the (non-enforced-live) 6500 cap.
_MAX_BLOCK_CHARS = 3000


# Extract rule wording as a constant: digest block (flag OFF) and batch 3 rule block (flag ON) share identical bytes.
_TOPIC_CONTINUITY_RULE = (
    '**Topic continuity**: if this turn the user gives only an **action, no subject** (e.g. "search the web" 「联网搜一下」, "look again" 「再查查」, '
    '"change the chart" 「换个图」, "what about X" 「那 X 呢」, "go deeper" 「细化一下」, "use another dimension" 「换个维度」), and the previous turn had a clear topic → **by default carry '
    'the previous turn\'s topic** into that action; **do not ask back "search what / look up what"**. Even if the previous turn was a refusal or had no data '
    '(e.g. "weather is not in the BI dataset"), a following "search the web" means searching **that topic** (the weather) on the web, '
    'not a new topic. **But** if the user **explicitly changes the subject** (e.g. "look up something **else**" 「查点**别的**」, "forget this, look at X" 「不看这个了、看 X」, '
    '"different question" 「换个问题」), or the previous turn **had several topics and you cannot tell which one the follow-up means** → treat it as a **new topic** or clarify '
    'as usual; do not force the previous topic onto it. If the previous turn was **cancelled / failed** (its answer in the conversation is a marker like "[本轮被用户中止… / cancelled by the user …]") '
    "→ that turn was **not fully delivered** (how far it got is whatever that turn's spawn results and the current canvas show); if the user then changes the request "
    '(e.g. "make it Q2" 「改成第二季度」), they want you to **finish that job under the new request** — do not ask "should I redo it", do not repeat the marker as an answer, '
    'and do not say "this is a new session".'
)
_SOURCE_CONTINUITY_RULE = (
    '**Source continuity**: if this turn follows up the previous turn\'s topic (e.g. "by quarter" 「分季度」, "more detail" 「再细点」, "another dimension" 「换个维度」, '
    '"what about X" 「那 X 呢」), and the previous turn\'s "source" was **web / external knowledge** (the topic is not in this dataset) → **keep using the web / '
    "answer directly; do not switch to bi_query just because a BI dataset is connected**, unless the user explicitly switches to a metric that is in the dataset."
)
_SECTION_REVISE_HINT = (
    "- **Follow-up mode** (every spawn MUST pass `canvas_mode`, one mode per turn): the user adds just one line narrowing the scope / changing numbers / changing wording "
    '(e.g. "Jan-Jun 2026" 「2026年1-6月」, "switch to a first-half basis" 「改成上半年口径」, "tighten the wording of chapter 2" 「第二章措辞收一下」) → `revise` '
    "(revise sections of the same report in place; both recomputed numbers and wording changes go to compute, which can refetch data and edit sections with write_report); "
    "the user wants an extra section → `append`; the user explicitly wants a separate report or a new topic → `new_report`."
)
_SOURCE_LABELS = {
    "web": "web / external knowledge (open world, not this dataset)",
    "external": "external tool (open world, not this dataset)",
    "dataset": "the Datarc workspace dataset (BI)",
    "kb": "knowledge base (KB)",
}


def _canvas_line(cid: Any) -> str:
    return f"- Output: the report is already on the canvas (canvas {cid}). To build a PPT / deck / chart from it, reuse this analysis directly; do not rerun the data."


def _turn_label(d: dict[str, Any]) -> str:
    """用用户原话前缀给一轮起名。review 修：「第 i 轮」是按最近 3 条 digest 编号的，而模型看到的是
    messages 里全部历史，两个序号对不上；按原话前缀点名，模型能在上文里找到那一轮。"""
    ut = (d.get("user_text") or "").strip().replace("\n", " ")
    return f'the turn "{ut[:24]}{"…" if len(ut) > 24 else ""}"' if ut else "an earlier turn"


def render_history_rules_block(
    digests: list[dict[str, Any]], *, source_continuity: bool = False, section_revise: bool = False, force: bool = False
) -> str:
    """批 3（`BOWHEAD_CTX_TURN_HISTORY=1`）：digest 块拆三样后，**只渲规则与每轮元数据**、不渲对话内容。

    对话内容（用户原话 + 回答全文）由 turn 级历史挂在 messages 里；这里剩下的是原文里没有的东西：
    话题连续性 / 来源连续性规则、每轮的「来源」、报告轮的结论（review 修：来自报告 payload，不是聊天回答的投影；
    r4 起关键数据 / 洞察不再进这里——它们是编数的原料，历史里的终答与 spawn 摘要已经带着）、
    「产出已在画布」、续轮档位提示（条件与 `render_recent_turns_block` 完全相同）。进 per-turn 尾块，不进 system。
    没有任何一条要说时返回空串；``force=True``（r7：历史里有未完成轮而没有 digest）时只渲规则不渲每轮事实。
    """
    rows = [d for d in (digests or []) if isinstance(d, dict)]
    if not rows and not force:
        return ""
    lines = [
        "## Follow-up rules for this session",
        "When answering a follow-up: **do not clarify what is already known**; when the user wants an existing analysis in another form (PPT / deck / add a chart / rewrite), **reuse that turn's output from the conversation above; do not fetch the data again**.",
        _TOPIC_CONTINUITY_RULE,
    ]
    if source_continuity and any(isinstance(d.get("answered_via"), dict) and d["answered_via"].get("open_world") for d in rows):
        lines.append(_SOURCE_CONTINUITY_RULE)
    for d in rows:
        facts: list[str] = []
        av = d.get("answered_via")
        if source_continuity and isinstance(av, dict):
            label = _SOURCE_LABELS.get(av.get("source"))
            if label:
                facts.append(f"source: {label}")
        concl = (d.get("conclusion") or "").strip()
        ans = (d.get("final_answer_full") or "").strip()
        if concl and concl not in ans:
            facts.append(f"conclusion: {concl}")
        # r4: key data / insights **no longer enter the tail block**. 116 R6 evidence: previous numbers immediately before task + matching history phrasing =
        # material + template; Lead copied them as "recalculated" without delegating. Numbers remain in historical final answers / spawn summaries (with sources and locations).
        if facts:
            lines.append(f"- {_turn_label(d)}: " + " · ".join(facts))
    last_canvas = next((d.get("canvas_id") for d in reversed(rows) if d.get("canvas_id")), None)
    if last_canvas:
        lines.append(_canvas_line(last_canvas))
    if section_revise:
        lines.append(_SECTION_REVISE_HINT)
    return "\n".join(lines)


def render_recent_turns_block(digests: list[dict[str, Any]], *, source_continuity: bool = False, section_revise: bool = False) -> str:
    """Render prior-turn digests (oldest→newest, as list_recent_digests returns)
    into a compact Lead system-prompt section. Empty string when no digests.

    Tells the Lead what already happened this session so it (a) doesn't re-ask
    what's already known and (b) reuses a prior report when asked to reformat it
    (e.g. "做个 ppt") instead of rebuilding from scratch. Pure/no-throw.
    """
    rows = [d for d in (digests or []) if isinstance(d, dict)]
    if not rows:
        return ""

    def _render(items: list[dict[str, Any]]) -> str:
        lines = [
            "",
            "## Recent turns in this session (oldest first, the last one is the latest)",
            "When answering a follow-up: **do not clarify what is already known**; when the user wants an existing analysis in another form (PPT / deck / add a chart / rewrite), **reuse the output of the turn below; do not fetch the data again**.",
            # cross-turn TOPIC-continuity (UNGATED — fires for any prior turn, incl. a refusal
            # / no-tool turn): a bare-action follow-up inherits the prior turn's SUBJECT.
            # Without this the Lead re-asks "搜什么?" on "你联网搜索一下" after a weather refusal
            # (prod s_c6887db3ad) — the lead model (deepseek-v4-pro) anchors on the refusal text
            # and re-clarifies 11/11. Distinct from, and complementary to, the open_world-gated
            # source-continuity clause below (which continues a SOURCE, not a topic). Dogfood: this line
            # flips deepseek 0/11 → 6/6 correct (qwen 5/6) on the prod-failing turn. Carve-out
            # (review-fix): a bare-action that SWITCHES topic ("查点别的") or an ambiguous multi-topic
            # prior turn must NOT be force-continued — so "不要反问" is not absolute.
            _TOPIC_CONTINUITY_RULE,
            "",
        ]
        # D1: source-continuity rule, only when a recent turn answered from the OPEN world
        # (web/external). Coheres with the lead.py decision tree so the two prompt sections
        # agree rather than contend (the tree's time-keyword→BI bias would otherwise win).
        if source_continuity and any(isinstance(d.get("answered_via"), dict) and d["answered_via"].get("open_world") for d in items):
            lines.append(_SOURCE_CONTINUITY_RULE)
            lines.append("")
        for i, d in enumerate(items, 1):
            ut = (d.get("user_text") or "").strip()
            lines.append(f"### Turn {i} · user: {ut}")
            # Render the assistant's ACTUAL reply, not just the first-sentence
            # `conclusion`. `conclusion` = `_first_sentence(final_answer)` for a
            # conversational turn, which silently drops a prose ask-back (e.g.
            # "我需要知道你在哪个城市…等你确认城市再查") — so the NEXT turn loses the
            # dialogue state and misreads a short reply ("北京") as a fresh query
            # (prod s_ae271982f3 seq3). `final_answer_full` (already ≤600 chars on
            # write) holds the full reply; fall back to `conclusion` if absent.
            ans = (d.get("final_answer_full") or "").strip()
            concl = (d.get("conclusion") or "").strip()
            if ans:
                lines.append(f"- answer: {ans}")
                # BI turns: `conclusion` is the exec_summary thesis, which may
                # carry analytical framing not echoed in the chat reply. Keep it
                # only when it isn't already contained in the rendered answer.
                if concl and concl not in ans:
                    lines.append(f"- conclusion: {concl}")
            elif concl:
                lines.append(f"- conclusion: {concl}")
            av = d.get("answered_via")
            if source_continuity and isinstance(av, dict):
                _src_label = _SOURCE_LABELS.get(av.get("source"))
                if _src_label:
                    lines.append(f"- source: {_src_label}")
            kf = [k for k in (d.get("key_facts") or []) if isinstance(k, dict)][:4]
            if kf:
                facts = "; ".join(f"{k.get('label')}={k.get('value')}" + (f"({k.get('delta')})" if k.get("delta") else "") for k in kf)
                lines.append(f"- key data: {facts}")
            ins = [s for s in (d.get("insights") or []) if isinstance(s, str)][:3]
            if ins:
                lines.append(f"- insights: {'; '.join(ins)}")
            cid = d.get("canvas_id")
            if cid:
                lines.append(_canvas_line(cid))
        if section_revise:
            # canvas-update-turn batch B0: if the canvas already has a report, express follow-up intent in spawn canvas_mode.
            # Review fix: mention it once in the block; caller passes `section_revise` when "flag on AND canvas has a previously visible report"
            # (canvas_id exists after just one chart; checking it alone calls chat+chart sessions "existing reports",
            # making Lead choose revise for a first report without a prior report).
            lines.append("")
            lines.append(_SECTION_REVISE_HINT)
        return "\n".join(lines)

    # Defensive: if the block is unexpectedly large, drop oldest turns until it fits.
    while rows:
        block = _render(rows)
        if len(block) <= _MAX_BLOCK_CHARS or len(rows) == 1:
            if len(block) > _MAX_BLOCK_CHARS:
                block = block[:_MAX_BLOCK_CHARS] + "\n…"
            return block
        rows = rows[1:]  # drop oldest
    return ""
