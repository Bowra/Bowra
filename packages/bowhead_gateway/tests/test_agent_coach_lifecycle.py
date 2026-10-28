"""Coach resource receipts compose through Owner creation into exact first use.

Only the model's replies are controlled; authoring stores, HTTP adoption,
private release, current admission, Skill reading and the Agent runner are real.
"""

import json
import sqlite3

import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.agents._lead_context import reset_current_spawn_trace, set_current_spawn_trace
from bowhead_core.events import TurnEventBus
from bowhead_core.session_store import SessionStore
from bowhead_core.skill_bridge import build_default_skill_stack, register_skill_tools
from bowhead_core.subagent import SubagentRunner, register_subagent_tool
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry
from test_agent_coach_draft_adapter import saved_draft as saved_draft
from test_agent_coach_draft_bindings import accept, activate, create, review
from test_agent_coach_plugins import administrator
from test_agent_coach_resources import resources as resources
from test_agent_coach_skills import BODY
from test_agent_coach_skills import skill_adapter as skill_adapter
from test_agent_coach_tool_handoff import call as handoff_call
from test_agent_coach_tool_handoff import create as create_requirement
from test_agent_coach_tool_handoff import tool_source
from test_team_subagent_drafts_routes import _run

from bowhead_router import AgentBinding, LLMResponse, MockAdapter, ProviderConfig, ProviderRegistry, Router, ToolCall

pytestmark = pytest.mark.integration


def compose_resources(resources, saved_draft, skill_adapter):
    administrator(resources)
    requirement = create_requirement(resources, saved_draft)
    source = tool_source(resources, saved_draft[5].id)
    page = _run(resources.search(kind="tool", query="records", offset=0, limit=50))
    reference = next(item["reference"] for item in page["items"] if source.id in item["reference"]["id"])
    observed = handoff_call(
        resources, "agent_tool_requirement_reconcile", requirement_id=requirement["requirement_id"], reference=reference, request_id="observed"
    )
    assert not observed["parent_bound"] and observed["business_trial"] == "not_run"
    plugin_args = {"target": None, "patch": {"name": "Customer evidence", "tools": [reference]}, "expected_impact": None, "request_id": "plugin"}
    bundle = _run(resources.plugins.call("agent_plugin_save", plugin_args, turn_id="t_lifecycle"))
    assert bundle["ok"], bundle
    target = _run(skill_adapter.call("agent_skill_inspect", {"name": "coached-method"}, turn_id="t_lifecycle"))["target"]
    skill_args = {"target": target, "changes": [{"path": "SKILL.md", "content": BODY}], "request_id": "method"}
    saved = _run(skill_adapter.call("agent_skill_save", skill_args, turn_id="t_lifecycle"))
    assert saved["ok"] and saved["import_inspection"] == "complete", saved
    skills = _run(resources.search(kind="skill", query="coached-method", offset=0, limit=50))
    item = next(item for item in skills["items"] if item["reference"]["id"] == saved["result"]["target"]["id"])
    grant = _run(resources.get(reference=item["reference"]))["resource"]["exact_read_grant"]
    return {
        "patch": {"tool_refs": [], "skill_grants": [grant], "capability_ids": [bundle["result_target"]["id"]]},
        "skill_args": skill_args,
        "skill": saved,
        "plugin_args": plugin_args,
        "plugin": bundle,
        "requirement": requirement,
        "observed": observed,
    }


async def first_use(resources, identity, *, read_skill):
    sessions = SessionStore(resources.store.db_path)
    session = await sessions.ensure_session(session_id=None, workspace_id=resources.workspace_id, user_id=resources.user.id)
    turn = await sessions.record_turn(turn_id="t_lifecycle_first", session_id=session.id, user_text="Summarize supplied evidence: sales grew 5%.")
    agent = await resources.store.get_team_subagent(resources.user.team_id, identity)
    adapter = MockAdapter(provider_name="lifecycle")
    adapter.scripted_responses = (
        [LLMResponse(content="", tool_calls=[ToolCall(id="read", name="skill_read", arguments={"name": "coached-method"})])] if read_skill else []
    ) + [LLMResponse(content="Sales grew 5%.")]
    providers = ProviderRegistry()
    providers.load([ProviderConfig(name="lifecycle", type="mock")])
    providers._adapters["lifecycle"] = adapter
    router = Router(providers)
    router.bind(AgentBinding(agent_type="analyst", primary=("lifecycle", "controlled")))
    registry = build_default_registry()
    catalog, runtime = build_default_skill_stack()
    register_skill_tools(registry, catalog, runtime)
    register_subagent_tool(registry)
    ctx = ToolExecutionContext(
        team_id=resources.user.team_id,
        user_id=resources.user.id,
        member_uid=resources.user.id,
        workspace_id=resources.workspace_id,
        session_id=session.id,
        turn_id=turn.id,
        datarc=None,
        access_control_store=AccessControlStore(resources.store.db_path),
        skill_instance_store=resources.request.app.state.skill_instance_store,
        skill_catalog=catalog,
        role="lead",
        parent_bus=TurnEventBus(),
    )
    completed = []

    class CapturingRunner(SubagentRunner):
        async def run(self, **kwargs):
            result = await super().run(**kwargs)
            completed.append((result, kwargs["exact_binding"]))
            return result

    runner = CapturingRunner(router=router, tool_registry=registry, team_subagent_store=resources.store)
    ctx.subagent_runner = runner
    args = {"profile": agent.name, "task": turn.user_text}
    token = set_current_spawn_trace("first-use-original", None)
    try:
        invocation = await registry.execute("subagent_spawn", args, ctx)
        if completed:
            replay = await registry.execute("subagent_spawn", args, ctx)
            assert replay.result.get("error") and len(completed) == 1, "The original completed dispatch cannot execute twice"
    finally:
        reset_current_spawn_trace(token)
    return invocation, completed


@pytest.mark.parametrize("with_resources", [False, True])
def test_owner_creation_reaches_exact_first_use_and_original_receipt_recovery(resources, saved_draft, skill_adapter, with_resources):
    activate(resources)
    composed = compose_resources(resources, saved_draft, skill_adapter) if with_resources else None
    patch = composed["patch"] if composed else {"description": "Summarize supplied business evidence."}
    url, applied = accept(saved_draft, patch)
    ready = review(saved_draft[-1], url, applied)
    created = create(saved_draft[-1], url, ready)
    assert created.status_code == 201, created.text
    identity = created.json()["definition_id"]
    invocation, completed = _run(first_use(resources, identity, read_skill=with_resources))
    assert invocation.error is None and len(completed) == 1, invocation
    result, binding = completed[0]
    assert result.ok and result.final_answer.strip() == "Sales grew 5%.", result
    with sqlite3.connect(resources.store.db_path) as db:
        row = db.execute("SELECT live_revision_id,live_release_id,audience_mode FROM team_subagent WHERE id=?", (identity,)).fetchone()
        assert (binding.revision_ref, binding.release_id, "private") == row
        assert db.execute("SELECT count(*) FROM turn_agent_execution WHERE agent_id=?", (identity,)).fetchone()[0] == 1
        assert db.execute("SELECT status FROM turn_agent_execution WHERE id=?", (binding.execution_id,)).fetchone()[0] == "completed"
    replay = create(saved_draft[-1], url, ready)
    assert replay.status_code == 200 and replay.json()["definition_id"] == identity and replay.json()["replayed"]
    if composed:
        assert [call.tool_name for call in result.tool_invocations] == ["skill_read"]
        assert result.tool_invocations[0].error is None and "Read the evidence" in str(result.tool_invocations[0].result)
        saved = _run(skill_adapter.call("agent_skill_save", composed["skill_args"], turn_id="t_lifecycle"))
        plugin = _run(resources.plugins.call("agent_plugin_save", composed["plugin_args"], turn_id="t_lifecycle"))
        assert saved["operation_id"] == composed["skill"]["operation_id"]
        assert plugin["operation_id"] == composed["plugin"]["operation_id"]
        recovered = handoff_call(
            resources,
            "agent_tool_requirement_get",
            requirement_id=composed["requirement"]["requirement_id"],
            observation_id=composed["observed"]["observation"]["id"],
        )
        assert recovered["original_parent"] == composed["requirement"]["original_parent"]
        assert not recovered["parent_status"]["baseline_matches"]
        with sqlite3.connect(resources.store.db_path) as db:
            definition = json.loads(
                db.execute("SELECT definition_json FROM team_subagent_revision WHERE id=?", (binding.revision_ref,)).fetchone()[0]
            )
        assert definition["skill_grants"] == composed["patch"]["skill_grants"]
        assert definition["capability_ids"] == composed["patch"]["capability_ids"]


def test_owner_roster_revocation_after_creation_prevents_first_provider_use(resources, saved_draft):
    activate(resources)
    url, applied = accept(saved_draft, {"description": "Summarize supplied evidence."})
    created = create(saved_draft[-1], url, review(saved_draft[-1], url, applied))
    assert created.status_code == 201, created.text
    with sqlite3.connect(resources.store.db_path) as db:
        db.execute("UPDATE agent_assignment SET personal_state='disabled'")
    invocation, completed = _run(first_use(resources, created.json()["definition_id"], read_skill=False))
    assert not completed and invocation.result.get("error"), invocation
    with sqlite3.connect(resources.store.db_path) as db:
        assert db.execute("SELECT count(*) FROM turn_agent_execution").fetchone()[0] == 0


def _created_after_trial(resources, saved_draft, skill_adapter):
    activate(resources)
    composed = compose_resources(resources, saved_draft, skill_adapter)
    url, applied = accept(saved_draft, composed["patch"])
    created = create(saved_draft[-1], url, review(saved_draft[-1], url, applied))
    assert created.status_code == 201, created.text
    return composed, created


@pytest.mark.parametrize("changed", ["tool_source", "plugin"])
def test_changed_resource_cannot_supply_stale_trial_evidence(resources, saved_draft, skill_adapter, changed):
    composed, created = _created_after_trial(resources, saved_draft, skill_adapter)
    with sqlite3.connect(resources.store.db_path) as db:
        if changed == "tool_source":
            db.execute("UPDATE team_tool_source SET status='disabled'")
        else:
            db.execute("UPDATE team_capability SET status='disabled'")
    if changed == "tool_source":
        recovered = handoff_call(
            resources,
            "agent_tool_requirement_get",
            requirement_id=composed["requirement"]["requirement_id"],
            observation_id=composed["observed"]["observation"]["id"],
        )
        assert recovered["observation"]["current_status"] == "unavailable_or_changed"
        assert recovered["observation"]["reference"] is None and "binding_suggestion" not in recovered
        return
    _invocation, completed = _run(first_use(resources, created.json()["definition_id"], read_skill=True))
    # A disabled team Plugin no longer stops the Agent: the run
    # proceeds without the Plugin's Tool, while the directly granted Skill stays readable.
    assert len(completed) == 1
    calls = completed[0][0].tool_invocations
    assert [call.tool_name for call in calls] == ["skill_read"]
    assert not calls[0].error, calls[0]


def test_published_agent_reads_the_latest_personal_skill_bytes_after_its_trial(resources, saved_draft, skill_adapter):
    """c9940b7c7: published Agents follow the owner's current personal Skill bundle, so the actual
    read returns the latest bytes rather than the revision observed during the trial."""
    _composed, created = _created_after_trial(resources, saved_draft, skill_adapter)
    document = skill_adapter.service.root / "coached-method" / "SKILL.md"
    document.write_text(BODY + "A later manual edit.\n")
    _invocation, completed = _run(first_use(resources, created.json()["definition_id"], read_skill=True))
    assert len(completed) == 1
    calls = completed[0][0].tool_invocations
    assert len(calls) == 1
    assert not calls[0].error and isinstance(calls[0].result, dict) and not calls[0].result.get("error"), calls[0]
    assert "A later manual edit" in str(calls[0].result)
