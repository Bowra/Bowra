"""The capability protocol block is a property of each Agent's own definition.

Built-in Agents declare ``capability_protocol_block`` in their system manifest;
team Agents carry the same key in their published revision definition. The
runtime never consults a list of routing names. Every wiring case below runs the
real ``SubagentRunner`` and inspects what the model receives, so a gate that is
computed but never consulted cannot pass.
"""

from __future__ import annotations

import copy

import pytest
from bowhead_core.agent_artifact_capability import reset_artifact_registry_cache_for_test
from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
from bowhead_core.agents.capability_protocols import render_capability_protocols
from bowhead_core.agents.registry import AgentRegistry
from bowhead_core.events import TurnEventBus
from bowhead_core.subagent import (
    PROFILES,
    SubagentRunner,
    _materialize_system_profile_snapshot,
    _system_profile_snapshot,
    _team_revision_projection,
)
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry
from bowhead_core.user_store import TeamSubagent

from bowhead_router import (
    AgentBinding,
    LLMResponse,
    MockAdapter,
    ProviderConfig,
    ProviderRegistry,
    Router,
    TokenUsage,
)

_BLOCK_BUILTINS = frozenset({"business-analyst", "financial-analyst", "kb-analyst", "marketing-analyst"})
_DATA_PROTOCOL = "# Data query protocol"
# A deployment that still carries the retired allowlist line (kept in .env until
# the rollback target no longer reads it) must not change any Agent's prompt.
_RETIRED_ENV = "FEATURE_CAPABILITY_PROTOCOL_BLOCK_PROFILES"


@pytest.fixture(autouse=True)
def _reset_registry_cache() -> None:
    reset_artifact_registry_cache_for_test()


def _system_profiles() -> dict:
    registry = AgentRegistry.bootstrap()
    return {name: manifest_to_profile(registry.get(name)) for name in registry.list_all()}


def test_builtin_manifests_declare_the_block_for_exactly_the_four_analysts() -> None:
    profiles = _system_profiles()
    assert "compute" in profiles, "positive control: the system registry did not load"
    declared = {name for name, profile in profiles.items() if profile.capability_protocol_block}
    assert declared == _BLOCK_BUILTINS
    assert profiles["compute"].capability_protocol_block is False


class _CaptureRouter(Router):
    def __init__(self, reg: ProviderRegistry) -> None:
        super().__init__(reg)
        self.systems: list[str] = []
        self.tool_names: list[tuple[str, ...]] = []

    async def invoke(self, agent_type, req, **kw):  # type: ignore[override]
        self.systems.append(req.system or "")
        self.tool_names.append(tuple(schema.name for schema in req.tools))
        return await super().invoke(agent_type, req, **kw)

    async def stream(self, agent_type, req, **kw):  # type: ignore[override]
        self.systems.append(req.system or "")
        self.tool_names.append(tuple(schema.name for schema in req.tools))
        return await super().stream(agent_type, req, **kw)


class _TeamStore:
    def __init__(self, rec: TeamSubagent) -> None:
        self.rec = rec

    async def get_team_subagent_by_name(self, team_id: str, name: str, *, only_enabled: bool = True):
        return self.rec if (team_id, name) == (self.rec.team_id, self.rec.name) else None


async def _system_prompt_seen_by_model(profile_name: str, *, team_store: _TeamStore | None = None) -> tuple[str, tuple[str, ...]]:
    adapter = MockAdapter(provider_name="p1")
    adapter.scripted_responses = [LLMResponse(content="done", usage=TokenUsage(input_tokens=10, output_tokens=5))]
    reg = ProviderRegistry()
    reg.load([ProviderConfig(name="p1", type="mock")])
    reg._adapters["p1"] = adapter  # type: ignore[attr-defined]
    router = _CaptureRouter(reg)
    for agent_type in ("lead", "analyst", "compute", "narrative", "echo", "modeler"):
        router.bind(AgentBinding(agent_type=agent_type, primary=("p1", "mock")))
    runner = SubagentRunner(
        router=router,
        tool_registry=build_default_registry(),
        backend="legacy",
        team_subagent_store=team_store,
    )
    ctx = ToolExecutionContext(workspace_id="w", member_uid="m", user_id="u", turn_id="t", datarc=None, team_id="team_a")
    await runner.run(
        profile_name=profile_name,
        task="How did sales do this year?",
        parent_turn_id="t",
        parent_bus=TurnEventBus(),
        parent_ctx=ctx,
    )
    assert router.systems, "no model request was captured; this gate would be vacuous"
    return router.systems[0], router.tool_names[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(_BLOCK_BUILTINS | {"compute"}))
async def test_builtin_prompt_follows_the_manifest_not_the_retired_allowlist(
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _system_profiles()[name]
    monkeypatch.setitem(PROFILES, name, profile)
    monkeypatch.setenv(_RETIRED_ENV, "compute" if name in _BLOCK_BUILTINS else "*")

    system, tool_names = await _system_prompt_seen_by_model(name)
    expected_block = render_capability_protocols(tool_names)
    # Positive control: these Agents advertise Datarc tools; the definition
    # decides whether their available capability protocol is included.
    assert _DATA_PROTOCOL in expected_block

    assert (expected_block in system) is (name in _BLOCK_BUILTINS)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_team_agent_prompt_follows_its_published_definition(enabled: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    from bowhead_core.agents import plan_admission

    async def _admitted(*_args, **_kwargs) -> bool:
        return True

    # The fake store has no assignment database, which fails closed since 8e6f85c9a; this case
    # covers prompt assembly after admission.
    monkeypatch.setattr(plan_admission, "team_agent_runtime_allowed", _admitted)
    monkeypatch.delenv(_RETIRED_ENV, raising=False)
    rec = TeamSubagent(
        id="osa_gina",
        team_id="team_a",
        name="gina",
        display_name="Data query",
        description="Queries business data.",
        system_prompt="Answer with data.",
        tool_refs=["bi_query", "bi_schema"],
        capability_protocol_block=enabled,
    )

    system, _ = await _system_prompt_seen_by_model("gina", team_store=_TeamStore(rec))

    assert "Answer with data." in system, "positive control: the team Agent itself did not run"
    assert (_DATA_PROTOCOL in system) is enabled


def test_system_snapshot_writes_the_key_only_when_enabled() -> None:
    profiles = _system_profiles()
    kb = _system_profile_snapshot(profiles["kb-analyst"])
    compute = _system_profile_snapshot(profiles["compute"])
    assert kb["capability_protocol_block"] is True
    # Agents without the block keep the exact snapshot bytes (and system
    # revision ids) they had before the field existed.
    assert "capability_protocol_block" not in compute


@pytest.mark.parametrize("name", sorted(_BLOCK_BUILTINS | {"compute"}))
def test_new_and_old_system_snapshots_both_materialize(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _system_profiles()[name]
    monkeypatch.setitem(PROFILES, name, profile)
    new_snapshot = _system_profile_snapshot(profile)
    old_snapshot = copy.deepcopy(new_snapshot)
    old_snapshot.pop("capability_protocol_block", None)

    new = _materialize_system_profile_snapshot(new_snapshot, expected_name=name)
    old = _materialize_system_profile_snapshot(old_snapshot, expected_name=name)

    assert new.capability_protocol_block is (name in _BLOCK_BUILTINS)
    # A snapshot frozen before the field existed follows the current manifest.
    assert old.capability_protocol_block is (name in _BLOCK_BUILTINS)


def test_explicit_snapshot_value_wins_over_the_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _system_profiles()["kb-analyst"]
    monkeypatch.setitem(PROFILES, "kb-analyst", profile)
    snapshot = {**_system_profile_snapshot(profile), "capability_protocol_block": False}
    assert _materialize_system_profile_snapshot(snapshot, expected_name="kb-analyst").capability_protocol_block is False
    with pytest.raises(ValueError, match="field shape is invalid"):
        _materialize_system_profile_snapshot({**snapshot, "capability_protocol_block": "yes"}, expected_name="kb-analyst")


def _definition(**extra) -> dict:
    return {"system_prompt": "Answer with data.", "tool_refs": ["bi_query"], "skills": [], "max_iterations": 10, **extra}


@pytest.mark.parametrize(
    ("extra", "expected"),
    [({}, False), ({"capability_protocol_block": False}, False), ({"capability_protocol_block": True}, True)],
)
def test_team_revision_projection_reads_the_definition_key(extra: dict, expected: bool) -> None:
    projected = _team_revision_projection(_definition(**extra), {"display_name": "Gina"}, expected_name="gina")
    assert projected["capability_protocol_block"] is expected


def test_team_revision_projection_rejects_a_non_boolean_key() -> None:
    with pytest.raises(ValueError, match="invalid Agent revision projection"):
        _team_revision_projection(_definition(capability_protocol_block=1), {"display_name": "Gina"}, expected_name="gina")


def test_profile_field_defaults_off() -> None:
    from bowhead_core.subagent import SubagentProfile

    assert SubagentProfile(name="x", llm_agent_type="analyst", tool_whitelist=(), system_prompt="").capability_protocol_block is False
