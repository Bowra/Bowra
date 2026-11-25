"""The chat reply of a Plan Turn whose results were saved as a report.

A Plan Turn runs its steps without a Lead model call after them, so the chat used to show a
fixed Chinese line ("计划执行完成:N 步成功,报告已生成在 canvas。") whatever the answer language
or the findings. One ``narrative`` call now writes a short reply from the facts of the report
and the steps; the caller appends the canonical report pointer, as for an ordinary report
Turn. Design: docs/sprints/plan-chat-reply-2026-11-24/design.md.

Every failure returns ``""`` and the caller states the plan's completion instead: a reply
that cannot be written must never hold back a finished report.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

import anyio

from bowhead_router import LLMRequest, Message

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from bowhead_router import Router

logger = logging.getLogger(__name__)

LANGUAGE_NAMES = {"zh-CN": "Simplified Chinese", "en-US": "English", "de-DE": "German", "fr-FR": "French"}

# Citation and figure markers belong to the report renderer, not to facts handed to the model.
_MARKERS = re.compile(r"\s*\[(?:\d+|cite:\d+|ref:[^\]]*|num:[^\]]*|data:[^\]]*|figure:[^\]]*|chart:[^\]]*)\]", re.IGNORECASE)
_FACTS_CAP = 12_000
_STEPS_CAP = 12
_CHAPTERS_CAP = 30
_KPI_CAP = 8

SYSTEM = (
    "You write the chat reply for a finished multi-step plan whose results were saved as a report. "
    "The user reads your reply in the chat; the report opens beside it with the full detail, Bowhead adds one sentence "
    "pointing to it after your reply, and each saved file appears as a card under the reply.\n"
    "Write 2 to 4 short sentences in {language}, at most about 120 words (200 Chinese characters): what the report covers, "
    "its one or two main conclusions, and, when a step finished only part of its work or a caveat limits the result, that limit. "
    "Use only the facts below. Copy each figure with its unit exactly as written there, without converting it, and name a "
    "file only if it is listed as saved. Plain sentences only: no headings, lists or tables, and do not point to the report "
    "or the canvas yourself."
)


def language_name(language: str) -> str:
    return LANGUAGE_NAMES.get(language) or f"the language with code {language or 'en-US'}"


def _clip(text: Any, limit: int) -> str:
    value = _MARKERS.sub("", str(text or "")).strip()
    return value if len(value) <= limit else value[:limit].rstrip() + "…"


def _field(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def _payload(item: Any) -> dict[str, Any]:
    payload = _field(item, "payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return {}
    return payload if isinstance(payload, dict) else {}


def plan_reply_facts(*, objective: str, sections: Iterable[Any], steps: Iterable[Any], files: Sequence[str]) -> str:
    """The facts the reply is written from: the request, the saved report, each step and the saved files.

    ``sections`` are this Turn's report sections (kind and payload); ``steps`` carry ``agent``,
    ``status``, ``objective`` and the step's ``payload``. Bounded; the end is cut when over the cap.
    """
    lines = [f"Request: {_clip(objective, 600)}"]
    live = [section for section in sections if not _field(section, "superseded")]
    title = next((_payload(s).get("text") for s in live if _field(s, "kind") == "h1" and _payload(s).get("text")), "")
    if title:
        lines.append(f"Report title: {_clip(title, 200)}")
    for section in live:
        if _field(section, "kind") != "kpi_row":
            continue
        cells = [cell for cell in _payload(section).get("cells") or [] if isinstance(cell, dict)][:_KPI_CAP]
        figures = [
            f"{_clip(c.get('lbl'), 60)}: {_clip(c.get('val'), 40)}" + (f" ({_clip(c.get('delta'), 40)})" if c.get("delta") else "") for c in cells
        ]
        if figures:
            lines.append("Key figures: " + "; ".join(figures))
    chapters: list[str] = []
    for section in live:
        if _field(section, "kind") != "narrative":
            continue
        payload = _payload(section)
        meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
        first = str(payload.get("markdown") or "").strip().split("\n", 1)[0]
        heading = meta.get("chapter_title") or (first.lstrip("#").strip() if first.startswith("#") else "")
        if heading and _clip(heading, 80) not in chapters:
            chapters.append(_clip(heading, 80))
    if chapters:
        lines.append("Report chapters: " + " | ".join(chapters[:_CHAPTERS_CAP]))
    lines.append("Steps:")
    for step in list(steps)[:_STEPS_CAP]:
        payload = _payload(step)
        partial = payload.get("partial_completion") if isinstance(payload.get("partial_completion"), dict) else None
        status = "finished only part of its work" if partial else str(_field(step, "status") or "")
        lines.append(f"- {_field(step, 'agent') or 'helper'} ({status}): {_clip(_field(step, 'objective'), 160)}")
        if payload.get("exec_summary"):
            lines.append(f"  Summary: {_clip(payload['exec_summary'], 700)}")
        for caveat in [c for c in payload.get("caveats") or [] if isinstance(c, str)][:3]:
            lines.append(f"  Caveat: {_clip(caveat, 200)}")
        if partial:
            lines.append(f"  Not done: {_clip(partial.get('remaining') or partial.get('stop_reason'), 300)}")
    lines.append("Files saved in this Turn: " + (", ".join(_clip(name, 120) for name in files) if files else "none"))
    facts = "\n".join(lines)
    return facts if len(facts) <= _FACTS_CAP else facts[:_FACTS_CAP].rstrip() + "…"


async def generate_plan_reply(
    router: Router | None,
    *,
    facts: str,
    language: str,
    trace_id: str = "",
    timeout_s: float = 30.0,
    usage_out: list[tuple[int, int, int]] | None = None,
) -> str:
    """Facts → the short chat reply in ``language``; ``""`` on any failure."""
    if router is None or not facts.strip():
        return ""
    name = language_name(language)
    try:
        with anyio.fail_after(timeout_s):
            response = await router.invoke(
                "narrative",
                LLMRequest(
                    model="",
                    messages=[
                        Message(role="user", content=f"{facts}\n\nWrite the reply in {name}, even if the facts above are in another language.")
                    ],
                    system=SYSTEM.format(language=name),
                    temperature=0.3,
                    max_output_tokens=400,
                    trace_id=f"plan_chat_reply:{trace_id}",
                ),
            )
    except TimeoutError:
        logger.warning("plan_chat_reply.timeout trace=%s timeout_s=%s", trace_id, timeout_s)
        return ""
    except Exception as exc:  # noqa: BLE001 - a missing reply must not hold back the report
        logger.warning("plan_chat_reply.llm_failed trace=%s error_type=%s", trace_id, type(exc).__name__)
        return ""
    usage: Any = getattr(response, "usage", None)
    if usage_out is not None and usage is not None:
        usage_out.append(
            (int(getattr(usage, "input_tokens", 0) or 0), int(getattr(usage, "output_tokens", 0) or 0), int(getattr(usage, "cached_tokens", 0) or 0))
        )
    reply = str(getattr(response, "content", "") or "").strip()
    if reply:
        logger.info("plan_chat_reply.ok trace=%s chars=%d", trace_id, len(reply))
    return reply
