"""V3.5 PRD-C · NL memory intent classifier (per-turn, fire-and-forget).

Detects user expressing memory intent in chat ("记住我老板叫张总") and proposes
a Note candidate. Falls back to non-detection on LLM error.

Pattern follows bowhead_core.meta_intent (LLM-driven binary classifier with
JSON output + fallback).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are a memory intent classifier for Bowhead.

Decide whether the user's turn expresses **memory intent** — wanting the AI to
remember a preference, fact, or constraint long-term.

Examples that ARE memory intent:
- "记住我老板叫张总"
- "以后都用正式书面语"
- "GMV 在我们公司指交易额"
- "Don't use emojis in reports"
- "remember that we're audited by KPMG"

Examples that are NOT memory intent:
- "今年 GMV 多少" (one-shot query)
- "记得我们上次说啥" (recall, not remember)
- "what did we discuss last time" (recall)
- "show me Q3 numbers" (data query)

Output JSON only, no prose:
{
  "detected": true|false,
  "proposed_content": "string ≤200 char describing what to remember",
  "kind": "preference|constraint|note",
  "confidence": 0.0-1.0
}

If detected=false, all other fields may be omitted.
"""


@dataclass
class NLMemoryIntent:
    detected: bool
    proposed_content: str | None = None
    kind: str = "note"
    confidence: float = 0.0


class NLMemoryIntentClassifier:
    """V3.5 default agent_type='lead' for simplicity. V4+ add dedicated 'classifier'
    agent_type with cheap model routing (qwen-turbo / haiku) for cost optimization.
    """

    def __init__(self, router: Any, agent_type: str = "echo") -> None:  # Tier routing: memory-intent classification → fast tier.
        self._router = router
        self._agent_type = agent_type

    async def classify(self, user_text: str, *, router: Any | None = None) -> NLMemoryIntent:
        """``router``：本 turn 生效的 Router（credential-readiness D2.1）。缺省回落构造期实例。"""
        if not user_text or not user_text.strip():
            return NLMemoryIntent(detected=False)
        # Truncate user_text to prevent prompt-injection via massive input
        truncated = user_text[:1000]

        try:
            from bowhead_router import LLMRequest, Message
        except ImportError:
            return NLMemoryIntent(detected=False)

        # Stable hashlib for trace_id (Python builtin hash() salts per-process)
        trace_hash = hashlib.sha256(user_text.encode("utf-8")).hexdigest()[:8]

        req = LLMRequest(
            model="",
            messages=[Message(role="user", content=truncated)],
            system=_SYSTEM_PROMPT,
            temperature=0.0,
            max_output_tokens=300,  # JSON wrapper + 200-char content + headroom
            trace_id=f"nl_intent:{trace_hash}",
        )
        try:
            resp = await (router or self._router).invoke(self._agent_type, req)
        except Exception as e:
            logger.warning("nl_memory_intent.llm_failed err=%s", e)
            return NLMemoryIntent(detected=False)

        return _parse(resp.content)


def _parse(content: str) -> NLMemoryIntent:
    if not content:
        return NLMemoryIntent(detected=False)
    text = content.strip()
    # Strip markdown code fences if present
    for fence in ("```json", "```"):
        if text.startswith(fence):
            text = text[len(fence) :].strip()
        if text.endswith("```"):
            text = text[:-3].strip()
    # Find first { (LLM may pre-amble despite "JSON only" instruction)
    brace = text.find("{")
    if brace > 0:
        text = text[brace:]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return NLMemoryIntent(detected=False)
    if not isinstance(obj, dict):
        return NLMemoryIntent(detected=False)
    detected = bool(obj.get("detected"))
    if not detected:
        return NLMemoryIntent(detected=False)
    proposed = str(obj.get("proposed_content") or "").strip()[:200]
    if not proposed:
        return NLMemoryIntent(detected=False)
    kind = str(obj.get("kind") or "note").strip()
    if kind not in ("preference", "constraint", "note"):
        kind = "note"
    try:
        confidence = float(obj.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return NLMemoryIntent(
        detected=True,
        proposed_content=proposed,
        kind=kind,
        confidence=max(0.0, min(1.0, confidence)),
    )
