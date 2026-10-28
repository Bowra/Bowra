"""Sandbox guidance reads the authorized entry only during its owning run."""

from __future__ import annotations

from types import SimpleNamespace

from bowhead_core.tool_registry import _caller_sandbox_entry
from bowhead_skill.sandbox_call_shape import FLAT_SANDBOX_TOOL, SKILL_INVOKE_TOOL


def _ctx(**kw):
    return SimpleNamespace(**kw)


def test_peer_holding_flat_entry_resolves_to_it() -> None:
    ctx = _ctx(_current_subagent_id="sub_1", _running_sandbox_entry=FLAT_SANDBOX_TOOL)
    assert _caller_sandbox_entry(ctx) == FLAT_SANDBOX_TOOL


def test_peer_authorized_to_invoke_the_sandbox_skill() -> None:
    ctx = _ctx(_current_subagent_id="sub_1", _running_sandbox_entry=SKILL_INVOKE_TOOL)
    assert _caller_sandbox_entry(ctx) == SKILL_INVOKE_TOOL


def test_stale_entry_is_not_trusted_after_the_peer_exits() -> None:
    ctx = _ctx(_running_sandbox_entry=FLAT_SANDBOX_TOOL)  # No _current_subagent_id.
    assert _caller_sandbox_entry(ctx) is None


def test_lead_without_any_snapshot_falls_back() -> None:
    assert _caller_sandbox_entry(_ctx()) is None


def test_peer_without_a_sandbox_entry_falls_back() -> None:
    ctx = _ctx(_current_subagent_id="sub_1", _running_sandbox_entry=None)
    assert _caller_sandbox_entry(ctx) is None


def test_hint_rendered_for_a_flat_caller_names_that_entry() -> None:
    """Keep the artifact binding and Python input shape in the rendered hint."""
    from bowhead_skill.sandbox_call_shape import sandbox_inputs_hint

    ctx = _ctx(_current_subagent_id="sub_1", _running_sandbox_entry=FLAT_SANDBOX_TOOL)
    hint = sandbox_inputs_hint(_caller_sandbox_entry(ctx), artifact_id="art_abc123456789")
    assert "python_exec(code=" in hint
    assert "art_abc123456789" in hint
    assert "INPUTS['rows']['path']" in hint
