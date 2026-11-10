"""A refused dispatch names the argument it rejected in the step diagnostic, never the value.

The step row's diagnostic is built from the raw handler result by `classify_tool_outcome` and
`publish_tool_failure` (the same calls the Tool event emitter makes), so its `safe_detail`
reads `field=<argument>` only when the handler reports that argument as `path`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from bowhead_core.published_diagnostics import publish_tool_failure
from bowhead_core.subagent import SubagentResult, register_subagent_tool
from bowhead_core.tool_outcome import classify_tool_outcome
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, build_default_registry

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _Runner:
    def __init__(self) -> None:
        self.called = False

    async def run(self, **kwargs):
        self.called = True
        return SubagentResult(profile=str(kwargs.get("profile_name") or "compute"), subagent_id="sub_test", ok=True, final_answer="42")


class _Bus:
    async def emit(self, ev) -> None:  # noqa: ARG002
        return None


class _StoreWithReport:
    async def get_canvas_by_session(self, _sid):
        return SimpleNamespace(id="cv_1")

    async def list_sections(self, _cid, include_hidden=False):  # noqa: ARG002
        return [SimpleNamespace(id="sec_h1", kind="h1", superseded=False, turn_id="t_prev", payload={"text": "Earlier report"})]


def _published_detail(result: object) -> str | None:
    outcome = classify_tool_outcome(result)
    assert outcome.code == "tool_arguments_invalid", outcome
    diagnostic = publish_tool_failure(outcome)
    assert diagnostic is not None
    return diagnostic.to_wire(occurrence_id="0" * 32, turn_id="t_test").get("safe_detail")


def _context(runner: _Runner, *, with_report: bool = False) -> ToolExecutionContext:
    ctx = ToolExecutionContext(
        workspace_id="1", member_uid="m", user_id="u", turn_id="parent_t", datarc=None, subagent_runner=runner, parent_bus=_Bus(), session_id="s1"
    )
    if with_report:
        ctx.canvas_store = _StoreWithReport()
    return ctx


@pytest.mark.parametrize("canvas_mode", [None, "rewrite"])
async def test_canvas_mode_refusal_names_only_the_argument(monkeypatch: pytest.MonkeyPatch, canvas_mode: str | None) -> None:
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    registry = build_default_registry()
    register_subagent_tool(registry)
    runner = _Runner()
    args = {"profile": "compute", "task": "Recolor the chart"} | ({"canvas_mode": canvas_mode} if canvas_mode else {})

    inv = await registry.execute("subagent_spawn", args, _context(runner, with_report=True))

    assert inv.result["error_kind"] == "invalid_args" and not runner.called
    detail = _published_detail(inv.result)
    assert detail == "field=canvas_mode"
    assert "rewrite" not in detail


async def test_invalid_required_sources_name_the_argument_without_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "0")
    registry = build_default_registry()
    register_subagent_tool(registry)
    runner = _Runner()
    value = "ftp://private.example/report"

    inv = await registry.execute("subagent_spawn", {"profile": "compute", "task": "Read the page", "required_source_urls": [value]}, _context(runner))

    assert inv.result["error_kind"] == "invalid_args" and not runner.called
    detail = _published_detail(inv.result)
    assert detail == "field=required_source_urls"
    assert "private.example" not in detail


class _RefusingRuntime:
    async def binding_for_delegation(self, **_kwargs: object) -> None:
        raise ValueError("reference_delegation_role_required")


async def test_missing_delegation_role_names_the_argument() -> None:
    registry = ToolRegistry()
    register_subagent_tool(registry)
    ctx = SimpleNamespace(subagent_runner=object(), parent_bus=SimpleNamespace(), _reference_lead_runtime=_RefusingRuntime())

    result = await registry.get("subagent_spawn").handler({"profile": "compute", "task": "Analyze"}, ctx)

    assert _published_detail(result) == "field=delegation_role"
