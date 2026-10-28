"""Phase 8b-3 tests: clarification.ask sentinel-based tool."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from bowhead_core.agents.clarification_tool import (
    SENTINEL_CLARIFICATION_PENDING,
    _clarification_ask,
    is_clarification_sentinel,
    make_clarification_sentinel,
)


@dataclass
class _StubCtx:
    turn_id: str = "t_test"


def test_sentinel_key_is_special() -> None:
    """Sentinel key starts with underscore (avoids collision with user data)."""
    assert SENTINEL_CLARIFICATION_PENDING == "_clarification_pending"


def test_make_sentinel_includes_required_fields() -> None:
    s = make_clarification_sentinel(
        candidates=["a", "b"],
        fallback_text=None,
        aborted_turn_id="t_x",
    )
    assert s[SENTINEL_CLARIFICATION_PENDING] is True
    assert s["candidates"] == ["a", "b"]
    assert s["aborted_turn_id"] == "t_x"
    assert isinstance(s["clarification_id"], str)
    assert len(s["clarification_id"]) > 8  # uuid


def test_is_clarification_sentinel_detects_dict() -> None:
    s = make_clarification_sentinel(
        candidates=["x"],
        fallback_text=None,
        aborted_turn_id="t1",
    )
    assert is_clarification_sentinel(s)


def test_is_clarification_sentinel_rejects_non_dict() -> None:
    assert not is_clarification_sentinel("not a dict")
    assert not is_clarification_sentinel(None)
    assert not is_clarification_sentinel({"some": "other_dict"})


@pytest.mark.asyncio
async def test_clarification_ask_returns_sentinel() -> None:
    """Tool handler returns sentinel dict (NOT raises exception per F3)."""
    ctx = _StubCtx(turn_id="t_test_123")
    result = await _clarification_ask(
        {
            "candidates": ["4月营业额", "各品牌排名", "GMV 同比"],
            "fallback_text": None,
        },
        ctx,
    )
    assert is_clarification_sentinel(result)
    assert result["candidates"] == ["4月营业额", "各品牌排名", "GMV 同比"]
    assert result["aborted_turn_id"] == "t_test_123"


@pytest.mark.asyncio
async def test_clarification_ask_refused_on_resume() -> None:
    """Same-turn resume guard: when ctx._resume_turn_id is set (the user already answered a
    clarification in THIS turn), the handler returns a NON-sentinel 'proceed' result so the Lead
    loop keeps running and finishes the task — no second clarification round (prod s_33399c04309a
    'card reappeared'). Round 1 (no _resume_turn_id) is unaffected — see the test below."""
    from types import SimpleNamespace

    ctx = SimpleNamespace(turn_id="t_resume", _resume_turn_id="t_orig", clarification_store=None)
    result = await _clarification_ask(
        {"questions": [{"question": "海报主题?", "options": ["产品推广", "品牌宣传"]}]},
        ctx,
    )
    assert is_clarification_sentinel(result) is False  # NOT a sentinel → loop continues, no card
    assert result.get("clarification_refused_on_resume") is True


@pytest.mark.asyncio
async def test_clarification_ask_first_round_unaffected() -> None:
    """The resume guard must NOT touch the first clarification round (no _resume_turn_id)."""
    from types import SimpleNamespace

    ctx = SimpleNamespace(turn_id="t1", clarification_store=None)
    result = await _clarification_ask({"candidates": ["a", "b"], "fallback_text": None}, ctx)
    assert is_clarification_sentinel(result) is True  # round 1 still clarifies normally


@pytest.mark.asyncio
async def test_clarification_ask_empty_candidates_uses_fallback_text() -> None:
    """S8(b): empty candidates → fallback_text auto-injected (no empty card)."""
    ctx = _StubCtx()
    result = await _clarification_ask({"candidates": []}, ctx)
    assert is_clarification_sentinel(result)
    assert result["candidates"] == []
    assert result["fallback_text"] == "您的问题我没听明白, 能换个问法吗?"


@pytest.mark.asyncio
async def test_clarification_ask_truncates_candidates() -> None:
    """Max 5 candidates per schema; extra dropped."""
    ctx = _StubCtx()
    result = await _clarification_ask(
        {"candidates": [f"c{i}" for i in range(8)]},
        ctx,
    )
    assert len(result["candidates"]) == 5
    assert result["candidates"][0] == "c0"
    assert result["candidates"][4] == "c4"


@pytest.mark.asyncio
async def test_clarification_ask_handles_invalid_candidate_types() -> None:
    """Defensive: non-string candidates filtered."""
    ctx = _StubCtx()
    result = await _clarification_ask(
        {"candidates": ["valid", 123, None, "valid2"]},
        ctx,
    )
    assert result["candidates"] == ["valid", "valid2"]


@pytest.mark.asyncio
async def test_clarification_ask_handles_long_candidate_truncated() -> None:
    """Per schema maxLength=80; tool truncates."""
    ctx = _StubCtx()
    long_str = "A" * 200
    result = await _clarification_ask({"candidates": [long_str]}, ctx)
    assert len(result["candidates"][0]) == 80


# ─── hitl-fix sprint B1: ClarificationStore persistence ──────────────────


@dataclass
class _StubCtxWithStore:
    """Extended stub matching ToolExecutionContext fields used by B1."""

    turn_id: str = "t_b1_test"
    user_id: str = "u_test"
    workspace_id: str = "ws_test"
    session_id: str = "s_b1_test"
    clarification_store: object = None  # set by tests


class _RecordingStore:
    """Captures put() calls for assertion. Returns a real ClarificationRequest."""

    def __init__(self) -> None:
        self.put_calls: list[dict] = []

    async def put(self, **kwargs):  # type: ignore[no-untyped-def]
        import uuid as _uuid

        from bowhead_core.clarification_store import ClarificationRequest

        self.put_calls.append(kwargs)
        return ClarificationRequest(
            request_id=str(_uuid.uuid4()),
            turn_id=kwargs["turn_id"],
            user_id=kwargs["user_id"],
            workspace_id=kwargs["workspace_id"],
            token=kwargs["token"],
            rationale=kwargs["rationale"],
            candidates=kwargs["candidates"],
            kind=kwargs.get("kind", "token"),
            multi_select=kwargs.get("multi_select", False),
            session_id=kwargs.get("session_id"),
        )


@pytest.mark.asyncio
async def test_reference_lead_defers_card_write_until_orchestrator_checks_actions() -> None:
    store = _RecordingStore()
    ctx = _StubCtxWithStore(clarification_store=store)
    ctx._reference_lead_runtime = object()
    result = await _clarification_ask({"candidates": ["甲", "乙"]}, ctx)
    assert is_clarification_sentinel(result)
    # The orchestrator owns this write and assigns the id; no throwaway uuid.
    assert result["store_persisted"] == "deferred"
    assert result["clarification_id"] is None
    assert store.put_calls == []


@pytest.mark.asyncio
async def test_bounded_card_sentinel_carries_no_throwaway_id(monkeypatch) -> None:
    async def _available(_ctx, **_kwargs) -> bool:
        return True

    monkeypatch.setattr("bowhead_core.clarification_rounds.available", _available)
    store = _RecordingStore()
    ctx = _StubCtxWithStore(clarification_store=store)
    result = await _clarification_ask(
        {"questions": [{"question": "Which period?", "options": ["Q1", "Q2"], "type": "single_select"}]},
        ctx,
    )
    assert is_clarification_sentinel(result)
    assert result["clarification_id"] is None
    assert result["store_persisted"] == "deferred"
    assert store.put_calls == []


@pytest.mark.asyncio
async def test_more_than_four_options_is_rejected_with_the_limit_and_overflow() -> None:
    """Options used to be cut to 4 silently; the model never learned what was dropped."""
    store = _RecordingStore()
    ctx = _StubCtxWithStore(clarification_store=store)
    options = ["华东", "华南", "华北", "西南", "西北"]
    result = await _clarification_ask(
        {"questions": [{"question": "看哪个区域?", "options": options, "type": "single_select"}]},
        ctx,
    )
    assert not is_clarification_sentinel(result)
    assert result["ok"] is False
    assert result["error_kind"] == "schema_invalid"
    assert result["max_options_per_question"] == 4
    assert result["questions_over_limit"] == [{"question_index": 0, "question": "看哪个区域?", "option_count": 5, "rejected_options": ["西北"]}]
    assert "at most 4 options" in result["error_message"]
    assert "西北" in result["error_message"]
    assert store.put_calls == []


@pytest.mark.asyncio
async def test_four_options_are_accepted_unchanged() -> None:
    ctx = _StubCtx(turn_id="t_four")
    options = ["华东", "华南", "华北", "西南"]
    result = await _clarification_ask({"questions": [{"question": "看哪个区域?", "options": options}]}, ctx)
    assert is_clarification_sentinel(result)
    assert result["questions"][0]["options"] == options


@pytest.mark.asyncio
async def test_clarification_ask_persists_to_store_when_available() -> None:
    """B1 fix: when ctx.clarification_store present, sentinel uuid MUST be
    persisted so FE POST /resolve can find the row (prod evidence t_441f3758)."""
    store = _RecordingStore()
    ctx = _StubCtxWithStore(clarification_store=store)
    result = await _clarification_ask(
        {"candidates": ["4月营业额", "各品牌排名", "GMV 同比"]},
        ctx,
    )
    assert is_clarification_sentinel(result)
    # Exactly one put() call
    assert len(store.put_calls) == 1
    call = store.put_calls[0]
    # Persisted shape matches sentinel
    assert call["turn_id"] == "t_b1_test"
    assert call["user_id"] == "u_test"
    assert call["workspace_id"] == "ws_test"
    assert call["session_id"] == "s_b1_test"
    assert call["kind"] == "ambiguous_turn"
    assert call["multi_select"] is False
    assert len(call["candidates"]) == 3
    assert call["candidates"][0].value == "4月营业额"
    assert call["candidates"][0].kind == "dim_value"
    # sentinel clarification_id == store request_id (round-trip property)
    # NOTE: persisted_request_id is wired into make_clarification_sentinel via
    # clarification_id= param; verify the SSE-bound uuid matches what
    # /resolve will lookup.


@pytest.mark.asyncio
async def test_clarification_ask_no_store_falls_back_to_local_uuid() -> None:
    """B1 backward-compat: ctx without clarification_store → sentinel still
    generated (with local uuid), tool doesn't crash. FE will 404 but that's
    expected when gateway lacks store wiring (e.g. test contexts)."""
    ctx = _StubCtxWithStore(clarification_store=None)
    result = await _clarification_ask(
        {"candidates": ["a", "b"]},
        ctx,
    )
    assert is_clarification_sentinel(result)
    assert isinstance(result["clarification_id"], str)
    assert len(result["clarification_id"]) > 8  # uuid format


@pytest.mark.asyncio
async def test_clarification_ask_store_failure_does_not_crash() -> None:
    """B1 defensive: store.put() raises → sentinel still returns (logged).
    User sees options but click will 404 — degraded but not crashed turn."""

    class _FailingStore:
        async def put(self, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("simulated DB write failure")

    ctx = _StubCtxWithStore(clarification_store=_FailingStore())
    result = await _clarification_ask({"candidates": ["x"]}, ctx)
    assert is_clarification_sentinel(result)
    assert "clarification_id" in result  # local uuid fallback


# ─────────────────────────────────────────────────────────────
# connector-context T1#6: forward tool discriminator through extract's three branches
# ─────────────────────────────────────────────────────────────


def test_extract_forwards_tool_from_shape2() -> None:
    """Shape-2 clarification.tool 保真透传（修前第一跳即丢，红）."""
    from bowhead_core.agents.clarification_tool import (
        extract_clarification_event_payload,
    )

    sent = {
        "ok": False,
        "error_kind": "needs_clarification",
        "clarification": {
            "kind": "tool_context_select",
            "tool": "pbi",
            "prompt": "选一个",
            "options": [{"value": "w1", "label": "仓库一"}],
            "remember_option": True,
        },
        "rows": [],
    }
    p = extract_clarification_event_payload(sent)
    assert p["tool"] == "pbi"


def test_extract_tool_none_for_shape1() -> None:
    """Shape-1（clarification_ask generic 卡）无 connector 语义 → tool=None."""
    from bowhead_core.agents.clarification_tool import (
        extract_clarification_event_payload,
        make_clarification_sentinel,
    )

    s = make_clarification_sentinel(
        candidates=["a"],
        fallback_text=None,
        aborted_turn_id="t_x",
    )
    p = extract_clarification_event_payload(s)
    assert p["tool"] is None


def test_extract_tool_none_for_non_dict() -> None:
    """非 dict 输入分支同样带 tool 键（防消费侧 KeyError）."""
    from bowhead_core.agents.clarification_tool import (
        extract_clarification_event_payload,
    )

    p = extract_clarification_event_payload("not a dict")
    assert p["tool"] is None


@pytest.mark.asyncio
async def test_option_limit_does_not_apply_to_free_text_questions() -> None:
    ctx = _StubCtx(turn_id="t_free")
    result = await _clarification_ask(
        {
            "questions": [
                {"question": "金额是多少?", "options": ["1", "2", "3", "4", "5"], "type": "free_text"},
                {"question": "看哪个区域?", "options": ["华东", "华南"], "type": "single_select"},
            ]
        },
        ctx,
    )
    assert result.get("error_kind") != "schema_invalid"
    assert is_clarification_sentinel(result)
