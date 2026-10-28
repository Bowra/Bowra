"""Sprint chat-redesign-2026-05-21 Phase 1.1 — L3 picker hint + failed-attachments
prompt injection.

Focused tests on the new prompt templates + LeadTurnTransient wiring. Full
Lead-loop integration is exercised by lead_orchestrator_test downstream.
"""

from __future__ import annotations

from dataclasses import dataclass


def test_l3_picker_hint_template_renders_candidates() -> None:
    from bowhead_core.agents.prompts.lead import _L3_PICKER_HINT_TEMPLATE

    rendered = _L3_PICKER_HINT_TEMPLATE.format(
        candidates='"做个分析", "收进知识库", "照这个格式做新的"',
    )
    assert "l3_picker_activation" in rendered
    assert "做个分析" in rendered
    assert "clarification_ask" in rendered
    assert "candidates =" in rendered


def test_failed_attachments_hint_template_renders_list() -> None:
    from bowhead_core.agents.prompts.lead import _FAILED_ATTACHMENTS_HINT

    rendered = _FAILED_ATTACHMENTS_HINT.format(
        failed_list="- `bad.pdf` (application/pdf) — read failed",
    )
    assert "failed_attachments" in rendered
    assert "bad.pdf" in rendered
    assert "tell them honestly that the files cannot be read" in rendered
    assert "Never invent or guess" in rendered


def test_init_lead_transient_wires_l3_picker_hint() -> None:
    from bowhead_core.agents.lead_transient import init_lead_transient

    @dataclass
    class _StubCtx:
        # minimal ctx — init writes to .l3_picker_hint via setattr
        pass

    @dataclass
    class _StubReq:
        text: str = "ignored"
        paradigm_v2: bool = False
        preferred_provider: str | None = None
        preferred_model: str | None = None
        resume_turn_id: str | None = None
        resume_glossary: dict | None = None
        l3_picker_hint: list[str] | None = None

    ctx = _StubCtx()
    init_lead_transient(ctx, _StubReq(l3_picker_hint=["a", "b", "c"]))  # type: ignore[arg-type]
    assert getattr(ctx, "l3_picker_hint", None) == ["a", "b", "c"]


def test_init_lead_transient_l3_hint_defaults_none() -> None:
    from bowhead_core.agents.lead_transient import init_lead_transient

    @dataclass
    class _StubCtx:
        pass

    @dataclass
    class _StubReq:
        text: str = ""
        paradigm_v2: bool = False
        preferred_provider: str | None = None
        preferred_model: str | None = None
        resume_turn_id: str | None = None
        resume_glossary: dict | None = None
        l3_picker_hint: list[str] | None = None

    ctx = _StubCtx()
    init_lead_transient(ctx, _StubReq())  # type: ignore[arg-type]
    assert getattr(ctx, "l3_picker_hint", "missing") is None


def test_turn_request_l3_picker_hint_default_none() -> None:
    """TurnRequest dataclass default for l3_picker_hint = None."""
    from bowhead_core.types import TurnRequest

    req = TurnRequest(
        session_id="s",
        workspace_id="w",
        user_id="u",
        member_uid="m",
        text="hello",
    )
    assert req.l3_picker_hint is None
