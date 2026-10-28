"""Canvas section AI rewrite (P3.4) — 走 LLM Router (narrative 绑定) 的流式改写.

POST /api/v1/canvas/{cid}/sections/{sid}/rewrite (gateway 路由调本模块):
- body: { selection_text, preset|custom_prompt, source_refs?: SourceRef[] }
- response: text/event-stream
    event: chunk\\ndata: {"delta": "..."}\\n\\n
    event: done\\ndata: {"final_text": "...", "unverified_numbers": [...]}\\n\\n

不直接读 / 改 canvas section payload — frontend accept 后再 PATCH 到 store.

模型选择: 由**团队 router** 的 `narrative` 绑定决定(primary + fallback + 熔断),
不在本模块写死 provider/model. 团队未配置 → gateway 在 StreamingResponse 前返
409/503 (A0-1), 本模块的 router=None 分支仅作防御.
report-refactor WP3 (2026-06-13): replaced direct DashScope requests using a hard-coded
qwen-plus model with router.stream("narrative"), sharing configured routing and cross-provider
fallback with other agent calls.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import TYPE_CHECKING, Any

from bowhead_core.citation_marks import CITE_MARKER_RE, NUMBERED_RE

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from bowhead_router import Router

logger = logging.getLogger(__name__)

# Router agent binding for narrative rewriting (config/routing.*.yaml: agents.narrative).
_REWRITE_AGENT = "narrative"

_PRESETS: dict[str, str] = {
    "concise": "把以下文字改写得更简洁, 只压缩表达, 不改变事实、数字、结论或引用.",
    "professional": "把以下文字改写得更专业 / 商务化, 只优化表达, 不改变事实、数字、结论或引用.",
    "grammar": "仅修正语法、错别字和标点, 不改变语义、事实、数字、结论或引用.",
    "different_angle": "在相同事实范围内换一个表达角度重述, 不新增事实、数字、例子、结论或引用.",
}

# Current numbered sources and turn-scoped chart/source references both survive rewriting.
# Source-linked numeric adjacency follows the frontend citation plugin. Spell
# out ASCII word boundaries: Python's Unicode \w would exclude adjacent CJK text.
_CITED_NUM_RE = re.compile(r"(?<![A-Za-z0-9_.])([+\-−]?[0-9][0-9,，.]*(?:e[+-]?[0-9]+)?(?:\s?[^\s\[\]]{1,4})?)\s\[([0-9]{1,3})\]")  # noqa: RUF001 regex intentionally includes Unicode minus and fullwidth comma
_BARE_YEAR_RE = re.compile(r"^[+\-−]?(19|20)\d{2}\s?年?$")  # noqa: RUF001 regex intentionally includes Unicode minus
_CUSTOM_PROMPT_MAX = 1000
# Literal comparison only: keep decimal/group separators, signs and common scale
# suffixes intact. This is not numeric parsing, conversion or source verification.
_NUMERIC_TOKEN_RE = re.compile(
    r"[+\-\u2212]?\d+(?:[.,\uff0c'\u2019\u00a0\u202f]\d+| \d{3}(?!\d))*(?:[eE][+\-]?\d+)?"
    r"(?:[ \u00a0\u202f]?(?:[%\uff05\u2030]|[\u4e07\u4ebf\u5343\u5146]|[KMBT](?![A-Za-z])|pp\b))?"
)


def validate_rewrite_request(preset: str | None, custom_prompt: str | None) -> None:
    """Validate the P1-B rewrite contract before starting an SSE response."""
    if custom_prompt and custom_prompt.strip():
        return
    if preset is None or preset in _PRESETS:
        return
    raise ValueError(f"unsupported rewrite preset: {preset}")


def _safety_suffix() -> str:
    return (
        "安全边界: 只能改写给定文字的表达方式; 不得新增、删除或替换事实、数字、"
        "SQL、图表/table 内容、案例、结论、source_refs 或 citation. "
        "所有原文中的引用角标 [N] 必须原样保留且数量不变, 它锚定的数字也不得改动. "
        "如果原文没有引用角标, 输出不得新增任何引用角标. "
        "Preserve [cite:N] chart/source references exactly as well. Keep the original language unless translation is explicitly requested. "
        "Expansion may explain only the supplied content; it cannot introduce new facts."
    )


def _resolve_prompt(preset: str | None, custom_prompt: str | None) -> str:
    if custom_prompt:
        return f"{custom_prompt.strip()[:_CUSTOM_PROMPT_MAX]}\n\n{_safety_suffix()}"
    if preset and preset in _PRESETS:
        return f"{_PRESETS[preset]}\n\n{_safety_suffix()}"
    validate_rewrite_request(preset, custom_prompt)
    return f"{_PRESETS['concise']}\n\n{_safety_suffix()}"


def _build_messages(
    selection_text: str,
    instruction: str,
    source_refs: list[dict[str, Any]] | None,
) -> list[dict[str, str]]:
    sys = (
        "你是 Bowhead 报告改写助手. "
        "用户给一段报告文字 + 指令, 你按指令改写. "
        "**强约束**: 输出**只**包含改写后的纯文本, 不加引号 / 解释 / 前后缀. "
        "不得新增、删除、替换或重排事实、数字、SQL、图表/table 内容、source_refs 或 citation. "
        "引用角标 [N] 必须**原样保留**, 出现次数与原文一致; 角标前锚定的数字（如 `9627万元 [1]`）"
        "本体也不得改动——改了会让「已验证」视觉套在错数字上. "
        "即使用户要求补数据或补引用, 也只能改写表达, 不能创造新数字或新 citation."
    )
    user_parts: list[str] = []
    user_parts.append(f"**指令**: {instruction}")
    if source_refs:
        try:
            ref_summary = json.dumps(source_refs[:5], ensure_ascii=False)[:1500]
            user_parts.append(f"**可用 source_refs (只用于理解已有引用, 不得据此新增 citation)**: {ref_summary}")
        except Exception:  # noqa: S110
            pass
    user_parts.append(f"**待改写原文**:\n{selection_text}")
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def _citation_token_counts(text: str) -> Counter[str]:
    return Counter(m.group(0) for pattern in (NUMBERED_RE, CITE_MARKER_RE) for m in pattern.finditer(text or ""))


def _detect_numeric_drift(original: str, rewritten: str) -> list[str]:
    """Flag changed literal numeric tokens, including uncited values and dates."""

    def tokens(text: str) -> Counter[str]:
        prose = CITE_MARKER_RE.sub("", NUMBERED_RE.sub("", text or ""))
        return Counter(match.group(0) for match in _NUMERIC_TOKEN_RE.finditer(prose))

    before, after = tokens(original), tokens(rewritten)
    return sorted(set((after - before) + (before - after)))


def _detect_citation_drift(original: str, rewritten: str) -> list[str]:
    """Return citation tokens added or removed by rewrite output."""
    before = _citation_token_counts(original)
    after = _citation_token_counts(rewritten)
    drift: list[str] = []
    for token, count in (after - before).items():
        drift.extend([token] * count)
    for token, count in (before - after).items():
        drift.extend([token] * count)
    return drift


def _cited_values(text: str) -> dict[str, Counter[str]]:
    """Count each numeric phrase per source, excluding bare-year claim citations."""
    out: dict[str, Counter[str]] = {}
    for m in _CITED_NUM_RE.finditer(text or ""):
        tok = m.group(1).strip()
        if _BARE_YEAR_RE.match(tok):
            continue
        out.setdefault(m.group(2), Counter())[tok] += 1
    return out


def _detect_value_drift(original: str, rewritten: str) -> list[str]:
    """Flag changed numeric phrases per source, even when citation counts match.

    These are review warnings, not verification or an instruction to rewrite
    references. Numeric source-link styling does not establish factual truth.
    """
    before = _cited_values(original)
    after = _cited_values(rewritten)
    drift: list[str] = []
    for n in set(before) | set(after):
        b = before.get(n, Counter())
        a = after.get(n, Counter())
        for v in a - b:
            drift.append(f"{v} [{n}]")  # Value now anchored by [N], absent from the original
        for v in b - a:
            drift.append(f"{v} [{n}]")  # Originally anchored value removed/changed by the rewrite
    return drift


async def stream_rewrite(
    *,
    router: Router | None,
    selection_text: str,
    preset: str | None = None,
    custom_prompt: str | None = None,
    source_refs: list[dict[str, Any]] | None = None,
) -> AsyncIterator[bytes]:
    """SSE generator. 每 chunk yield bytes (event: chunk / data: {delta}).

    最后 yield event: done with final_text + unverified_numbers.
    出错: yield event: error / data: {error}, 然后停.

    模型 = router 的 `narrative` 绑定 (config/routing.*.yaml). router 为 None
    (未配置 / mock 关) 时返 error 事件.
    """
    if router is None:
        # Defensive branch: the gateway resolves the team router before returning StreamingResponse (A0-1);
        # this is normally unreachable.
        msg = json.dumps(
            {"error": "当前团队未配置模型，无法使用 AI 改写", "code": "team_model_unconfigured"},
            ensure_ascii=False,
        )
        yield f"event: error\ndata: {msg}\n\n".encode()
        return

    # Import bowhead_router at runtime to avoid a hard top-level dependency on router package load order.
    from bowhead_core.output_recovery import output_exhausted
    from bowhead_router import LLMRequest, LLMResponse, Message

    instruction = _resolve_prompt(preset, custom_prompt)
    msg_dicts = _build_messages(selection_text, instruction, source_refs)

    # _build_messages produces [{role:system,...},{role:user,...}]; split into LLMRequest.system
    # and user Message(s) for the router (its contract gives system a separate field).
    system_content = next((m["content"] for m in msg_dicts if m["role"] == "system"), None)
    user_messages = [
        Message(role=m["role"], content=m["content"])  # type: ignore[arg-type]
        for m in msg_dicts
        if m["role"] != "system"
    ]
    req = LLMRequest(
        model="",  # Empty → router selects the narrative binding
        messages=user_messages,
        system=system_content,
        temperature=0.4,
        max_output_tokens=None,
        stream=True,
        trace_id="canvas_rewrite",
    )

    accumulated = ""
    response = LLMResponse(content="")
    try:
        async for chunk in router.stream(_REWRITE_AGENT, req):
            if chunk.finish_reason is not None:
                response.finish_reason = chunk.finish_reason
            if chunk.diagnostics is not None:
                response.diagnostics = chunk.diagnostics
            delta = chunk.delta  # Separate thinking_delta stream; rewrite forwards only content delta
            if not delta:
                continue
            accumulated += delta
            chunk_data = json.dumps({"delta": delta}, ensure_ascii=False)
            yield f"event: chunk\ndata: {chunk_data}\n\n".encode()
    except Exception:
        # A0-1 (report-edit-audit P0-3): do not expose exception class names/internal implementation to users. Previously,
        # raw `KeyError: "No binding ..."` appeared in the error banner. Details go only to logs (already redacted
        # by logging); SSE error frames provide a normalized code and readable message.
        logger.exception("rewrite stream failed")
        err_msg = json.dumps(
            {"error": "改写服务暂时不可用，请稍后重试", "code": "rewrite_upstream_failed"},
            ensure_ascii=False,
        )
        yield f"event: error\ndata: {err_msg}\n\n".encode()
        return

    if output_exhausted(response):
        err_msg = json.dumps(
            {"error": "The rewrite exceeded the model output limit. Select a shorter passage and try again.", "code": "rewrite_output_exhausted"}
        )
        yield f"event: error\ndata: {err_msg}\n\n".encode()
        return

    final_text = accumulated.strip()
    # Keep both current citation families and uncited numbers under review. The
    # existing warning is advisory, not a claim of independent fact verification.
    unverified: list[str] = []
    citation_drift = _detect_citation_drift(selection_text, final_text)
    value_drift = _detect_value_drift(selection_text, final_text)
    if citation_drift:
        unverified.extend(citation_drift)
    if value_drift:
        unverified.extend(value_drift)
    unverified.extend(_detect_numeric_drift(selection_text, final_text))
    done_data = json.dumps(
        {
            "final_text": final_text,
            "unverified_numbers": unverified,
            "citation_drift": citation_drift,
            "value_drift": value_drift,
        },
        ensure_ascii=False,
    )
    yield f"event: done\ndata: {done_data}\n\n".encode()
