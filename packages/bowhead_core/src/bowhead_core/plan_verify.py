"""Plan step verifier — 对照式核验（graph-mode 批 1, tech-design §1.3）.

职责边界:adapter 三查=传输诚实(步有没有真产出);本模块=计划层判定(产出
符不符合批准过的验收标准)。两层不混。

设计要点(全部来自 design-review-2026-08-04 裁决):
- **四值判定**(A4 + 2026-08-16 enforce 标定): LLM 逐条出
  met/unmet/cannot_tell/not_applicable;verdict 由代码归约。**fail 要求每条判据
  都判过且都不达**(`unmet == len(items)`)——fail 是唯一会屏蔽下游的判决,掺一条
  cannot_tell 就不算干净失败。全 not_applicable=域外拒答,不是失败。
- **专用证据向 digest**(A1, P0): 不复用 agent_runner_adapter 的 writer 向瘦身
  (那套保结论删证据,与核验互斥)。白名单=kpis/ground_facts/sources/insights
  narrative/caveats/fallback_markdown/结构计数;排除 chart spec 本体与裸 section id。
- **thin-path 回填**(A3): insight_section_ids 形态的实质内容在 canvas section,
  caller(orchestrator hook 闭包)负责拉 section 文本经 ``extra_sections`` 传入。
- **fail-open**: criteria 空/LLM 异常/超时/输出不可解析 → skipped——验证器
  故障不得杀掉真实产出;skipped 必须与 pass 可分(A8, 覆盖率信号)。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anyio

from bowhead_router import LLMRequest, Message

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bowhead_router import Router

logger = logging.getLogger(__name__)

# Four item values.
ITEM_MET = "met"
ITEM_UNMET = "unmet"
ITEM_CANNOT_TELL = "cannot_tell"
# ★2026-08-16 enforce precondition: **not achieving something differs from honestly stating it cannot be done.**
# Actual 116 calibration false negative: `coldchain-temp-audit` output was an exemplary refusal
# ("当前工作区…**没有冷链温控相关的数据字段**" + missing-data list + suggested next steps),
# all three criteria unmet → verdict=fail. Enforce would mark FAILED and isolate downstream,
# while user sees acceptance unmet, hiding real reason. **Clearest explanation was treated as failure.**
ITEM_NOT_APPLICABLE = "not_applicable"
_ITEM_STATUSES = frozenset({ITEM_MET, ITEM_UNMET, ITEM_CANNOT_TELL, ITEM_NOT_APPLICABLE})

# Five verdict values.
VERDICT_PASS = "pass"
VERDICT_PARTIAL = "partial"
VERDICT_FAIL = "fail"
VERDICT_SKIPPED = "skipped"
# Output honestly states this space lacks domain data: honest disclosure rather than failure.
# Driver enforce branch recognizes only `VERDICT_FAIL`, so this value cannot be rewritten to criteria_unmet.
VERDICT_OUT_OF_DOMAIN = "out_of_domain"

_DIGEST_BUDGET_CHARS = 6000
_FALLBACK_MD_CAP = 3000
_EXTRA_SECTION_CAP = 400
_NOTE_CAP = 80


@dataclass(frozen=True)
class VerifyItem:
    text: str
    status: str  # met | unmet | cannot_tell | not_applicable
    note: str = ""  # One-sentence evidence for unmet.


@dataclass(frozen=True)
class VerifyOutcome:
    verdict: str  # pass | partial | fail | skipped | out_of_domain
    items: tuple[VerifyItem, ...] = ()
    note: str = ""  # E.g. reason for skipped.

    def to_json(self) -> str:
        return json.dumps(
            {
                "verdict": self.verdict,
                "items": [{"text": i.text, "status": i.status, "note": i.note} for i in self.items],
                "note": self.note,
            },
            ensure_ascii=False,
        )

    def unmet_texts(self) -> tuple[str, ...]:
        return tuple(i.text for i in self.items if i.status == ITEM_UNMET)


def outcome_from_json(raw: str | None) -> VerifyOutcome | None:
    """verify_json 列 → VerifyOutcome(坏值 → None)。caveats/FE 消费用."""
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    items = []
    for it in d.get("items") or []:
        if isinstance(it, dict) and it.get("text"):
            status = str(it.get("status") or ITEM_CANNOT_TELL)
            items.append(
                VerifyItem(
                    text=str(it["text"]),
                    status=status if status in _ITEM_STATUSES else ITEM_CANNOT_TELL,
                    note=str(it.get("note") or ""),
                )
            )
    verdict = str(d.get("verdict") or VERDICT_SKIPPED)
    return VerifyOutcome(verdict=verdict, items=tuple(items), note=str(d.get("note") or ""))


def reduce_verdict(items: Sequence[VerifyItem]) -> str:
    """归约规则(代码定, 不交给 LLM): 全 met=pass;部分 met=partial;
    **全部判据都判了且全 unmet=fail**;全 not_applicable=out_of_domain;
    其余(unmet 掺着 cannot_tell/N-A、全 cannot_tell、空)=partial / skipped.

    `not_applicable` 只在**没有任何 met/unmet** 时才独自成一档：产出如实说明
    「本工作区没有这个领域的数据」时，每一条判据都无从谈起，这才是域外拒答的形状。
    若同时还有 met/unmet，说明它其实做了一部分——按老规则走，别让一个 N/A 项把
    真实的缺口洗白（否则这道豁免就成了逃生口）。

    ★fail 要求 `unmet == len(items)`：fail 是唯一会在 enforce 下**屏蔽下游**的判决,
    它的含义必须是「每一条判据都判过、且都没达到」。只要还有一条 cannot_tell
    (证据面看不到, 不代表没做), 这一步就不构成干净的失败 → partial, 下游照常拿它。
    真机语料上这条正是一次真实回归的根因: [unmet, cannot_tell] 曾被归约成 fail。
    """
    met = sum(1 for i in items if i.status == ITEM_MET)
    unmet = sum(1 for i in items if i.status == ITEM_UNMET)
    na = sum(1 for i in items if i.status == ITEM_NOT_APPLICABLE)
    if met and not unmet and met == len(items):
        return VERDICT_PASS
    if met:
        return VERDICT_PARTIAL
    if unmet:
        return VERDICT_FAIL if unmet == len(items) else VERDICT_PARTIAL
    if na:
        return VERDICT_OUT_OF_DOMAIN
    return VERDICT_SKIPPED


# ── Evidence-oriented digest ───────────────────────────────────


def _kpi_row(k: Any) -> str:
    if not isinstance(k, dict):
        return ""
    parts = [str(k.get("label") or ""), str(k.get("value") or "")]
    if k.get("delta"):
        parts.append(str(k["delta"]))
    return " ".join(p for p in parts if p)


def _source_row(s: Any) -> str:
    if not isinstance(s, dict):
        return str(s)[:80]
    return " ".join(str(s.get(key) or "") for key in ("name", "label", "kind", "tool") if s.get(key))


def build_verify_digest(
    payload: dict[str, Any],
    *,
    extra_sections: Sequence[str] = (),
    input_manifest: dict[str, Any] | None = None,
    budget_chars: int = _DIGEST_BUDGET_CHARS,
) -> str:
    """payload → 证据向摘要(白名单键表, 与 writer 向 _slim_payload 互斥的取向).

    extra_sections: thin-path 时 caller 拉的 canvas section 文本(已各自截断)。
    input_manifest: 批3 D-E——本步实收上游清单;degraded 时 verifier 能把「上游
    缺腿导致内容缺」判成有据可依而非 cannot_tell。体积近零, 恒保留(不进预算
    drop 序, review R1-F10)。
    """
    insights = payload.get("insights") or []
    ground_facts = payload.get("ground_facts") or []
    # Context-engineering sprint batch 1-c: **remove all field-level truncation** (retain list-count caps).
    #
    # Verifier judges whether this output suffices and meets criteria. Giving it field-by-field truncated
    # fragments effectively cuts the criteria themselves. Not speculation: original `_NARRATIVE_CAP` comment recorded
    # 200-character truncation hid later inline citations, **inevitably false-negative** for source-citation criteria;
    # the fix then raised cap to 400 only for the no-sources shape, rather than removing truncation.
    #
    # Size is bounded by **existing overall budget below** (`budget_chars`, default 6000): it drops
    # whole fields in importance order and records `_truncated_keys`, telling verifier it sees a partial view.
    # That approach is sound; silent field-level cuts leave verifier unaware the missing half ever existed.
    d: dict[str, Any] = {
        "title": str(payload.get("title") or ""),
        "exec_summary": str(payload.get("exec_summary") or ""),
        "kpis": [_kpi_row(k) for k in (payload.get("kpis") or [])[:16] if _kpi_row(k)],
        "ground_facts": [str(g) for g in ground_facts[:16]],
        "sources": [_source_row(s) for s in (payload.get("sources") or [])[:16] if _source_row(s)],
        "insights": [
            {
                "title": str(i.get("title") or ""),
                "thesis": str(i.get("thesis") or ""),
                "narrative": str(i.get("narrative") or ""),
            }
            for i in insights[:10]
            if isinstance(i, dict)
        ],
        "caveats": [str(c) for c in (payload.get("caveats") or [])[:8]],
        "counts": {
            "kpis": len(payload.get("kpis") or []),
            "insights": len(insights),
            "canvas_sections": len(payload.get("insight_section_ids") or []),
            "recommendations": len(payload.get("recommendations") or []),
        },
    }
    fb = payload.get("fallback_markdown")
    if isinstance(fb, str) and fb.strip():
        d["fallback_markdown"] = fb[:_FALLBACK_MD_CAP]
    if extra_sections:
        d["canvas_sections_text"] = [str(s)[:_EXTRA_SECTION_CAP] for s in extra_sections[:12]]
        d["_note"] = "canvas_sections_text 为部分视图(thin-path 回填)"
    if input_manifest:
        _exp = input_manifest.get("expected") or []
        _rec = input_manifest.get("received") or []
        _deg = input_manifest.get("degraded_inputs") or []
        if _exp:
            _mi: dict[str, Any] = {"expected_upstream": len(_exp), "received_upstream": len(_rec)}
            if _deg:
                _mi["missing_failed_upstream"] = [str(x)[:60] for x in _deg[:8]]
                _mi["_note"] = "该步执行时部分上游未成功, 对应维度缺失属输入缺口而非本步失职"
            d["input_manifest"] = _mi
    # Remove empties, then fit budget: drop long text first; if still over, minimal core (implementation review B6:
    # raw truncation feeds LLM broken JSON; reading half a digest as complete loses coverage / causes misjudgment).
    d = {k: v for k, v in d.items() if v}
    out = json.dumps(d, ensure_ascii=False)
    if len(out) > budget_chars:
        for key in ("fallback_markdown", "canvas_sections_text", "insights", "exec_summary"):
            if key in d:
                d.pop(key)
                d.setdefault("_truncated_keys", []).append(key)  # type: ignore[union-attr]
                out = json.dumps(d, ensure_ascii=False)
                if len(out) <= budget_chars:
                    break
    if len(out) > budget_chars:
        core = {
            "title": d.get("title", ""),
            "kpis": (d.get("kpis") or [])[:8],
            "ground_facts": (d.get("ground_facts") or [])[:8],
            "sources": (d.get("sources") or [])[:8],
            "counts": d.get("counts", {}),
            # Batch 3 R1-F10: manifest always in minimal core; fuller payload (most prone to cannot_tell)
            # makes evidence of missing upstream legs even more indispensable.
            "input_manifest": d.get("input_manifest"),
            "_truncated": True,
        }
        out = json.dumps({k: v for k, v in core.items() if v not in ("", [], {}, None)}, ensure_ascii=False)
    return out


_SECTION_TEXT_KEYS = ("title", "eyebrow", "thesis", "narrative", "text", "markdown", "caption", "summary", "body")


def section_payload_text(payload: dict[str, Any], *, cap: int = 400) -> str:
    """canvas section payload → 可核对文本(thin-path 回填用, review A3).

    section kind 各异, 取常见文本键拼接;全无 → 紧凑 JSON 截断兜底。
    """
    parts = [str(payload[k]).strip() for k in _SECTION_TEXT_KEYS if isinstance(payload.get(k), str) and str(payload[k]).strip()]
    out = " | ".join(parts) if parts else json.dumps(payload, ensure_ascii=False)
    return out[:cap]


# ── Verification call ──────────────────────────────────────────

# Implementation review B5: no checklist-text echo required (parse aligns by index; echo wastes output budget:
# full Chinese criteria echo exceeds cap → truncation → systematic skipped, penalizing most fully specified steps).
_SYSTEM_PROMPT = """你是分析步骤的核验员。给定一步任务的目标、期望产出形态、编号验收清单,以及该步产出的摘要(可能是部分视图)。
对验收清单**逐条按编号顺序**判定,四选一:
- 先判「这一条能否用摘要核对」——摘要里根本没有对应维度的信息、或该条本身不可核对 → "cannot_tell";
- 能核对且摘要中有明确证据支持 → "met";
- 能核对但摘要中找不到 → "unmet",并在 note 里用一句话(≤40字)说明缺什么。
- 产出**如实声明**本工作区/数据源里根本没有这个领域的数据(如「本工作区没有冷链温控相关字段」
  「该任务超出当前数据能力范围」),该条无从谈起 → "not_applicable"。
  **这不是没做到,是诚实告知**,不要判 unmet。

三条容易判错的,注意:
① **摘要是压缩视图**,它只保留代表性数值(几百字 + 有限条事实)。判据要求**逐项列举**
   (如「1-12 月各月数值」「Top10 各自的数值」「每个门店的明细」)而摘要只给了其中几项时,
   那是**摘要的取舍**,不是产出缺失 → "cannot_tell",**不要判 unmet**。
   只有当摘要给出了完整口径却与判据明显矛盾、或该维度整个没出现,才算 unmet。
② 上一条不适用于**小基数**判据(如「最高的 3 个品牌名称及数值」):摘要装得下,
   给了就 met、没给就 unmet。
③ 摘要里的「来源」一节列的是这一步**真实跑过的查询**。若判据要的维度/区间正好对应
   其中一条(例:判据要「1-12 月各月营业额」,来源里有 `bi_query: 2025年1-12月 每月营业额`),
   说明**数据确实取到了**、只是明细没进摘要 → "cannot_tell",不要判 unmet。
   来源里找不到对应查询时,照常判 unmet。

The checklist also includes the original task objective. Check all of its mandatory requirements,
including required exchanges or checks that shorter criteria may omit. A result explicitly saying
a required check is pending does not meet that objective. If the summary cannot establish whether
the required action happened, use cannot_tell; do not infer completion from a correct number alone.

不评产出质量高低,只对照清单。输出严格 JSON(items 按清单编号顺序、数量一致,**不要回显清单原文**):
{"items": [{"status": "met|unmet|cannot_tell|not_applicable", "note": "unmet 时填"}]}
不要解释,只输出 JSON。"""


async def verify_step_output(
    router: Router,
    *,
    objective: str,
    deliverable: str,
    criteria: Sequence[str],
    digest: str,
    agent_type: str = "lead",
    trace_id: str = "",
    timeout_s: float = 45.0,
) -> VerifyOutcome:
    """Check the criteria and original objective; verifier failures remain skipped."""
    crit = [c for c in (str(x).strip() for x in criteria) if c]
    if not crit:
        return VerifyOutcome(VERDICT_SKIPPED, note="no_criteria")
    # Generated criteria can omit an explicit requirement from the approved step.
    # Retain it in the same item protocol so missing evidence cannot become a pass.
    if objective.strip() and objective.strip() not in crit:
        crit.append(objective.strip())
    user = (
        f"Task objective: {objective}\n"
        f"期望产出形态: {deliverable[:160] or '(未指定)'}\n"
        f"验收清单:\n" + "\n".join(f"{i + 1}. {c}" for i, c in enumerate(crit)) + f"\n\n产出摘要:\n{digest}\n\n输出 JSON:"
    )
    try:
        with anyio.fail_after(timeout_s):
            resp = await router.invoke(
                agent_type,
                LLMRequest(
                    model="",
                    messages=[Message(role="user", content=user)],
                    system=_SYSTEM_PROMPT,
                    temperature=0.0,
                    max_output_tokens=600,
                    trace_id=f"plan_verify:{trace_id}",
                    response_format={"type": "json_object"},
                ),
            )
    except TimeoutError:
        logger.warning("plan_verify.timeout trace=%s timeout_s=%s", trace_id, timeout_s)
        return VerifyOutcome(VERDICT_SKIPPED, note="timeout")
    except Exception as e:
        logger.warning("plan_verify.llm_failed trace=%s err=%s", trace_id, e)
        return VerifyOutcome(VERDICT_SKIPPED, note=f"llm_error:{type(e).__name__}")
    items = _parse_items(resp.content or "", crit)
    if items is None:
        logger.info("plan_verify.unparseable trace=%s raw=%r", trace_id, (resp.content or "")[:200])
        return VerifyOutcome(VERDICT_SKIPPED, note="unparseable")
    return VerifyOutcome(reduce_verdict(items), items=tuple(items), note="")


def _parse_items(raw: str, criteria: list[str]) -> list[VerifyItem] | None:
    """LLM 输出 → VerifyItem[](按 criteria 顺序对齐;缺项=cannot_tell;全坏 → None)."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        cleaned = raw.strip().strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            return None
    got = data.get("items") if isinstance(data, dict) else None
    if not isinstance(got, list):
        return None
    out: list[VerifyItem] = []
    for i, text in enumerate(criteria):
        it = got[i] if i < len(got) and isinstance(got[i], dict) else {}
        status = str(it.get("status") or ITEM_CANNOT_TELL).strip().lower()
        if status not in _ITEM_STATUSES:
            status = ITEM_CANNOT_TELL
        out.append(VerifyItem(text=text, status=status, note=str(it.get("note") or "")[:_NOTE_CAP]))
    return out
