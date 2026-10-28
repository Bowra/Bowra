"""V3.5 PRD-C · NLMemoryIntentClassifier unit tests."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from bowhead_core.nl_memory_intent import (
    NLMemoryIntentClassifier,
    _parse,
)

# ─── _parse JSON robustness ───────────────────────────


def test_parse_detected_positive():
    raw = json.dumps(
        {
            "detected": True,
            "proposed_content": "test prefer",
            "kind": "preference",
            "confidence": 0.9,
        }
    )
    out = _parse(raw)
    assert out.detected is True
    assert out.proposed_content == "test prefer"
    assert out.kind == "preference"
    assert 0.89 < out.confidence < 0.91


def test_parse_detected_negative():
    out = _parse(json.dumps({"detected": False}))
    assert out.detected is False


def test_parse_strips_code_fence():
    raw = '```json\n{"detected": true, "proposed_content": "x", "kind": "note"}\n```'
    out = _parse(raw)
    assert out.detected is True


def test_parse_strips_pre_amble():
    raw = 'Here is the result: {"detected": true, "proposed_content": "x", "kind": "note"}'
    out = _parse(raw)
    assert out.detected is True


def test_parse_invalid_json_returns_undetected():
    out = _parse("not json")
    assert out.detected is False


def test_parse_empty_returns_undetected():
    assert _parse("").detected is False


def test_parse_caps_proposed_content_at_200():
    long_content = "x" * 500
    raw = json.dumps({"detected": True, "proposed_content": long_content})
    out = _parse(raw)
    assert out.detected is True
    assert len(out.proposed_content) == 200


def test_parse_invalid_kind_falls_back_to_note():
    raw = json.dumps({"detected": True, "proposed_content": "x", "kind": "weird"})
    assert _parse(raw).kind == "note"


def test_parse_clamps_confidence():
    raw = json.dumps({"detected": True, "proposed_content": "x", "confidence": 99})
    assert _parse(raw).confidence == 1.0
    raw_neg = json.dumps({"detected": True, "proposed_content": "x", "confidence": -5})
    assert _parse(raw_neg).confidence == 0.0


def test_parse_detected_but_empty_content_returns_undetected():
    raw = json.dumps({"detected": True, "proposed_content": "  "})
    assert _parse(raw).detected is False


# ─── classify with mock router ────────────────────────


@dataclass
class _FakeResp:
    content: str


class _FakeRouter:
    def __init__(self, response: str = "", raise_on_invoke: bool = False):
        self._response = response
        self._raise = raise_on_invoke
        self.calls: list = []

    async def invoke(self, agent_type, req):
        self.calls.append((agent_type, req))
        if self._raise:
            raise RuntimeError("router crashed")
        return _FakeResp(content=self._response)


@pytest.mark.asyncio
async def test_classify_empty_text_skips_llm():
    router = _FakeRouter()
    c = NLMemoryIntentClassifier(router=router)
    out = await c.classify("")
    assert out.detected is False
    assert router.calls == []  # no LLM call


@pytest.mark.asyncio
async def test_classify_whitespace_only_skips():
    router = _FakeRouter()
    c = NLMemoryIntentClassifier(router=router)
    out = await c.classify("   \n  ")
    assert out.detected is False
    assert router.calls == []


@pytest.mark.asyncio
async def test_classify_router_error_falls_back():
    router = _FakeRouter(raise_on_invoke=True)
    c = NLMemoryIntentClassifier(router=router)
    out = await c.classify("记住我老板叫张总")
    assert out.detected is False  # graceful


@pytest.mark.asyncio
async def test_classify_returns_parsed_result():
    payload = json.dumps(
        {
            "detected": True,
            "proposed_content": "用户老板叫张总",
            "kind": "note",
            "confidence": 0.85,
        }
    )
    router = _FakeRouter(response=payload)
    c = NLMemoryIntentClassifier(router=router)
    out = await c.classify("记住我老板叫张总")
    assert out.detected is True
    assert "张总" in out.proposed_content
    assert out.kind == "note"


@pytest.mark.asyncio
async def test_classify_truncates_long_input():
    """Inputs > 1000 char are truncated before sending to LLM (defense in depth)."""
    payload = json.dumps({"detected": False})
    router = _FakeRouter(response=payload)
    c = NLMemoryIntentClassifier(router=router)
    long_text = "x" * 5000
    await c.classify(long_text)
    # Verify the request content was truncated to 1000
    assert len(router.calls) == 1
    _, req = router.calls[0]
    assert len(req.messages[0].content) <= 1000


@pytest.mark.asyncio
async def test_classify_uses_stable_trace_id():
    """Same user_text → same trace_id (CTO r10 P1: hashlib not Python builtin hash)."""
    payload = json.dumps({"detected": False})
    r1 = _FakeRouter(response=payload)
    r2 = _FakeRouter(response=payload)
    text = "记住这个偏好"
    c1 = NLMemoryIntentClassifier(router=r1)
    c2 = NLMemoryIntentClassifier(router=r2)
    await c1.classify(text)
    await c2.classify(text)
    assert r1.calls[0][1].trace_id == r2.calls[0][1].trace_id


# ─── candidates kind 'note' supported ────────────────


@pytest.mark.asyncio
async def test_candidates_enqueue_kind_note(tmp_path):
    from bowhead_memory.candidates import MemoryCandidateStore

    store = MemoryCandidateStore(db_path=str(tmp_path / "c.db"))
    await store.initialize()
    cand = await store.enqueue(
        user_id="u1",
        kind="note",
        key="",
        value="用户偏好按渠道拆 GMV",
        reason=None,
        source_turn_id=None,
    )
    assert cand.kind == "note"


@pytest.mark.asyncio
async def test_candidates_enqueue_if_new_dedupes(tmp_path):
    from bowhead_memory.candidates import MemoryCandidateStore

    store = MemoryCandidateStore(db_path=str(tmp_path / "c.db"))
    await store.initialize()
    a = await store.enqueue_if_new(
        user_id="u1",
        kind="note",
        key="",
        value="alpha",
        reason=None,
        source_turn_id="t_dedupe",
    )
    assert a is not None
    b = await store.enqueue_if_new(
        user_id="u1",
        kind="note",
        key="",
        value="alpha",
        reason=None,
        source_turn_id="t_dedupe",
    )
    assert b is None  # dedupe within window
