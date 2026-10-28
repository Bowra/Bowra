"""Tests for ``bowhead_core.agents.lead_transient``.

Covers:

- ``init_lead_transient`` sets every slot listed in LeadTurnTransient.
- ``init_lead_transient`` reads optional request fields safely.
- ``lead_transient`` cast provides the expected schema for read sites.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from bowhead_core.agents.lead_transient import (
    init_lead_transient,
    lead_transient,
)


@dataclass
class _FakeReq:
    """Minimal stand-in for TurnRequest — only fields init_lead_transient reads."""

    text: str = "查 Q1 销售"
    paradigm_v2: bool = False
    preferred_provider: str | None = None
    preferred_model: str | None = None
    resume_turn_id: str | None = None
    resume_glossary: dict[str, str] | None = None


def _make_ctx() -> Any:
    """SimpleNamespace stand-in for ToolExecutionContext (handler-time tests
    pass loose mock ctx; we want init to work on them too)."""
    return SimpleNamespace()


# ─── init_lead_transient ─────────────────────────────────────────────────


def test_init_lead_transient_writes_all_required_v4_slots() -> None:
    """V4 streaming slots must all be initialized — Bug 2 regression guard.

    Bug 2: ``_v4_phase_plan`` was never written; read site got ``None``
    silently. After init_lead_transient, ALL slots are written (some to
    None as a sentinel, but they exist).
    """
    ctx = _make_ctx()
    req = _FakeReq()

    init_lead_transient(ctx, req)

    # Required slots written (Sprint phase-plan-removal: 6 fields removed —
    # _v4_phase_plan / _v4_turn_plan_emitted / _v4_phase_seq /
    # _v4_phase_update_emitted_keys / _v4_spawn_id_to_phase_id /
    # _v4_last_peer_dispatched_spawn_id).
    assert ctx._v4_final_payload_emitted is False


def test_init_lead_transient_writes_identity_hints_from_request() -> None:
    """Bug 3 regression guard — paradigm_v2 / preferred_provider /
    preferred_model must arrive on ctx from the request, not getattr-default."""
    ctx = _make_ctx()
    req = _FakeReq(
        paradigm_v2=True,
        preferred_provider="aliyun_qwen",
        preferred_model="qwen-plus",
    )

    init_lead_transient(ctx, req)

    assert ctx.paradigm_v2 is True
    assert ctx.preferred_provider == "aliyun_qwen"
    assert ctx.preferred_model == "qwen-plus"


def test_init_lead_transient_paradigm_v2_falsy_defaults_to_false() -> None:
    """When request has no paradigm_v2 flag, ctx gets explicit False —
    not a missing attribute that getattr-default would mask."""
    ctx = _make_ctx()
    req = _FakeReq()  # paradigm_v2 default False

    init_lead_transient(ctx, req)

    assert ctx.paradigm_v2 is False  # explicit, not missing
    assert ctx.preferred_provider is None
    assert ctx.preferred_model is None


def test_init_lead_transient_stashes_user_text_for_psi_compose() -> None:
    """Sprint ψ-E: subagent_return handler reads _psi_turn_user_text to
    rebuild TurnRequest for eager compose. Must always be written."""
    ctx = _make_ctx()
    req = _FakeReq(text="对比 Q1 vs Q2")

    init_lead_transient(ctx, req)

    assert ctx._psi_turn_user_text == "对比 Q1 vs Q2"


def test_init_lead_transient_resume_signals_only_when_resume_turn_id() -> None:
    """HITL resume signals should only land on ctx for resume turns —
    fresh turns leave them unset so downstream isinstance/truthy checks
    behave normally."""
    ctx = _make_ctx()
    req = _FakeReq()  # no resume

    init_lead_transient(ctx, req)

    assert not hasattr(ctx, "_resume_turn_id")
    assert not hasattr(ctx, "_resume_glossary")


def test_init_lead_transient_resume_signals_set_when_resuming() -> None:
    ctx = _make_ctx()
    req = _FakeReq(
        resume_turn_id="t_abc",
        resume_glossary={"近期": "2024-Q3"},
    )

    init_lead_transient(ctx, req)

    assert ctx._resume_turn_id == "t_abc"
    assert ctx._resume_glossary == {"近期": "2024-Q3"}


def test_init_lead_transient_resume_glossary_defaults_to_empty_dict() -> None:
    """resume_turn_id without glossary still creates a dict slot — callers
    can do ``ctx._resume_glossary.get(...)`` safely."""
    ctx = _make_ctx()
    req = _FakeReq(resume_turn_id="t_abc", resume_glossary=None)

    init_lead_transient(ctx, req)

    assert ctx._resume_turn_id == "t_abc"
    assert ctx._resume_glossary == {}


def test_init_lead_transient_is_idempotent() -> None:
    """Calling twice (e.g. after a per-turn reset) re-initializes — no
    accumulation of state, no exceptions. Sprint phase-plan-removal: assertions
    updated for surviving slots only (_v4_final_payload_emitted)."""
    ctx = _make_ctx()
    req = _FakeReq()

    init_lead_transient(ctx, req)
    ctx._v4_final_payload_emitted = True  # simulate mid-turn write
    init_lead_transient(ctx, req)

    assert ctx._v4_final_payload_emitted is False  # reset


# ─── lead_transient view ─────────────────────────────────────────────────


def test_lead_transient_returns_dict_view_of_ctx() -> None:
    """The typed view should let callers .get() with default for read-side
    migration of getattr patterns."""
    ctx = _make_ctx()
    req = _FakeReq(paradigm_v2=True)
    init_lead_transient(ctx, req)

    view = lead_transient(ctx)

    assert view.get("paradigm_v2") is True
    # Sprint phase-plan-removal: switched probe key from _v4_phase_seq (deleted)
    # to _v4_final_payload_emitted (surviving).
    assert view.get("_v4_final_payload_emitted") is False
    # Schema-only key not yet populated returns the .get() default:
    assert view.get("_resume_turn_id") is None
