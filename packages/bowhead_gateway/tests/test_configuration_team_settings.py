"""Configuration Tools and real Team writers preserve types, roles and versions."""

from __future__ import annotations

import pytest
from bowhead_core.config_copilot import reset_registry_for_tests
from bowhead_core.config_copilot_chat_tools import set_proposal_writer_provider
from bowhead_core.config_draft_store import ConfigDraftStore
from bowhead_core.configuration_agent import AGENT_NAME, FORM_SKILLS, prepare_configuration_agent, register_configuration_agent_tools
from bowhead_core.events import TurnEventBus
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
from bowhead_gateway.config_copilot_forms import register_gateway_forms
from bowhead_gateway.configuration_agent_access import configuration_access_resolver
from bowhead_gateway.routes import config_proposals
from test_agent_run_limits_routes import URL, _as, app_ctx, client

__all__ = ["app_ctx", "client"]
pytestmark = pytest.mark.asyncio


@pytest.fixture
async def configuration(app_ctx, client, monkeypatch):
    reset_registry_for_tests()
    register_gateway_forms(app_ctx.app)
    store = ConfigDraftStore(app_ctx.users.db_path)
    await store.initialize()
    app_ctx.app.state.config_draft_store = store
    app_ctx.app.include_router(config_proposals.router)
    set_proposal_writer_provider(lambda: store)
    yield app_ctx
    reset_registry_for_tests()
    set_proposal_writer_provider(None)


async def child_for(user, form=""):
    ctx = ToolExecutionContext(
        user_id=user.id,
        team_id=user.team_id,
        member_uid=user.id,
        workspace_id="",
        session_id="s_settings",
        turn_id="t_settings",
        role="lead",
        datarc=None,
        config_lite_supported=True,
        parent_bus=TurnEventBus(),
    )
    child, error = await prepare_configuration_agent(ctx, form_id=form)
    assert child is not None, error
    child.role = "analyst"
    return child


async def call(app, child, name, **args):
    from bowhead_core.agents._lead_context import reset_current_peer_name, set_current_peer_name
    from bowhead_core.skill_bridge import build_default_skill_stack, register_skill_tools

    registry = ToolRegistry()
    register_configuration_agent_tools(registry, access_resolver=configuration_access_resolver(app))
    catalog, runtime = build_default_skill_stack()
    register_skill_tools(registry, catalog, runtime)
    token = set_current_peer_name(AGENT_NAME)
    try:
        if form_id := args.get("form_id"):
            skill = await registry.get("skill_read").handler({"name": FORM_SKILLS[form_id]}, child)
            assert skill.get("body") and not skill.get("error"), skill
        return await registry.get(name).handler(args, child)
    finally:
        reset_current_peer_name(token)


async def propose(app, user, form="team_run_limits", **args):
    child = await child_for(user, form)
    read = await call(app, child, "configuration_read", form_id=form)
    assert read["ok"], read
    result = await call(app, child, "configuration_propose", form_id=form, **args)
    return child, read, result


async def test_configuration_agent_explains_but_cannot_write_system_controls(configuration, client, monkeypatch):
    from bowhead_core.agent_run_limits import get_runtime_controls

    monkeypatch.setenv("BOWHEAD_AGENT_MAX_STEPS", "100")
    get_runtime_controls.cache_clear()
    try:
        child = await child_for(configuration.owner, "team_run_limits")
        result = await call(configuration.app, child, "configuration_read", form_id="team_run_limits")
        assert result["runtime_controls"]["effective_step_limit"] == 100
        assert result["fields"]["step_limit"]["value"] is None
        assert "runtime_controls" not in result["editable_fields"]
        denied = await call(
            configuration.app, child, "configuration_propose", form_id="team_run_limits", patch={"runtime_controls": {"system_step_limit": None}}
        )
        assert not denied.get("ok"), denied
        assert client.get(URL, cookies=_as(configuration.owner)).json()["is_default"]
    finally:
        get_runtime_controls.cache_clear()


async def test_integer_null_confirm_and_reset_use_original_limits(configuration, client):
    ctx = configuration
    _, read, proposal = await propose(ctx.app, ctx.owner, patch={"tool_call_limit": 450, "step_limit": 12})
    assert read["schema"]["properties"]["tool_call_limit"]["type"] == "integer"
    assert read["reset"]["all"] and proposal["ok"], proposal
    assert client.get(URL, cookies=_as(ctx.owner)).json()["tool_call_limit"] == 300
    response = client.post(f"/api/v1/config-proposals/{proposal['proposal_id']}/apply", cookies=_as(ctx.owner))
    assert response.status_code == 200, response.text
    assert client.get(URL, cookies=_as(ctx.owner)).json()["tool_call_limit"] == 450
    _, _, unlimited = await propose(ctx.app, ctx.owner, patch={"step_limit": None})
    assert unlimited["ok"] and unlimited["rows"][0]["to"] is None
    assert client.post(f"/api/v1/config-proposals/{unlimited['proposal_id']}/apply", cookies=_as(ctx.owner)).status_code == 200
    assert client.get(URL, cookies=_as(ctx.owner)).json()["step_limit"] is None
    _, _, reset = await propose(ctx.app, ctx.owner, reset_all=True)
    assert reset["ok"] and all(row["operation"] == "reset" for row in reset["rows"]), reset
    assert client.post(f"/api/v1/config-proposals/{reset['proposal_id']}/apply", cookies=_as(ctx.owner)).status_code == 200
    assert client.get(URL, cookies=_as(ctx.owner)).json()["is_default"]


@pytest.mark.parametrize("patch", [{"tool_call_limit": True}, {"tool_call_limit": "500"}, {"tool_call_limit": 0}, {"turn_time_limit_s": None}])
async def test_typed_invalid_values_never_create_proposals(configuration, patch):
    _, _, result = await propose(configuration.app, configuration.owner, patch=patch)
    assert not result["ok"] and result["error"] == "invalid_patch", result


async def test_directory_filters_roles(configuration):
    admin = await call(configuration.app, await child_for(configuration.owner), "configuration_list")
    member = await call(configuration.app, await child_for(configuration.member), "configuration_list")
    assert "team_run_limits" in {item["form_id"] for item in admin["forms"]}
    assert not any(item["scope"] == "team" for item in member["forms"])


async def test_reset_rejects_concurrent_write(configuration, client):
    ctx = configuration
    initial = client.get(URL, cookies=_as(ctx.owner)).json()
    body = {key: initial[key] for key in ("turn_time_limit_s", "step_limit", "tool_call_limit", "model_call_time_limit_s")}
    body.update(tool_call_limit=400, expected_version=initial["version"])
    assert client.put(URL, json=body, cookies=_as(ctx.owner)).status_code == 200
    _, _, reset = await propose(ctx.app, ctx.owner, reset_all=True)
    current = client.get(URL, cookies=_as(ctx.owner)).json()
    body.update(tool_call_limit=500, expected_version=current["version"])
    assert client.put(URL, json=body, cookies=_as(ctx.owner)).status_code == 200
    path = f"/api/v1/config-proposals/{reset['proposal_id']}/apply"
    assert client.post(path, cookies=_as(ctx.owner)).status_code == 409
    assert client.get(URL, cookies=_as(ctx.owner)).json()["tool_call_limit"] == 500
    assert client.get(f"/api/v1/config-proposals/{reset['proposal_id']}", cookies=_as(ctx.member)).status_code == 404


async def test_role_loss_blocks_confirmation_and_receipt(configuration, client):
    ctx = configuration
    _, _, proposal = await propose(ctx.app, ctx.admin, patch={"tool_call_limit": 900})
    assert proposal["ok"]
    await ctx.users.promote_team_role(user_id=ctx.admin.id, team_id=ctx.team.id, new_role="member", promoted_by=ctx.owner.id)
    path = f"/api/v1/config-proposals/{proposal['proposal_id']}"
    assert client.post(path + "/apply", cookies=_as(ctx.admin)).status_code == 403
    assert client.get(path, cookies=_as(ctx.admin)).status_code == 403
    assert client.get(path, cookies=_as(ctx.other_owner)).status_code == 404
    assert client.get(URL, cookies=_as(ctx.owner)).json()["tool_call_limit"] == 300


async def test_sandbox_and_html_limits_save_reset_independently_of_task_limits(configuration, client, monkeypatch):
    ctx = configuration
    directory = await call(ctx.app, await child_for(ctx.owner), "configuration_list")
    names = {form["form_id"] for form in directory["forms"]}
    assert {"team_execution_limits", "team_run_limits"} <= names
    path = "/api/v1/team-settings/execution-limits"
    _, read, proposal = await propose(
        ctx.app, ctx.owner, "team_execution_limits", patch={"sandbox_default_timeout_s": 60, "sandbox_max_timeout_s": 120, "html_max_bytes": 1048576}
    )
    assert proposal["ok"], proposal
    assert (
        read["schema"]["properties"]["html_max_bytes"]["maximum"]
        == client.get(path, cookies=_as(ctx.owner)).json()["ranges"]["html_max_bytes"]["max"]
    )
    assert client.post(f"/api/v1/config-proposals/{proposal['proposal_id']}/apply", cookies=_as(ctx.owner)).status_code == 200
    saved = client.get(path, cookies=_as(ctx.owner)).json()
    assert (saved["sandbox_default_timeout_s"], saved["sandbox_max_timeout_s"], saved["html_max_bytes"]) == (60, 120, 1048576)
    _, _, reset = await propose(ctx.app, ctx.owner, "team_execution_limits", reset_all=True)
    assert reset["ok"], reset
    assert client.post(f"/api/v1/config-proposals/{reset['proposal_id']}/apply", cookies=_as(ctx.owner)).status_code == 200
    assert client.get(path, cookies=_as(ctx.owner)).json()["is_default"]


async def test_sandbox_cross_field_limits_and_stale_proposals_are_rejected(configuration, client):
    ctx = configuration
    _, _, invalid = await propose(ctx.app, ctx.owner, "team_execution_limits", patch={"sandbox_default_timeout_s": 121, "sandbox_max_timeout_s": 120})
    assert invalid["error"] == "invalid_patch" and "sandbox_default_timeout_s" in str(invalid["issues"])
    _, _, proposal = await propose(ctx.app, ctx.owner, "team_execution_limits", patch={"html_max_bytes": 1048576})
    path = "/api/v1/team-settings/execution-limits"
    current = client.get(path, cookies=_as(ctx.owner)).json()
    body = {key: current[key] for key in current["defaults"]}
    body["html_max_bytes"] = 2097152
    assert client.put(path, json={"limits": body, "expected_version": current["version"]}, cookies=_as(ctx.owner)).status_code == 200
    assert client.post(f"/api/v1/config-proposals/{proposal['proposal_id']}/apply", cookies=_as(ctx.owner)).status_code == 409
    assert client.get(path, cookies=_as(ctx.owner)).json()["html_max_bytes"] == 2097152


async def test_page_help_preserves_settings_and_cannot_grant_member_access(configuration, client):
    before = await configuration.app.state.team_settings_store.get(configuration.owner.team_id, "agent_run_limits")
    response = client.get(URL, cookies=_as(configuration.owner))
    assert response.status_code == 200
    assert response.json()["field_contract"]["domain"] == "team_run_limits"
    assert client.get(URL, cookies=_as(configuration.member)).status_code == 403
    assert await configuration.app.state.team_settings_store.get(configuration.owner.team_id, "agent_run_limits") == before
