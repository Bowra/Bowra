"""chat-v4 CTO P1-C fix UT — assert 4 new EventType literals registered.

CTO finding on commit 191fb98: the 4 chat-v4 event literals (`turn.plan`,
`phase.update`, `lead.subagent_summary`, `lead.final.payload`) were added
to `bowhead_core.events.EventType` but no test pinned them. A typo or
accidental removal during refactor (e.g. `turn_plan` instead of `turn.plan`)
would be caught only when FE wire-up surfaces the gap.

This test uses `typing.get_args(EventType)` to walk the Literal members and
asserts each new literal is present.
"""

from __future__ import annotations

from typing import get_args

from bowhead_core.events import EventType

_V4_EVENT_LITERALS = (
    # Sprint phase-plan-removal: "turn.plan" + "phase.update" removed.
    "lead.subagent_summary",
    "lead.final.payload",
)

# Sprint chat-redesign-2026-05-21 Phase 1.1 — Step List Card events
_CHAT_REDESIGN_EVENT_LITERALS = (
    "step_started",
    "step_progress",
    "step_done",
    "step_list_card_created",
    "step_list_card_finalized",
    "emoji_ack",
    # chat-solidify 2026-07-03: dead emoji_remove / plan_announced removed from EventType.
    # Phase 1.6 G3 — Lead narrative streaming
    "narrative.delta",
)


def test_all_chat_redesign_event_literals_registered() -> None:
    """Phase 1.1 step_*/emoji_*/card_* events MUST appear in EventType."""
    members = set(get_args(EventType))
    missing = [lit for lit in _CHAT_REDESIGN_EVENT_LITERALS if lit not in members]
    assert not missing, f"chat-redesign EventType literals missing: {missing}. Available: {sorted(members)}"


def test_chat_redesign_payload_dataclasses_importable() -> None:
    """Phase 1.1 dataclasses are importable + constructible (smoke)."""
    from bowhead_core.events import (
        EmojiAckPayload,
        NarrativeDeltaPayload,
        PlanItem,
        StepDonePayload,
        StepError,
        StepListCardCreatedPayload,
        StepListCardFinalizedPayload,
        StepProgressPayload,
        StepStartedPayload,
        SubStepResult,
    )

    # Minimal construction (required fields only)
    err = StepError(type="timeout", message_zh="超时", is_transient=True)
    assert err.is_transient
    sub = SubStepResult(label="SELECT 1", state="success")
    assert sub.label == "SELECT 1"
    started = StepStartedPayload(
        card_id="t_1",
        step_id="s_1",
        step_type="tool_run",
        header_zh="查询数据",
        started_at="2026-05-22T00:00:00Z",
    )
    assert started.concurrency_group_id is None  # default
    prog = StepProgressPayload(card_id="t_1", step_id="s_1", content_delta="{")
    assert prog.percent is None
    done = StepDonePayload(
        card_id="t_1",
        step_id="s_1",
        step_type="tool_run",
        state="success",
        duration_ms=42,
    )
    assert done.error is None
    card_created = StepListCardCreatedPayload(card_id="t_1")
    assert card_created.plan is None
    card_done = StepListCardFinalizedPayload(card_id="t_1", state="completed", step_count=3)
    assert card_done.step_count == 3
    emoji = EmojiAckPayload(target_msg_id="m_1", emoji="🤝")
    assert emoji.emoji == "🤝"
    plan = PlanItem(item_id="p_1", label="x", tag="t", priority="p1", status="pending")
    assert plan.related_step_id is None
    nar = NarrativeDeltaPayload(card_id="t_1", delta="好的，我先查 Q3 数据…")
    assert nar.delta.endswith("…")


def test_all_v4_event_literals_registered() -> None:
    """Each new chat-v4 event MUST appear in the EventType Literal."""
    members = set(get_args(EventType))
    missing = [lit for lit in _V4_EVENT_LITERALS if lit not in members]
    assert not missing, f"chat-v4 EventType literals missing: {missing}. Available: {sorted(members)}"


def test_event_literal_dot_separator_used() -> None:
    """Regression guard: dot-separated names (matches FE channel convention).

    Catches accidental rename to underscore form (`turn_plan`) since the
    handler keys on the wire literal verbatim.
    """
    members = set(get_args(EventType))
    # Dotted forms must exist; underscore forms must NOT shadow them.
    for dotted in _V4_EVENT_LITERALS:
        assert dotted in members
        underscore_form = dotted.replace(".", "_")
        if underscore_form != dotted:
            # underscore variant should NOT exist as a separate literal
            # (otherwise FE+BE may diverge on which to use).
            assert underscore_form not in members, f"both '{dotted}' and '{underscore_form}' registered — pick one to avoid wire ambiguity."
