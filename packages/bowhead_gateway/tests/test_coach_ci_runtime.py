"""Internal CI uses the current runtime resolver at every admission boundary."""

from types import SimpleNamespace

import pytest
from bowhead_core.agent_eval_service import AgentEvalServiceError
from bowhead_core.entitlements import EntitlementDenied
from bowhead_gateway.agent_eval_runtime import AgentEvalRuntime

from bowhead_gateway import agent_eval_runtime as module

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("mode,edition", [("local", "business"), ("saas", "community")])
async def test_missing_runtime_resolver_blocks_internal_ci(monkeypatch, mode, edition):
    run = SimpleNamespace(subject=SimpleNamespace(team_id="team_test"))

    class Store:
        async def read_run(self, _run_id):
            return run

    monkeypatch.setattr(module, "AgentEvalStore", lambda _path: Store())
    runtime = AgentEvalRuntime("unused", SimpleNamespace(deployment_mode=mode, edition=edition))
    with pytest.raises(AgentEvalServiceError, match="entitlement_unavailable"):
        await runtime.require_coach_runtime("aer_test")


async def test_license_replacement_and_model_change_are_read_again(monkeypatch):
    run = SimpleNamespace(subject=SimpleNamespace(team_id="team_test"))
    checks = []

    class Store:
        async def read_run(self, _run_id):
            return run

    class Resolver:
        def __init__(self, allowed):
            self.allowed = allowed

        async def assert_can_run_turn(self, team_id):
            checks.append(("license", team_id, self.allowed))
            if not self.allowed:
                raise EntitlementDenied("license_expired", 403, "License expired")

    class Gate:
        def __init__(self, name):
            self.name = name

        async def require_current(self, subject):
            assert subject is run
            checks.append(self.name)
            if self.name == "model" and not state.model_available:
                raise AgentEvalServiceError("agent_eval_model_config_changed")

    async def role(_path, subject):
        assert subject is run
        return "analyst"

    monkeypatch.setattr(module, "AgentEvalStore", lambda _path: Store())
    monkeypatch.setattr(module, "_SafetyGate", lambda _path: Gate("safety"))
    monkeypatch.setattr(module, "_ModelGate", lambda _state, _role: Gate("model"))
    monkeypatch.setattr(module, "_model_role", role)
    state = SimpleNamespace(deployment_mode="local", edition="business", entitlements=Resolver(True), model_available=True)
    runtime = AgentEvalRuntime("unused", state)
    await runtime.require_coach_runtime("aer_test")
    assert checks == [("license", "team_test", True), "safety", "model"]
    state.entitlements = Resolver(False)
    with pytest.raises(AgentEvalServiceError, match="coach_ci_entitlement_denied"):
        await runtime.require_coach_runtime("aer_test")
    state.entitlements = Resolver(True)
    state.model_available = False
    with pytest.raises(AgentEvalServiceError, match="model_config_changed"):
        await runtime.require_coach_runtime("aer_test")
