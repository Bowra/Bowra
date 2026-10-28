"""Sprint chat-v3 Phase A · `tool_finding` SSE event + micro LLM hook.

Design source: ``docs/sprints/chat-v3-agent-sections-findings/tech-design-r2.md`` §2.

Pipeline (per phase A flow):
    tool_result emitted (subagent.py:1898)
        ↓
    if tool_finding_enabled() && category in {query, compute} && preview_payload:
        fire-and-forget _emit_finding_async() — bounded 1.5s wait_for
            ↓
        truncate_preview_for_finding(payload, max_chars=1600)
            ↓
        Router.invoke(agent_type="narrative", prompt + truncated_payload)
            ↓
        sanitize finding text (strip "因为/所以" lead-in, hard 120-char cap)
            ↓
        emit OrchestratorEvent(event_type="tool_finding", call_id=..., finding=...)

Invariants:
    I-A1  emit is non-blocking (fire-and-forget via asyncio.create_task)
    I-A2  task ref retained in module-level set + add_done_callback discard
    I-A3  per-turn cap 12 findings · soft (no exception, just skip)
    I-A4  category whitelist (query / compute only)
    I-A5  preview_payload None → skip (closes r2-review P0-Eng-2)
    I-A6  feature flag `BOWHEAD_TOOL_FINDING_ENABLED` default 1 (ON)
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any

from bowhead_core.agents._generated.tool_renames import canonical_tool_name
from bowhead_core.env_utils import env_float
from bowhead_core.tool_category import (
    lookup_kind,
    should_emit_finding_for_kind,
)
from bowhead_core.tool_finding_metrics import (
    emit_finding_error,
    emit_finding_ok,
    emit_finding_skipped,
)

# ─────────────────────────────────────────────────────────────────
# config / constants
# ─────────────────────────────────────────────────────────────────
_DEFAULT_ENABLED = "1"  # I-A6
_DEFAULT_MAX_PER_TURN = 12  # I-A3
_DEFAULT_TIMEOUT_S = 1.5
_DEFAULT_AGENT_TYPE = "narrative"  # reuse existing routing (Doubao Seed cheap+fast)

#: r2-review §2.1: cap LLM prompt payload ≤ ~400 token = ~1600 char ASCII.
#: Worst case prevented: 4 KB result_inline × 12 cap = 48 KB input → over-budget.
_PROMPT_PAYLOAD_MAX_CHARS = 1600

#: Hard cap on finding output text (Chinese chars, after model emits).
_FINDING_TEXT_MAX_CHARS = 120

#: Process-wide cap on in-flight finding LLM calls.
#: Phase A review P0-SRE-2: 100 turns × 12/turn = 1200 concurrent would
#: hit Doubao RPM. Default 8 keeps RPS bounded while letting per-turn
#: parallelism (χ batch) still benefit.
_DEFAULT_MAX_INFLIGHT = 8

#: Lazy-init semaphore (avoid asyncio.new_event_loop at module load).
_inflight_sem: asyncio.Semaphore | None = None


def _get_inflight_sem() -> asyncio.Semaphore:
    global _inflight_sem
    if _inflight_sem is None:
        try:
            cap = int(os.environ.get("BOWHEAD_TOOL_FINDING_MAX_INFLIGHT", str(_DEFAULT_MAX_INFLIGHT)))
        except ValueError:
            cap = _DEFAULT_MAX_INFLIGHT
        _inflight_sem = asyncio.Semaphore(cap)
    return _inflight_sem


#: Strip-from-start patterns (lazy model lead-ins forbidden by the design).
#: All lowercased before match for `因为` / `所以` / `总之` / `综上` / `because`/`so`.
_LEAD_IN_REJECT = (
    "因为",
    "所以",
    "总之",
    "综上",
    "since",
    "because",
    "so ",
    "thus",
)


def tool_finding_enabled() -> bool:
    """I-A6 · env override · default ON."""
    return os.environ.get("BOWHEAD_TOOL_FINDING_ENABLED", _DEFAULT_ENABLED) == "1"


def max_findings_per_turn() -> int:
    """I-A3 · soft cap, env override."""
    try:
        return int(os.environ.get("BOWHEAD_TOOL_FINDING_MAX_PER_TURN", str(_DEFAULT_MAX_PER_TURN)))
    except ValueError:
        return _DEFAULT_MAX_PER_TURN


def finding_timeout_s() -> float:
    """LLM call timeout · bounded so SSE stream not blocked."""
    # env_utils consolidation: old version lacked >0 clamping (negative wait_for timeout = immediate expiry); now ≤0 also uses default.
    return env_float("BOWHEAD_TOOL_FINDING_TIMEOUT_S", _DEFAULT_TIMEOUT_S)


def finding_agent_type() -> str:
    """Router agent_type override · default narrative (Doubao / DeepSeek flash)."""
    return os.environ.get("BOWHEAD_TOOL_FINDING_AGENT_TYPE", _DEFAULT_AGENT_TYPE)


# ─────────────────────────────────────────────────────────────────
# request / payload shaping
# ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ToolFindingRequest:
    """Inputs to finding LLM call. I-A5: preview_payload Optional."""

    tool_name: str
    args_text: str
    preview_payload: dict[str, Any] | None
    tool_category: str  # already-resolved via lookup_kind


def truncate_preview_for_finding(preview: dict[str, Any] | None) -> dict[str, Any]:
    """r2-review §2.1 closer · cap payload chars ≤ ~400 token.

    Strategy:
      - keep ``cols`` (cap 6)
      - keep top 3 rows verbatim, but cell value truncated to 40 chars
      - if still over budget, keep cols + 1 row + total

    Returns ``{}`` if input None / empty.
    """
    if not preview or not isinstance(preview, dict):
        return {}
    out: dict[str, Any] = {}

    # table preview path
    cols = preview.get("cols") or preview.get("columns") or []
    if isinstance(cols, list):
        out["cols"] = [str(c)[:30] for c in cols[:6]]

    rows = preview.get("rows") or []
    if isinstance(rows, list):
        out["rows"] = [[(str(v)[:40] if v is not None else "") for v in (row if isinstance(row, list) else [])] for row in rows[:3]]

    # carry total for cap context
    if "total" in preview:
        out["total"] = preview["total"]
    elif "total_rows" in preview:
        out["total"] = preview["total_rows"]

    # carry kind hint
    if "kind" in preview:
        out["kind"] = preview["kind"]

    # over-budget? truncate to cols + 1 row + total
    serialized = json.dumps(out, ensure_ascii=False)
    if len(serialized) > _PROMPT_PAYLOAD_MAX_CHARS:
        out["rows"] = out.get("rows", [])[:1]
    return out


# ─────────────────────────────────────────────────────────────────
# prompt builder
# ─────────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = (
    "你是 tool result summary 助手。"
    "看到 tool 调用的 args + 关键 preview 数据后, "
    "用一句中文 ≤ 80 字总结**关键发现** (numbers / facts)。\n"
    "禁止: 解释 why · 给 next step · 「因为」「所以」「总之」「综上」起手。\n"
    "样例:\n"
    "  args: 2026-04 销售量同比按品牌\n"
    "  preview: cols=[品牌,销量,同比], rows=[[阿迪,1.82M,-1.28%],[耐克,1.60M,-1.25%]]\n"
    "  → 30 品牌全数为负, top 跌幅: 阿迪 -1.28% / 耐克 -1.25%。"
)


def _build_user_prompt(req: ToolFindingRequest) -> str:
    """Compact prompt: tool + args + preview (already truncated)."""
    truncated = truncate_preview_for_finding(req.preview_payload)
    return f"tool: {req.tool_name}\nargs: {req.args_text[:200]}\npreview: {json.dumps(truncated, ensure_ascii=False)}"


# ─────────────────────────────────────────────────────────────────
# sanitizer
# ─────────────────────────────────────────────────────────────────
def sanitize_finding(text: str | None) -> str | None:
    """Reject lead-in starters + hard cap 120 chars + strip whitespace.

    Returns None if rejection-worthy. Otherwise returns trimmed string.
    """
    if not text:
        return None
    s = text.strip()
    if not s:
        return None
    s_lower = s.lower()
    for bad in _LEAD_IN_REJECT:
        if s_lower.startswith(bad.lower()):
            return None
    if len(s) > _FINDING_TEXT_MAX_CHARS:
        s = s[:_FINDING_TEXT_MAX_CHARS]
    return s


# ─────────────────────────────────────────────────────────────────
# core: generate_finding (called from fire-and-forget task)
# ─────────────────────────────────────────────────────────────────
async def generate_finding(
    req: ToolFindingRequest,
    *,
    router: Any,  # bowhead_router.Router (Any to avoid cyclic import)
    timeout_s: float | None = None,
) -> tuple[str | None, Any, str | None]:
    """Run LLM call · return (sanitized finding or None, TokenUsage or None, provider or None).

    Returns tuple so caller can compute real cost from usage (closes Phase A
    review P0-A5 — hardcoded $0.00024 is replaced with usage-derived cost).
    Provider is returned separately since TokenUsage doesn't carry provider
    (P0-B-3 close: previous code read non-existent `usage._provider` attr).
    """
    if not tool_finding_enabled():
        return None, None, None
    if not should_emit_finding_for_kind(req.tool_category):
        return None, None, None
    if req.preview_payload is None:
        return None, None, None

    # Lazy imports to avoid cyclic backend ↔ router
    from bowhead_router.types import LLMRequest, Message

    user_prompt = _build_user_prompt(req)
    llm_req = LLMRequest(
        model="",  # router resolves via agent_type config
        messages=[Message(role="user", content=user_prompt)],
        system=_SYSTEM_PROMPT,
        max_output_tokens=120,  # 80 characters + safety
        temperature=0.3,  # mostly factual
        stream=False,
    )

    timeout = timeout_s if timeout_s is not None else finding_timeout_s()

    # Phase A review P0-SRE-2: bounded concurrency via module-level semaphore.
    sem = _get_inflight_sem()
    try:
        async with sem:
            resp = await asyncio.wait_for(
                router.invoke(finding_agent_type(), llm_req),
                timeout=timeout,
            )
    except TimeoutError:
        emit_finding_error("timeout")
        return None, None, None
    except Exception:
        emit_finding_error("llm_fail")
        return None, None, None

    return (
        sanitize_finding(getattr(resp, "content", None)),
        getattr(resp, "usage", None),
        getattr(resp, "provider", None),  # P0-B-3 close: real provider field
    )


# ─────────────────────────────────────────────────────────────────
# chat-truth Phase B.3.b · static finding templates (non-LLM eligible tools)
# ─────────────────────────────────────────────────────────────────
#: Per tech-design §B.3.b: non LLM-eligible tools (subagent_spawn / skill_read /
#: add_chart / kb_read / kb_list_documents) get a
#: template-based static finding (0 LLM cost). FE then renders real text instead
#: of "—" placeholder. All templates emit ≤ 80 Chinese chars.
_STATIC_FINDING_TEMPLATES: dict[str, Any] = {
    "subagent_spawn": lambda args, _r: f"派 {args.get('profile') or '?'} peer · {str(args.get('task') or '')[:30]}",
    "skill_read": lambda args, _r: f"读 SOP · {args.get('name') or args.get('skill_id') or '?'}",
    "skill_invoke": lambda args, _r: f"调 skill · {args.get('name') or args.get('skill_id') or '?'}",
    "add_chart": lambda args, _r: f"渲染 {args.get('hint') or 'chart'} chart · {str(args.get('title') or '')[:30]}",
    "kb_read": lambda args, _r: f"读 KB · {args.get('doc_id') or args.get('title') or '?'}",
    "kb_list_documents": lambda _a, _r: "列 KB 文档",
    # chat-truth-hotfix-2 (2026-05-15): bi_schema result shape =
    # {measures: [...], dimensions: [...]}. Template summarizes counts.
    "bi_schema": lambda _a, r: f"查看 {len(r.get('measures') or [])} 测度 + {len(r.get('dimensions') or [])} 维度",
    # bi-workspace-list review r2#4: kind=lookup is not LLM-eligible; without a template,
    # no finding is emitted (the "—" slot). bi_schema follows the same consolidation.
    "tool_list_workspaces": lambda _a, r: f"{r.get('total') or 0} 个可访问工作区" + (f" · 当前 {r.get('current')}" if r.get("current") else ""),
    # bi-workspace-tour: complete the switch family. Defensive reads: miss/streak-pause and other
    # ok=False shapes lack switched; fall back to generic copy.
    "tool_switch_context": lambda _a, r: (
        f"切换到 {r['switched'].get('name') or '?'}" if isinstance(r, dict) and isinstance(r.get("switched"), dict) else "切换工作区"
    ),
}


def static_finding_for(
    tool_name: str,
    args: dict[str, Any] | None,
    result: dict[str, Any] | None,
) -> str | None:
    """chat-truth Phase B.3.b: template-based static finding for non-LLM tools.

    Returns finding string (≤80 chars) for known non-eligible tools, None for
    unknown tools. Caller short-circuits LLM-path (`schedule_finding`) when
    static template matches.
    """
    # C4 consolidation (display): **historical turns always store the old names**; tool_invocations /
    # event streams are immutable history. Normalize only on read, never write back. Change the lookup function,
    # not individual table entries. See docs/design/工具改名迁移.md.
    tpl = _STATIC_FINDING_TEMPLATES.get(canonical_tool_name(tool_name))
    if tpl is None:
        return None
    try:
        s = str(tpl(args or {}, result or {})).strip()
        if not s:
            return None
        return s[:80]
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────
# fire-and-forget emit (called from subagent.py wire site)
# ─────────────────────────────────────────────────────────────────
#: I-A2 · task registry is scoped by turn_id.  A process-global flat set lets
#: Turn B observe and rethrow Turn A's durable failure during finalization.
_PENDING_FINDINGS: dict[str, set[asyncio.Task[Any]]] = {}


async def schedule_finding(
    *,
    bus: Any,  # OrchestratorBus
    turn_id: str,
    tool_name: str,
    args_text: str,
    preview_payload: dict[str, Any] | None,
    call_id: str,
    parent_agent_id: str | None,
    turn_finding_count: int,
    router: Any,
) -> bool:
    """Fire-and-forget schedule. Returns True iff a task was scheduled.

    Decisions (in order):
      1. flag disabled                → skip (metric `disabled`)
      2. category not whitelisted     → skip (metric `category`)
      3. preview_payload None         → skip (metric `no_preview`)
      4. turn cap hit                 → skip (metric `cap`)
      5. else                         → create_task, retain ref
    """
    if not tool_finding_enabled():
        emit_finding_skipped("disabled")
        return False

    kind = lookup_kind(tool_name)
    if not should_emit_finding_for_kind(kind):
        emit_finding_skipped("category")
        return False

    if preview_payload is None:
        emit_finding_skipped("no_preview")
        return False

    if turn_finding_count >= max_findings_per_turn():
        emit_finding_skipped("cap")
        return False

    req = ToolFindingRequest(
        tool_name=tool_name,
        args_text=args_text,
        preview_payload=preview_payload,
        tool_category=kind,
    )

    producer_lease = None
    if getattr(bus, "terminal_protocol_version", None) == 1:
        register_producer = getattr(bus, "register_producer", None)
        if not callable(register_producer):
            raise RuntimeError("terminal finding bus has no producer registry")
        producer_lease = await register_producer()

    task = asyncio.create_task(
        _emit_finding_async(
            req=req,
            bus=bus,
            turn_id=turn_id,
            call_id=call_id,
            parent_agent_id=parent_agent_id,
            router=router,
            producer_lease=producer_lease,
        )
    )
    if producer_lease is not None:
        producer_lease.bind_task(task)
    pending_for_turn = _PENDING_FINDINGS.setdefault(turn_id, set())
    pending_for_turn.add(task)
    return True


#: Phase A review P0-A5: per-provider rates (USD per 1k tokens).
#: Adjusted by agent_type → provider mapping. Doubao Seed Pro is default
#: (`narrative` agent_type in routing config). Values approximate 2026-Q2.
_PROVIDER_RATES_USD_PER_1K: dict[str, tuple[float, float]] = {
    # provider → (input, output)
    "volces_doubao": (0.0008, 0.0012),
    "deepseek_anthropic": (0.00027, 0.00110),
    "deepseek_v4_pro": (0.00027, 0.00110),
    "aliyun_qwen": (0.0006, 0.0012),
}
_DEFAULT_RATE_USD_PER_1K = (0.0008, 0.0012)  # safe fallback


def _estimate_cost_usd(usage: Any, provider: str | None) -> float:
    """Compute real cost from LLMResponse.usage. Falls back to 0 on missing."""
    if usage is None:
        return 0.0
    in_tok = getattr(usage, "input_tokens", 0) or 0
    out_tok = getattr(usage, "output_tokens", 0) or 0
    rate_in, rate_out = _PROVIDER_RATES_USD_PER_1K.get(
        (provider or "").strip().lower(),
        _DEFAULT_RATE_USD_PER_1K,
    )
    return (in_tok / 1000.0) * rate_in + (out_tok / 1000.0) * rate_out


async def _emit_finding_async(
    *,
    req: ToolFindingRequest,
    bus: Any,
    turn_id: str,
    call_id: str,
    parent_agent_id: str | None,
    router: Any,
    producer_lease: Any = None,
) -> None:
    """Inner coroutine. Bounded wait_for + emit on success.

    P0-A5 fix: cost derived from LLMResponse.usage, not hardcoded.
    """
    try:
        t0 = time.monotonic()
        finding, usage, provider = await generate_finding(req, router=router)
        latency_ms = (time.monotonic() - t0) * 1000.0

        if not finding:
            # generate_finding already emitted error metric on actual error;
            # nothing else to do.
            return

        # P0-A5 + P0-B-3 close: real cost from real provider + usage tokens.
        cost_usd = _estimate_cost_usd(usage, provider)

        emit_finding_ok(
            tool_name=req.tool_name,
            category=req.tool_category,
            finding_chars=len(finding),
            latency_ms=latency_ms,
            cost_usd=cost_usd,
        )

        # Import here to avoid cyclic import at module load.
        from bowhead_core.events import OrchestratorEvent

        try:
            await bus.emit(
                OrchestratorEvent(
                    event_type="tool_finding",
                    turn_id=turn_id,
                    payload={
                        "call_id": call_id,
                        "finding": finding,
                        "tool_name": req.tool_name,
                    },
                    parent_agent_id=parent_agent_id,
                )
            )
        except Exception:
            emit_finding_error("emit_fail")
    finally:
        if producer_lease is not None:
            await producer_lease.release()


async def drain_pending_findings(
    *,
    turn_id: str,
    timeout_s: float = 2.0,
) -> None:
    """Turn-finalization helper · waits for in-flight findings ≤ timeout_s.

    Pending tasks past timeout get cancelled silently. Called from
    SubagentRunner turn-cleanup path before final close.
    """
    pending = list(_PENDING_FINDINGS.get(turn_id, ()))
    if not pending:
        return
    results: list[Any] | tuple[Any, ...] = ()
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True),
            timeout=timeout_s,
        )
    except TimeoutError:
        for t in pending:
            if not t.done():
                t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        current = _PENDING_FINDINGS.get(turn_id)
        if current is not None:
            current.difference_update(pending)
            if not current:
                _PENDING_FINDINGS.pop(turn_id, None)
    for result in results:
        if isinstance(result, BaseException) and not isinstance(
            result,
            asyncio.CancelledError,
        ):
            raise result


# Test helper — clear module state between unit tests.
def _reset_for_tests() -> None:
    global _inflight_sem
    _PENDING_FINDINGS.clear()
    _inflight_sem = None  # B-4 close: reset semaphore so each test gets fresh state
