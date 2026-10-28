"""bi-hitl-cleanup P1 / B1 — `bi_concept_resolve` atomic tool.

Exposes deterministic concept resolution (dictionary recall + verified suffix split)
as an LLM-callable tool, so the LLM — not a middleware — decides when to disambiguate
a BI token before calling bi_query. The tool makes no LLM call of its own: when the
dictionary has no answer, the calling agent interprets the term (expands a region,
composes a formula) and verifies the result with this tool or bi_query.

Per Anthropic 11 tool design principles (per discovery.md §4.5 / PRD r2.1 §4 B1):
  #1 verb-led: `bi_concept_resolve` (verb_object)
  #2 曾用 `kind_hint` discriminator 路由三条内部路径 —— 已删除（它 57% 被填错，
     且曾决定查不查词表）；现在恒先查词表，猜测分支只看召回证据
  #4 batching: `tokens: list[str]` in one call (recall costs the same for 1 or N tokens)
  #7 next-action-hint: result carries advisory hint (NOT directive — LLM may override)
  #11 cross-tool refs: `clarification_payload` pre-shaped to clarification_ask B5 schema

Sprint scope (per discovery.md §4.4 sprint clarification): BI source instance only.
Future KB / web / memory peer "concept_resolve" tools can copy this pattern but
are out of scope here.

Per review CTO-P1-1: `next_action_hint` is **advisory**, NOT directive. Description
explicitly says LLM may ignore the hint based on query context.

Per review P1-CTO-2 (close): per-token graceful failure — one token's suffix check
raising must NOT break the batch; failing tokens get `next_action_hint="skip_token"`
+ `error` field populated.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from bowhead_router.types import ToolSchema

from bowhead_core.concept_recall import TokenRecall, recall_tokens
from bowhead_core.concept_resolver import detect_dim_suffix
from bowhead_core.env_utils import env_float
from bowhead_core.tool_registry import ToolSpec

if TYPE_CHECKING:
    from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry

logger = logging.getLogger(__name__)


# ── Result shape (TypedDicts for type-safe wire) ─────────────────────────


class Candidate(TypedDict, total=False):
    label: str  # human-facing (e.g. "净销售额").
    value: str  # dim_id / m_id / formula text
    rationale: str  # short why-this-candidate
    confidence: Literal["high", "medium", "low"]


class Question(TypedDict):
    """Pre-shaped 3-field shape matching B5 `clarification_ask` schema."""

    question: str
    options: list[str]
    type: Literal["single_select", "multi_select"]


class ClarificationPayload(TypedDict):
    questions: list[Question]


class ConceptResult(TypedDict, total=False):
    """召回与判定分离后的对外形状。

    每条 match 自带 `type`，且 `dim`/`value` 来自同一条 hit。
    """

    token: str
    # unique | ambiguous | weak | not_found | unusable_dimension | no_permission
    verdict: str
    matches: list[dict[str, Any]]
    truncated: int
    # used=false dimensions (declared, no data), distinct from "not found"; upstream uses this for domain_mismatch.
    unusable: list[dict[str, str]]
    # Advisory (per CTO-P1-1) — LLM may ignore based on query context:
    next_action_hint: Literal["use_top_candidate", "call_clarification_ask", "skip_token"]
    # Present iff next_action_hint == "call_clarification_ask"; matches B5 schema.
    clarification_payload: ClarificationPayload | None
    error: str | None


# ── Tool registration ────────────────────────────────────────────────────


def register_bi_concept_resolve_tool(registry: ToolRegistry) -> None:
    """Register bi_concept_resolve tool. Called from gateway bootstrap."""
    registry.register(
        ToolSpec(
            schema=_bi_concept_resolve_schema(),
            handler=_bi_concept_resolve_handler,
        )
    )


def _bi_concept_resolve_schema() -> ToolSchema:
    """Tool schema — description follows Anthropic ask_user_input_v0 / skill-creator
    style: WHEN TO USE / WHEN NOT TO USE / CRITICAL / OUTPUT FORMAT / 2 BI anchor
    examples (per PRD §4 B1 r2.1 — concrete BI anchors only, no hypothetical KB).
    """
    return ToolSchema(
        name="bi_concept_resolve",
        description=(
            "Look up NL tokens in the workspace BI dictionary BEFORE calling bi_query. "
            "Deterministic: dictionary recall over field names and dimension values, no LLM inside; "
            "returns candidates with evidence and an advisory next_action_hint per token (batched).\n"
            "\n"
            "用在**用户写法与 workspace 数据字典对不上**的 token 上: dim_value 缩略('万达店' — 哪家?)、"
            "别名、你自己展开出来的一批候选值('上海'/'江苏'… 数据里叫什么); 同 turn 的多个 token 一次 batch 传完省 round-trip.\n"
            "CRITICAL — 什么时候**别**用: ws 硬词('SKU' / '净利润' / '销量')、用户已明示候选、上一轮已解析过的 token —— "
            "直接 bi_query, 别绕这里.\n"
            "\n"
            "**这个工具只查词表, 不猜。** 词表里没有的概念（区域'华东'、集合'重点产品'、派生指标'客单价'）"
            "由你自己按常识和用户语境解释: 区域/集合展开成成员值, 派生指标拆成数据里已有的指标; "
            "再把展开后的值或指标名传进来核对哪些真在数据里, 只用核对过的去查, 并在结论里写明口径。\n"
            "\n"
            "OUTPUT FORMAT — list[ConceptResult], len(results) == len(tokens):\n"
            "{\n"
            "  token: str,\n"
            "  verdict: 'unique'|'ambiguous'|'weak'|'not_found'|'unusable_dimension'|'no_permission',\n"
            "  matches: [{type, dim_name, value, evidence}],\n"
            "  truncated: int,          // 因每词上限被截掉的候选数\n"
            "  unusable: [{dim_name, reason}],   // reason: declared_but_no_data | no_permission\n"
            "  next_action_hint: 'use_top_candidate'|'call_clarification_ask'|'skip_token',\n"
            "  clarification_payload: { questions: [{question, options, type}] } | null,\n"
            "  error: str | null,\n"
            "}\n"
            "\n"
            "**match.type**: `dim_value`(某维度的取值, 拿去 bi_query 当筛选值) · "
            "`dim_name`(维度名, 是**分组**意图不是筛选值) · `metric` · `time`.\n"
            "**match.evidence**: `literal`(逐字) · `token_in_doc`(词表值含你的 token, "
            "如 `爱必妥`→`爱必妥 5`) · `doc_in_token` · `near`(仅语义近) · "
            "`suffix`(`万达店` → 维度`门店` + 包含`万达`, 已核对该维度下真有含`万达`的值; 用 `rewrite` 原样查).\n"
            "\n"
            "拿去 bi_query 的是 **`value`**（自然语言串）, 不是 `dim`(内部 id 如 `d54`, "
            '**bi_query 不吃 id**)。`type=dim_name` 时 `value` 是维度名, 用于"按 X 拆分", '
            "别当筛选值用。\n"
            "\n"
            "verdict 怎么用（**建议非指令**）:\n"
            "- `unique` → matches[0] 直接用\n"
            "- `ambiguous` → 同一个词对上了多个口径（如 `爱必妥` 既是品牌也是通用名）; "
            "有 `clarification_ask` 就调它, 没有就把 `clarification_payload` 原样放进 "
            "`subagent_return(payload={'error':{'kind':'needs_user_input','message':<问题说明>,'clarification_payload':<原样>}})`\n"
            "- `weak` → 只有语义近的候选, 证据不硬; 别直接当答案用, 结合用户语境判断或问用户\n"
            "- `unusable_dimension` → 该字段**已声明但无数据**, 查了会被静默兜底成别的维度 ⇒ "
            "直接 `domain_mismatch` 告诉用户本 workspace 无此数据, **别硬查**\n"
            "- `no_permission` → 该字段存在, 但**当前用户没有这个字段的权限** ⇒ 别查也别换词绕过; "
            "告诉用户缺哪个字段 (`unusable[].dim_name`) 的权限, 请他联系数据管理员开通\n"
            "- `not_found` / `skip_token` → **别急着问用户**: 先原样发一次 bi_query, "
            "看返回的 `_parse.tokens` 里这个词有没有被识别成 `dimension_value` (the downstream catalog may contain additional values); 绑上了就照常出结果, "
            "**绑不上**时, 区域/集合/派生指标由你自己按常识解释(展开成员值或拆公式), "
            "把展开结果再传进本工具核对, 只用数据里有的去查并在结论里写明口径; "
            "解释不了再拿 `matches` 去问用户。\n"
            "\n"
            "Don't gate on a fixed entity list — workspace 字典随租户变, "
            "backend resolver 是 source of truth.\n"
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "tokens": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 80},
                    "minItems": 1,
                    "maxItems": 8,  # soft cap (per feedback_design_no_hard_caps)
                    "description": "Tokens to look up: words from the user's query, or values/metric names you expanded yourself. Pass ALL in one call.",
                },
            },
            "required": ["tokens"],
        },
    )


# ── Per-token dispatch ───────────────────────────────────────────────────


# ── Shape mappers (MetricResolution / ConceptResult → tool ConceptResult) ─

# Unified embed-backend-unavailable prefix in `error`. Empty candidates mean two different things: "queried and genuinely
# absent from dictionary" vs "query never succeeded". Previously ConceptResult bytes were identical, so concept
# resolve silently returned all zeros on private deployments (incorrect collection tenant segment), undetectably. To judge
# actual absence, inspect this field, never candidate emptiness.
EMBED_UNAVAILABLE_PREFIX = "embed_unavailable"
# Reason prefix in `TokenRecall.embed_unavailable` when recall exceeds timebox.
_RECALL_TIMEOUT_REASON = "recall_timeout"

# Datarc cut splits values on this separator; refuse a rewrite whose value contains it.
_VALUE_SEP = "、"


# ── Tool handler (batched + graceful per-token failure) ──────────────────


def _concept_resolve_timeout_s() -> float:
    """concept-resolve 软 timebox (秒)，召回段与每个 token 的后缀核对段各用一份.

    召回段：datarc `/keywords/recall/` 服务端逐词串行、并发请求之间也排队（2026-09-15
    实测每词 0.2–0.55s，4 路并发 p50 13s），繁忙时一批词可能等很久。超时 → 对应 token
    skip_token 降级, 不阻塞整 turn. 默认 40s, env 可调 (BOWHEAD_CONCEPT_RESOLVE_TIMEOUT_S;
    soft cap per feedback_design_no_hard_caps)."""
    return env_float("BOWHEAD_CONCEPT_RESOLVE_TIMEOUT_S", 40.0)


def _return_n() -> int:
    """每个 token 最多回传几个 match（soft，per feedback_design_no_hard_caps）。

    旧路径是"召回 10 → 只返 1"，而那 1 个还是跨集合拼出来的。
    """
    # 5 →12 (2026-08-31 user decision): `_rank_and_cap` now counts **distinct values**,
    # accommodating "上海仁济医院": four campuses × two dimensions (old 5-slot budget lost
    # 4 slots to same-value mirrors, always excluding south campus).
    return int(env_float("BOWHEAD_CONCEPT_RETURN_N", 12.0))


async def _suffix_split(token: str, ctx: Any) -> Any:
    """词表没结论时唯一的确定性兜底：维度名后缀拆分（`万达店` → 剥「店」→ 门店包含万达）。

    零 LLM。拆出来的前缀必须在该维度下真有值（见 `_suffix_prefix_exists`），否则返回
    None。`华东` / `客单价` 这类不是字面值的概念不在这里处理——由调用方 Agent 自己解释，
    再把展开结果传回本工具核对。
    """
    try:
        suffix = await detect_dim_suffix(token, ctx)
    except Exception as e:  # A failed check leaves the recall result as is.
        logger.debug("concept.suffix_detect_failed token=%r err=%s", token, e)
        return None
    if suffix is not None and await _suffix_prefix_exists(suffix, ctx):
        return suffix
    return None


async def _suffix_prefix_exists(suffix: Any, ctx: Any) -> bool:
    """suffix 拆分必须**验证过前缀真的存在**才算数。

    ★ 116 真机 dogfood 抓到的：`万达店` 被拆成 门店(d57) + `万达`，输出
    `source:"dict"` + `rewrite:"门店包含万达"` —— 而这个 workspace 的 40 家门店
    **没有一家含「万达」**。`detect_dim_suffix` 只验了后缀「店」对得上维度名，
    从没验过前缀在该维度里有值。于是又成了"自信地给错"。

    ⇒ 拿 `value_part` 回查 keywords coll，要求命中**同一个 dim** 且证据够硬。
    验不过就当没检测到，别硬给一个粘贴即用的错改写。
    """
    from bowhead_core.concept_recall import _is_strong, recall_tokens

    part = str(getattr(suffix, "value_part", "") or "").strip()
    dim = str(getattr(suffix, "dim_id", "") or "").strip()
    if not part or not dim:
        return False
    try:
        recs = await recall_tokens(ctx, [part], cap=8)
    except Exception as e:
        logger.debug("concept.suffix_verify_failed part=%r err=%s", part, e)
        return False
    if not recs:
        return False
    ok = any(m.dim == dim and _is_strong(m) for m in recs[0].matches)
    if not ok:
        logger.info("concept.suffix_rejected token_part=%r dim=%s — 该维度下没有这个值", part, dim)
    return ok


def _contains_rewrite(dim_name: str, value: str) -> str:
    """A verified suffix split → the Chinese phrase Datarc cut recognizes (`门店包含万达`).

    Withhold the phrase rather than hand over a ready-to-paste wrong one: without a
    dimension name bi_query cannot tell which field to filter, and a value containing the
    separator is split by cut into two values (feedback_punctuation_doubles_as_data_separator).
    """
    value = (value or "").strip()
    if not dim_name or not value or _VALUE_SEP in value:
        return ""
    return f"{dim_name}包含{value}"


def _match_public(m: Any) -> dict[str, Any]:
    # Fields are named, never Datarc ids (`d54`): no tool accepts an id.
    return {
        "type": m.type,
        "dim_name": m.dim_name,
        "value": m.value,
        "evidence": m.evidence,
        "dist": round(float(m.dist), 4),
    }


def _is_same_value_across_dims(matches: list[Any]) -> bool:
    """歧义组之间是不是「同一个串、不同维度」。

    True  = `河南省中业医疗器械有限公司` 同时是客户名称和经销商名称 ⇒ 不是语义分叉
    False = `爱必妥`(品牌) vs `西妥昔单抗`(通用名) ⇒ 真分叉，该问用户
    """
    from bowhead_core.concept_recall import _is_strong

    vals = {str(getattr(m, "value", "") or "").strip() for m in matches if _is_strong(m)}
    return len(vals) <= 1


def _no_literal_candidate(matches: list[Any]) -> bool:
    """Detect candidates that contain the token without a literal match.

    The local catalog may be incomplete. Retain the candidates and let the downstream
    natural-language parser attempt a binding before asking the user to clarify.
    Do not remove matches or hide the clarification fallback.
    """
    return not any(str(getattr(m, "evidence", "")) == "literal" for m in matches)


def _clarification_for(token: str, matches: list[Any]) -> ClarificationPayload:
    """`ambiguous` 的 options：**按 `(type, dim)` 组取代表，每组 1 个**。

    B5 schema 限 2-4 options 而返回可到 5 个 match，两者必须对齐（review D6）。
    措辞带上维度名——只给值用户没法选（「爱必妥」到底是品牌还是通用名？）。
    """
    seen: set[tuple[str, str]] = set()
    opts: list[str] = []
    for m in matches:
        if m.group_key in seen:
            continue
        seen.add(m.group_key)
        label = m.dim_name
        if m.type == "dim_value":
            opts.append(f"{label}：{m.value}" if label else m.value)
        else:
            opts.append(f"{label or m.value}（按此维度拆分）")
    extra = max(0, len(seen) - 4)
    q = f"「{token}」在数据里对得上多个口径，你要哪个？"
    if extra:
        q += f"（另有 {extra} 个未列出）"
    return ClarificationPayload(questions=[Question(question=q, options=opts[:4], type="single_select")])


def _compose_result(token: str, rec: Any, suffix: Any, timeout_s: float) -> ConceptResult:
    """把召回结果与（可选的）后缀拆分结果合成对外 shape。

    **`weak` ≠ `not_found`**：`weak` 的召回候选照样回传，后缀拆分是**追加**不是替代。
    永远不因为"不够硬"就把已召回的证据丢掉——那正是旧实现那个 bug 的成因。
    """
    matches: list[dict[str, Any]] = []
    unusable: list[dict[str, str]] = []
    truncated = 0
    verdict = "not_found"
    err: str | None = None

    if rec is not None:
        verdict = rec.verdict
        truncated = rec.truncated
        unusable = [{k: v for k, v in u.items() if k != "dim"} for u in rec.unusable]
        matches = [_match_public(m) for m in rec.matches]
        if rec.embed_unavailable:
            err = f"{EMBED_UNAVAILABLE_PREFIX}:{rec.embed_unavailable}"

    if isinstance(suffix, BaseException):
        if err is None:
            err = f"concept_resolve_timeout>{timeout_s:.0f}s" if isinstance(suffix, (asyncio.TimeoutError, TimeoutError)) else str(suffix)[:200]
    elif suffix is not None:
        # First: the verdict below says "use matches[0]", and recall may hold weak neighbours.
        matches.insert(0, _suffix_match(suffix))
        # ★ 116 dogfood fix: **verified suffix splits are deterministic conclusions, not weak evidence**.
        # Previously `万达店` → 门店包含万达 (executable rewrite) was marked weak+skip_token,
        # asking the model to abandon a token actually resolved.
        verdict = "unique"

    hint: str = "skip_token"
    payload: ClarificationPayload | None = None
    if verdict == "ambiguous":
        rms = list(rec.matches) if rec else []
        if _is_same_value_across_dims(rms):
            # ★ 116 dogfood fix: **same string under multiple dimensions** (`河南省中业医疗器械有限公司`
            # is both "客户名称" and "经销商名称") is not semantic divergence: either dimension gives users
            # the same row. Asking a card is a false prompt, precisely why bi-hitl-cleanup
            # removed automatic cards (13 false prompts in 30 days; 22 of 37 cards unanswered).
            # ⇒ Return all candidates with clear dimensions, let model choose without interrupting user.
            hint = "use_top_candidate"
        elif _no_literal_candidate(rms):
            # No literal candidate can mean an incomplete catalog, not semantic ambiguity.
            # Try the downstream parser before falling back to clarification through matches;
            # see _no_literal_candidate.
            hint = "skip_token"
        else:
            # Distinct values (`爱必妥`=brand vs `西妥昔单抗`=generic name) ⇒ genuine divergence; ask user.
            hint = "call_clarification_ask"
            payload = _clarification_for(token, rms)
    elif verdict == "unique" and matches:
        hint = "use_top_candidate"
    # `weak` (semantic neighbors only) **never gets** `use_top_candidate`: embedding nearest neighbor maps
    # "广州" to "广渠门"; direct use upgrades empty results to confidently wrong dimensions,
    # worse than empty. Old EXACT branch explicitly guarded this (comment used this exact example);
    # retain it when rewriting. Still return candidates for model judgment; hints are suggestions, not instructions.

    out = ConceptResult(
        token=token,
        verdict=verdict,
        matches=matches,
        truncated=truncated,
        next_action_hint=hint,  # type: ignore[typeddict-item]
        clarification_payload=payload,
        error=err,
    )
    if unusable:
        out["unusable"] = unusable  # type: ignore[typeddict-unknown-key]
    return out


def _suffix_match(g: Any) -> dict[str, Any]:
    """Verified suffix split (`万达店` → dimension "门店" + value_part "万达").

    Contains semantics, not equality; treating value_part as the full value gives wrong queries.
    """
    dim_word = getattr(g, "dim_word", "") or ""
    value_part = getattr(g, "value_part", "") or ""
    return {
        "type": "dim_value",
        "dim_name": dim_word,
        "value": value_part,
        "evidence": "suffix",
        "op": "contains",
        "rewrite": _contains_rewrite(dim_word, value_part),
    }


async def _bi_concept_resolve_handler(
    args: dict[str, Any],
    ctx: ToolExecutionContext,
) -> dict[str, Any]:
    tokens: list[str] = args.get("tokens") or []

    if not tokens:
        return {"results": []}

    # ── Phase 1: deterministic recall (zero LLM, 2 embed calls regardless of token count) ──
    # **Always** consult vocabulary first. `kind_hint` once decided whether to do so, causing 57% of production calls to
    # skip it entirely, with structurally unreachable `dict_hit` (observed 0/28); first demoted to
    # a guess-branch suggestion, then removed completely.
    # Recall is timeboxed too: Datarc serializes server-side requests, so busy batches can queue long.
    # Timeout ⇒ report unavailable, never fake not_found (dictionary absence).
    timeout_s = _concept_resolve_timeout_s()
    try:
        recalls = await asyncio.wait_for(recall_tokens(ctx, tokens, cap=_return_n()), timeout=timeout_s)
    except TimeoutError:
        logger.warning("bi_concept_resolve.recall_timeout tokens=%d timeout=%.0fs", len(tokens), timeout_s)
        reason = f"{_RECALL_TIMEOUT_REASON}>{timeout_s:.0f}s"
        recalls = [TokenRecall(token=t, verdict="not_found", embed_unavailable=reason) for t in dict.fromkeys(tokens) if t and t.strip()]
    by_token = {r.token: r for r in recalls}

    # ── Phase 2: tokens without usable vocabulary evidence get the deterministic suffix split ──
    # No LLM here: a concept the dictionary cannot answer is interpreted by the calling agent.
    # Blank tokens are filtered by recall, so absent from by_token. dict.fromkeys deduplicates.
    # Skip recall-timeout tokens: verification calls the same queued endpoint.
    split_tokens = [
        t
        for t in dict.fromkeys(tokens)
        if t
        and t.strip()
        and (by_token.get(t) is None or by_token[t].verdict in ("weak", "not_found"))
        and not (by_token.get(t) is not None and by_token[t].embed_unavailable.startswith(_RECALL_TIMEOUT_REASON))
    ]
    splits: dict[str, Any] = {}
    if split_tokens:
        coros = [asyncio.wait_for(_suffix_split(t, ctx), timeout=timeout_s) for t in split_tokens]
        raw_splits = await asyncio.gather(*coros, return_exceptions=True)
        splits = dict(zip(split_tokens, raw_splits, strict=True))

    raw = [_compose_result(t, by_token.get(t), splits.get(t), timeout_s) for t in tokens]

    results: list[ConceptResult] = list(raw)

    # Embed-backend unavailability is an **operational failure**, not an unknown user term; report separately,
    # or it looks identical to ordinary no_match as "concept resolve returned 0 again".
    _down = [r for r in results if str(r.get("error") or "").startswith(EMBED_UNAVAILABLE_PREFIX)]
    if _down:
        logger.warning(
            "bi_concept_resolve.embed_unavailable tokens=%d/%d reasons=%s",
            len(_down),
            len(results),
            sorted({str(r.get("error")) for r in _down})[:3],
        )

    # Telemetry (per PRD §4 B8 + tech-design §P8): emit one counter per token.
    # `source_kind` label: "bi" (this tool is L1 BI source-specific);
    # `caller` label: peer profile name when available, else "lead" fallback.
    try:
        from bowhead_core import metrics as _m

        caller = str(getattr(ctx, "_running_profile", None) or "lead")
        for r in results:
            outcome = "embed_unavailable" if str(r.get("error") or "").startswith(EMBED_UNAVAILABLE_PREFIX) else str(r.get("verdict") or "not_found")
            src = "dict" if r.get("matches") else "none"
            _m.inc(
                "bowhead_bi_concept_resolve_call_total",
                labels={
                    "caller": caller,
                    "kind_resolved": str(r.get("verdict") or "not_found"),
                    "outcome": outcome,
                    # Discriminator: did deterministic dictionary-hit path activate? Before changing criteria or
                    # prompts, absence of this observed ratio makes all subsequent changes blind.
                    # Low cardinality (4 values), added to existing counter rather than another: duplicate
                    # implementations of one concept are a recurring repo failure.
                    "source": src,
                    "source_kind": "bi",
                },
                help="bi_concept_resolve per-token outcome (B8 L1 BI metric).",
            )
    except Exception:  # noqa: S110
        pass

    return {"results": results}
