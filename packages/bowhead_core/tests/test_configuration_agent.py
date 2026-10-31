"""Settings delegation preserves caller scope, staging and domain authority."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.agents import AgentRegistry
from bowhead_core.agents._lead_context import reset_current_peer_name, set_current_peer_name
from bowhead_core.agents.agent_runner_adapter import manifest_to_profile, register_manifest_as_profile
from bowhead_core.config_copilot import (
    POLICY_GENERATED,
    POLICY_SECRET,
    POLICY_SERVER_OWNED,
    FormRegistration,
    MaterializeResult,
    get_form,
    register_form,
    reset_registry_for_tests,
)
from bowhead_core.config_copilot_chat_tools import register_config_copilot_chat_tools, set_proposal_writer_provider
from bowhead_core.configuration_agent import AGENT_NAME, TOOL_SUBMIT, configuration_agent_result, prepare_configuration_agent
from bowhead_core.events import TurnEventBus
from bowhead_core.skill_bridge import build_default_skill_stack, register_skill_tools
from bowhead_core.skill_upload import bundle_sha256
from bowhead_core.subagent import PROFILES, SubagentRunner, register_subagent_tool
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry
from pydantic import BaseModel, ConfigDict, Field
from test_agent_execution_binding import _setup

from bowhead_router import AgentBinding, LLMChunk, LLMResponse, MockAdapter, ProviderConfig, ProviderRegistry, Router, ToolCall

pytestmark = pytest.mark.unit


class SpacePreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    week_start_day: Literal["monday", "sunday"] | None = None
    default_data_language: Literal["zh-CN", "en-US"] | None = None


class ProposalWriter:
    def __init__(self):
        self.proposals = []

    async def create_lite_proposal(self, **kwargs):
        self.proposals.append(kwargs)


class StreamingToolAdapter(MockAdapter):
    async def invoke(self, request):
        if not hasattr(self, "requests"):
            self.requests = []
        self.requests.append(request)
        return await super().invoke(request)

    async def stream(self, request):
        response = await self.invoke(request)
        if response.raw.get("simulate_error"):
            raise ValueError("Simulated provider failure after settings operations.")
        if response.content:
            yield LLMChunk(delta=response.content)
        for call in response.tool_calls:
            yield LLMChunk(tool_call_delta=call)
        yield LLMChunk(finish_reason=response.finish_reason, usage=response.usage)


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", "1")
    reset_registry_for_tests()
    current = {"week_start_day": "monday", "default_data_language": "zh-CN"}
    applied = []

    async def read(ctx):
        return {"workspace_id": ctx.workspace_id, "fields": {key: {"value": value, "unset": False} for key, value in current.items()}}

    async def materialize(ctx, patch):
        return MaterializeResult(normalized=patch)

    async def apply(ctx, rows, op):
        applied.append((ctx, rows, op))
        return []

    register_form(
        FormRegistration(
            form_id="workspace_i18n",
            schema=SpacePreferences,
            guidance="Use the registered Space preferences.",
            field_policy=dict.fromkeys(current, POLICY_GENERATED),
            context_provider=read,
            materializer=materialize,
            risk_tier=1,
            read_accessor=read,
            read_scope="ws_member",
            write_scope="ws_admin",
            lite_applier=apply,
            write_descriptor="Existing Space settings service",
        )
    )
    writer = ProposalWriter()
    set_proposal_writer_provider(lambda: writer)
    original_profiles = dict(PROFILES)
    register_manifest_as_profile(AgentRegistry.bootstrap().get(AGENT_NAME))
    yield current, applied, writer
    reset_registry_for_tests()
    set_proposal_writer_provider(None)
    PROFILES.clear()
    PROFILES.update(original_profiles)


def context(*, admin=True, web=True):
    return ToolExecutionContext(
        workspace_id="ws_original",
        team_id="team_original",
        user_id="u_original",
        member_uid="m_original",
        turn_id="t_original",
        session_id="s_original",
        datarc=None,
        role="lead",
        access_context={"team_role": "member", "workspace_role": "ws_admin" if admin else "member"},
        config_lite_supported=web,
        parent_bus=TurnEventBus(),
    )


async def test_context_factory_and_pause_snapshot_preserve_the_target_restriction(settings):
    from bowhead_core.agents.lead_orchestrator import _clarification_resume_context
    from bowhead_core.configuration_settings_target import (
        SETTINGS_TARGET_CLIENT_KEY,
        AdmittedWorkspaceSettingsTarget,
        WorkspaceSettingsSendTarget,
        unrestored_settings_target,
        validate_settings_target,
    )
    from bowhead_core.turn_runner import build_tool_exec_ctx

    checks = []

    async def recheck():
        checks.append(True)

    target = WorkspaceSettingsSendTarget(
        form_id="workspace_i18n",
        user_id="u_original",
        team_id="team_original",
        workspace_id="ws_original",
        edit_generation=3,
        saved_ready=True,
        pending_writes=0,
        unconfirmed_fields=[],
    )
    admitted = AdmittedWorkspaceSettingsTarget(target, recheck)
    request = SimpleNamespace(**target.projection(), configuration_settings_target=admitted, team_router=object())
    ctx = build_tool_exec_ctx(SimpleNamespace(tool_registry=build_default_registry()), request, None)
    assert ctx.configuration_settings_target is admitted
    assert await validate_settings_target(ctx, "workspace_i18n") is None and checks == [True]
    snapshot = _clarification_resume_context(SimpleNamespace(turn_id="t_original", session_id="s_original"), "Change week start", ctx)
    assert snapshot[SETTINGS_TARGET_CLIENT_KEY] == {"requires_new_send": True}
    ctx.configuration_settings_target = unrestored_settings_target(snapshot)
    assert (await validate_settings_target(ctx, "workspace_i18n"))["error"] == "configuration_target_not_admitted"


async def access(ctx, form_id, write):
    permissions = dict(ctx.access_context or {})
    if write and form_id == "workspace_i18n" and permissions.get("workspace_role") != "ws_admin":
        raise PermissionError("Space admin required")
    return permissions


async def read_matching_skill(registry, child, form_id):
    from bowhead_core.configuration_agent import FORM_SKILLS

    catalog, runtime = build_default_skill_stack()
    register_skill_tools(registry, catalog, runtime)
    result = await registry.get("skill_read").handler({"name": FORM_SKILLS[form_id]}, child)
    assert result.get("body") and not result.get("error"), result


async def call(child, name, *, read_skill=True, **args):

    registry = build_default_registry()
    register_config_copilot_chat_tools(registry, configuration_access_resolver=access)
    token = set_current_peer_name(AGENT_NAME)
    child.role = "analyst"
    try:
        form_id = args.get("form_id") or getattr(getattr(child, "_configuration_agent_binding", None), "form_id", "")
        if read_skill and form_id in {"workspace_i18n", "workspace_metadata", "personal_appearance", "user_language"}:
            await read_matching_skill(registry, child, form_id)
        return await registry.get(name).handler(args, child)
    finally:
        reset_current_peer_name(token)


async def test_specialist_reads_and_stages_without_lead_or_settings_writes(settings):
    current, applied, writer = settings
    lead = context()
    child, instructions = await prepare_configuration_agent(lead, form_id="workspace_i18n")
    assert "snapshot" not in instructions
    assert not writer.proposals and not applied
    read = await call(child, "configuration_read", form_id="workspace_i18n")
    assert read["editable_fields"] == ["default_data_language", "week_start_day"]
    receipt = await call(child, "configuration_propose", form_id="workspace_i18n", patch={"week_start_day": "sunday"})
    assert receipt["ok"] and receipt["status"] == "proposed" and not receipt["saved"]
    assert receipt["confirmation_event_emitted"] is True and receipt["ui_status"] == "unacknowledged"
    assert len(writer.proposals) == 1 and current["week_start_day"] == "monday" and not applied
    await call(child, TOOL_SUBMIT, message="Please confirm the suggestion.")
    assert configuration_agent_result(child)["receipts"] == [read, receipt]
    assert not hasattr(lead, "_m137_returned_payload")


async def test_read_required_baseline_conflict_and_unknown_fields(settings):
    child, _ = await prepare_configuration_agent(context(), form_id="workspace_i18n")
    assert (await call(child, "configuration_propose", form_id="workspace_i18n", patch={"week_start_day": "sunday"}))[
        "error"
    ] == "configuration_read_required"
    await call(child, "configuration_read", form_id="workspace_i18n")
    assert (await call(child, "configuration_propose", form_id="workspace_i18n", patch={"default_ui_language": "en-US"}))[
        "error"
    ] == "invalid_configuration_fields"
    settings[0]["week_start_day"] = "sunday"
    receipt = await call(child, "configuration_propose", form_id="workspace_i18n", patch={"default_data_language": "en-US"})
    assert receipt["error"] == "configuration_baseline_changed"
    assert not settings[2].proposals and not settings[1]


async def test_read_projects_authorized_space_and_default_sources_without_changing_baseline(settings):
    from bowhead_core.config_copilot import get_form

    async def read(ctx):
        return {
            "workspace_id": ctx.workspace_id,
            "workspace_name": "Authorized Space",
            "fields": {
                "week_start_day": {"value": "monday", "unset": True},
                "default_data_language": {"value": "en-US", "unset": False},
                "default_ui_language": {"value": "fr-FR", "unset": False},
            },
        }

    register_form(replace(get_form("workspace_i18n"), read_accessor=read))
    child, _ = await prepare_configuration_agent(context(), form_id="workspace_i18n")
    child.workspace_name = "Stale connection label"
    receipt = await call(child, "configuration_read", form_id="workspace_i18n")
    assert receipt["target"] == {
        "user_id": "u_original",
        "team_id": "team_original",
        "workspace_id": "ws_original",
        "workspace_name": "Authorized Space",
    }
    assert receipt["required_skill"] == "settings-space-preferences"
    assert receipt["fields"] == {
        "week_start_day": {"value": "monday", "unset": True, "value_source": "default"},
        "default_data_language": {"value": "en-US", "unset": False, "value_source": "saved"},
    }
    assert "value_source" not in child._configuration_agent_binding.baselines["workspace_i18n"]["fields"]["week_start_day"]


async def test_registered_field_and_documentation_updates_reach_reads_and_proposals(settings):
    child, _ = await prepare_configuration_agent(context(), form_id="workspace_i18n")
    first = await call(child, "configuration_read", form_id="workspace_i18n")
    assert "report_label" not in first["schema"]["properties"]

    class UpdatedPreferences(SpacePreferences):
        report_label: str | None = Field(default=None, max_length=24, description="Label used for this Space's reports.")

    settings[0]["report_label"] = "Finance"
    registration = get_form("workspace_i18n")
    register_form(
        replace(
            registration,
            schema=UpdatedPreferences,
            field_policy={**registration.field_policy, "report_label": POLICY_GENERATED},
            guidance="Change only the requested report label or preference.",
        )
    )
    # Registration alone cannot authorize a write to a field absent from this execution's read.
    unread = await call(child, "configuration_propose", form_id="workspace_i18n", patch={"report_label": "Sales"})
    assert unread["error"] == "invalid_configuration_fields"
    read = await call(child, "configuration_read", form_id="workspace_i18n")
    assert read["schema"]["properties"]["report_label"]["description"] == "Label used for this Space's reports."
    assert read["field_constraints"]["report_label"] == {"maxLength": 24}
    assert read["guidance"] == "Change only the requested report label or preference."
    assert "report_label" in read["editable_fields"]
    assert read["fields"]["report_label"]["value"] == "Finance"
    invalid = await call(child, "configuration_propose", form_id="workspace_i18n", patch={"report_label": "x" * 25})
    assert invalid["error"] == "invalid_patch" and not settings[2].proposals
    proposal = await call(child, "configuration_propose", form_id="workspace_i18n", patch={"report_label": "Sales"})
    assert proposal["ok"] and not proposal["saved"]
    assert settings[2].proposals[0]["rows"][0]["to"] == "Sales"


async def test_read_contract_excludes_secret_server_owned_and_unread_fields(settings):
    class PrivateConnection(BaseModel):
        token: str = Field(description="Private credential metadata.")

    class RestrictedPreferences(SpacePreferences):
        credential: PrivateConnection | None = None
        internal_mode: str | None = None
        headers: dict[str, str] | None = None
        unread_choice: str | None = None

    settings[0].update(credential={"token": "private-value"}, internal_mode="private-mode", headers={"Authorization": "private-header"})
    registration = get_form("workspace_i18n")
    register_form(
        replace(
            registration,
            schema=RestrictedPreferences,
            field_policy={
                **registration.field_policy,
                "credential": POLICY_SECRET,
                "internal_mode": POLICY_SERVER_OWNED,
                "headers": POLICY_GENERATED,
                "unread_choice": POLICY_GENERATED,
            },
            secret_value_fields=frozenset({"headers"}),
        )
    )
    child, _ = await prepare_configuration_agent(context(admin=False), form_id="workspace_i18n")
    read = await call(child, "configuration_read", form_id="workspace_i18n")
    assert set(read["schema"]["properties"]) == set(read["fields"]) == {"week_start_day", "default_data_language"}
    assert "$defs" not in read["schema"]
    assert read["editable_fields"] == [] and read["actions"] == ["read"]
    assert "private-" not in json.dumps(read)
    assert "Private credential metadata" not in json.dumps(read)
    for field in ("credential", "internal_mode", "headers", "unread_choice"):
        denied = await call(child, "configuration_propose", form_id="workspace_i18n", patch={field: "changed"})
        assert not denied["ok"]
    assert not settings[2].proposals and not settings[1]


@pytest.mark.parametrize("cache", ["0", "1"])
async def test_matching_skill_is_required_for_staging_and_read_only_delivery(settings, monkeypatch, cache):
    monkeypatch.setenv("BOWHEAD_SKILL_READ_CACHE", cache)
    child, _ = await prepare_configuration_agent(context(), form_id="workspace_i18n")
    read = await call(child, "configuration_read", read_skill=False, form_id="workspace_i18n")
    required = {"name": "skill_read", "arguments": {"name": "settings-space-preferences"}}
    rejected = await call(child, TOOL_SUBMIT, read_skill=False, message="Read the current Space preferences.")
    assert rejected["error"] == "configuration_skill_required" and rejected["required_tool"] == required
    assert not child._configuration_agent_binding.finished
    token = set_current_peer_name(AGENT_NAME)
    try:
        await read_matching_skill(build_default_registry(), child, "user_language")
    finally:
        reset_current_peer_name(token)
    rejected = await call(child, "configuration_propose", read_skill=False, form_id="workspace_i18n", patch={"week_start_day": "sunday"})
    assert rejected["error"] == "configuration_skill_required" and rejected["required_tool"] == required
    assert not settings[2].proposals and not settings[1]
    assert (await call(child, TOOL_SUBMIT, message="Read the current Space preferences."))["ok"]
    assert configuration_agent_result(child)["receipts"] == [read, rejected]
    next_parent = context()
    next_parent._ξ_skill_cache = child._ξ_skill_cache
    next_child, _ = await prepare_configuration_agent(next_parent, form_id="workspace_i18n")
    await call(next_child, "configuration_read", read_skill=False, form_id="workspace_i18n")
    assert (await call(next_child, TOOL_SUBMIT, read_skill=False, message="Read preferences."))["error"] == "configuration_skill_required"
    assert (await call(next_child, TOOL_SUBMIT, message="Read preferences."))["ok"]


async def test_domain_denial_can_be_delivered_without_a_successful_skill_read(settings):
    child, _ = await prepare_configuration_agent(context(), form_id="workspace_i18n")
    denied = await call(child, "configuration_read", read_skill=False, form_id="workspace_metadata")
    assert denied["ok"] is False
    assert (await call(child, TOOL_SUBMIT, read_skill=False, message="This execution cannot access the requested form."))["ok"]
    assert not settings[2].proposals and not settings[1]


async def test_cached_sibling_read_cannot_bypass_current_skill_authorization(settings):
    from bowhead_skill.runtime_scope import ExactSkillRuntimeScope

    child, _ = await prepare_configuration_agent(context(), form_id="workspace_i18n")
    registry = build_default_registry()
    catalog, runtime = build_default_skill_stack()
    register_skill_tools(registry, catalog, runtime)
    args = {"name": "settings-space-preferences"}
    token = set_current_peer_name("other-agent")
    try:
        assert (await registry.get("skill_read").handler(args, child))["body"]
    finally:
        reset_current_peer_name(token)
    assert not child._configuration_agent_binding.read_skills
    child.skill_runtime_scope = ExactSkillRuntimeScope(read_names=(), invoke_names=())
    token = set_current_peer_name(AGENT_NAME)
    try:
        assert (await registry.get("skill_read").handler(args, child))["ok"] is False
        assert not child._configuration_agent_binding.read_skills
        child.skill_runtime_scope = None
        await registry.get("skill_read").handler({**args, "file": "SKILL.md"}, child)
        assert not child._configuration_agent_binding.read_skills
        assert (await registry.get("skill_read").handler(args, child))["body"]
        assert child._configuration_agent_binding.read_skills == {args["name"]}
    finally:
        reset_current_peer_name(token)


async def test_caller_scope_and_live_permissions_rechecked(settings):
    child, _ = await prepare_configuration_agent(context(), form_id="workspace_i18n")
    await call(child, "configuration_read", form_id="workspace_i18n")
    child.access_context["workspace_role"] = "member"
    assert (await call(child, "configuration_propose", form_id="workspace_i18n", patch={"week_start_day": "sunday"}))[
        "error"
    ] == "configuration_access_denied"
    read = await call(child, "configuration_read", form_id="workspace_i18n")
    assert read["ok"] and not read["editable_fields"]
    child.workspace_id = "ws_other"
    assert (await call(child, "configuration_read", form_id="workspace_i18n"))["error"] == "configuration_execution_not_admitted"


def lead_context(*, specialist_in_roster: bool):
    """Lead context with the Turn's frozen actor-scoped roster, as _gather_actor_scoped_prompt_peers leaves it."""
    lead = context()
    names = ("compute", AGENT_NAME) if specialist_in_roster else ("compute",)
    lead._actor_scoped_prompt_peers = tuple(SimpleNamespace(name=name) for name in names)
    return lead


@pytest.mark.parametrize("specialist_in_roster", [True, False])
async def test_lead_cannot_execute_supported_configuration_tools(settings, specialist_in_roster):
    from bowhead_core.config_copilot_chat_tools import _propose_handler, _read_handler

    lead = lead_context(specialist_in_roster=specialist_in_roster)
    read = await _read_handler({"form_id": "workspace_i18n"}, lead)
    propose = await _propose_handler({"form_id": "workspace_i18n", "patch": {"week_start_day": "sunday"}}, lead)
    for refusal in (read, propose):
        assert refusal["ok"] is False
        if specialist_in_roster:
            assert refusal == {"ok": False, "error": "configuration_agent_delegation_required", "profile": AGENT_NAME}
        else:
            # An absent specialist is a configuration state; the refusal never names it.
            assert refusal["error"] == "configuration_settings_unavailable"
            assert AGENT_NAME not in repr(refusal)
    assert not settings[1] and not settings[2].proposals


async def test_open_is_a_precise_pointer_and_never_a_save(settings, monkeypatch):
    child, _ = await prepare_configuration_agent(context(), form_id="workspace_i18n")
    result = await call(child, "configuration_open", form_id="workspace_i18n")
    assert result["status"] == "open_requested" and result["saved"] is False
    assert result["ui_status"] == "unacknowledged"
    event = child.parent_bus.snapshot()[0]
    assert event.event_type == "configuration.canvas_open" and event.payload["target"]["workspace_id"] == "ws_original"
    assert not settings[2].proposals and not settings[1]
    assert len(child.parent_bus.snapshot()) == 1


@pytest.mark.parametrize("extra", [{"form_id": "workspace_i18n"}, {"proposal_id": "ccd_invented"}, {"saved": True}])
async def test_submission_correction_preserves_domain_receipts(settings, extra):
    child, _ = await prepare_configuration_agent(context(), form_id="workspace_i18n")
    read = await call(child, "configuration_read", form_id="workspace_i18n")
    proposed = await call(child, "configuration_propose", form_id="workspace_i18n", patch={"week_start_day": "sunday"})
    rejected = await call(child, TOOL_SUBMIT, message="Review the saved suggestion when the original page is ready.", **extra)
    assert rejected["error"] == "invalid_configuration_submission" and not rejected.get("__terminal")
    assert set(rejected["expected_arguments"]) == {"message"}
    assert configuration_agent_result(child)["error"] == "configuration_submission_missing"
    finished = await call(child, TOOL_SUBMIT, message="Review the saved suggestion when the original page is ready.")
    assert finished["ok"] and finished["__terminal"]
    assert configuration_agent_result(child)["receipts"] == [read, proposed]
    assert len(settings[2].proposals) == 1 and not settings[1]


async def test_oversized_submission_identifies_length_and_retains_pending_proposal(settings):
    child, _ = await prepare_configuration_agent(context(), form_id="workspace_i18n")
    read = await call(child, "configuration_read", form_id="workspace_i18n")
    proposed = await call(child, "configuration_propose", form_id="workspace_i18n", patch={"week_start_day": "sunday"})
    rejected = await call(child, TOOL_SUBMIT, message="x" * 2001)
    assert rejected["error"] == "invalid_configuration_submission"
    assert rejected["reason"] == "message_too_long"
    assert rejected["actual_chars"] == 2001 and rejected["max_chars"] == 2000
    assert "shorten" in rejected["message"].lower()
    assert configuration_agent_result(child)["error"] == "configuration_submission_missing"
    finished = await call(child, TOOL_SUBMIT, message="The suggestion is ready for your review; settings have not changed.")
    assert finished["ok"] and finished["__terminal"]
    assert configuration_agent_result(child)["receipts"] == [read, proposed]
    assert len(settings[2].proposals) == 1 and not settings[1]


async def test_incomplete_submission_retains_read_and_open_without_manufacturing_completion(settings):
    child, _ = await prepare_configuration_agent(context(), form_id="workspace_i18n")
    read = await call(child, "configuration_read", form_id="workspace_i18n")
    opened = await call(child, "configuration_open", form_id="workspace_i18n")
    collected = configuration_agent_result(child)
    assert collected == {"ok": False, "error": "configuration_submission_missing", "receipts": [read, opened]}
    collected["receipts"][0]["fields"].clear()
    assert configuration_agent_result(child)["receipts"][0]["fields"]["week_start_day"]["value"] == "monday"
    assert not settings[1] and not settings[2].proposals


@pytest.mark.parametrize("scenario", ["only_config", "mixed", "prior_work", "unfinished", "failed"])
async def test_lead_direct_receipt_requires_a_complete_settings_only_task(settings, monkeypatch, scenario):
    from test_subagent import _router

    registry = build_default_registry()
    register_subagent_tool(registry)
    message = "The suggestion is saved for review; no setting has been changed."

    async def dispatched(args, ctx):
        if args["profile"] != AGENT_NAME:
            return {"ok": True, "profile": "compute", "final_answer": "The business result is 42."}
        complete = scenario not in {"unfinished", "failed"}
        return {"ok": scenario != "failed", "profile": AGENT_NAME, "configuration": {"ok": complete, "message": message}}

    monkeypatch.setattr(registry.get("subagent_spawn"), "handler", dispatched)
    settings_call = ToolCall(id="settings", name="subagent_spawn", arguments={"profile": AGENT_NAME, "task": "Change week start."})
    business_call = ToolCall(id="business", name="subagent_spawn", arguments={"profile": "compute", "task": "Read the business value."})
    scripted = [LLMResponse(content="", tool_calls=[settings_call, business_call] if scenario == "mixed" else [settings_call])]
    if scenario == "prior_work":
        scripted.insert(0, LLMResponse(content="", tool_calls=[business_call]))
    narration = "The business work and any incomplete configuration still need this explanation."
    scripted.append(LLMResponse(content=narration))
    router = _router(*scripted)
    lead = context()
    final = await SubagentRunner(router=router, tool_registry=registry, backend="legacy").run(
        profile_name="lead", task=lead.user_text, parent_turn_id=lead.turn_id, parent_bus=lead.parent_bus, parent_ctx=lead
    )
    assert final.ok
    assert final.final_answer == (message if scenario == "only_config" else narration)
    assert router._registry._adapters["p1"]._script_index == (1 if scenario == "only_config" else 3 if scenario == "prior_work" else 2)


async def test_selected_target_is_fixed_and_persisted(settings):
    from bowhead_core.configuration_settings_target import AdmittedWorkspaceSettingsTarget, WorkspaceSettingsSendTarget

    lead = context()

    async def recheck():
        return None

    target = WorkspaceSettingsSendTarget(
        form_id="workspace_i18n",
        user_id=lead.user_id,
        team_id=lead.team_id,
        workspace_id=lead.workspace_id,
        edit_generation=3,
        saved_ready=True,
        pending_writes=0,
        unconfirmed_fields=[],
    )
    lead.configuration_settings_target = AdmittedWorkspaceSettingsTarget(target, recheck)
    child, _ = await prepare_configuration_agent(lead)
    assert (await call(child, "configuration_read", form_id="user_language"))["error"] == "configuration_form_not_admitted"
    await call(child, "configuration_read", form_id="workspace_i18n")
    await call(child, "configuration_propose", form_id="workspace_i18n", patch={"week_start_day": "sunday"})
    assert settings[2].proposals[0]["settings_target"] == target.projection()


@pytest.mark.parametrize("selected,requested", [("workspace_metadata", "workspace_i18n"), ("workspace_i18n", "workspace_metadata")])
@pytest.mark.parametrize(
    "operation", ["spawn", "configuration_read", "configuration_open", "configuration_propose", "configuration_apply", "configuration_result"]
)
async def test_shared_page_form_mismatch_explains_recovery_without_retargeting(settings, monkeypatch, selected, requested, operation):
    from bowhead_core.configuration_settings_target import AdmittedWorkspaceSettingsTarget, WorkspaceSettingsSendTarget

    lead = context()

    async def recheck():
        pytest.fail("A mismatched selection must stop before domain access")

    target = WorkspaceSettingsSendTarget(
        form_id=selected,
        user_id=lead.user_id,
        team_id=lead.team_id,
        workspace_id=lead.workspace_id,
        edit_generation=0,
        saved_ready=True,
        pending_writes=0,
        unconfirmed_fields=[],
    )
    admitted = AdmittedWorkspaceSettingsTarget(target, recheck)
    lead.configuration_settings_target = admitted

    async def deny_access(*args):
        pytest.fail("A mismatched selection must stop before resolving access")

    monkeypatch.setattr(f"{__name__}.access", deny_access)
    if operation == "spawn":
        child, error = await prepare_configuration_agent(lead, form_id=requested)
        assert child is None
        assert error["error"] == "configuration_target_mismatch"
    else:
        child, _ = await prepare_configuration_agent(lead)
        error = await call(child, operation, read_skill=False, form_id=requested)
        assert error["error"] == "configuration_form_not_admitted"
        assert child.configuration_settings_target is admitted
        assert configuration_agent_result(child)["receipts"][-1]["message"] == error["message"]
    assert error["selected_form_id"] == selected
    assert error["requested_form_id"] == requested
    assert error["selected_page"] == error["requested_page"] == "/space/settings/general/"
    assert "Close that Canvas, then send a new request" in error["message"]
    assert lead.configuration_settings_target is admitted
    assert not settings[1] and not settings[2].proposals


async def test_form_binding_without_canvas_does_not_invent_canvas_recovery(settings):
    child, _ = await prepare_configuration_agent(context(), form_id="workspace_i18n")
    error = await call(child, "configuration_read", read_skill=False, form_id="workspace_metadata")
    assert error == {"ok": False, "error": "configuration_form_not_admitted"}


async def test_disabled_settings_plugin_makes_its_form_unavailable(settings):
    from bowhead_core.resource_disable import DisableState

    current, applied, writer = settings
    team_off = context()
    team_off.resource_disable_state = DisableState(team_builtin_plugins=frozenset({"settings-space-preferences"}))
    child, refusal = await prepare_configuration_agent(team_off, form_id="workspace_i18n")
    assert child is None and refusal["error"] == "configuration_form_unavailable"

    # A personal disable without a preselected form: the form tools refuse that domain only.
    mine_off = context()
    mine_off.resource_disable_state = DisableState(user_builtin_plugins=frozenset({"settings-language-preferences"}))
    child, _ = await prepare_configuration_agent(mine_off)
    assert (await call(child, "configuration_read", form_id="user_language"))["error"] == "configuration_form_unavailable"
    assert (await call(child, "configuration_read", form_id="workspace_i18n")).get("ok") is True
    assert not writer.proposals and not applied and current["week_start_day"] == "monday"


@pytest.mark.parametrize("form_id", ["team_subagent", "llm_provider", "user_appearance"])
async def test_unsupported_domains_fail_closed(settings, form_id):
    child, refusal = await prepare_configuration_agent(context(), form_id=form_id)
    assert child is None and refusal["error"] == "configuration_form_not_admitted"


async def test_language_intent_must_be_original_user_text(settings):
    lead = context()
    lead.user_text = "Explain answer languages."
    child, refusal = await prepare_configuration_agent(lead, form_id="user_language", intent_quote="Always answer in English.")
    assert child is None and refusal["error"] == "configuration_intent_not_grounded"


def test_plugins_compile_real_tools_and_exact_domain_skills(settings):
    profile = manifest_to_profile(AgentRegistry.bootstrap().get(AGENT_NAME))
    assert "configuration_propose" in profile.tool_whitelist and "config_propose" not in profile.tool_whitelist
    assert profile.team_tool_bundle == ()
    assert profile.skill_runtime_scope.read_names == (
        "settings-language-preferences",
        "settings-personal-appearance",
        "settings-space-instructions",
        "settings-space-metadata",
        "settings-space-preferences",
        "settings-team-administration",
    )
    for grant in profile.skill_runtime_grants:
        skill_dir = Path(__file__).resolve().parents[2] / "bowhead_skill" / "catalog" / grant.name
        assert grant.revision == "sha256:" + bundle_sha256(skill_dir)
    runner = SubagentRunner(router=None, tool_registry=build_default_registry())
    known, effective = runner._resolve_system_profile(
        AGENT_NAME,
        team_id="team_original",
        builtin_overrides={AGENT_NAME: SimpleNamespace(enabled=True, prompt_append="Use a different settings policy.", skills_json='["other"]')},
    )
    assert known and effective is PROFILES[AGENT_NAME]


def test_plugins_reject_unknown_versions_and_duplicate_selections(settings):
    from bowhead_core.system_plugins import resolve_system_plugins

    with pytest.raises(ValueError):
        resolve_system_plugins(["builtin:settings-space-preferences@99"], [], [])
    with pytest.raises(ValueError):
        resolve_system_plugins(["builtin:settings-space-preferences@1"] * 2, [], [])


@pytest.mark.parametrize("section_revise", ["0", "1"])
@pytest.mark.parametrize("selected_reference", [False, True])
@pytest.mark.parametrize("via_lead", [False, True])
@pytest.mark.parametrize("omit_submission", [False, True])
@pytest.mark.parametrize("read_only", [False, True])
async def test_spawn_uses_real_runner_and_existing_confirmation(
    settings, monkeypatch, tmp_path, section_revise, selected_reference, via_lead, omit_submission, read_only
):
    monkeypatch.setenv("FEATURE_CANVAS_SECTION_REVISE", section_revise)
    terminal_message = (
        "The current week start is Monday. Opening the original editor was requested; no setting has been changed."
        if read_only
        else "A suggestion is ready for your confirmation."
    )
    continuation_message = (
        "The specialist handoff is incomplete. Settings were read and opening was requested; no setting has been changed."
        if read_only
        else "The specialist handoff is incomplete, but the suggestion remains saved for review; no setting has been changed."
    )
    adapter = StreamingToolAdapter(provider_name="mock")
    adapter.scripted_responses = [
        LLMResponse(content="", tool_calls=[ToolCall(id="method", name="skill_read", arguments={"name": "settings-space-preferences"})]),
        LLMResponse(content="", tool_calls=[ToolCall(id="read", name="configuration_read", arguments={"form_id": "workspace_i18n"})]),
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="candidate",
                    name="configuration_propose",
                    arguments={"form_id": "workspace_i18n", "patch": {"week_start_day": "sunday"}},
                )
            ],
        ),
        LLMResponse(content="", tool_calls=[ToolCall(id="finish", name=TOOL_SUBMIT, arguments={"message": terminal_message})]),
    ]
    if read_only:
        adapter.scripted_responses[2] = LLMResponse(
            content="", tool_calls=[ToolCall(id="open", name="configuration_open", arguments={"form_id": "workspace_i18n"})]
        )
    if omit_submission:
        adapter.scripted_responses[-1] = LLMResponse(content=terminal_message)
    spawn_args = {
        "profile": AGENT_NAME,
        "task": "Read current preferences and request opening without changes." if read_only else "Start weeks on Sunday.",
        "configuration_form_id": "workspace_i18n",
        "canvas_mode": "new_report",
        "keep_prior_report": True,
    }
    if via_lead:
        adapter.scripted_responses.insert(
            0, LLMResponse(content="", tool_calls=[ToolCall(id="delegate", name="subagent_spawn", arguments=spawn_args)])
        )
        adapter.scripted_responses.append(LLMResponse(content=continuation_message))
    providers = ProviderRegistry()
    providers.load([ProviderConfig(name="mock", type="mock")])
    providers._adapters["mock"] = adapter
    router = Router(providers)
    router.bind(AgentBinding(agent_type="analyst", primary=("mock", "model")))
    router.bind(AgentBinding(agent_type="lead", primary=("mock", "model")))
    registry = build_default_registry()
    catalog, runtime = build_default_skill_stack()
    register_skill_tools(registry, catalog, runtime)
    register_config_copilot_chat_tools(registry, configuration_access_resolver=access)
    register_subagent_tool(registry)
    lead = context()
    lead.parent_bus._lead_canvas_mode = "append"
    from bowhead_core.canvas_revise import note_revise_scope_unresolved

    note_revise_scope_unresolved(lead.parent_bus, batch=1)
    lead._lead_spawn_batch_seq = 2
    fixture = await _setup(tmp_path)
    lead.user_id = fixture.member_id
    lead.member_uid = fixture.member_id
    lead.team_id = fixture.team_id
    lead.workspace_id = fixture.workspace_id
    lead.session_id = "s_m3b"
    lead.turn_id = fixture.turn_id
    lead.access_control_store = AccessControlStore(fixture.db_path)
    runner = SubagentRunner(router=router, tool_registry=registry, team_subagent_store=fixture.users)
    results = []
    original_run = runner.run

    async def capture(**kwargs):
        result = await original_run(**kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(runner, "run", capture)
    lead.subagent_runner = runner
    if selected_reference:
        from hashlib import sha256

        from bowhead_core.agent_execution_binding import AgentExecutionBindingStore

        binding = await runner.admit_execution_binding(
            profile_name=AGENT_NAME,
            parent_turn_id=lead.turn_id,
            parent_ctx=lead,
            dispatch_key="reference:configuration",
            dispatch_source="explicit",
        )

        class SelectedReference:
            async def binding_for_delegation(self, **kwargs):
                return binding

            async def run(
                self,
                *,
                runner,
                parent_ctx,
                parent_bus,
                delegation_task,
                delegation_call_id,
                extra_instructions,
                language_override,
                expect_first_dispatch=False,
            ):
                await AgentExecutionBindingStore(fixture.db_path).mark_running(binding.execution_id)
                return await runner.run(
                    profile_name=binding.runtime_profile_name,
                    task=delegation_task,
                    parent_turn_id=parent_ctx.turn_id,
                    parent_bus=parent_bus,
                    parent_ctx=parent_ctx,
                    exact_binding=binding,
                    pre_minted_subagent_id="sub_" + sha256(binding.execution_id.encode()).hexdigest()[:20],
                    extra_instructions=extra_instructions,
                    language_override=language_override,
                )

        lead._reference_lead_runtime = SelectedReference()
    if via_lead:
        final = await runner.run(profile_name="lead", task=lead.user_text, parent_turn_id=lead.turn_id, parent_bus=lead.parent_bus, parent_ctx=lead)
        invocation = next(inv for inv in final.tool_invocations if inv.tool_name == "subagent_spawn")
        assert final.ok
        assert final.final_answer == (continuation_message if omit_submission else terminal_message)
        assert adapter._script_index == (6 if omit_submission else 5)
    else:
        invocation = await registry.execute("subagent_spawn", spawn_args, lead)
    assert invocation.error is None, invocation.error
    assert results, invocation.result
    assert results[0].ok == (not omit_submission)
    if omit_submission:
        assert results[0].error == "configuration_submission_missing"
    assert invocation.result.get("configuration", {}).get("ok") == (not omit_submission), (
        invocation.result,
        [(inv.tool_name, inv.error, inv.result) for inv in results[0].tool_invocations],
    )
    if omit_submission:
        assert invocation.result["error"] == "configuration_submission_missing"
        receipts = invocation.result["configuration"]["receipts"]
        assert receipts[0]["fields"]["week_start_day"]["value"] == "monday"
        if read_only:
            assert receipts[1]["status"] == "open_requested" and receipts[1]["target"]["workspace_id"] == fixture.workspace_id
        else:
            assert receipts[1]["proposal_id"] and receipts[1]["status"] == "proposed"
        assert receipts[1]["saved"] is False
        assert "do not describe them as failed" in invocation.result["directive"]
        summary = next(event for event in lead.parent_bus.snapshot() if event.event_type == "lead.subagent_summary")
        assert summary.payload["status"] == "failed"
    method = next(inv for inv in results[0].tool_invocations if inv.tool_name == "skill_read")
    assert method.error is None and "Configure current Space preferences" in str(method.result)
    assert len(settings[2].proposals) == (0 if read_only else 1) and not settings[1]
    assert settings[0]["week_start_day"] == "monday"
    event_types = [event.event_type for event in lead.parent_bus.snapshot()]
    assert "peer_dispatched" in event_types
    assert ("configuration.canvas_open" if read_only else "config.lite_propose") in event_types
    assert not getattr(lead, "_psi_parsed_results", [])
    assert "Do not call config_propose again" in invocation.result["directive"]
    assert lead.parent_bus._lead_canvas_mode == "append"
    assert not getattr(lead.parent_bus, "_lead_keep_prior_report", False)
    assert lead.parent_bus._lead_delivery_spawn == "chat"


async def test_ordinary_agent_cannot_acquire_authority_from_tool_availability(settings):
    from bowhead_core.agents._lead_context import reset_current_peer_name, set_current_peer_name

    child, _ = await prepare_configuration_agent(context(), form_id="workspace_i18n")
    registry = build_default_registry()
    register_config_copilot_chat_tools(registry, configuration_access_resolver=access)
    token = set_current_peer_name("compute")
    try:
        result = await registry.get("configuration_read").handler({"form_id": "workspace_i18n"}, child)
    finally:
        reset_current_peer_name(token)
    assert result["error"] == "configuration_execution_not_admitted"
    assert not settings[2].proposals and not settings[1]


@pytest.mark.parametrize("outcome", ["empty", "provider_error", "recovered", "blocked_domain_calls", "invalid_submit"])
@pytest.mark.parametrize("read_only", [False, True])
async def test_settings_receipts_survive_empty_or_failed_peer(settings, tmp_path, outcome, read_only):
    adapter = StreamingToolAdapter(provider_name="mock")
    adapter.scripted_responses = [
        LLMResponse(content="", tool_calls=[ToolCall(id="method", name="skill_read", arguments={"name": "settings-space-preferences"})]),
        LLMResponse(content="", tool_calls=[ToolCall(id="read", name="configuration_read", arguments={"form_id": "workspace_i18n"})]),
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="operation",
                    name="configuration_open" if read_only else "configuration_propose",
                    arguments={"form_id": "workspace_i18n", **({} if read_only else {"patch": {"week_start_day": "sunday"}})},
                )
            ],
        ),
        LLMResponse(content="", raw={"simulate_error": outcome == "provider_error"}),
    ]
    recovered = outcome in {"recovered", "blocked_domain_calls"}
    message = (
        "Settings were read and opening was requested; no settings changed."
        if read_only
        else "The original suggestion is saved for review; no settings changed."
    )
    if outcome != "provider_error":
        calls = []
        if outcome == "blocked_domain_calls":
            calls = [
                ToolCall(id="repeat_read", name="configuration_read", arguments={"form_id": "workspace_i18n"}),
                ToolCall(id="repeat_open", name="configuration_open", arguments={"form_id": "workspace_i18n"}),
                ToolCall(
                    id="repeat_propose", name="configuration_propose", arguments={"form_id": "workspace_i18n", "patch": {"week_start_day": "sunday"}}
                ),
                ToolCall(id="apply", name="configuration_apply", arguments={"form_id": "workspace_i18n", "proposal_id": "not-an-authorization"}),
            ]
        if recovered or outcome == "invalid_submit":
            calls.append(
                ToolCall(id="closeout", name=TOOL_SUBMIT, arguments={"message": message, **({"saved": True} if outcome == "invalid_submit" else {})})
            )
        adapter.scripted_responses.append(LLMResponse(content="", tool_calls=calls))
    providers = ProviderRegistry()
    providers.load([ProviderConfig(name="mock", type="mock")])
    providers._adapters["mock"] = adapter
    router = Router(providers)
    router.bind(AgentBinding(agent_type="analyst", primary=("mock", "model")))
    registry = build_default_registry()
    catalog, runtime = build_default_skill_stack()
    register_skill_tools(registry, catalog, runtime)
    register_config_copilot_chat_tools(registry, configuration_access_resolver=access)
    register_subagent_tool(registry)
    fixture = await _setup(tmp_path)
    lead = context()
    persisted = []

    async def persist(event):
        persisted.append(json.loads(json.dumps(asdict(event))))

    lead.parent_bus = TurnEventBus(log_sink=persist, persist_before_live=True)
    lead.user_id = lead.member_uid = fixture.member_id
    lead.team_id, lead.workspace_id, lead.turn_id = fixture.team_id, fixture.workspace_id, fixture.turn_id
    lead.session_id = "s_m3b"
    lead.access_control_store = AccessControlStore(fixture.db_path)
    lead.subagent_runner = SubagentRunner(router=router, tool_registry=registry, team_subagent_store=fixture.users)
    invocation = await registry.execute(
        "subagent_spawn",
        {"profile": AGENT_NAME, "task": "Read settings." if read_only else "Propose Sunday.", "configuration_form_id": "workspace_i18n"},
        lead,
    )
    assert invocation.error is None
    result = invocation.result
    assert result["ok"] is recovered and result["configuration"]["ok"] is recovered
    if recovered:
        assert result["configuration"]["message"] == message
    else:
        assert result["error"] == ("subagent_internal_error" if outcome == "provider_error" else "configuration_submission_missing")
    receipts = result["configuration"]["receipts"]
    assert len(receipts) == 2
    assert receipts[0]["fields"]["week_start_day"]["value"] == "monday"
    assert receipts[1]["status"] == ("open_requested" if read_only else "proposed") and receipts[1]["saved"] is False
    assert not settings[1] and len(settings[2].proposals) == (0 if read_only else 1)
    assert adapter._script_index == (4 if outcome == "provider_error" else 5)
    if outcome != "provider_error":
        assert [tool.name for tool in adapter.requests[-1].tools] == [TOOL_SUBMIT]
        assert any("pending suggestion does not mean settings were applied" in m.content for m in adapter.requests[-1].messages)
        timings = [event.payload for event in lead.parent_bus.snapshot() if event.event_type == "llm.call_timing"]
        assert sum(timing["closeout_attempt"] for timing in timings) == 1
    live = [json.loads(json.dumps(asdict(event))) for event in lead.parent_bus.snapshot()]
    assert [e for e in persisted if e["event_type"] == "llm.call_timing"] == [e for e in live if e["event_type"] == "llm.call_timing"]
    summary = next(event for event in lead.parent_bus.snapshot() if event.event_type == "lead.subagent_summary")
    assert summary.payload["status"] == ("succeeded" if recovered else "failed")


async def test_read_only_skill_correction_uses_actual_runner_without_repeating_domain_reads(settings, tmp_path):
    adapter = StreamingToolAdapter(provider_name="mock")
    message = "The current preferences were read; opening the original page was requested. No settings were saved."
    adapter.scripted_responses = [
        LLMResponse(content="", tool_calls=[ToolCall(id="read", name="configuration_read", arguments={"form_id": "workspace_i18n"})]),
        LLMResponse(content="", tool_calls=[ToolCall(id="open", name="configuration_open", arguments={"form_id": "workspace_i18n"})]),
        LLMResponse(content="", tool_calls=[ToolCall(id="premature", name=TOOL_SUBMIT, arguments={"message": message})]),
        LLMResponse(content="", tool_calls=[ToolCall(id="method", name="skill_read", arguments={"name": "settings-space-preferences"})]),
        LLMResponse(content="", tool_calls=[ToolCall(id="finish", name=TOOL_SUBMIT, arguments={"message": message})]),
    ]
    providers = ProviderRegistry()
    providers.load([ProviderConfig(name="mock", type="mock")])
    providers._adapters["mock"] = adapter
    router = Router(providers)
    router.bind(AgentBinding(agent_type="analyst", primary=("mock", "model")))
    registry = build_default_registry()
    catalog, runtime = build_default_skill_stack()
    register_skill_tools(registry, catalog, runtime)
    register_config_copilot_chat_tools(registry, configuration_access_resolver=access)
    register_subagent_tool(registry)
    fixture = await _setup(tmp_path)
    lead = context()
    lead.user_id = lead.member_uid = fixture.member_id
    lead.team_id, lead.workspace_id, lead.turn_id = fixture.team_id, fixture.workspace_id, fixture.turn_id
    lead.access_control_store = AccessControlStore(fixture.db_path)
    lead.subagent_runner = SubagentRunner(router=router, tool_registry=registry, team_subagent_store=fixture.users)
    lead.session_id = "s_m3b"
    invocation = await registry.execute("subagent_spawn", {"profile": AGENT_NAME, "task": "Read preferences and open the page."}, lead)
    assert invocation.error is None and invocation.result["ok"] is True, invocation.result
    assert invocation.result["configuration"]["message"] == message
    assert [receipt["status"] if "status" in receipt else receipt["form_id"] for receipt in invocation.result["configuration"]["receipts"]] == [
        "workspace_i18n",
        "open_requested",
    ]
    assert adapter._script_index == 5 and not settings[2].proposals and not settings[1]


async def test_disabled_language_choice_cannot_be_staged(settings, monkeypatch):
    from bowhead_core.config_copilot import get_form

    class LanguagePreferences(BaseModel):
        output_language: Literal["de-DE", "en-US", "auto"] | None = None

    reg = get_form("workspace_i18n")

    async def language_read(ctx):
        return {"fields": {"output_language": {"value": "auto", "unset": True}}}

    register_form(
        replace(
            reg,
            form_id="user_language",
            schema=LanguagePreferences,
            field_policy={"output_language": POLICY_GENERATED},
            read_accessor=language_read,
            read_scope="user",
            write_scope="user",
        )
    )
    monkeypatch.setenv("FEATURE_GERMAN_AI_OUTPUT", "0")
    lead = context()
    lead.user_text = "Always answer in German from now on."
    child, _ = await prepare_configuration_agent(lead, form_id="user_language", intent_quote=lead.user_text)
    read = await call(child, "configuration_read", form_id="user_language")
    assert "de-DE" not in read["allowed_values"]["output_language"]
    assert "de-DE" not in json.dumps(read["schema"])
    assert read["schema"]["properties"]["output_language"]["anyOf"][0]["enum"] == ["en-US", "auto"]
    result = await call(child, "configuration_propose", form_id="user_language", patch={"output_language": "de-DE"})
    assert result["error"] == "invalid_patch" and not settings[2].proposals


async def test_lead_collects_actual_failed_operation_receipts(settings):
    child, _ = await prepare_configuration_agent(context(), form_id="workspace_i18n")
    read = await call(child, "configuration_read", form_id="workspace_i18n")
    denied = await call(child, "configuration_propose", form_id="workspace_i18n", patch={"week_start_day": "invalid"})
    assert denied["error"] == "invalid_patch"
    await call(child, TOOL_SUBMIT, message="The requested value is unavailable.")
    assert configuration_agent_result(child)["receipts"] == [read, denied]
    assert not settings[2].proposals


def test_spawn_form_guidance_tracks_domain_catalog(monkeypatch):
    from bowhead_core.configuration_contracts import DOMAINS

    monkeypatch.setitem(DOMAINS, "team_run_limits", replace(DOMAINS["team_run_limits"], description="Updated Team loop control guidance."))
    registry = build_default_registry()
    register_subagent_tool(registry)
    selector = registry.get("subagent_spawn").schema.input_schema["properties"]["configuration_form_id"]

    assert set(selector["enum"]) == set(DOMAINS)
    for form_id, domain in DOMAINS.items():
        assert f"{form_id}: {domain.description}" in selector["description"]


@pytest.mark.parametrize("form_id,fields", [("workspace_i18n", "default_data_language, week_start_day"), ("workspace_metadata", "description, name")])
def test_selected_page_routes_to_its_actual_admitted_form(settings, form_id, fields):
    from bowhead_core.configuration_settings_target import AdmittedWorkspaceSettingsTarget, WorkspaceSettingsSendTarget, active_settings_target_prompt

    if form_id == "workspace_metadata":

        class Metadata(BaseModel):
            name: str | None = None
            description: str | None = None

        register_form(
            replace(
                get_form("workspace_i18n"),
                form_id=form_id,
                schema=Metadata,
                field_policy=dict.fromkeys(Metadata.model_fields, POLICY_GENERATED),
            )
        )

    async def recheck():
        pass

    ctx = context()
    ctx.configuration_settings_target = AdmittedWorkspaceSettingsTarget(
        WorkspaceSettingsSendTarget(
            form_id=form_id,
            user_id=ctx.user_id,
            team_id=ctx.team_id,
            workspace_id=ctx.workspace_id,
            edit_generation=0,
            saved_ready=True,
            pending_writes=0,
            unconfirmed_fields=[],
        ),
        recheck,
    )
    prompt = active_settings_target_prompt(ctx)
    assert f"configuration_form_id={form_id}" in prompt
    assert f"Only these fields are admitted: {fields}" in prompt
    ctx.configuration_settings_target = "configuration_target_requires_new_send"
    assert "not admitted" in active_settings_target_prompt(ctx)


@pytest.mark.parametrize("specialist_in_roster", [True, False])
async def test_legacy_tools_route_supported_forms_without_reading_values(settings, specialist_in_roster):
    from bowhead_core.config_copilot_chat_tools import _read_handler, roster_scoped_tool_schemas

    registry = build_default_registry()
    register_config_copilot_chat_tools(registry)
    lead = lead_context(specialist_in_roster=specialist_in_roster)
    registered = [registry.get(name).schema for name in ("config_read", "config_propose")]
    for schema in registered:
        # Registered copy has no Turn roster, so it never names the specialist.
        assert "Do not call this Tool for" in schema.description and AGENT_NAME not in schema.description
    for schema in roster_scoped_tool_schemas(registered, lead):
        assert "Do not call this Tool for" in schema.description
        assert ("configuration-agent through subagent_spawn" in schema.description) is specialist_in_roster
        if not specialist_in_roster:
            assert AGENT_NAME not in schema.description and "no Agent that can change them" in schema.description
    result = await _read_handler({}, lead)
    form = next(row for row in result["forms"] if row["form_id"] == "workspace_i18n")
    if specialist_in_roster:
        assert form["delegate_to"] == AGENT_NAME
    else:
        assert "delegate_to" not in form and form["routing"] == "configuration_settings_unavailable"
    assert "fields" not in form
    assert not settings[1] and not settings[2].proposals


@pytest.mark.parametrize("specialist_in_roster", [True, False])
async def test_lead_advertisement_names_the_specialist_only_from_the_turn_roster(settings, monkeypatch, specialist_in_roster):
    from test_subagent import _router

    registry = build_default_registry()
    register_subagent_tool(registry)
    register_config_copilot_chat_tools(registry)
    router = _router(LLMResponse(content="Done."))
    adapter = router._registry._adapters["p1"]
    seen = []
    original = adapter.invoke

    async def recording(request):
        seen.append({tool.name: tool.description for tool in request.tools or []})
        return await original(request)

    monkeypatch.setattr(adapter, "invoke", recording)
    # The stub test Lead profile lacks the production config Tools; add them as lead.yaml does.
    stub = PROFILES["lead"]
    monkeypatch.setitem(PROFILES, "lead", replace(stub, tool_whitelist=(*stub.tool_whitelist, "config_read", "config_propose")))
    lead = lead_context(specialist_in_roster=specialist_in_roster)
    final = await SubagentRunner(router=router, tool_registry=registry, backend="legacy").run(
        profile_name="lead", task=lead.user_text, parent_turn_id=lead.turn_id, parent_bus=lead.parent_bus, parent_ctx=lead
    )
    assert final.ok and seen
    description = seen[0]["config_read"]
    assert ("configuration-agent through subagent_spawn" in description) is specialist_in_roster
    assert (AGENT_NAME in seen[0]["config_propose"]) is specialist_in_roster


async def test_configuration_empty_closeout_respects_the_runtime_decision_budget(settings, monkeypatch):
    from bowhead_core.tool_registry import ToolRegistry, ToolSpec
    from test_peer_empty_closeout import ScriptedRouter

    from bowhead_router import ToolSchema

    profile = replace(PROFILES[AGENT_NAME], max_iterations=1, tool_whitelist=("verified_read", TOOL_SUBMIT))
    monkeypatch.setitem(PROFILES, AGENT_NAME, profile)
    registry = ToolRegistry()

    async def verified_read(_args, _ctx):
        return {"ok": True, "value": "monday"}

    registry.register(
        ToolSpec(
            schema=ToolSchema(name="verified_read", description="Read a verified setting", input_schema={"type": "object"}), handler=verified_read
        )
    )
    router = ScriptedRouter(
        [
            LLMResponse(content="", tool_calls=[ToolCall(id="read", name="verified_read", arguments={})]),
            LLMResponse(content=""),
        ]
    )
    child, _ = await prepare_configuration_agent(context())
    from bowhead_core.agent_run_limits import AgentRunLimits, AgentRuntimeControls

    child.agent_runtime_budget = AgentRuntimeControls(system_step_limit=2).budget(AgentRunLimits())
    result = await SubagentRunner(router=router, tool_registry=registry).run(
        profile_name=AGENT_NAME,
        task="Read preferences",
        parent_turn_id=child.turn_id,
        parent_bus=child.parent_bus,
        parent_ctx=child,
    )
    assert len(router.requests) == 2 and not result.ok
    assert result.stop_reason == "step_limit"
    assert not settings[1] and not settings[2].proposals
