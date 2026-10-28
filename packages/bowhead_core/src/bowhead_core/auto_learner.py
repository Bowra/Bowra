"""个人记忆分类器（δ-#7 起，r3 后只剩一个类目）。

用户在 turn 里说了一句话，判断它值不值得记进**这个用户的个人记忆**。

⚠ r3（2026-09-09）之前这里有四类 fact（data_correction / formula_definition /
concept_definition / preference），判完直接写 `RegisteredFactStore`。
那条路**整条删了** —— `scope=project` 会落成 `user_id IS NULL` ＝ 工作区共享，
也就是模型判定、零确认、直接写成团队可见的东西。设计权威＝
`docs/sprints/agent-feedback-loop-2026-09-08/tech-design.md` §1.4「两个写入口」。

现在只产出 `modification_request`（「你以后换个做法」），只写个人记忆，
够不着任何团队面。判据是提示词里那三句自然语言，不是词表。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Literal

from bowhead_core import metrics
from bowhead_core.clarification_store import _sanitize_directive
from bowhead_router import LLMRequest, Message, Router

logger = logging.getLogger(__name__)


CLASSIFIER_AGENT_TYPE = "echo"  # ζ pattern — turbo/mini, ~$0.0001 / call
DEFAULT_TIMEOUT_SEC = 10.0
MAX_DIRECTIVE_INPUT_CHARS = 4000  # match clarification_store.MAX_DIRECTIVE_CHARS
DEFAULT_CONFIDENCE_FLOOR = 7  # below this → skip writing


#: This classifier's only output category: modification request, asking for a different future approach.
#: Writes only **personal memory** (`memory_entries`), reaching no team-visible surface.
MODIFICATION_REQUEST: str = "modification_request"

#: ★ r3 (2026-09-09, user decision): old data_correction / formula_definition /
#: concept_definition / preference categories **entirely removed**. They wrote directly to
#: `RegisteredFactStore`; `scope=project` became `user_id IS NULL` = **workspace-shared**
#: (specified by `registered_fact_store.py`'s `_scope_filter`), meaning **model judgment, zero confirmation,
#: direct writes to team-visible content**. Under the personal/team-domain two-write-entry design,
#: that was the only door that should not exist; team definitions have their own entry and approvals, inaccessible through this model path.
#: Design authority: `docs/sprints/agent-feedback-loop-2026-09-08/tech-design.md` §1.4.
FactType = Literal["modification_request"]

_VALID_TYPES: frozenset[str] = frozenset({MODIFICATION_REQUEST})


CLASSIFIER_PROMPT = """\
你是 Bowhead 的个人记忆分类器。用户在 turn 里说了一句话,你要判断它**值不值得记进
这个用户的个人记忆**(跨 session 持久化, 以后自动注入)。

输出 JSON:
{
  "type": "modification_request" | "skip",
  "fact_key": "<lowercase + 下划线的主题词; e.g. ranking_chart_type / sales_analysis_depth>",
  "value_json": "",
  "confidence": 0-10
}

# 只有一种要记的东西
modification_request: 用户在**要求你以后换个做法**——针对你的行为或呈现方式,
带长期语气 (以后都 / 每次 / 一律 / 记住 / from now on / always)。
例: "以后销售分析都给我完整复盘" / "记住排名图用横向条形图"

value_json 留空 (收窄改写是后面那一步的活)。
**fact_key 必须给**: 它是「同一主题复用同一 key」的抓手, 同一件事换个说法再讲一遍
要能认出是同一条。用 lowercase + 下划线的主题词。实在给不出才留空 (那样不会自动记录)。

# 记之前先问这三句
1. **以后还有用吗** —— 只对这一轮成立的 ("这次先按月看" / "这张图先去掉图例")
   → skip。它不是长期偏好, 记下来会在以后每一轮里误导你。
2. **别处是不是已经有了** —— 工作区词表、业务规则、或用户已有的记忆里已经写着的
   → skip。重复记一遍只会让两处慢慢对不上。
3. **是不是在说「你该怎么做」** —— 个人记忆只管**风格、形态、语言、详略**。
   - 说的是"你以后应该怎么做" → modification_request
   - 说的是"数据/口径应该是什么" (指标怎么算、概念怎么定义、某个数字对不对)
     → **skip**

# ★ 为什么口径类一律 skip
指标口径属于**团队**, 不属于某一个人。一个人在自己的记忆里把「复购率」改成 90 天,
他和同事的数从此对不上, 而双方都不知道。口径有自己的入口 (工作区词表 / 业务规则)
和审批流程, 个人记忆这条路**够不着也不该够得着**。
你只需判成 skip; 该怎么引导用户走团队通道, 是上层的事, 不用你写在这里。

# confidence 怎么给 (它决定后面走哪条路, 请如实给)
- ≥ 8: 用户明说的长期要求, 语气清楚 → 会**直接记下**并告知用户 (可撤销)
- 7: 像是长期要求但不够确定 → 会**弹一张卡问用户一句**, 不会擅自记
- < 7: 强制 skip
拿不准就给 7 —— 那条路会去问用户, 比你替他猜要好。

# 示例
Q: "记一下: 以后凡是排名的图, 都画成横着的条, 别用竖着的柱子"
A: {"type":"modification_request","fact_key":"ranking_chart_type","value_json":"","confidence":9}

Q: "以后销售分析都给我完整复盘, 别只给个结论"
A: {"type":"modification_request","fact_key":"sales_analysis_depth","value_json":"","confidence":8}

Q: "EBITDA 我们不算外包人力, 只算正式工"
A: {"type":"skip","fact_key":"","value_json":"","confidence":0}
   (口径定义 —— 属于团队, 不进个人记忆)

Q: "Q1 营收应该是 13 亿不是 12 亿"
A: {"type":"skip","fact_key":"","value_json":"","confidence":0}
   (在改具体数字 —— 属于团队数据, 不进个人记忆)

Q: "这次先按月看"
A: {"type":"skip","fact_key":"","value_json":"","confidence":0}
   (只对这一轮成立)

Q: "你好"
A: {"type":"skip","fact_key":"","value_json":"","confidence":0}

只输出 JSON, 不加 markdown fence.
"""


@dataclass
class LearnedFact:
    """auto_learner 输出 — 调用方把它交给个人记忆那条路（`delta_lifecycle`）。

    ★ r3 删掉了 `scope` 字段：老四档走后只剩 `modification_request` 一类，
    而它**永远是个人域**。留一个恒为 "user" 的字段，下一个人会以为这里还有选择。
    """

    fact_type: FactType
    fact_key: str
    value_json: str
    confidence: int
    source_session_id: str | None = None
    source_turn_id: str | None = None


# Bounded concurrency: ≥20 simultaneous classifiers in one process is wasteful; the 21st waits for SEM release.
_AUTO_LEARNER_SEM = asyncio.Semaphore(20)


def _emit_metric(name: str, *, labels: dict[str, str] | None = None) -> None:
    """Best-effort metric counter (gracefully no-op if registry off)."""
    try:
        metrics.inc(f"delta.auto_learner.{name}", labels=labels)
    except Exception as e:  # pragma: no cover — metric infra failure non-fatal
        logger.debug("delta.auto_learner.metric_emit_failed name=%s err=%s", name, e)


def _emit_latency(ms: float) -> None:
    try:
        metrics.observe("delta.auto_learner.latency_ms", ms)
    except Exception as e:  # pragma: no cover
        logger.debug("delta.auto_learner.latency_emit_failed err=%s", e)


def _build_user_prompt(directive: str, hitl_ctx: dict[str, Any] | None) -> str:
    parts = [f"用户 directive:\n{directive[:MAX_DIRECTIVE_INPUT_CHARS]}"]
    if hitl_ctx:
        # HITL resolve context: LLM-proposed + user-edited values help the classifier determine type.
        parts.append(f"\nHITL context (来自 formula_confirm 等 HITL):\n{json.dumps(hitl_ctx, ensure_ascii=False)[:1000]}")
    return "\n".join(parts)


def _parse_response(content: str) -> dict[str, Any] | None:
    """Robust JSON parser — strip markdown fence if present, then json.loads."""
    s = content.strip()
    if s.startswith("```"):
        # Fallback for ```json ... ```
        lines = s.split("\n")
        s = "\n".join(lines[1:-1] if len(lines) >= 3 else lines[1:])
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _validate_fact(parsed: dict[str, Any]) -> LearnedFact | None:
    """Type-narrow, range-check, scrub value_json, return LearnedFact or None.

    Returns None when type=skip or confidence below floor — signals the caller
    to not write a fact (with metric emit).

    δ-#8.5 Sec P0-3: value_json comes from a weak echo classifier and gets
    persisted then injected into next-turn system prompt. A user directive
    containing prompt-injection (or just a confused LLM) can produce
    value_json that poisons every future turn. Run it through
    ``_sanitize_directive(kind="formula_confirm")`` (the strictest pattern
    list — code-execution + prompt-injection blocklist) before persist.
    """
    t = parsed.get("type", "skip")
    if not isinstance(t, str):
        return None
    if t == "skip":
        return None
    if t not in _VALID_TYPES:
        logger.warning("delta.auto_learner.unknown_type value=%r", t)
        return None
    try:
        conf = int(parsed.get("confidence", 0))
    except (TypeError, ValueError):
        return None
    if conf < DEFAULT_CONFIDENCE_FLOOR:
        return None
    fact_key = str(parsed.get("fact_key") or "").strip().lower()
    value_json = parsed.get("value_json")
    # ★ Modification requests **have no natural key/value pair**: a request for full reviews in future sales analyses
    # contains neither fact_key nor canonical value; narrowing the rewrite belongs to the later drafting step.
    # Previously these two required-field checks returned None **without logging**, **silently swallowing** the new category,
    # appearing as a new type plus updated prompt but no output. One of the pitfalls individually confirmed during this handover.
    if t == MODIFICATION_REQUEST:
        # Still **accept key where possible**: it enables reusing the same key for the same topic (memory redesign's
        # key-reuse rule relies on it), but absence is not an error.
        # ⚠ Still apply P0-3 sanitization: this function's docstring says value_json comes from a weak classifier,
        # persists, and enters next turn's system prompt, so it must pass `_sanitize_directive`.
        # Early return for the new category is **no exemption** (caught in 2026-09-08 consolidation review).
        # Do not fall back to `str()` either: str(dict) becomes a **single-quoted repr**,
        # the shape `combined_text`'s docstring records as evading secret scanning.
        raw_mr = value_json if isinstance(value_json, str) else ""
        if raw_mr.strip():
            try:
                raw_mr = _sanitize_directive(raw_mr, kind="formula_confirm") or ""
            except ValueError as e:
                logger.warning("delta.auto_learner.value_blocked err=%s", e)
                raw_mr = ""
        return LearnedFact(
            fact_type=t,  # type: ignore[arg-type]
            fact_key=fact_key,
            value_json=raw_mr.strip(),
            confidence=max(0, min(10, conf)),
        )
    # ★ r3: unreachable; `_VALID_TYPES` has only one category, and the preceding
    # `t not in _VALID_TYPES` excludes all others. Keep this to fail immediately when a future category is added
    # without a branch, instead of **silently returning None** (the individually confirmed sprint pitfall:
    # new type + updated prompt, no output, and no logs).
    raise AssertionError(f"未处理的类目 {t!r}——加类目时必须同时加分发分支")


async def classify(
    directive: str,
    *,
    router: Router,
    hitl_ctx: dict[str, Any] | None = None,
    source_session_id: str | None = None,
    source_turn_id: str | None = None,
    timeout_sec: float | None = None,
    trace_id: str = "",
) -> LearnedFact | None:
    """Classify a user directive into a LearnedFact, or return None to skip.

    Designed to be called fire-and-forget post-turn: ``asyncio.create_task``.
    Failures (timeout / parse / config) all return None — next directive
    re-classifies. Caller writes to RegisteredFactStore if non-None.

    Bounded by ``_AUTO_LEARNER_SEM`` (20 concurrent). Metrics tracked for
    invocations / timeouts / parse_failures / classify_skip / facts_written /
    latency_ms / last_failure.
    """
    _emit_metric("invocations")
    if not directive or not directive.strip():
        _emit_metric("classify_skip", labels={"reason": "empty_directive"})
        return None
    # δ-#8.5 Sec P1-10: sanitize directive at classify entry — the classifier's
    # system prompt is itself an injection target; an unsanitized directive can
    # flip the classifier to emit attacker-chosen JSON. Use formula_confirm
    # rules (strictest blocklist) since output gets persisted as a fact.
    try:
        directive = _sanitize_directive(directive, kind="formula_confirm")
    except ValueError as e:
        _emit_metric("classify_skip", labels={"reason": "blocked_input"})
        logger.warning("delta.auto_learner.directive_blocked err=%s", e)
        return None
    if not directive:
        _emit_metric("classify_skip", labels={"reason": "empty_after_sanitize"})
        return None
    timeout = timeout_sec if timeout_sec is not None else float(os.getenv("BOWHEAD_AUTO_LEARNER_TIMEOUT_SEC", str(DEFAULT_TIMEOUT_SEC)))

    user_prompt = _build_user_prompt(directive, hitl_ctx)
    req = LLMRequest(
        model="",
        messages=[Message(role="user", content=user_prompt)],
        system=CLASSIFIER_PROMPT,
        temperature=0.1,
        max_output_tokens=300,
        trace_id=trace_id,
    )

    started = time.monotonic()
    async with _AUTO_LEARNER_SEM:
        try:
            resp = await asyncio.wait_for(
                router.invoke(CLASSIFIER_AGENT_TYPE, req),
                timeout=timeout,
            )
        except TimeoutError:
            _emit_metric("timeouts")
            _emit_metric("last_failure")
            logger.warning("delta.auto_learner.timeout sec=%s", timeout)
            return None
        except (RuntimeError, KeyError, LookupError, TypeError) as e:
            # KeyError: router agent_type not configured (yaml mismatch);
            # RuntimeError / TypeError: invocation infra fault.
            _emit_metric("last_failure")
            logger.warning("delta.auto_learner.invoke_failed err=%s", e)
            return None
        finally:
            _emit_latency((time.monotonic() - started) * 1000)

    parsed = _parse_response(resp.content or "")
    if parsed is None:
        _emit_metric("parse_failures")
        _emit_metric("last_failure")
        logger.warning(
            "delta.auto_learner.non_json content=%r",
            (resp.content or "")[:200],
        )
        return None

    fact = _validate_fact(parsed)
    if fact is None:
        _emit_metric(
            "classify_skip",
            labels={"reason": str(parsed.get("type", "unknown"))[:40]},
        )
        return None

    fact.source_session_id = source_session_id
    fact.source_turn_id = source_turn_id
    _emit_metric(
        "facts_written",
        labels={"type": fact.fact_type},
    )
    return fact
