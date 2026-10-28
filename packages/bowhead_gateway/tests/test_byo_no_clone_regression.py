"""Regression tripwire for the byo-no-clone fix (prod P0).

Background: teams with BYO LLM config had EVERY turn's `subagent_spawn` crash with
"subagent_runner/bus not configured on context" because the chat + feishu routes used
`dataclasses.replace(orchestrator, router=team_router)` to make a per-turn orchestrator —
which silently dropped the Orchestrator's dynamically-attached sidecars (subagent_runner,
plan_store, scheduled_store, ...). The fix carries the team router/KB on the TurnRequest and
passes the SHARED orchestrator through.

The helper-level behavior is covered by test_turn_runner_unit.py. THIS file guards the route
wiring: re-introducing `dataclasses.replace(orchestrator, ...)` in either dispatcher would
resurrect the exact prod crash while passing every helper unit test. These source tripwires
fail loudly if the clone comes back, and assert the request-carried override is still in place.
"""

from __future__ import annotations

import re
from pathlib import Path

import bowhead_gateway.feishu.bridge as _bridge_mod
import bowhead_gateway.routes.chat as _chat_mod
import pytest

# The clone anti-pattern: dataclasses.replace / _dc_replace applied to the Orchestrator
# (matches both `orchestrator` and the `orch` shorthand). Comments/docstrings are stripped
# first so the explanatory prose in the modules doesn't trip it.
_CLONE_ANTIPATTERN = re.compile(r"(?:_dc_)?replace\(\s*orch(?:estrator)?\b", re.MULTILINE)


def _code_only(path: Path) -> str:
    """Return module source with # comments and triple-quoted blocks removed."""
    src = path.read_text(encoding="utf-8")
    src = re.sub(r'"""[\s\S]*?"""', "", src)
    src = re.sub(r"'''[\s\S]*?'''", "", src)
    src = re.sub(r"(?m)#.*$", "", src)
    return src


def test_chat_route_does_not_clone_the_orchestrator() -> None:
    code = _code_only(Path(_chat_mod.__file__))
    hit = _CLONE_ANTIPATTERN.search(code)
    assert hit is None, (
        "routes/chat.py must NOT reconstruct the Orchestrator (dataclasses.replace drops "
        "dynamically-attached subagent_runner/plan_store/... → BYO subagent_spawn crash). "
        f"Carry BYO overrides on the TurnRequest instead. Offending match: {hit and hit.group(0)!r}"
    )


def test_feishu_bridge_does_not_clone_the_orchestrator() -> None:
    code = _code_only(Path(_bridge_mod.__file__))
    hit = _CLONE_ANTIPATTERN.search(code)
    assert hit is None, f"feishu/bridge.py must NOT reconstruct the Orchestrator (see chat.py guard). Offending match: {hit and hit.group(0)!r}"


def test_chat_route_carries_byo_override_on_the_request() -> None:
    """The fix's positive shape: BYO override applied to turn_req (not orch), keyed by the
    TurnRequest field names."""
    code = _code_only(Path(_chat_mod.__file__))
    assert "_dc_replace(turn_req" in code, "chat.py should apply BYO overrides to turn_req via dataclasses.replace(turn_req, ...)"
    assert '"team_router"' in code, "chat.py should set the team_router override key on the request"


def test_feishu_bridge_carries_team_router_on_the_request() -> None:
    code = _code_only(Path(_bridge_mod.__file__))
    assert "team_router=" in code, "feishu bridge should set team_router on the resume/dispatch TurnRequest"


# ── resume path carries BYO (P1-A fix): the resume turn honors the team model ──
from types import SimpleNamespace  # noqa: E402


def _valid_team_model_config() -> dict:
    binding = {
        "primary": {"provider": "team_provider", "model": "team_model"},
        "fallback": [],
    }
    return {
        "schema_version": 2,
        "providers": {
            "team_provider": {
                "type": "openai_compatible",
                "base_url": "https://93.184.216.34/v1",
                "api_key": "team-secret-for-tests",
                "models": ["team_model"],
            }
        },
        "agents": {
            "lead": dict(binding),
            "modeler": dict(binding),
            "analyst": {"default": dict(binding), "profiles": {}},
            "narrative": dict(binding),
            "echo": dict(binding),
            "judge": dict(binding),
        },
        "kb": {"models": {}, "active": {}},
    }


def test_build_resume_turn_request_carries_team_router_and_kb() -> None:
    from bowhead_gateway.turn_service import build_resume_turn_request

    req = SimpleNamespace(
        resume_context={"session_id": "s1", "user_id": "u1", "workspace_id": "w1", "user_text": "继续分析"},
        token="",
        kind="",
        chosen_value=None,
        chosen_values=None,
        turn_id="t1",
        workspace_id="w1",
        user_id="u1",
    )
    out = build_resume_turn_request(req, team_router="TEAM_ROUTER", team_kb_retriever="TEAM_KB")
    assert out is not None
    assert out.team_router == "TEAM_ROUTER"
    assert out.team_kb_retriever == "TEAM_KB"
    # default None when the caller resolves nothing (non-BYO / miss).
    out2 = build_resume_turn_request(req)
    assert out2.team_router is None
    assert out2.team_kb_retriever is None


async def test_resolve_team_model_runtime_resolves_router_and_kb() -> None:
    from bowhead_gateway.turn_service import resolve_team_model_runtime

    class _Reg:
        async def get(self, *a: object) -> str:
            return "TEAM_ROUTER"

    class _KbReg:
        async def get(self, *a: object) -> object:
            return SimpleNamespace(retriever="TEAM_KB")

    class _US:
        async def get_team_llm_config_with_version(self, team_id: str) -> tuple[dict, int]:
            return _valid_team_model_config(), 3

    app_state = SimpleNamespace(
        team_router_registry=_Reg(),
        user_store=_US(),
        team_kb_runtime_registry=_KbReg(),
        routing_config_loaded=None,
    )
    resolved = await resolve_team_model_runtime(
        app_state,
        "org_x",
        entrypoint="clarification",
    )
    assert resolved.result == "team_safe_success"
    assert resolved.reason == "resolved"
    assert resolved.router == "TEAM_ROUTER"
    assert resolved.kb_retriever == "TEAM_KB"


async def test_resolve_team_model_runtime_fails_closed_without_registry() -> None:
    from bowhead_gateway.turn_service import resolve_team_model_runtime

    resolved = await resolve_team_model_runtime(
        SimpleNamespace(router="DEPLOYMENT_ROUTER"),
        "org_x",
        entrypoint="clarification",
    )

    assert resolved.result == "team_unavailable"
    assert resolved.reason == "runtime_dependency_unavailable"
    assert resolved.router is None
    assert resolved.kb_retriever is None


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        ({}, "config_missing"),
        ({"providers": [{"provider_name": "unknown_team_provider"}]}, "config_invalid"),
    ],
)
async def test_resolve_team_model_runtime_fails_closed_for_empty_or_invalid_config(
    config: dict,
    reason: str,
) -> None:
    from bowhead_gateway.turn_service import resolve_team_model_runtime

    deployment_router = object()

    class _RegistrySpy:
        calls = 0

        async def get(self, *_args: object) -> object:
            self.calls += 1
            return deployment_router

    class _US:
        async def get_team_llm_config_with_version(self, _team_id: str) -> tuple[dict, int]:
            return config, 7

    registry = _RegistrySpy()
    resolved = await resolve_team_model_runtime(
        SimpleNamespace(
            router=deployment_router,
            team_router_registry=registry,
            user_store=_US(),
        ),
        "org_x",
        entrypoint="clarification",
    )

    assert resolved.result == "team_unavailable"
    assert resolved.reason == reason
    assert resolved.router is None
    assert registry.calls == 0


async def test_resolve_team_model_runtime_fails_closed_on_config_read_exception() -> None:
    from bowhead_gateway.turn_service import resolve_team_model_runtime

    class _US:
        async def get_team_llm_config_with_version(self, _team_id: str) -> tuple[dict, int]:
            raise RuntimeError("store unavailable")

    resolved = await resolve_team_model_runtime(
        SimpleNamespace(
            router="DEPLOYMENT_ROUTER",
            team_router_registry=object(),
            user_store=_US(),
        ),
        "org_x",
        entrypoint="clarification",
    )

    assert resolved.result == "resolver_error"
    assert resolved.reason == "config_load_failed"
    assert resolved.router is None


async def test_resolve_team_model_runtime_fails_closed_on_build_failure() -> None:
    from bowhead_gateway.turn_service import resolve_team_model_runtime

    class _BadReg:
        async def get(self, *a: object) -> object:
            raise RuntimeError("boom")

    class _US:
        async def get_team_llm_config_with_version(self, team_id: str) -> tuple[dict, int]:
            return _valid_team_model_config(), 1

    app_state = SimpleNamespace(team_router_registry=_BadReg(), user_store=_US(), team_kb_runtime_registry=None)
    resolved = await resolve_team_model_runtime(
        app_state,
        "org_x",
        entrypoint="clarification",
    )

    assert resolved.result == "resolver_error"
    assert resolved.reason == "router_build_failed"
    assert resolved.router is None
    assert resolved.kb_retriever is None


def test_core_router_boundary_rejects_team_request_without_team_router() -> None:
    from bowhead_core.turn_runner import (
        TeamModelRuntimeUnavailableError,
        _effective_router,
    )

    with pytest.raises(TeamModelRuntimeUnavailableError):
        _effective_router(
            SimpleNamespace(router="DEPLOYMENT_ROUTER"),
            SimpleNamespace(team_id="org_x", team_router=None),
        )


def test_core_router_boundary_keeps_explicit_no_team_legacy_path() -> None:
    from bowhead_core.turn_runner import _effective_router

    assert (
        _effective_router(
            SimpleNamespace(router="DEPLOYMENT_ROUTER"),
            SimpleNamespace(team_id=None, team_router=None),
        )
        == "DEPLOYMENT_ROUTER"
    )


def test_unsafe_fallback_metric_never_increments_success_counter() -> None:
    from bowhead_gateway.turn_service import record_team_model_resolution

    from bowhead_core import metrics

    metrics.get_registry().reset()
    record_team_model_resolution(
        entrypoint="catalog",
        result="unsafe_deployment_fallback",
        reason="deployment_catalog_ignored",
    )

    assert metrics.counter_items("bowhead_team_model_resolution_total") == [
        (
            {
                "entrypoint": "catalog",
                "reason": "deployment_catalog_ignored",
                "result": "unsafe_deployment_fallback",
            },
            1.0,
        )
    ]
    assert metrics.counter_items("bowhead_team_model_resolution_success_total") == []
