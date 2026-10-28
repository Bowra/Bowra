"""Gateway Subagent compatibility heartbeat contracts."""

from __future__ import annotations

import pytest
from bowhead_core.subagent_compatibility import (
    ApplicationCompatibilityDecision,
    TeamSubagentApplicationIncompatibleError,
)
from bowhead_gateway.subagent_compatibility import (
    GatewaySubagentCompatibilityHeartbeat,
)


def _decision(*, compatible: bool = True) -> ApplicationCompatibilityDecision:
    return ApplicationCompatibilityDecision(
        schema_version=2,
        reader_version="0001",
        writer_version="0001",
        minimum_reader_version=("0001" if compatible else "0002"),
        minimum_writer_version=("0001" if compatible else "0002"),
        reader_compatible=compatible,
        writer_compatible=compatible,
        compatible=compatible,
        checked_at="2026-07-28T07:00:00Z",
    )


class _FakeStore:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.removed: list[str] = []

    async def record_heartbeat(self, **_kwargs):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def remove_heartbeat(self, instance_id: str) -> None:
        self.removed.append(instance_id)


@pytest.mark.asyncio
async def test_gateway_admission_is_fail_closed_and_health_safe() -> None:
    store = _FakeStore([_decision()])
    heartbeat = GatewaySubagentCompatibilityHeartbeat(
        store,  # type: ignore[arg-type]
        instance_id="gateway:secret-instance",
        reader_version="0001",
        writer_version="0001",
    )

    admitted = await heartbeat.admit()

    assert admitted.compatible is True
    assert heartbeat.state.status == "ready"
    runtime_info = heartbeat.state.safe_runtime_info()
    assert runtime_info["subagent_compatibility"] == "ready"
    assert runtime_info["subagent_minimum_writer_version"] == "0001"
    assert "secret-instance" not in str(runtime_info)


@pytest.mark.asyncio
async def test_gateway_startup_rejects_incompatible_application() -> None:
    incompatible = _decision(compatible=False)
    store = _FakeStore(
        [TeamSubagentApplicationIncompatibleError(incompatible)],
    )
    heartbeat = GatewaySubagentCompatibilityHeartbeat(
        store,  # type: ignore[arg-type]
        instance_id="gateway:old",
        reader_version="0001",
        writer_version="0001",
    )

    with pytest.raises(TeamSubagentApplicationIncompatibleError):
        await heartbeat.admit()
    assert heartbeat.state.status == "starting"
    assert store.removed == []


@pytest.mark.asyncio
async def test_transient_heartbeat_failure_recovers_readiness() -> None:
    store = _FakeStore(
        [
            _decision(),
            RuntimeError("database busy"),
            _decision(),
        ],
    )
    heartbeat = GatewaySubagentCompatibilityHeartbeat(
        store,  # type: ignore[arg-type]
        instance_id="gateway:test",
        reader_version="0001",
        writer_version="0001",
    )
    await heartbeat.admit()

    assert await heartbeat.heartbeat_once() is True
    assert heartbeat.state.status == "heartbeat_failed"
    assert await heartbeat.heartbeat_once() is True
    assert heartbeat.state.status == "ready"


@pytest.mark.asyncio
async def test_raised_floor_permanently_fences_running_gateway() -> None:
    incompatible = _decision(compatible=False)
    store = _FakeStore(
        [
            _decision(),
            TeamSubagentApplicationIncompatibleError(incompatible),
        ],
    )
    heartbeat = GatewaySubagentCompatibilityHeartbeat(
        store,  # type: ignore[arg-type]
        instance_id="gateway:test",
        reader_version="0001",
        writer_version="0001",
    )
    await heartbeat.admit()

    assert await heartbeat.heartbeat_once() is False
    assert heartbeat.state.status == "incompatible"
    assert heartbeat.state.error_code == "team_subagent_application_incompatible"

    await heartbeat.unregister()
    assert store.removed == ["gateway:test"]
    assert heartbeat.state.status == "stopped"
