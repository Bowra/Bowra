"""Provider failures produce a searchable server-side record.

Run the real SubagentRunner with a synthetic structured provider rejection and
verify that logs and user-visible diagnostics identify the same failure.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

import pytest
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
from bowhead_router.errors import (
    InvocationTargetsExhaustedError,
    LLMTargetFailure,
    ProviderFailureKind,
)


class _StubDatarc:
    async def aclose(self) -> None:
        pass


class _RejectingRouter:
    """Reject non-streaming invocation with a structured synthetic provider error."""

    def __init__(self) -> None:
        self.invoke_calls = 0

    async def invoke(self, _agent_type: str, _req: Any, **_kw: Any) -> Any:
        self.invoke_calls += 1
        raise InvocationTargetsExhaustedError(
            [
                LLMTargetFailure(
                    provider="fixture-provider",
                    model="openai_gpt56_luna",
                    provider_family="openai_compatible",
                    mode="invoke",
                    target_index=0,
                    attempt=1,
                    kind=ProviderFailureKind.REQUEST_REJECTED,
                    retryable=False,
                    status_code=400,
                    upstream_error_type="invalid_request_error",
                    upstream_error_code="unsupported_value",
                    upstream_error_param="temperature",
                    upstream_message="Unsupported value: 'temperature' does not support 0.7 with this model.",
                )
            ]
        )

    def stream(self, _agent_type: str, _req: Any, **_kw: Any) -> Any:
        raise AssertionError("This fixture must use non-streaming invocation.")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lead_llm_failure_leaves_a_greppable_server_log(caplog) -> None:
    """A provider rejection leaves a searchable log identifying its rejected parameter."""
    runner = SubagentRunner(router=_RejectingRouter(), tool_registry=ToolRegistry())  # type: ignore[arg-type]
    bus = TurnEventBus()
    ctx = ToolExecutionContext(
        workspace_id="1",
        member_uid="00000000000000000000000000000001",
        user_id="u",
        turn_id="t_log_probe",
        datarc=_StubDatarc(),  # type: ignore[arg-type]
    )

    # Check logging, not return values/exceptions; the upper chokepoint decides whether Lead-tier failures are re-raised.
    with caplog.at_level(logging.ERROR, logger="bowhead_core.subagent"), contextlib.suppress(Exception):
        await runner.run(
            profile_name="lead",
            task="Compare synthetic product sales by channel.",
            parent_turn_id="t_log_probe",
            parent_bus=bus,
            parent_ctx=ctx,
        )

    failures = [record.getMessage() for record in caplog.records if "subagent.llm_failed" in record.getMessage()]

    assert failures, "Lead 的 LLM 失败必须留下 subagent.llm_failed 日志（否则运维无从查起）"
    line = failures[-1]
    assert "upstream_error_param=temperature" in line, f"日志要带 provider 的指认，实际：{line}"
    assert "upstream_error_code=unsupported_value" in line
    assert "status=400" in line
    # Prose enters logs after credential scrubbing, never user-facing diagnostics; this is its only destination.
    assert "does not support 0.7" in line
