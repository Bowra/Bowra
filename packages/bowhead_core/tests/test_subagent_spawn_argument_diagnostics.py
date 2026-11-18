"""A refused dispatch names the argument it rejected in the step diagnostic, never the value.

The step row's diagnostic is built from the raw handler result by `classify_tool_outcome` and
`publish_tool_failure` (the same calls the Tool event emitter makes), so its `safe_detail`
reads `field=<argument>` only when the handler reports that argument as `path`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from bowhead_core.delivered_files import CANVAS_WRITE_TOOLS
from bowhead_core.published_diagnostics import publish_tool_failure
from bowhead_core.subagent import SubagentResult, register_subagent_tool
from bowhead_core.tool_outcome import classify_tool_outcome
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry, build_default_registry

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _Runner:
    def __init__(self) -> None:
        self.called = False
        self.kwargs: dict[str, Any] = {}

    async def run(self, **kwargs: Any) -> SubagentResult:
        self.called = True
        self.kwargs = kwargs
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


@pytest.mark.parametrize(
    ("overrides", "ignored"),
    [
        ({"delegation_role": "assistant"}, ["delegation_role"]),
        ({"delegation_role": []}, ["delegation_role"]),
        ({"role_id": "image", "candidate_id": [], "reason_code": {}}, ["role_id", "candidate_id", "reason_code"]),
        ({"skills": ["made-up-method"]}, ["skills"]),
    ],
)
async def test_optional_dispatch_arguments_do_not_block_ordinary_work(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], ignored: list[str]
) -> None:
    monkeypatch.setenv("FEATURE_AGENT_BEHAVIOR_M3", "0")
    registry = build_default_registry()
    register_subagent_tool(registry)
    runner = _Runner()

    args = {"profile": "compute", "task": "Inspect sales.", "delivery": "chat", "required_source_urls": []}
    result = await registry.execute("subagent_spawn", args | overrides, _context(runner))

    assert runner.called and result.result["ok"]
    assert result.result["ignored_arguments"] == ignored
    assert "required_model_input" not in runner.kwargs


async def test_incomplete_chat_lookup_dispatches_without_canvas_write_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    registry = build_default_registry()
    register_subagent_tool(registry)
    runner = _Runner()
    ctx = _context(runner, with_report=True)
    ctx.tool_view = registry
    ctx.datarc_source_evidence = {"state": "selected"}
    ctx.user_text = "OBU monthly sales volume"

    result = await registry.execute(
        "subagent_spawn",
        {
            "profile": "compute",
            "task": "Query monthly OBU sales in Datarc.",
            "delivery": "chat",
            "data_hint": ["monthly sales", 12],
            "extra_instructions": {"unexpected": "object"},
        },
        ctx,
    )

    assert result.result["ok"] and runner.called
    assert runner.kwargs["parent_ctx"].required_web_sources == ()
    assert runner.kwargs["tool_deny"] == CANVAS_WRITE_TOOLS
    assert not hasattr(ctx.parent_bus, "_lead_canvas_mode")
    assert "data_hint**: monthly sales" in runner.kwargs["extra_instructions"]
    assert "unexpected" not in runner.kwargs["extra_instructions"]


@pytest.mark.parametrize(
    ("request_text", "task"),
    [
        ("Read https://example.com/sales before answering.", "Verify sales in Datarc."),
        ("Read the official sales report before answering.", "Verify sales in the official report."),
    ],
)
async def test_missing_source_with_ambiguous_or_linked_request_requires_correction(
    monkeypatch: pytest.MonkeyPatch, request_text: str, task: str
) -> None:
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "0")
    registry = build_default_registry()
    register_subagent_tool(registry)
    runner = _Runner()
    ctx = _context(runner)
    ctx.tool_view = registry
    ctx.datarc_source_evidence = {"state": "selected"}
    ctx.user_text = request_text
    args = {"profile": "compute", "task": task, "delivery": "chat"}

    refusal = await registry.execute("subagent_spawn", args, ctx)

    assert not runner.called
    assert _published_detail(refusal.result) == "field=required_source_urls"
    ctx.required_web_sources = ("https://example.com/method",)
    corrected = await registry.execute("subagent_spawn", args | {"required_source_urls": "https://example.com/sales"}, ctx)
    assert corrected.result["ok"]
    assert runner.kwargs["parent_ctx"].required_web_sources == ("https://example.com/method", "https://example.com/sales")


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"task": {"query": "sales"}}, "task"),
        ({"profile": ["compute"]}, "profile"),
        ({"agent_id": ["agent"]}, "agent_id"),
        ({"_args_parse_error": "private provider payload"}, "arguments"),
        ({"revise_scope": {"chapter": 1}}, "revise_scope"),
        ({"revise_scope": ["Sales"], "canvas_mode": None}, "canvas_mode"),
    ],
)
async def test_invalid_dispatch_inputs_are_rejected_before_execution(monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], field: str) -> None:
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", "1")
    registry = build_default_registry()
    register_subagent_tool(registry)
    runner = _Runner()
    args = {"profile": "compute", "task": "Inspect sales.", "delivery": "chat", "required_source_urls": []}

    refusal = await registry.execute("subagent_spawn", args | overrides, _context(runner, with_report=True))

    assert not runner.called
    assert _published_detail(refusal.result) == f"field={field}"
