"""Phase 2.3 UT — `build_lead_system_prompt` env-var gate for response_kind marker.

r3.1 P0 fixes verified:
- Flag OFF (default): no `<response_kind_protocol>` block in prompt
- Flag ON: block present at expected position (right after `<role>`)
- Flag read at FUNCTION-CALL time (CTO + Risk+PM r3.1 P0): same process,
  same import, flip env between calls → behavior changes
- Atomic with scanner wire: both env-gated on same `BOWHEAD_OMEGA_RESPONSE_KIND_V3`
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bowhead_core.agents.prompts.lead import build_lead_system_prompt

if TYPE_CHECKING:
    import pytest

# ─── Flag OFF (default) ──────────────────────────────────────────────────


def test_flag_explicit_off_no_response_kind_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-05-12 default flipped ON; explicit `=0` is the OFF opt-out."""
    monkeypatch.setenv("BOWHEAD_OMEGA_RESPONSE_KIND_V3", "0")
    prompt = build_lead_system_prompt(
        workspace_id="test-ws",
        facts=[],
    )
    assert "<response_kind_protocol>" not in prompt
    assert "response_kind>chat" not in prompt
    assert "response_kind>canvas" not in prompt


def test_default_unset_now_includes_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-05-12: default flipped ON. Unset env = block present."""
    monkeypatch.delenv("BOWHEAD_OMEGA_RESPONSE_KIND_V3", raising=False)
    prompt = build_lead_system_prompt(
        workspace_id="test-ws",
        facts=[],
    )
    assert "<response_kind_protocol>" in prompt


def test_flag_explicitly_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit `=0` env still produces no marker block."""
    monkeypatch.setenv("BOWHEAD_OMEGA_RESPONSE_KIND_V3", "0")
    prompt = build_lead_system_prompt(
        workspace_id="test-ws",
        facts=[],
    )
    assert "<response_kind_protocol>" not in prompt


def test_flag_other_truthy_values_treated_as_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strict `== "1"` check — `true`/`yes`/etc. should be OFF (defensive)."""
    for val in ["true", "yes", "ON", "2", ""]:
        monkeypatch.setenv("BOWHEAD_OMEGA_RESPONSE_KIND_V3", val)
        prompt = build_lead_system_prompt(
            workspace_id="test-ws",
            facts=[],
        )
        assert "<response_kind_protocol>" not in prompt, f"value {val!r} should not enable flag"


# ─── Flag ON ─────────────────────────────────────────────────────────────


def test_flag_on_includes_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOWHEAD_OMEGA_RESPONSE_KIND_V3", "1")
    prompt = build_lead_system_prompt(
        workspace_id="test-ws",
        facts=[],
    )
    assert "<response_kind_protocol>" in prompt
    assert "</response_kind_protocol>" in prompt
    # Instruction mentions both kinds
    assert "chat" in prompt
    assert "canvas" in prompt


def test_flag_on_block_positioned_right_after_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Block ordering: <role> ... </role>, then <response_kind_protocol>, then
    facts_block. Ensures Lead reads the marker rule before other directives."""
    monkeypatch.setenv("BOWHEAD_OMEGA_RESPONSE_KIND_V3", "1")
    prompt = build_lead_system_prompt(
        workspace_id="test-ws",
        facts=[],
    )
    role_close = prompt.find("</role>")
    rk_open = prompt.find("<response_kind_protocol>")
    provenance = prompt.find("<provenance_block>")
    assert role_close < rk_open < provenance, f"order: role={role_close} rk={rk_open} prov={provenance}"


# ─── Function-call-time read (CTO + Risk+PM r3.1 P0) ─────────────────────


def test_flag_flip_between_calls_changes_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Module-top read would freeze flag at import. Function-call-time read
    means we can flip env mid-process and the NEXT call sees the change.

    Critical: scanner wire (Phase 2.2) also reads at call time — same
    semantic, both should always agree on the flag state. This atomicity
    is what allows single env-var to gate the entire response_kind v3 path.
    """
    # Start OFF (explicit)
    monkeypatch.setenv("BOWHEAD_OMEGA_RESPONSE_KIND_V3", "0")
    p_off = build_lead_system_prompt(
        workspace_id="test-ws",
        facts=[],
    )
    assert "<response_kind_protocol>" not in p_off

    # Flip ON — same process, no module reload, just env flip
    monkeypatch.setenv("BOWHEAD_OMEGA_RESPONSE_KIND_V3", "1")
    p_on = build_lead_system_prompt(
        workspace_id="test-ws",
        facts=[],
    )
    assert "<response_kind_protocol>" in p_on

    # Flip OFF again — back to no-block
    monkeypatch.setenv("BOWHEAD_OMEGA_RESPONSE_KIND_V3", "0")
    p_off2 = build_lead_system_prompt(
        workspace_id="test-ws",
        facts=[],
    )
    assert "<response_kind_protocol>" not in p_off2


# ─── Token budget impact ─────────────────────────────────────────────────


def test_marker_criteria_is_user_stated_intent_not_question_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-flight delivery markers use explicit document intent, not question shape.

    A trend or comparison request alone cannot establish eventual answer volume.
    """
    monkeypatch.setenv("BOWHEAD_OMEGA_RESPONSE_KIND_V3", "1")
    prompt = build_lead_system_prompt(workspace_id="ws_t", facts=[])
    block = prompt.split("<response_kind_protocol>")[1].split("</response_kind_protocol>")[0]

    # Positive: criterion uses explicit user request + default chat + explains why the agent cannot judge
    assert 'did the user explicitly ask for a "document" this time' in block
    assert "Not explicitly asked → chat" in block
    assert "you cannot tell how much\n  content this turn will produce" in block
    # Same definition as delivery parameter (conflicting instructions contributed to this incident)
    assert "subagent_spawn(delivery=)" in block

    # Negative: question phrasing no longer supplies canvas triggers
    for shape_word in ("趋势 → canvas", "图表/多段分析", "对比/趋势"):
        assert shape_word not in block, shape_word


def test_compute_delivery_criterion_separates_depth_from_surface() -> None:
    """peer 的判据不能用「体量」当尺子。

    116 dogfood（1.2.61）实测：账号里存着一条记忆「以后销售分析都给完整复盘，不只给结论」，
    Lead 把它逐字写进 task，peer 读成"要一份文档" ⇒ 一句「月度销售趋势」出了 16 段画布。
    peer 每一步都没违规——错的是判据把**深度诉求**和**交付面诉求**混成了一件事，
    而且「一屏读得完」是个体量尺：深度一多就必然溢出成 report。
    """
    from pathlib import Path

    import bowhead_core.agents.prompts as _p

    md = (Path(_p.__file__).parent / "system" / "compute.md").read_text(encoding="utf-8")
    assert "深度诉求 ≠ 交付面诉求" in md
    assert "篇幅长、图多也照样是 chat" in md
    assert "长而线性的答复仍然是 chat" in md
    # Remove the volume criterion; judge how content is read, not how long it is
    assert "一屏读得完" not in md
    assert "跳着看" in md
