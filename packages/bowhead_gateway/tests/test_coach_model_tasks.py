"""Opt-in real Coach evaluation over isolated original domain services.

Run through scripts/eval_coach.py. Ordinary test discovery makes no model calls.
The self-test verifies the harness; it is never reported as model acceptance.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path

import anyio
import pytest
from bowhead_core.access_control import AccessControlStore
from bowhead_core.agent_coach import AGENT_NAME, register_agent_coach_tools
from bowhead_core.agents import AgentRegistry
from bowhead_core.agents.agent_runner_adapter import manifest_to_profile
from bowhead_core.coach_operation_reference import with_operation_reference
from bowhead_core.events import TurnEventBus
from bowhead_core.session_store import SessionStore
from bowhead_core.skill_bridge import build_default_skill_stack, register_skill_tools
from bowhead_core.skill_upload import bundle_sha256
from bowhead_core.subagent import PROFILES, SubagentRunner, register_subagent_tool
from bowhead_core.team_plugin_store import TeamPluginStore
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry
from bowhead_router.config_loader import build_router_from_config
from bowhead_router.invocation import physical_observation_scope
from test_agent_coach_draft_adapter import saved_draft as saved_draft
from test_agent_coach_draft_bindings import accept, activate, create, review
from test_agent_coach_plugins import administrator
from test_agent_coach_resources import resources as resources
from test_agent_coach_skills import BODY
from test_agent_coach_skills import skill_adapter as skill_adapter
from test_team_subagent_drafts_routes import _run

from bowhead_router import AgentBinding, LLMChunk, LLMResponse, MockAdapter, ProviderConfig, ProviderRegistry, Router, ToolCall

ROOT = Path(__file__).resolve().parents[3]
SUITE_PATH = ROOT / "eval/fixtures/coach/tasks-v1.json"
SUITE = json.loads(SUITE_PATH.read_text())
TABLES = (
    "team_subagent_draft",
    "team_subagent_draft_suggestion",
    "team_subagent",
    "team_subagent_revision",
    "team_capability",
    "team_capability_revision",
    "coach_plugin_operation",
    "coach_skill_operation",
    "coach_tool_requirement",
    "agent_eval_run",
    "agent_eval_suite",
    "agent_change_proposal",
    "agent_change_proposal_decision",
    "coach_ci_delegation",
    "team_subagent_working_copy",
)
pytestmark = pytest.mark.integration


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def snapshot(path):
    with sqlite3.connect(path) as db:
        return {table: sorted(db.execute(f'SELECT * FROM "{table}"').fetchall(), key=str) for table in TABLES}


class Attempts:
    """Bound and observe physical transports without retaining request bodies."""

    def __init__(self, limit):
        self.limit, self.rows = limit, []

    async def begin(self, spec, **kwargs):
        if len(self.rows) >= self.limit:
            raise ValueError("Coach evaluation physical-call limit reached")
        row = {"provider": spec.provider, "model": spec.model, "request_digest": spec.request_digest, "status": "unknown", "tokens": None}
        self.rows.append(row)

        class Attempt:
            async def complete(self, *, usage, response_digest):
                row.update(status="complete", tokens={"input": usage.input_tokens, "output": usage.output_tokens, "cached": usage.cached_tokens})
                return True

            async def confirmed_not_applied(self, **kwargs):
                row["status"] = "not_applied"
                return True

            async def unknown(self, *, reason_code):
                row.update(status="unknown", reason_code=reason_code)

        return Attempt()


class ScriptedAdapter(MockAdapter):
    async def stream(self, request):
        response = await self.invoke(request)
        if response.content:
            yield LLMChunk(delta=response.content)
        for call in response.tool_calls:
            if call.name == "agent_operation_open" and call.arguments == {"reference": None}:
                original = next(
                    message for message in reversed(request.messages) if message.role == "tool" and message.name == "agent_tool_requirement_create"
                )
                call = replace(call, arguments={"reference": json.loads(original.content)["operation_ref"]})
            yield LLMChunk(tool_call_delta=call)
        yield LLMChunk(finish_reason=response.finish_reason, usage=response.usage)


def controlled_responses(case, values):
    calls = [("skill_read", {"name": "agent-authoring"})]
    outcome = case["outcome"]
    method = {
        "draft_proposal": "agents.md",
        "draft_conflict": "agents.md",
        "draft_scope_blocked": "agents.md",
        "draft_create": "agents.md",
        "validation": "agents.md",
        "agent_baseline": "agents.md",
        "agent_proposal": "agents.md",
        "skill_create": "resources.md",
        "skill_patch": "resources.md",
        "plugin_create": "resources.md",
        "plugin_denied": "resources.md",
        "tool_requirement": "resources.md",
        "case_proposal": "evaluation-cases.md",
        "ci_request": "evaluation-results.md",
    }.get(outcome)
    if method is not None:
        calls.append(("skill_read", {"name": "agent-authoring", "ref": method}))
    if case.get("selection"):
        calls.append(("agent_authoring_inspect", {}))
    if outcome == "discovery":
        calls += [("agent_resource_search", {"kind": "tool", "query": "web"}), ("agent_resource_get", {"reference": values["tool"]})]
    elif outcome == "draft_proposal":
        calls += [
            ("agent_authoring_inspect", {}),
            ("agent_authoring_propose", {"patch": {"description": "Summarize supplied evidence with citations."}, "request_id": "proposal"}),
        ]
    elif outcome == "draft_conflict":
        calls += [("agent_authoring_inspect", {})]
    elif outcome == "draft_scope_blocked":
        calls += [("agent_authoring_inspect", {})]
        patch = {"display_name": "Focused name edit"}
        if case.get("submission_required"):
            calls += [("agent_authoring_propose", {"patch": patch, "request_id": "blocked-submission"})]
        else:
            calls += [("agent_authoring_validate", {"patch": patch})]
    elif outcome == "draft_create":
        calls += [("agent_draft_start", {"purpose": "Summarize supplied evidence", "request_id": "create"})]
    elif outcome == "skill_create":
        target = {"kind": "personal_skill", "name": "data-question-clarifier", "id": None, "revision": None}
        content = "---\nname: data-question-clarifier\ndescription: Clarify vague data questions\ntrigger_examples: ['How is business recently?']\n---\nAsk at most three material questions, then summarize query requirements.\n"
        changes = [{"path": "SKILL.md", "content": content}]
        calls += [
            ("agent_skill_inspect", {"name": target["name"]}),
            ("agent_skill_validate", {"target": target, "changes": changes}),
            ("agent_skill_save", {"target": target, "changes": changes, "request_id": "create-personal"}),
        ]
    elif outcome == "skill_patch":
        calls += [
            ("agent_skill_inspect", {"name": "coached-method"}),
            (
                "agent_skill_save",
                {
                    "target": values["skill"]["result"]["target"],
                    "changes": [{"path": "SKILL.md", "content": BODY.replace("Read the evidence.", "Read the evidence and cite every claim.")}],
                    "request_id": "edit",
                },
            ),
        ]
    elif outcome in {"plugin_create", "plugin_denied"}:
        calls += [("agent_plugin_save", {"patch": {"name": "Evidence tools", "tools": [values["tool"]]}, "request_id": "plugin"})]
    elif outcome == "tool_requirement":
        calls += [
            (
                "agent_tool_requirement_create",
                {
                    "parent": values["parent"],
                    "requirement": {
                        "workflow": "engineering",
                        "operation": "Read offline scanner",
                        "reason": "No supported executor",
                        "input_schema": {"type": "object"},
                        "output_schema": {"type": "object"},
                    },
                    "request_id": "scanner",
                },
            ),
            ("agent_operation_open", {"reference": None}),
        ]
    elif outcome == "validation":
        calls += [("agent_authoring_validate", {"patch": {}})]
    elif outcome == "operation_recovery":
        calls += [("agent_operation_get", {"reference": values["skill"]["operation_ref"]})]
    elif outcome == "agent_baseline":
        calls += [("agent_working_copy_open", {"agent_id": values["agent_id"], "expected_live_release_id": values["live_release_id"]})]
    elif outcome == "agent_proposal":
        calls += [
            (
                "agent_change_propose",
                {
                    "target": values["agent_target"],
                    "operations": [{"op": "replace", "path": "/listing/description", "value": "Summarize supplied evidence with citations."}],
                    "atomic_groups": [{"id": "description", "operation_indexes": [0]}],
                    "request_id": "description",
                },
            )
        ]
    elif outcome == "case_proposal":
        calls += [
            (
                "agent_eval_case_propose",
                {
                    "target": {"kind": "initial_agent_eval_suite", "agent_id": values["agent_id"], "name": "Evidence quality"},
                    "cases": [
                        {
                            "id": "empty",
                            "task": "Summarize this empty evidence input without inventing facts.",
                            "failure_mode": "Inventing absent facts",
                            "case_spec": {"case_key": "quality.empty", "effect_policy": {"mode": "no_effects"}},
                        }
                    ],
                    "request_id": "cases",
                },
            )
        ]
    elif outcome == "ci_request":
        calls += [
            (
                "agent_ci_request",
                {
                    "target": values["accepted_proposal"],
                    "budget": {"max_provider_calls": 6, "max_comparisons": 1, "duration_seconds": 120},
                    "request_id": "ci",
                },
            )
        ]
    return [
        LLMResponse(content="", tool_calls=[ToolCall(id=f"step_{index}", name=name, arguments=args)]) for index, (name, args) in enumerate(calls)
    ] + [LLMResponse(content="The original domain result is available for review. No Agent was created or business trial run.")]


def original_receipt_read(call, reference):
    return (
        call.tool_name in {"agent_operation_get", "agent_skill_operation_get"}
        and not call.arguments.get("recover", False)
        and call.error is None
        and isinstance(call.result, dict)
        and call.result.get("ok") is True
        and call.result.get("operation_ref") == reference
    )


def outcome_checks(case, before, after, result, values, resources, saved_draft, skill_adapter, dispatches=()):
    """Judge durable effects and original identities, allowing different Tool paths."""
    kind = case["outcome"]
    allowed = {
        "draft_proposal": {"team_subagent_draft_suggestion"},
        "draft_create": {"team_subagent_draft"},
        "skill_patch": {"coach_skill_operation"},
        "skill_create": {"coach_skill_operation"},
        "plugin_create": {"team_capability", "team_capability_revision", "coach_plugin_operation"},
        "tool_requirement": {"coach_tool_requirement"},
        "agent_proposal": {"agent_change_proposal", "agent_change_proposal_decision", "team_subagent_working_copy"},
        "case_proposal": {"agent_change_proposal"},
        "ci_request": {"coach_ci_delegation"},
        "agent_baseline": {"team_subagent_working_copy"},
    }.get(kind, set())
    checks = {
        "completed": result is not None and result.ok,
        "unrelated_state_preserved": all(before[key] == after[key] for key in TABLES if key not in allowed),
    }
    if case.get("conflict_stage") == "admission":
        return {
            "coach_not_started": result is None,
            "unrelated_state_preserved": checks["unrelated_state_preserved"],
            "one_blocked_dispatch": len(dispatches) == 1
            and isinstance(dispatches[0], dict)
            and dispatches[0].get("ok") is False
            and dispatches[0].get("error") == "draft_unavailable_or_changed"
            and dispatches[0].get("retry") == "new_user_turn",
        }
    calls = result.tool_invocations if result else []
    checks["pinned_method_read"] = any(
        call.tool_name == "skill_read"
        and call.arguments.get("name") == "agent-authoring"
        and call.error is None
        and "Choose operations" in str(call.result)
        for call in calls
    )
    for table in allowed - {"team_subagent_working_copy"}:
        checks[f"one_{table}"] = len(after[table]) == len(before[table]) + 1
    if kind == "agent_baseline":
        checks["one_team_subagent_working_copy"] = len(after["team_subagent_working_copy"]) == len(before["team_subagent_working_copy"]) + 1
    successful = [call.result for call in calls if call.error is None and isinstance(call.result, dict) and call.result.get("ok")]
    if case.get("selection"):
        original_selection = resources.canvas_target.target.target.model_dump()
        checks["original_selection_read"] = any(item.get("selected_editor", {}).get("target") == original_selection for item in successful)
    draft = saved_draft[3]
    if kind == "draft_proposal":
        proposals = _run(
            saved_draft[1].list_canvas_suggestions(
                draft.id, team_id=resources.user.team_id, owner_user_id=resources.user.id, workspace_id=resources.workspace_id
            )
        )
        checks["exact_pending_fields"] = (
            len(proposals) == 1
            and proposals[0].status == "pending"
            and proposals[0].payload["changed_fields"] == ["description"]
            and proposals[0].payload["definition"]["description"] == "Summarize supplied evidence with citations."
        )
    if kind == "draft_conflict":
        authoring = [call for call in calls if call.tool_name.startswith("agent_authoring_")]
        checks["stopped_on_original_conflict"] = (
            len(authoring) == 1
            and authoring[0].tool_name == "agent_authoring_inspect"
            and isinstance(authoring[0].result, dict)
            and authoring[0].result.get("retry") == "new_user_turn"
        )
    if kind == "draft_scope_blocked":
        edits = [call for call in calls if call.tool_name in {"agent_authoring_validate", "agent_authoring_propose"}]
        checks["requested_edit_checked"] = any(call.arguments.get("patch") == {"display_name": "Focused name edit"} for call in edits)
        checks["requested_scope_preserved"] = all(set(call.arguments.get("patch", {})) <= {"display_name"} for call in edits)
        checks["original_blocker_read"] = any(
            isinstance(call.result, dict) and any(issue.get("code") == "subagent_tool_required" for issue in call.result.get("issues", []))
            for call in edits
        )
        if case.get("submission_required"):
            submissions = [call for call in edits if call.tool_name == "agent_authoring_propose"]
            checks["one_original_rejected_submission"] = (
                len(submissions) == 1
                and submissions[0].error is None
                and isinstance(submissions[0].result, dict)
                and submissions[0].result.get("ok") is False
                and submissions[0].result.get("error") == "proposal_invalid"
            )
    if kind == "discovery":
        checks["exact_schema_read"] = any(
            item.get("resource", {}).get("reference") == values["tool"] and "input_schema" in item["resource"] for item in successful
        )
    if kind == "validation":
        checks["no_trial_claim_in_evidence"] = any(
            item.get("validation_report", {}).get("checks", {}).get("business_trial", {}).get("status") == "not_run" for item in successful
        )
    if kind == "operation_recovery":
        checks["original_record_read"] = any(original_receipt_read(call, values["skill"]["operation_ref"]) for call in calls)
    if kind == "skill_create":
        from bowhead_skill.loader import load_skill

        package = skill_adapter.service.root / "data-question-clarifier"
        checks["personal_package_created"] = package.is_dir() and load_skill(package).name == "data-question-clarifier"
        saves = [call for call in calls if call.tool_name == "agent_skill_save"]
        checks["one_original_save_receipt"] = len(saves) == 1 and isinstance(saves[0].result, dict) and saves[0].result.get("state") == "succeeded"
        checks["short_path_without_agent_or_discovery"] = all(
            call.tool_name in {"skill_read", "agent_skill_inspect", "agent_skill_validate", "agent_skill_save", "agent_skill_operation_get"}
            for call in calls
        )
        checks["candidate_validated"] = any(
            call.tool_name == "agent_skill_validate" and isinstance(call.result, dict) and call.result.get("structural_validation") == "passed"
            for call in calls
        )
    if kind == "skill_patch":
        package = skill_adapter.service.root / "coached-method"
        checks["focused_package_edit"] = (package / "SKILL.md").read_text() == BODY.replace(
            "Read the evidence.", "Read the evidence and cite every claim."
        )
        checks["unrelated_file_preserved"] = (package / "references/keep.txt").read_text() == "Keep the supplied evidence unchanged.\n"
    if kind in {"plugin_create", "tool_requirement"}:
        with sqlite3.connect(resources.store.db_path) as db:
            if kind == "plugin_create":
                plugins = _run(TeamPluginStore(resources.store.db_path).list(resources.user.team_id))
                checks["exact_plugin_selection"] = (
                    len(plugins) == 1
                    and plugins[0]["name"] == "Evidence tools"
                    and plugins[0]["tool_refs"] == ["web_fetch"]
                    and plugins[0]["skill_grants"] == []
                )
            else:
                row = db.execute("SELECT parent_json,requirement_json,id,onboarding_operation_id FROM coach_tool_requirement").fetchone()
                checks["original_engineering_parent"] = (
                    row is not None and json.loads(row[0]) == values["parent"] and json.loads(row[1])["workflow"] == "engineering"
                )
                checks["receipt_view_without_onboarding"] = (
                    row is not None
                    and row[3] is None
                    and any(
                        call.tool_name == "agent_operation_open"
                        and call.error is None
                        and isinstance(call.result, dict)
                        and call.result.get("ok")
                        and call.result.get("operation_ref", {}).get("requirement_id") == row[2]
                        and call.result.get("view_delivery") == "event_emitted"
                        for call in calls
                    )
                )
    if kind in {"agent_proposal", "case_proposal", "ci_request"}:
        with sqlite3.connect(resources.store.db_path) as db:
            if kind == "ci_request":
                row = db.execute("SELECT state,max_provider_calls,max_comparisons FROM coach_ci_delegation").fetchone()
                checks["awaiting_bounded_owner_consent"] = row == ("requested", 6, 1)
            else:
                row = db.execute(
                    "SELECT status,patch_json,suggested_cases_json FROM agent_change_proposal WHERE agent_id=?", (values["agent_id"],)
                ).fetchone()
                checks["expected_change_state"] = row is not None and row[0] == ("accepted" if kind == "agent_proposal" else "open")
                if row and kind == "agent_proposal":
                    draft = db.execute(
                        "SELECT listing_json,row_version FROM team_subagent_working_copy WHERE subagent_id=?", (values["agent_id"],)
                    ).fetchone()
                    checks["draft_write_applied"] = (
                        draft is not None
                        and json.loads(draft[0])["description"] == "Summarize supplied evidence with citations."
                        and draft[1] == values["agent_target"]["version"] + 1
                    )
                    patch = json.loads(row[1])
                    checks["focused_agent_patch"] = (
                        len(patch) == 1
                        and patch[0]["path"] == "/listing/description"
                        and patch[0]["value"] == "Summarize supplied evidence with citations."
                    )
                elif row:
                    cases = json.loads(row[2])
                    checks["independent_case_only"] = (
                        json.loads(row[1]) == []
                        and len(cases) == 1
                        and cases[0]["task"] == "Summarize this empty evidence input without inventing facts."
                    )
    if kind == "agent_baseline":
        with sqlite3.connect(resources.store.db_path) as db:
            row = db.execute("SELECT base_release_id,definition_json,row_version FROM team_subagent_working_copy").fetchone()
            checks["exact_live_baseline"] = (
                row is not None and row[0] == values["live_release_id"] and json.loads(row[1]) == values["live_definition"] and row[2] == 1
            )
    return checks


@pytest.fixture
def evaluation(request):
    value = getattr(request.config, "coach_evaluation", None)
    if value is None:
        pytest.skip("Explicit scripts/eval_coach.py invocation required")
    return value


@pytest.mark.parametrize("case", SUITE["cases"], ids=[case["id"] for case in SUITE["cases"]])
def test_coach_task(evaluation, case, resources, saved_draft, skill_adapter, monkeypatch):
    args = evaluation.args
    manifest = AgentRegistry.bootstrap().get(AGENT_NAME)
    monkeypatch.setitem(PROFILES, AGENT_NAME, manifest_to_profile(manifest))
    if args.through_lead:
        monkeypatch.setitem(PROFILES, "lead", manifest_to_profile(AgentRegistry.bootstrap().get("lead")))
        monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "0")
    monkeypatch.setenv("BOWHEAD_SSE_FANOUT", "1")
    if case["outcome"] == "draft_scope_blocked":
        from bowhead_gateway.agent_coach_draft_adapter import AgentCoachDraftAdapter

        monkeypatch.setenv("FEATURE_SUBAGENT_TEXT_ONLY", "0")
        adapter, store, users, draft, owner, other, client = saved_draft
        incomplete = _run(
            store.save_manual_draft(
                draft.id,
                team_id=owner.team_id,
                owner_user_id=owner.id,
                workspace_id="ws_a",
                expected_version=draft.row_version,
                purpose=draft.purpose_text,
                definition={**draft.definition, "tool_refs": []},
                text_only_confirmed=False,
            )
        )
        adapter = AgentCoachDraftAdapter(replace(adapter.editor, expected_version=incomplete.row_version))
        saved_draft = adapter, store, users, incomplete, owner, other, client
    if case["outcome"] == "plugin_create":
        administrator(resources)
    else:
        with sqlite3.connect(resources.store.db_path) as db:
            db.execute("UPDATE team_members SET role='member' WHERE team_id=? AND user_id=?", (resources.user.team_id, resources.user.id))
    session = _run(
        SessionStore(resources.store.db_path).ensure_session(session_id=None, workspace_id=resources.workspace_id, user_id=resources.user.id)
    )
    resources = replace(resources, session_id=session.id)
    target = _run(skill_adapter.call("agent_skill_inspect", {"name": "coached-method"}, turn_id="t_fixture"))["target"]
    saved = _run(
        skill_adapter.call(
            "agent_skill_save",
            {
                "target": target,
                "changes": [
                    {"path": "SKILL.md", "content": BODY},
                    {"path": "references/keep.txt", "content": "Keep the supplied evidence unchanged.\n"},
                ],
                "request_id": "fixture",
            },
            turn_id="t_fixture",
        )
    )
    assert saved["ok"]
    saved = with_operation_reference("agent_skill_save", saved)
    tool = next(
        item["reference"]
        for item in _run(resources.search(kind="tool", query="", offset=0, limit=50))["items"]
        if item["reference"]["id"] == "web_fetch"
    )
    values = {"tool": tool, "skill": saved, "parent": {"kind": "agent_draft", "id": saved_draft[3].id, "version": saved_draft[3].row_version}}
    if case["outcome"] in {"agent_proposal", "case_proposal", "ci_request", "agent_baseline"}:
        activate(resources)
        url, applied = accept(saved_draft, {"description": "Summarize supplied evidence."})
        created = create(saved_draft[-1], url, review(saved_draft[-1], url, applied))
        assert created.status_code == 201, created.text
        values["agent_id"] = created.json()["definition_id"]
        reference = next(
            item["reference"]
            for item in _run(resources.search(kind="agent", query="", offset=0, limit=50))["items"]
            if item["reference"]["id"] == values["agent_id"]
        )
        detail = _run(resources.get(reference=reference))["resource"]["working_copy"]
        values["live_release_id"] = detail["live"]["release_id"]
        values["live_definition"] = detail["live"]["definition"]
        values["agent_target"] = (
            None
            if case["outcome"] == "agent_baseline"
            else _run(resources.authoring.open_working_copy(agent_id=values["agent_id"], expected_live_release_id=values["live_release_id"]))[
                "target"
            ]
        )
        if case["outcome"] == "ci_request":
            proposed = _run(
                resources.authoring.propose_change(
                    target=values["agent_target"],
                    operations=[{"op": "replace", "path": "/listing/description", "value": "Reviewed evidence instructions."}],
                    atomic_groups=[{"id": "description", "operation_indexes": [0]}],
                    suggested_cases=[],
                    rationale="Clarify the purpose",
                    request_id="fixture",
                    turn_id="t_fixture",
                )
            )["proposal"]
            values["accepted_proposal"] = {"kind": "accepted_agent_proposal", "agent_id": values["agent_id"], "proposal_id": proposed["id"]}
    registry = build_default_registry()
    catalog, runtime = build_default_skill_stack()
    register_skill_tools(registry, catalog, runtime)
    register_agent_coach_tools(registry)
    register_subagent_tool(registry)
    if args.self_test:
        providers = ProviderRegistry()
        providers.load([ProviderConfig(name="controlled", type="mock")])
        adapter = ScriptedAdapter(provider_name="controlled")
        adapter.scripted_responses = [] if case.get("conflict_stage") == "admission" else controlled_responses(case, values)
        if args.through_lead:
            adapter.scripted_responses.insert(
                0,
                LLMResponse(
                    content="",
                    tool_calls=[ToolCall(id="lead_dispatch", name="subagent_spawn", arguments={"profile": AGENT_NAME, "task": case["task"]})],
                ),
            )
            adapter.scripted_responses.append(LLMResponse(content="The original Coach result is available for review."))
        providers._adapters["controlled"] = adapter
        router = Router(providers)
        router.bind(AgentBinding(agent_type="analyst", primary=("controlled", "harness-self-test")))
        if args.through_lead:
            router.bind(AgentBinding(agent_type="lead", primary=("controlled", "harness-self-test")))
    else:
        router, providers, config = build_router_from_config(
            args.router_config, required_agents=("analyst", "lead") if args.through_lead else ("analyst",)
        )
        assert all(provider.type not in {"mock", "refusal"} for provider in config.providers), "Real-model mode rejects synthetic providers"
    admitted_selection = None
    if case.get("selection"):
        from bowhead_core.coach_canvas_target import CoachCanvasSendTarget
        from bowhead_gateway.coach_canvas_target import admit_coach_canvas_target

        async def current_user(request):
            return resources.user

        monkeypatch.setattr("bowhead_gateway.coach_canvas_target._current_user", current_user)
        monkeypatch.setattr("bowhead_gateway.routes.skills._user_dir_for", lambda uid: skill_adapter.service.root.parent / uid)
        resources.request.app.state.access_control_store = AccessControlStore(resources.store.db_path)
        if case["selection"] == "skill":
            selected = {
                "kind": "skill",
                "source_kind": "user",
                "id": saved["result"]["target"]["id"],
                "revision": saved["result"]["target"]["revision"],
            }
        else:
            from bowhead_core.agent_release_read_store import AgentReleaseReadStore

            detail = _run(
                AgentReleaseReadStore(resources.store.db_path).working_copy(
                    team_id=resources.user.team_id, agent_id=values["agent_id"], actor_user_id=resources.user.id
                )
            )
            copy = detail["working_copy"]
            selected = {
                "kind": "agent",
                "id": values["agent_id"],
                "version": detail["aggregate_row_version"],
                "live_release_id": detail["live"]["release_id"],
                "working_copy": {"id": copy["id"], "version": copy["row_version"], "base_release_id": copy["base_release_id"]},
            }
        target = CoachCanvasSendTarget.model_validate(
            {
                "schema_version": 1,
                "user_id": resources.user.id,
                "team_id": resources.user.team_id,
                "workspace_id": resources.workspace_id,
                "session_id": session.id,
                "target": selected,
                "dirty": False,
                "pending_write": False,
            }
        )
        admitted_selection = _run(
            admit_coach_canvas_target(
                resources.request,
                target,
                user_id=resources.user.id,
                team_id=resources.user.team_id,
                workspace_id=resources.workspace_id,
                session_id=session.id,
            )
        )
        resources = replace(resources, canvas_target=admitted_selection)
    task = case["task"]
    if case["outcome"] == "operation_recovery":
        task += "\nOriginal operation reference: " + json.dumps(saved["operation_ref"])
    if "agent_id" in values and not case.get("selection"):
        task += "\nOriginal Agent: " + json.dumps(
            values.get(
                "accepted_proposal", values["agent_target"] or {"agent_id": values["agent_id"], "expected_live_release_id": values["live_release_id"]}
            )
        )
    turn = _run(SessionStore(resources.store.db_path).record_turn(turn_id="t_coach_evaluation", session_id=session.id, user_text=task))
    before = snapshot(resources.store.db_path)
    observations = Attempts(args.max_calls)
    results = []
    dispatches = []
    record = {
        "id": case["id"],
        "selected_target": admitted_selection.target.model_dump() if admitted_selection else None,
        "passed": False,
        "suite_sha256": hashlib.sha256(SUITE_PATH.read_bytes()).hexdigest(),
        "manifest_version": manifest.version,
        "manifest_sha256": digest(manifest.model_dump(mode="json")),
        "prompt_sha256": digest(PROFILES[AGENT_NAME].system_prompt),
        "skill_revision": "sha256:" + bundle_sha256(ROOT / "packages/bowhead_skill/catalog/agent-authoring"),
        "tool_schema_sha256": digest([asdict(schema) for schema in registry.to_schemas() if schema.name in PROFILES[AGENT_NAME].tool_whitelist]),
        "configured_route": asdict(router.resolve("analyst", AGENT_NAME)),
        "lead_route": asdict(router.resolve("lead", "lead")) if args.through_lead else None,
        "physical_attempts": observations.rows,
        "limits": {"physical_calls": args.max_calls, "seconds": args.case_timeout},
        "cost_microusd": None,
        "semantic_review": "required",
        "narration": [],
    }
    evaluation.results.append(record)

    async def run():
        runner = SubagentRunner(router=router, tool_registry=registry, team_subagent_store=resources.store)
        original_run = runner.run

        async def save_newer_draft():
            _, store, _, draft, owner, _, _ = saved_draft
            await store.save_manual_draft(
                draft.id,
                team_id=owner.team_id,
                owner_user_id=owner.id,
                workspace_id="ws_a",
                expected_version=draft.row_version,
                purpose=draft.purpose_text,
                definition={**draft.definition, "description": "A newer manual edit."},
                text_only_confirmed=False,
            )
            before.update(snapshot(resources.store.db_path))

        async def capture(**kwargs):
            is_coach = kwargs["profile_name"] == AGENT_NAME
            if is_coach and case["outcome"] == "draft_conflict" and case.get("conflict_stage") != "admission":
                await save_newer_draft()
            result = await original_run(**kwargs)
            if is_coach:
                results.append(result)
            elif kwargs["profile_name"] == "lead":
                record["lead_prompt_sha256"] = digest(kwargs["extra_instructions"])
                dispatches.extend(call.result for call in result.tool_invocations if call.tool_name == "subagent_spawn")
            return result

        runner.run = capture
        ctx = ToolExecutionContext(
            team_id=resources.user.team_id,
            user_id=resources.user.id,
            member_uid=resources.user.id,
            workspace_id=resources.workspace_id,
            session_id=session.id,
            turn_id=turn.id,
            user_text=task,
            datarc=None,
            role="lead",
            parent_bus=TurnEventBus(),
            access_control_store=AccessControlStore(resources.store.db_path),
            skill_instance_store=resources.request.app.state.skill_instance_store,
            skill_catalog=catalog,
        )
        ctx.agent_coach_resources = resources
        ctx.coach_canvas_target = admitted_selection
        ctx.subagent_draft_editor = (
            saved_draft[0].editor
            if case["outcome"] in {"draft_proposal", "draft_scope_blocked", "draft_conflict", "validation", "tool_requirement"}
            else None
        )
        ctx.subagent_runner = runner
        original_emit = ctx.parent_bus.emit

        async def capture_narration(event):
            if event.event_type in {"partial_answer", "narrative.delta"}:
                record["narration"].append({"type": event.event_type, "parent_agent_id": event.parent_agent_id, "payload": event.payload})
            return await original_emit(event)

        monkeypatch.setattr(ctx.parent_bus, "emit", capture_narration)
        try:
            with anyio.fail_after(args.case_timeout), physical_observation_scope(observations):
                if case.get("conflict_stage") == "admission":
                    await save_newer_draft()
                if args.through_lead:
                    from bowhead_core.agents.lead_orchestrator import LeadRuntime, _lead_loop
                    from bowhead_core.agents.turn_context import SerializableTurnContext

                    delivery = await _lead_loop(
                        SerializableTurnContext(
                            user_id=ctx.user_id, workspace_id=ctx.workspace_id, session_id=ctx.session_id, turn_id=ctx.turn_id, role="user"
                        ),
                        task,
                        LeadRuntime(
                            router=router,
                            tool_registry=registry,
                            parent_bus=ctx.parent_bus,
                            parent_ctx=ctx,
                            subagent_runner=runner,
                            multi_agent_registry=AgentRegistry.bootstrap(),
                            team_subagent_store=resources.store,
                        ),
                    )
                    record["lead_final_answer"] = delivery.final_answer
                    record["lead_error"] = delivery.error
                    return delivery
                invocation = await registry.execute("subagent_spawn", {"profile": AGENT_NAME, "task": task}, ctx)
                dispatches.append(invocation.result)
                return invocation
        finally:
            await providers.close_all()

    _run(run())
    result = results[0] if results else None
    checks = outcome_checks(case, before, snapshot(resources.store.db_path), result, values, resources, saved_draft, skill_adapter, dispatches)
    if args.through_lead:
        if case.get("conflict_stage") != "admission":
            checks["one_coach_dispatch"] = len(results) == 1
        checks["lead_delivery_completed"] = bool(record.get("lead_final_answer")) and record.get("lead_error") is None
    record.update(
        checks=checks,
        passed=all(checks.values()),
        final_answer=result.final_answer if result else "",
        duration_ms=result.duration_ms if result else None,
        model_turn_count=result.model_turn_count if result else None,
        dispatch_receipts=dispatches,
        tools=[{"name": call.tool_name, "error": call.error is not None} for call in result.tool_invocations] if result else [],
    )
    if not args.self_test:
        if case.get("conflict_stage") == "admission" and not args.through_lead:
            record["checks"]["no_model_before_admission"] = not observations.rows
        else:
            record["checks"]["real_transport_observed"] = bool(observations.rows)
        record["passed"] = all(record["checks"].values())
    assert record["passed"], record["checks"]
