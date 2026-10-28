"""Real personal settings services behind bounded Lead delegation and confirmation."""

from __future__ import annotations

from dataclasses import replace

import pytest
import test_personal_theme_routes as personal_routes
from bowhead_core.config_copilot import get_form, register_form, reset_registry_for_tests
from bowhead_core.config_copilot_chat_tools import set_proposal_writer_provider
from bowhead_core.config_draft_store import ConfigDraftStore
from bowhead_core.configuration_agent import AGENT_NAME, prepare_configuration_agent, register_configuration_agent_tools
from bowhead_core.events import TurnEventBus
from bowhead_core.tool_registry import ToolExecutionContext, ToolRegistry
from bowhead_gateway.config_copilot_forms import register_gateway_forms
from bowhead_gateway.routes import config_proposals
from test_personal_theme_routes import _as

app_ctx = personal_routes.app_ctx
client = personal_routes.client


@pytest.fixture
async def settings_ctx(app_ctx, monkeypatch):
    reset_registry_for_tests()
    register_gateway_forms(app_ctx.app)
    app_ctx.app.state.config_draft_store = ConfigDraftStore(app_ctx.user_store.db_path)
    await app_ctx.app.state.config_draft_store.initialize()
    app_ctx.app.include_router(config_proposals.router)
    set_proposal_writer_provider(lambda: app_ctx.app.state.config_draft_store)
    yield app_ctx
    set_proposal_writer_provider(None)
    reset_registry_for_tests()


def _lead(ctx, *, user_text="Change my appearance to dark and compact."):
    return ToolExecutionContext(
        user_id=ctx.member.id,
        team_id=ctx.member.team_id,
        member_uid=ctx.member.id,
        workspace_id="",
        session_id="s_personal",
        turn_id="t_personal",
        role="lead",
        datarc=None,
        config_lite_supported=True,
        user_text=user_text,
        parent_bus=TurnEventBus(),
    )


async def _call(ctx, child, name, **args):
    from bowhead_core.agents._lead_context import reset_current_peer_name, set_current_peer_name
    from bowhead_gateway.configuration_agent_access import configuration_access_resolver

    registry = ToolRegistry()
    register_configuration_agent_tools(registry, access_resolver=configuration_access_resolver(ctx.app))
    child.role = "analyst"
    token = set_current_peer_name(AGENT_NAME)
    try:
        from bowhead_core.configuration_agent import FORM_SKILLS
        from bowhead_core.skill_bridge import build_default_skill_stack, register_skill_tools

        catalog, runtime = build_default_skill_stack()
        register_skill_tools(registry, catalog, runtime)
        skill = await registry.get("skill_read").handler({"name": FORM_SKILLS[args["form_id"]]}, child)
        assert skill.get("body") and not skill.get("error"), skill
        return await registry.get(name).handler(args, child)
    finally:
        reset_current_peer_name(token)


async def _propose(ctx, patch):
    child, _ = await prepare_configuration_agent(_lead(ctx), form_id="personal_appearance")
    assert (await _call(ctx, child, "configuration_read", form_id="personal_appearance"))["ok"]
    return await _call(ctx, child, "configuration_propose", form_id="personal_appearance", patch=patch)


async def test_personal_read_explains_registered_fields_and_requires_confirmation(settings_ctx):
    child, _ = await prepare_configuration_agent(_lead(settings_ctx), form_id="personal_appearance")
    read = await _call(settings_ctx, child, "configuration_read", form_id="personal_appearance")
    assert read["ok"]
    assert set(read["schema"]["properties"]) == set(read["fields"]) == set(read["editable_fields"])
    assert all(field["description"] for field in read["schema"]["properties"].values())
    assert read["guidance"] == get_form("personal_appearance").guidance
    assert read["save_policy"] == "user_confirmation" and "apply" not in read["actions"]
    assert read["schema"]["properties"]["mode"]["anyOf"][0]["enum"] == read["allowed_values"]["mode"]


async def test_v3_confirmation_and_undo_share_the_original_page_service(settings_ctx, client):
    ctx = settings_ctx
    before = client.get("/api/v1/theme/personal", cookies=_as(ctx.member)).json()
    receipt = await _propose(ctx, {"mode": "dark", "density": "compact", "typographyProfile": "readable"})
    assert receipt["ok"] and not receipt.get("applied")
    assert client.get("/api/v1/theme/personal", cookies=_as(ctx.member)).json() == before
    pid = receipt["proposal_id"]
    applied = client.post(f"/api/v1/config-proposals/{pid}/apply", cookies=_as(ctx.member))
    assert applied.status_code == 200, applied.text
    current = client.get("/api/v1/theme/personal", cookies=_as(ctx.member))
    assert current.json()["overrides"] == {"mode": "dark", "density": "compact", "typographyProfile": {"type": "preset", "id": "readable"}}
    assert await ctx.user_store.get(ctx.member.id, "appearance_theme_config") is None
    manual = client.patch(
        "/api/v1/theme/personal", cookies=_as(ctx.member), headers={"If-Match": current.headers["etag"]}, json={"changes": {"radius": "soft"}}
    )
    assert manual.status_code == 200
    undone = client.post(f"/api/v1/config-proposals/{pid}/undo", cookies=_as(ctx.member))
    assert undone.status_code == 200, undone.text
    assert client.get("/api/v1/theme/personal", cookies=_as(ctx.member)).json()["overrides"] == {"radius": "soft"}


async def test_manual_save_before_confirmation_rejects_all_candidate_fields(settings_ctx, client):
    receipt = await _propose(settings_ctx, {"mode": "dark", "density": "compact"})
    current = client.get("/api/v1/theme/personal", cookies=_as(settings_ctx.member))
    manual = client.patch(
        "/api/v1/theme/personal",
        cookies=_as(settings_ctx.member),
        headers={"If-Match": current.headers["etag"]},
        json={"changes": {"mode": "light"}},
    )
    assert manual.status_code == 200
    failed = client.post(f"/api/v1/config-proposals/{receipt['proposal_id']}/apply", cookies=_as(settings_ctx.member))
    assert failed.status_code == 409
    assert failed.json()["detail"]["error"]["code"] == "configuration_baseline_changed"
    assert client.get("/api/v1/theme/personal", cookies=_as(settings_ctx.member)).json()["overrides"] == {"mode": "light"}


async def test_manual_save_during_confirmation_hits_v3_occ(settings_ctx, client, monkeypatch):
    receipt = await _propose(settings_ctx, {"mode": "dark"})
    service = settings_ctx.app.state.personal_appearance_service
    original = service.patch

    async def racing_patch(**kwargs):
        await original(**{**kwargs, "changes": {"radius": "soft"}})
        return await original(**kwargs)

    monkeypatch.setattr(service, "patch", racing_patch)
    failed = client.post(f"/api/v1/config-proposals/{receipt['proposal_id']}/apply", cookies=_as(settings_ctx.member))
    assert failed.status_code == 409
    assert failed.json()["detail"]["error"]["code"] == "configuration_baseline_changed"
    assert client.get("/api/v1/theme/personal", cookies=_as(settings_ctx.member)).json()["overrides"] == {"radius": "soft"}


async def test_undo_preserves_a_later_manual_change(settings_ctx, client):
    receipt = await _propose(settings_ctx, {"mode": "dark"})
    pid = receipt["proposal_id"]
    assert client.post(f"/api/v1/config-proposals/{pid}/apply", cookies=_as(settings_ctx.member)).status_code == 200
    current = client.get("/api/v1/theme/personal", cookies=_as(settings_ctx.member))
    client.patch(
        "/api/v1/theme/personal", cookies=_as(settings_ctx.member), headers={"If-Match": current.headers["etag"]}, json={"changes": {"mode": "light"}}
    )
    failed = client.post(f"/api/v1/config-proposals/{pid}/undo", cookies=_as(settings_ctx.member))
    assert failed.status_code == 409
    assert client.get("/api/v1/theme/personal", cookies=_as(settings_ctx.member)).json()["overrides"] == {"mode": "light"}


async def test_persistent_language_staging_is_zero_write_then_specialist_applies(settings_ctx):
    lead = _lead(settings_ctx, user_text="From now on, always answer in English.")
    child, instructions = await prepare_configuration_agent(lead, form_id="user_language", intent_quote=lead.user_text)
    assert child is not None and lead.user_text in instructions
    assert (await _call(settings_ctx, child, "configuration_read", form_id="user_language"))["ok"]
    staged = await _call(settings_ctx, child, "configuration_propose", form_id="user_language", patch={"output_language": "en-US"})
    assert staged["ok"] and not staged["saved"]
    assert staged["confirmation_event_emitted"] is False and staged["ui_status"] == "unacknowledged"
    assert await settings_ctx.user_store.get(settings_ctx.member.id, "output_language") is None
    receipt = await _call(settings_ctx, child, "configuration_apply", form_id="user_language", proposal_id=staged["proposal_id"])
    assert receipt["saved"] and receipt["form_id"] == "user_language"
    assert await settings_ctx.user_store.get(settings_ctx.member.id, "output_language") == "en-US"
    assert not lead.parent_bus.snapshot()
    assert await _call(settings_ctx, child, "configuration_apply", form_id="user_language", proposal_id=staged["proposal_id"]) == receipt


async def test_one_turn_language_instruction_does_not_admit_a_saved_patch(settings_ctx):
    lead = _lead(settings_ctx, user_text="Write this paragraph in English.")
    child, _ = await prepare_configuration_agent(lead, form_id="user_language")
    assert child is not None
    assert (await _call(settings_ctx, child, "configuration_read", form_id="user_language"))["editable_fields"] == []
    assert (await _call(settings_ctx, child, "configuration_propose", form_id="user_language", patch={"output_language": "en-US"}))[
        "error"
    ] == "persistent_intent_required"
    assert await settings_ctx.user_store.get(settings_ctx.member.id, "output_language") is None


async def test_language_apply_compares_the_value_inside_the_write_transaction(settings_ctx):
    lead = _lead(settings_ctx, user_text="Always answer in English from now on.")
    child, _ = await prepare_configuration_agent(lead, form_id="user_language", intent_quote=lead.user_text)
    assert (await _call(settings_ctx, child, "configuration_read", form_id="user_language"))["ok"]
    staged = await _call(settings_ctx, child, "configuration_propose", form_id="user_language", patch={"output_language": "en-US"})
    assert staged["ok"] and not staged["saved"]
    registration = get_form("user_language")

    async def race(ctx, rows, op):
        await settings_ctx.user_store.set(settings_ctx.member.id, "output_language", "fr-FR")
        return await registration.lite_applier(ctx, rows, op)

    register_form(replace(registration, lite_applier=race))
    receipt = await _call(settings_ctx, child, "configuration_apply", form_id="user_language", proposal_id=staged["proposal_id"])
    assert receipt["error"] == "configuration_baseline_changed"
    assert await settings_ctx.user_store.get(settings_ctx.member.id, "output_language") == "fr-FR"


@pytest.mark.parametrize("patch", [{"accent": "ocean"}, {"rowVersion": "1"}, {"output_language": "en-US"}, {"system_prompt": "changed"}])
async def test_personal_appearance_cannot_submit_other_domains_or_metadata(settings_ctx, patch):
    child, _ = await prepare_configuration_agent(_lead(settings_ctx), form_id="personal_appearance")
    await _call(settings_ctx, child, "configuration_read", form_id="personal_appearance")
    assert (await _call(settings_ctx, child, "configuration_propose", form_id="personal_appearance", patch=patch))[
        "error"
    ] == "invalid_configuration_fields"
    assert (await settings_ctx.personal_store.read(settings_ctx.member.id)).config_raw is None


async def test_domain_tools_recheck_actual_membership_and_owned_proposal(settings_ctx, monkeypatch):
    child, _ = await prepare_configuration_agent(_lead(settings_ctx), form_id="personal_appearance")
    assert (await _call(settings_ctx, child, "configuration_read", form_id="personal_appearance"))["ok"]
    staged = await _call(settings_ctx, child, "configuration_propose", form_id="personal_appearance", patch={"mode": "dark"})
    assert staged["ok"]
    denied = await _call(settings_ctx, child, "configuration_apply", form_id="personal_appearance", proposal_id=staged["proposal_id"])
    assert denied["error"] == "user_confirmation_required"
    missing = await _call(settings_ctx, child, "configuration_result", form_id="personal_appearance", proposal_id="ccd_foreign")
    assert missing["error"] == "configuration_proposal_not_found"

    async def revoked(*args):
        return False

    monkeypatch.setattr(settings_ctx.app.state.user_store, "user_has_active_team_membership", revoked)
    for tool in ("configuration_read", "configuration_propose", "configuration_result"):
        denied = await _call(settings_ctx, child, tool, form_id="personal_appearance", patch={"mode": "dark"}, proposal_id=staged["proposal_id"])
        assert denied["error"] == "configuration_access_denied"
    assert (await settings_ctx.personal_store.read(settings_ctx.member.id)).config_raw is None


async def test_entitlement_uses_the_original_execution_team_without_http_auth_fallback(settings_ctx, monkeypatch):
    from types import SimpleNamespace

    from bowhead_gateway.configuration_agent_access import configuration_access_resolver

    seen = []

    async def check(team_id):
        seen.append(team_id)

    monkeypatch.setattr(settings_ctx.app.state, "entitlements", SimpleNamespace(assert_can_run_turn=check), raising=False)
    resolve = configuration_access_resolver(settings_ctx.app)
    lead = _lead(settings_ctx)
    assert not (await resolve(lead, "personal_appearance", False))["configuration_readonly"]
    await resolve(lead, "personal_appearance", True)
    assert seen == [lead.team_id, lead.team_id]

    async def denied(team_id):
        raise PermissionError("read-only entitlement")

    monkeypatch.setattr(settings_ctx.app.state, "entitlements", SimpleNamespace(assert_can_run_turn=denied))
    assert (await resolve(lead, "personal_appearance", False))["configuration_readonly"]
    with pytest.raises(PermissionError):
        await resolve(lead, "personal_appearance", True)


@pytest.mark.parametrize("closeout_succeeds", [False, True])
async def test_saved_language_survives_empty_closeout_without_another_apply(settings_ctx, monkeypatch, closeout_succeeds):
    from bowhead_core.agents import AgentRegistry
    from bowhead_core.agents._lead_context import reset_current_peer_name, set_current_peer_name
    from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
    from bowhead_core.configuration_agent import configuration_agent_result
    from bowhead_core.subagent import PROFILES, SubagentRunner
    from bowhead_gateway.configuration_agent_access import configuration_access_resolver

    from bowhead_router import AgentBinding, LLMChunk, LLMResponse, MockAdapter, ProviderConfig, ProviderRegistry, Router, ToolCall

    class ScriptedSettingsAdapter(MockAdapter):
        async def stream(self, request):
            response = await self.invoke(request)
            for call in response.tool_calls:
                yield LLMChunk(tool_call_delta=call)
            yield LLMChunk(finish_reason=response.finish_reason, usage=response.usage)

    lead = _lead(settings_ctx, user_text="From now on, always answer in English.")
    child, _ = await prepare_configuration_agent(lead, form_id="user_language", intent_quote=lead.user_text)
    assert (await _call(settings_ctx, child, "configuration_read", form_id="user_language"))["ok"]
    staged = await _call(settings_ctx, child, "configuration_propose", form_id="user_language", patch={"output_language": "en-US"})
    args = {"form_id": "user_language", "proposal_id": staged["proposal_id"]}
    message = "Your persistent answer language was saved as English."
    adapter = ScriptedSettingsAdapter(
        scripted_responses=[
            LLMResponse(content="", tool_calls=[ToolCall(id="apply", name="configuration_apply", arguments=args)]),
            LLMResponse(content=""),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="repeat", name="configuration_apply", arguments=args),
                    ToolCall(id="submit", name="configuration_submit", arguments={"message": message}),
                ]
                if closeout_succeeds
                else [],
            ),
        ],
        raise_on_exhaust=True,
    )
    providers = ProviderRegistry()
    providers.load([ProviderConfig(name="mock", type="mock")])
    providers._adapters["mock"] = adapter
    router = Router(providers)
    router.bind(AgentBinding(agent_type="analyst", primary=("mock", "model")))
    registry = ToolRegistry()
    register_configuration_agent_tools(registry, access_resolver=configuration_access_resolver(settings_ctx.app))
    monkeypatch.setitem(PROFILES, AGENT_NAME, manifest_to_profile(AgentRegistry.bootstrap().get(AGENT_NAME)))
    token = set_current_peer_name(AGENT_NAME)
    try:
        result = await SubagentRunner(router=router, tool_registry=registry).run(
            profile_name=AGENT_NAME,
            task=lead.user_text,
            parent_turn_id=lead.turn_id,
            parent_bus=child.parent_bus,
            parent_ctx=child,
        )
    finally:
        reset_current_peer_name(token)
    assert result.ok is closeout_succeeds
    assert await settings_ctx.user_store.get(settings_ctx.member.id, "output_language") == "en-US"
    receipt = configuration_agent_result(child)
    assert receipt["receipts"][-1]["saved"] is True and receipt["receipts"][-1]["status"] == "applied"
    assert len(receipt["receipts"]) == 3
    assert [inv.tool_name for inv in result.tool_invocations] == ["configuration_apply"] + (["configuration_submit"] if closeout_succeeds else [])
    assert adapter._script_index == 3


async def test_scalar_reset_removes_override_and_undo_restores_it(settings_ctx, client):
    ctx = settings_ctx
    initial = await _propose(ctx, {"mode": "dark", "density": "compact"})
    assert client.post(f"/api/v1/config-proposals/{initial['proposal_id']}/apply", cookies=_as(ctx.member)).status_code == 200
    child, _ = await prepare_configuration_agent(_lead(ctx), form_id="personal_appearance")
    read = await _call(ctx, child, "configuration_read", form_id="personal_appearance")
    reset = await _call(ctx, child, "configuration_propose", form_id="personal_appearance", reset=["mode"])
    assert reset["ok"], reset
    assert reset["rows"][0]["operation"] == "reset"
    assert reset["rows"][0]["to"] == read["fields"]["mode"]["default"]
    path = f"/api/v1/config-proposals/{reset['proposal_id']}"
    assert client.post(path + "/apply", cookies=_as(ctx.member)).status_code == 200
    assert client.get("/api/v1/theme/personal", cookies=_as(ctx.member)).json()["overrides"] == {"density": "compact"}
    assert client.post(path + "/undo", cookies=_as(ctx.member)).status_code == 200
    assert client.get("/api/v1/theme/personal", cookies=_as(ctx.member)).json()["overrides"] == {"mode": "dark", "density": "compact"}
