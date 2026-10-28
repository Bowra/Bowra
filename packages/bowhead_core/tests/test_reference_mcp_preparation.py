from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import aiosqlite
import pytest
import respx
from bowhead_core._db import open_shared_ctx
from bowhead_core.access_control import AccessControlStore
from bowhead_core.agent_behavior_contract import AgentBehaviorDescriptor, behavior_fingerprint
from bowhead_core.agent_behavior_materializer import default_agent_behavior_materializer
from bowhead_core.agent_execution_binding import AgentExecutionBindingError, AgentExecutionBindingStore
from bowhead_core.chat_reference_admission import admit_reference_start_in_connection, reference_start_receiver
from bowhead_core.chat_reference_preparation import prepare_reference_team_start_admission
from bowhead_core.chat_reference_projection import read_reference_projection, reference_resource_identity
from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime
from bowhead_core.chat_reference_scope import ReferenceRequestScope, require_reference_request_scope
from bowhead_core.chat_reference_storage import ReferenceCipher, ReferenceScope
from bowhead_core.chat_references import AgentResourceRef, CapabilityResourceRef, InputDocument, ReferenceNode, SkillResourceRef
from bowhead_core.chat_turn_source import ChatTurnUserSource
from bowhead_core.events import TerminalDurabilityAbort, TurnEventBus
from bowhead_core.openapi_tool_source import openapi_entry, parse_openapi_ops
from bowhead_core.reference_mcp_execution import claim_reference_mcp_primary_for_worker, restore_reference_mcp_composition_in_connection
from bowhead_core.reference_mcp_metadata import build_reference_mcp_metadata
from bowhead_core.reference_mcp_preparation import (
    ReferenceMcpPreparationError,
    prepare_reference_mcp_prompt_selection,
    prepare_reference_mcp_selection,
    project_reference_mcp_admissions_in_connection,
    project_reference_mcp_prompt_admissions_in_connection,
    reference_mcp_prompt_selection_digest,
    revalidate_reference_mcp_prompt_selection_in_connection,
    revalidate_reference_mcp_selection_in_connection,
)
from bowhead_core.reference_mcp_prompt_execution import (
    claim_reference_mcp_prompt_primary_for_worker,
    restore_reference_mcp_prompt_composition_in_connection,
)
from bowhead_core.reference_mcp_runtime import ReferenceMcpHandlerScope
from bowhead_core.session_store import SessionStore, _insert_chat_turn_user_source_in_tx
from bowhead_core.skill_read_resolution import SkillReadSourceResolver
from bowhead_core.subagent import SubagentResult, SubagentRunner
from bowhead_core.team_tool_registry_view import build_team_specs
from bowhead_core.tool_registry import TeamToolExecutionBinding, ToolExecutionContext, ToolRegistry
from bowhead_router.tool_names import to_wire_tool_name
from bowhead_router.types import ToolSchema
from cryptography.fernet import Fernet
from test_agent_admission_transaction import _team_admission_case
from test_agent_execution_binding import _insert_release
from test_reference_mcp_metadata import _SPEC, _insert_source
from test_reference_mcp_receiver_metadata import _dependency, _tool
from test_skill_extension_runtime import tool_response
from test_skill_model_invocation import adapter_for, router_for, success

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class _McpResultClient:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        return SimpleNamespace(is_error=False, structured_content={"ok": True, "data": {"margin": 42}}, content=[])


async def _enable_mcp_agent(case, *, sequence: int = 2):
    stable_ref = "teammcp.ots_finance.getMargin"
    revision_id, release_id = await _insert_release(
        case.fixture,
        sequence=sequence,
        prompt="MCP reader",
        definition_overrides={"tool_refs": [stable_ref]},
    )
    _insert_source(
        case.fixture.db_path,
        team_id=case.fixture.team_id,
        source_id="ots_finance",
        spec=_SPEC,
        selected=["getMargin", "setMargin"],
        created_by=case.fixture.owner_id,
    )
    with sqlite3.connect(case.fixture.db_path) as db:
        db.row_factory = sqlite3.Row
        db.execute("UPDATE team_subagent_schema_meta SET release_semantics_activated_at=? WHERE singleton=1", (datetime.now(UTC).isoformat(),))
        revision_row = db.execute(
            "SELECT * FROM team_subagent_revision WHERE id=?",
            (revision_id,),
        ).fetchone()
        revision = revision_row["dependency_fingerprint"]
        assignment_id = db.execute(
            "SELECT id FROM agent_assignment WHERE agent_id='osa_exact' AND user_id=?",
            (case.fixture.member_id,),
        ).fetchone()[0]
        db.execute(
            """INSERT INTO agent_catalog_projection(
                 team_id,agent_id,source_kind,source_version,compiler_version,canonical_name,
                 canonical_summary,owner_label,capability_summary_json,facet_json,risk_summary_json,
                 safety_summary_json,dependency_json,search_text,projection_hash,built_at
               ) VALUES (?,?,'team',?,'agent-dependency-v1','exact-agent',
                         'summary',NULL,'[]','{}','{}','{}',?,'exact agent',?,?)""",
            (
                case.fixture.team_id,
                "osa_exact",
                revision_id,
                _dependency(_tool(stable_ref), fingerprint=revision),
                "sha256:projection",
                datetime.now(UTC).isoformat(),
            ),
        )
    materializer = default_agent_behavior_materializer()
    descriptor = materializer.compile_legacy_team_revision(
        revision_id=revision_row["id"],
        definition=json.loads(revision_row["definition_json"]),
        source_definition_hash=revision_row["definition_hash"],
        dependency_fingerprint=revision_row["dependency_fingerprint"],
        capability_fingerprint=revision_row["capability_fingerprint"],
    )
    draft = descriptor.model_copy(update={"verification_state": "reviewed", "reason_codes": ()})
    descriptor = AgentBehaviorDescriptor.model_validate({**draft.model_dump(mode="python"), "behavior_fingerprint": behavior_fingerprint(draft)})
    async with open_shared_ctx(case.fixture.db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        await materializer.persist_team_revision_conn(
            db,
            revision_id=revision_row["id"],
            team_id=case.fixture.team_id,
            subagent_id="osa_exact",
            descriptor=descriptor,
            created_at=datetime.now(UTC).isoformat(),
        )
        await db.commit()
    index = await build_reference_mcp_metadata(case.fixture.db_path)
    source = index.for_team(case.fixture.team_id)[0]
    read = next(operation for operation in source.operations if operation.id == stable_ref)
    write = next(operation for operation in source.operations if operation.id.endswith(".setMargin"))
    agent = AgentResourceRef(
        kind="agent",
        source_kind="team",
        id="osa_exact",
        version=revision_id,
        assignment_id=assignment_id,
        release_id=release_id,
    )
    scope = ReferenceRequestScope(
        team_id=case.fixture.team_id,
        user_id=case.fixture.member_id,
        workspace_id=case.fixture.workspace_id,
    )
    return case, scope, agent, index, source, read, write, AccessControlStore(case.fixture.db_path)


async def _case(tmp_path):
    return await _enable_mcp_agent(await _team_admission_case(tmp_path))


async def _prompt_case(tmp_path, *, prompt_count: int = 1):
    case, scope, agent, _index, _source, _read, _write, access = await _case(tmp_path)
    prompts = [
        {
            "name": "profitability",
            "title": "Profitability",
            "description": "Explain profitability",
            "arguments": [
                {
                    "name": "period",
                    "description": "Reporting period",
                    "required": True,
                }
            ],
        }
    ]
    if prompt_count == 2:
        prompts.append(
            {
                "name": "regional_profitability",
                "title": "Regional profitability",
                "description": "Explain regional profitability",
                "arguments": [
                    {
                        "name": "region",
                        "description": "Reporting region",
                        "required": True,
                    }
                ],
            }
        )
    elif prompt_count != 1:
        raise ValueError("unsupported prompt count")
    _insert_source(
        case.fixture.db_path,
        team_id=case.fixture.team_id,
        source_id="ots_prompts",
        name="Finance Prompts",
        kind="mcp",
        tools=[],
        prompts=prompts,
        created_by=case.fixture.owner_id,
    )
    index = await build_reference_mcp_metadata(case.fixture.db_path)
    source = next(item for item in index.for_team(case.fixture.team_id) if item.source_id == "ots_prompts")
    prompt = source.prompts[0]
    assert prompt.resource is not None
    return case, scope, agent, index, prompt, access


def _document(agent, *selections):
    return InputDocument.model_validate(
        {
            "schema_version": 1,
            "nodes": [
                {
                    "type": "reference",
                    "node_id": "00000000-0000-4000-8000-000000000001",
                    "resource": agent.model_dump(mode="python"),
                    "label": "Analyst",
                    "parameters": {},
                },
                *[
                    {
                        "type": "reference",
                        "node_id": f"00000000-0000-4000-8000-{index:012d}",
                        "resource": resource.model_dump(mode="python"),
                        "label": label,
                        "parameters": parameters,
                    }
                    for index, (resource, label, parameters) in enumerate(selections, 2)
                ],
                {"type": "text", "text": " Analyze the selected period."},
            ],
        }
    )


async def test_prepares_exact_mcp_prompt_contract_without_remote_io(tmp_path):
    case, scope, agent, index, prompt, access = await _prompt_case(tmp_path)
    document = _document(
        agent,
        (prompt.resource, "Profitability", {"period": "FY2025"}),
    )

    prepared = await prepare_reference_mcp_prompt_selection(
        case.fixture.db_path,
        input_document=document,
        scope=scope,
        receiver=agent,
        index=index,
        access=access,
    )

    assert prepared.receiver == agent
    assert prepared.policy_epoch >= 0
    assert prepared.selection_digest.startswith("sha256:")
    assert len(prepared.prompts) == 1
    selected = prepared.prompts[0]
    assert selected.resource == prompt.resource
    assert selected.source_revision == prompt.source_revision
    assert json.loads(selected.parameter_schema_json)["required"] == ["period"]
    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN")
        projected = await project_reference_mcp_prompt_admissions_in_connection(
            db,
            db_path=case.fixture.db_path,
            prepared=prepared,
            input_document=document,
            index=index,
            access=access,
        )
    assert len(projected) == 1
    assert projected[0].resource == prompt.resource
    assert projected[0].operation == "expand"
    assert projected[0].policy_digest == selected.policy_digest


async def test_mcp_prompt_preparation_rejects_missing_parameters_and_stale_source(tmp_path):
    case, scope, agent, index, prompt, access = await _prompt_case(tmp_path)
    invalid = _document(agent, (prompt.resource, "Profitability", {}))
    with pytest.raises(
        ReferenceMcpPreparationError,
        match="reference_mcp_prompt_parameters_invalid",
    ):
        await prepare_reference_mcp_prompt_selection(
            case.fixture.db_path,
            input_document=invalid,
            scope=scope,
            receiver=agent,
            index=index,
            access=access,
        )

    stale_agent = agent.model_copy(update={"version": "asrev_stale"})
    stale_receiver_document = _document(
        stale_agent,
        (prompt.resource, "Profitability", {"period": "FY2025"}),
    )
    with pytest.raises(
        ReferenceMcpPreparationError,
        match="reference_mcp_receiver_changed",
    ):
        await prepare_reference_mcp_prompt_selection(
            case.fixture.db_path,
            input_document=stale_receiver_document,
            scope=scope,
            receiver=stale_agent,
            index=index,
            access=access,
        )
    forged_assignment = agent.model_copy(update={"assignment_id": "aa_forged"})
    forged_receiver_document = _document(
        forged_assignment,
        (prompt.resource, "Profitability", {"period": "FY2025"}),
    )
    with pytest.raises(
        ReferenceMcpPreparationError,
        match="reference_mcp_receiver_changed",
    ):
        await prepare_reference_mcp_prompt_selection(
            case.fixture.db_path,
            input_document=forged_receiver_document,
            scope=scope,
            receiver=forged_assignment,
            index=index,
            access=access,
        )

    document = _document(agent, (prompt.resource, "Profitability", {"period": "FY2025"}))
    prepared = await prepare_reference_mcp_prompt_selection(
        case.fixture.db_path,
        input_document=document,
        scope=scope,
        receiver=agent,
        index=index,
        access=access,
    )
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute(
            "UPDATE team_tool_source SET updated_at='2026-09-17T00:00:00+00:00' WHERE id='ots_prompts'",
        )
    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN")
        with pytest.raises(
            ReferenceMcpPreparationError,
            match="reference_mcp_source_unavailable",
        ):
            await revalidate_reference_mcp_prompt_selection_in_connection(
                db,
                db_path=case.fixture.db_path,
                prepared=prepared,
                input_document=document,
                index=index,
                access=access,
            )


async def test_mcp_prompt_revalidation_rejects_removed_receiver_assignment(tmp_path):
    case, scope, agent, index, prompt, access = await _prompt_case(tmp_path)
    document = _document(agent, (prompt.resource, "Profitability", {"period": "FY2025"}))
    prepared = await prepare_reference_mcp_prompt_selection(
        case.fixture.db_path,
        input_document=document,
        scope=scope,
        receiver=agent,
        index=index,
        access=access,
    )
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("DELETE FROM agent_assignment WHERE id=?", (agent.assignment_id,))
    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN")
        with pytest.raises(
            ReferenceMcpPreparationError,
            match="reference_mcp_receiver_changed",
        ):
            await revalidate_reference_mcp_prompt_selection_in_connection(
                db,
                db_path=case.fixture.db_path,
                prepared=prepared,
                input_document=document,
                index=index,
                access=access,
            )


async def test_mcp_prompt_preparation_requires_ready_prompt_cache(tmp_path):
    case, scope, agent, index, prompt, access = await _prompt_case(tmp_path)
    document = _document(agent, (prompt.resource, "Profitability", {"period": "FY2025"}))

    with pytest.raises(
        ReferenceMcpPreparationError,
        match="reference_mcp_prompt_metadata_unavailable",
    ):
        await prepare_reference_mcp_prompt_selection(
            case.fixture.db_path,
            input_document=document,
            scope=scope,
            receiver=agent,
            index=replace(index, prompt_cache_ready=False),
            access=access,
        )


async def _prompt_admission_case(tmp_path, *, turn_id: str, prompt_count: int = 1):
    case, scope, agent, index, prompt, access = await _prompt_case(tmp_path, prompt_count=prompt_count)
    prompt_source = next(item for item in index.for_team(case.fixture.team_id) if item.source_id == "ots_prompts")
    selections = [(prompt_source.prompts[0].resource, "Profitability", {"period": "FY2025"})]
    if prompt_count == 2:
        selections.append((prompt_source.prompts[1].resource, "Regional profitability", {"region": "APAC"}))
    document = _document(agent, *selections)
    admission = await prepare_reference_team_start_admission(
        case.fixture.db_path,
        input_document=document,
        scope=scope,
        mcp_index=index,
        mcp_access=access,
    )
    session_id = f"s_{turn_id}"
    sessions = SessionStore(case.fixture.db_path)
    session = await sessions.ensure_session(
        session_id=session_id,
        workspace_id=case.fixture.workspace_id,
        user_id=case.fixture.member_id,
    )
    await sessions.record_turn(turn_id=turn_id, session_id=session.id, user_text=document.display_text())
    source = ChatTurnUserSource(
        turn_id=turn_id,
        session_id=session.id,
        user_id=case.fixture.member_id,
        workspace_id=case.fixture.workspace_id,
        user_text=document.display_text(),
        attachment_ids=(),
        attachment_refs=(),
        parts=None,
        input_document=document,
    )
    return case, agent, prompt, admission, source


async def test_prompt_start_admission_persists_exact_plan_without_expanding(tmp_path):
    turn_id = "t_reference_mcp_prompt_plan"
    case, _agent, prompt, admission, source = await _prompt_admission_case(tmp_path, turn_id=turn_id)
    assert admission.mcp_selection is None
    assert admission.mcp_prompt_selection is not None
    now = datetime.now(UTC)
    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _insert_chat_turn_user_source_in_tx(db, source=source, created_at=now.isoformat())
        await admit_reference_start_in_connection(
            db,
            admission=admission,
            source=source,
            team_id=case.fixture.team_id,
            bindings=AgentExecutionBindingStore(case.fixture.db_path),
            expires_at=(now + timedelta(days=1)).isoformat(),
        )
        await db.commit()

    with sqlite3.connect(case.fixture.db_path) as db:
        plan = json.loads(db.execute("SELECT plan_json FROM chat_reference_plans WHERE turn_id=?", (turn_id,)).fetchone()[0])
        assert [(item["resource"]["kind"], item["operation"]) for item in plan["admissions"]] == [
            ("agent", "delegate"),
            ("mcp_prompt", "expand"),
        ]
        prompt_admission = plan["admissions"][1]
        assert prompt_admission["resource"] == prompt.resource.model_dump(mode="json")
        assert prompt_admission["policy_digest"] == admission.mcp_prompt_selection.prompts[0].policy_digest
        assert db.execute("SELECT COUNT(*) FROM turn_agent_binding WHERE turn_id=?", (turn_id,)).fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM turn_agent_execution WHERE turn_id=?", (turn_id,)).fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM chat_reference_resolutions WHERE turn_id=?", (turn_id,)).fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM chat_reference_content_snapshots WHERE turn_id=?", (turn_id,)).fetchone() == (0,)


@pytest.mark.parametrize(
    "extra",
    [
        SkillResourceRef(kind="skill", source_kind="team", id="skill_extra", version="sha256:" + "1" * 64),
        CapabilityResourceRef(kind="capability", id="cap_visualize", version=1),
    ],
)
async def test_prompt_start_receiver_rejects_unimplemented_mixed_resources(tmp_path, extra):
    _case, _scope, agent, _index, prompt, _access = await _prompt_case(tmp_path)
    mixed = _document(
        agent,
        (prompt.resource, "Profitability", {"period": "FY2025"}),
        (extra, "Unsupported mix", {}),
    )
    with pytest.raises(ValueError, match="reference_start_unsupported"):
        reference_start_receiver(mixed)


async def test_prompt_start_receiver_rejects_automatic_receiver(tmp_path):
    _case, _scope, agent, _index, prompt, _access = await _prompt_case(tmp_path)
    document = _document(agent, (prompt.resource, "Profitability", {"period": "FY2025"}))
    without_explicit_agent = document.model_copy(
        update={"nodes": [node for node in document.nodes if not isinstance(node, ReferenceNode) or not isinstance(node.resource, AgentResourceRef)]}
    )
    with pytest.raises(ValueError, match="reference_start_unsupported"):
        reference_start_receiver(without_explicit_agent, agent)


@pytest.mark.parametrize("changed", ["source", "assignment"])
async def test_prompt_start_admission_rolls_back_when_current_authority_changes(tmp_path, changed):
    turn_id = f"t_reference_mcp_prompt_changed_{changed}"
    case, agent, _prompt, admission, source = await _prompt_admission_case(tmp_path, turn_id=turn_id)
    with sqlite3.connect(case.fixture.db_path) as db:
        if changed == "source":
            db.execute("UPDATE team_tool_source SET updated_at='2026-09-17T00:00:00+00:00' WHERE id='ots_prompts'")
        else:
            db.execute("DELETE FROM agent_assignment WHERE id=?", (agent.assignment_id,))
    now = datetime.now(UTC)
    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _insert_chat_turn_user_source_in_tx(db, source=source, created_at=now.isoformat())
        with pytest.raises(ReferenceMcpPreparationError):
            await admit_reference_start_in_connection(
                db,
                admission=admission,
                source=source,
                team_id=case.fixture.team_id,
                bindings=AgentExecutionBindingStore(case.fixture.db_path),
                expires_at=(now + timedelta(days=1)).isoformat(),
            )
        await db.rollback()

    with sqlite3.connect(case.fixture.db_path) as db:
        for table in (
            "chat_turn_user_sources",
            "chat_reference_plans",
            "turn_agent_binding",
            "turn_agent_execution",
        ):
            assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE turn_id=?", (turn_id,)).fetchone() == (0,)


async def test_prompt_start_admission_rejects_parameters_changed_after_preparation(tmp_path):
    turn_id = "t_reference_mcp_prompt_parameters_changed"
    case, _agent, _prompt, admission, source = await _prompt_admission_case(tmp_path, turn_id=turn_id)
    prompt_node = next(node for node in admission.input_document.nodes if isinstance(node, ReferenceNode) and node.resource.kind == "mcp_prompt")
    prompt_node.parameters["period"] = "FY2026"
    now = datetime.now(UTC)
    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _insert_chat_turn_user_source_in_tx(db, source=source, created_at=now.isoformat())
        with pytest.raises(ReferenceMcpPreparationError, match="reference_mcp_prompt_preparation_changed"):
            await admit_reference_start_in_connection(
                db,
                admission=admission,
                source=source,
                team_id=case.fixture.team_id,
                bindings=AgentExecutionBindingStore(case.fixture.db_path),
                expires_at=(now + timedelta(days=1)).isoformat(),
            )
        await db.rollback()
    with sqlite3.connect(case.fixture.db_path) as db:
        for table in (
            "chat_turn_user_sources",
            "chat_reference_plans",
            "turn_agent_binding",
            "turn_agent_execution",
        ):
            assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE turn_id=?", (turn_id,)).fetchone() == (0,)


async def test_prompt_start_admission_does_not_open_released_entry(tmp_path, monkeypatch):
    # Released-entry admission with the primary runtime switched off (explicit 0; default is on since 2026-10-14).
    # Prompt admission itself is gated in Gateway by FEATURE_CHAT_REFERENCE_MCP_PROMPT.
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME", "0")
    turn_id = "t_reference_mcp_prompt_unreleased"
    case, _agent, _prompt, admission, source = await _prompt_admission_case(tmp_path, turn_id=turn_id)
    admission = replace(admission, require_released_entry=True)
    now = datetime.now(UTC)
    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _insert_chat_turn_user_source_in_tx(db, source=source, created_at=now.isoformat())
        with pytest.raises(ValueError, match="reference_entry_unavailable"):
            await admit_reference_start_in_connection(
                db,
                admission=admission,
                source=source,
                team_id=case.fixture.team_id,
                bindings=AgentExecutionBindingStore(case.fixture.db_path),
                expires_at=(now + timedelta(days=1)).isoformat(),
            )
        await db.rollback()
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM chat_reference_plans WHERE turn_id=?", (turn_id,)).fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM turn_agent_execution WHERE turn_id=?", (turn_id,)).fetchone() == (0,)


async def _admitted_prompt_worker_case(tmp_path, *, turn_id: str, prompt_count: int = 1, lead: bool = False):
    case, agent, prompt, admission, source = await _prompt_admission_case(tmp_path, turn_id=turn_id, prompt_count=prompt_count)
    if lead:
        admission = replace(admission, orchestration_contract="lead-delegation.v1").snapshot()
    now = datetime.now(UTC)
    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _insert_chat_turn_user_source_in_tx(db, source=source, created_at=now.isoformat())
        await admit_reference_start_in_connection(
            db,
            admission=admission,
            source=source,
            team_id=case.fixture.team_id,
            bindings=AgentExecutionBindingStore(case.fixture.db_path),
            expires_at=(now + timedelta(days=1)).isoformat(),
        )
        await db.execute(
            "INSERT INTO chat_turn_spawn_dispatch(turn_id,session_id,dispatch_kind,state,lease_expires_at,created_at,updated_at) "
            "VALUES(?,?,'start','claimed',?,?,?)",
            (
                turn_id,
                source.session_id,
                (now + timedelta(minutes=5)).isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )
        await db.commit()
    return case, agent, prompt, admission, source


async def test_prompt_worker_claim_persists_exact_composition_without_remote_io(tmp_path):
    turn_id = "t_reference_mcp_prompt_claim"
    case, agent, prompt, admission, source = await _admitted_prompt_worker_case(tmp_path, turn_id=turn_id)
    selection = admission.mcp_prompt_selection
    assert selection is not None
    scope = selection.scope.model_copy(update={"session_id": source.session_id})
    current = await prepare_reference_mcp_prompt_selection(
        case.fixture.db_path,
        input_document=source.input_document,
        scope=scope,
        receiver=agent,
        index=admission.mcp_index,
        access=admission.mcp_access,
    )

    claim = await claim_reference_mcp_prompt_primary_for_worker(
        index=admission.mcp_index,
        access=admission.mcp_access,
        scope=scope,
        turn_id=turn_id,
        owner_id="prompt-worker",
        expected_generation=1,
    )
    assert claim.outcome == "claimed" and claim.primary.binding.status == "running"
    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN")
        epoch = await require_reference_request_scope(db, scope)
        composition = await restore_reference_mcp_prompt_composition_in_connection(
            db,
            primary=claim.primary,
            scope=ReferenceScope(**scope.model_dump(), policy_epoch=epoch),
            generation=1,
        )
        # A later generation needs the original reserved BI pause; the saved
        # Prompt composition alone cannot authorize a new model conversation.
        with pytest.raises(ValueError, match="reference_mcp_prompt_composition_changed"):
            await restore_reference_mcp_prompt_composition_in_connection(
                db, primary=claim.primary, scope=ReferenceScope(**scope.model_dump(), policy_epoch=epoch), generation=2
            )
        await db.rollback()
    assert composition.resource == agent
    assert composition.prompt_policy_epoch == selection.policy_epoch
    assert composition.prompt_selection_digest == current.selection_digest
    assert len(composition.prompts) == 1
    assert composition.prompts[0].resource == prompt.resource
    assert composition.prompts[0].policy_digest == selection.prompts[0].policy_digest
    assert json.loads(composition.prompts[0].parameter_schema_json)["required"] == ["period"]

    retry = await claim_reference_mcp_prompt_primary_for_worker(
        index=admission.mcp_index,
        access=admission.mcp_access,
        scope=scope,
        turn_id=turn_id,
        owner_id="different-worker",
        expected_generation=1,
    )
    assert retry.outcome == "already_running"
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute(
            "SELECT status FROM turn_agent_execution WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == ("running",)
        assert db.execute(
            "SELECT state,owner_id FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == ("spawned", "prompt-worker")
        assert db.execute(
            "SELECT COUNT(*) FROM chat_reference_resolutions WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT COUNT(*) FROM chat_reference_content_snapshots WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == (0,)


async def test_prompt_composition_guard_keeps_non_result_later_slots_available(tmp_path):
    turn_id = "t_reference_mcp_prompt_future_result"
    case, _agent, _prompt, admission, source = await _admitted_prompt_worker_case(tmp_path, turn_id=turn_id)
    selection = admission.mcp_prompt_selection
    assert selection is not None
    claim = await claim_reference_mcp_prompt_primary_for_worker(
        index=admission.mcp_index,
        access=admission.mcp_access,
        scope=selection.scope.model_copy(update={"session_id": source.session_id}),
        turn_id=turn_id,
        owner_id="prompt-worker",
        expected_generation=1,
    )
    with sqlite3.connect(case.fixture.db_path) as db:
        saved = json.loads(
            db.execute(
                "SELECT composition_json FROM chat_reference_resolutions WHERE turn_id=? AND resolution_seq=1",
                (turn_id,),
            ).fetchone()[0],
        )
    saved["runtime_policy"] = "future-mcp-prompt-result.v1"
    payload = json.dumps(saved, sort_keys=True, separators=(",", ":"))
    digest = "sha256:" + hashlib.sha256(payload.encode()).hexdigest()
    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "INSERT INTO chat_reference_resolutions("
            "turn_id,resolution_seq,schema_version,kind,idempotency_key,source_turn_id,created_generation,execution_id,"
            "composition_digest,composition_json,created_at) VALUES (?,3,1,'composition',?,?,?,?,?,?,?)",
            (
                turn_id,
                "reference:prompt-result:future",
                turn_id,
                1,
                claim.primary.binding.execution_id,
                digest,
                payload,
                datetime.now(UTC).isoformat(),
            ),
        )
        await db.commit()
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute(
            "SELECT resolution_seq FROM chat_reference_resolutions WHERE turn_id=? ORDER BY resolution_seq",
            (turn_id,),
        ).fetchall() == [(1,), (3,)]


@pytest.mark.parametrize("changed", ["source", "assignment"])
async def test_prompt_worker_claim_rolls_back_when_authority_changes(tmp_path, changed):
    turn_id = f"t_reference_mcp_prompt_claim_changed_{changed}"
    case, agent, _prompt, admission, source = await _admitted_prompt_worker_case(tmp_path, turn_id=turn_id)
    selection = admission.mcp_prompt_selection
    assert selection is not None
    with sqlite3.connect(case.fixture.db_path) as db:
        if changed == "source":
            db.execute("UPDATE team_tool_source SET updated_at='2026-09-17T01:00:00+00:00' WHERE id='ots_prompts'")
        else:
            db.execute("DELETE FROM agent_assignment WHERE id=?", (agent.assignment_id,))

    expected_error = ReferenceMcpPreparationError if changed == "source" else AgentExecutionBindingError
    with pytest.raises(expected_error):
        await claim_reference_mcp_prompt_primary_for_worker(
            index=admission.mcp_index,
            access=admission.mcp_access,
            scope=selection.scope.model_copy(update={"session_id": source.session_id}),
            turn_id=turn_id,
            owner_id="prompt-worker",
            expected_generation=1,
        )
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute(
            "SELECT status FROM turn_agent_execution WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == ("admitted",)
        assert db.execute(
            "SELECT state,owner_id FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == ("claimed", None)
        assert db.execute(
            "SELECT COUNT(*) FROM chat_reference_resolutions WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == (0,)


async def test_unclaimed_prompt_primary_is_recoverable_without_expansion(tmp_path):
    turn_id = "t_reference_mcp_prompt_unclaimed_recovery"
    case, _agent, _prompt, _admission, _source = await _admitted_prompt_worker_case(tmp_path, turn_id=turn_id)
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), turn_id),
        )

    assert await SessionStore(case.fixture.db_path).recover_expired_chat_spawn_dispatches() == [turn_id]
    assert await SessionStore(case.fixture.db_path).recover_expired_chat_spawn_dispatches() == []
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute(
            "SELECT status,failure_code FROM turn_agent_execution WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == ("failed", "reference_worker_stopped")
        assert db.execute(
            "SELECT state,owner_id FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == ("failed", None)
        assert db.execute(
            "SELECT COUNT(*) FROM chat_reference_resolutions WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == (0,)


async def test_prompt_worker_requires_composition_migration_before_ownership(tmp_path):
    turn_id = "t_reference_mcp_prompt_claim_migration"
    case, _agent, _prompt, admission, source = await _admitted_prompt_worker_case(tmp_path, turn_id=turn_id)
    selection = admission.mcp_prompt_selection
    assert selection is not None
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("DROP TRIGGER trg_reference_mcp_prompt_composition")

    with pytest.raises(ValueError, match="reference_mcp_prompt_composition_migration_required"):
        await claim_reference_mcp_prompt_primary_for_worker(
            index=admission.mcp_index,
            access=admission.mcp_access,
            scope=selection.scope.model_copy(update={"session_id": source.session_id}),
            turn_id=turn_id,
            owner_id="prompt-worker",
            expected_generation=1,
        )
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute(
            "SELECT status FROM turn_agent_execution WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == ("admitted",)
        assert db.execute(
            "SELECT state,owner_id FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == ("claimed", None)
        assert db.execute(
            "SELECT COUNT(*) FROM chat_reference_resolutions WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == (0,)


@pytest.mark.parametrize("mutation", ["policy", "source_revision", "schema", "selection"])
async def test_prompt_composition_guard_rejects_plan_mismatch_atomically(tmp_path, monkeypatch, mutation):
    turn_id = "t_reference_mcp_prompt_claim_guard"
    case, _agent, _prompt, admission, source = await _admitted_prompt_worker_case(tmp_path, turn_id=turn_id)
    selection = admission.mcp_prompt_selection
    assert selection is not None
    import bowhead_core.reference_mcp_prompt_execution as execution

    original = execution._composition

    def malformed(primary, prepared):
        composition = original(primary, prepared)
        prompt = composition.prompts[0]
        if mutation == "policy":
            prompt = prompt.model_copy(update={"policy_digest": "sha256:" + "f" * 64})
        elif mutation == "source_revision":
            prompt = prompt.model_copy(update={"source_revision": "forged-revision"})
        elif mutation == "schema":
            prompt = prompt.model_copy(update={"parameter_schema_json": '{"type":"object"}'})
        else:
            return composition.model_copy(update={"prompt_selection_digest": "sha256:" + "f" * 64})
        return composition.model_copy(update={"prompts": (prompt, *composition.prompts[1:])})

    monkeypatch.setattr(execution, "_composition", malformed)
    with pytest.raises(ValueError, match="reference_mcp_prompt_composition_changed"):
        await claim_reference_mcp_prompt_primary_for_worker(
            index=admission.mcp_index,
            access=admission.mcp_access,
            scope=selection.scope.model_copy(update={"session_id": source.session_id}),
            turn_id=turn_id,
            owner_id="prompt-worker",
            expected_generation=1,
        )
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute(
            "SELECT status FROM turn_agent_execution WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == ("admitted",)
        assert db.execute(
            "SELECT state,owner_id FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == ("claimed", None)
        assert db.execute(
            "SELECT COUNT(*) FROM chat_reference_resolutions WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == (0,)


async def test_prompt_database_guard_rejects_plan_mismatch_atomically(tmp_path, monkeypatch):
    turn_id = "t_reference_mcp_prompt_claim_database_guard"
    case, _agent, _prompt, admission, source = await _admitted_prompt_worker_case(tmp_path, turn_id=turn_id)
    selection = admission.mcp_prompt_selection
    assert selection is not None
    import bowhead_core.reference_mcp_prompt_execution as execution

    original = execution._composition

    def malformed(primary, prepared):
        composition = original(primary, prepared)
        prompt = composition.prompts[0].model_copy(update={"policy_digest": "sha256:" + "f" * 64})
        return composition.model_copy(update={"prompts": (prompt, *composition.prompts[1:])})

    monkeypatch.setattr(execution, "_composition", malformed)
    monkeypatch.setattr(execution, "_validate_composition", lambda *_args: None)
    with pytest.raises(sqlite3.IntegrityError, match="reference MCP Prompt composition mismatch"):
        await claim_reference_mcp_prompt_primary_for_worker(
            index=admission.mcp_index,
            access=admission.mcp_access,
            scope=selection.scope.model_copy(update={"session_id": source.session_id}),
            turn_id=turn_id,
            owner_id="prompt-worker",
            expected_generation=1,
        )
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute(
            "SELECT status FROM turn_agent_execution WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == ("admitted",)
        assert db.execute(
            "SELECT state,owner_id FROM chat_turn_spawn_dispatch WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == ("claimed", None)
        assert db.execute(
            "SELECT COUNT(*) FROM chat_reference_resolutions WHERE turn_id=?",
            (turn_id,),
        ).fetchone() == (0,)


async def test_prompt_composition_rejects_reordered_prompts_atomically(tmp_path, monkeypatch):
    turn_id = "t_reference_mcp_prompt_claim_reordered"
    case, _agent, _prompt, admission, source = await _admitted_prompt_worker_case(tmp_path, turn_id=turn_id, prompt_count=2)
    selection = admission.mcp_prompt_selection
    assert selection is not None
    import bowhead_core.reference_mcp_prompt_execution as execution

    original = execution._composition

    def reordered(primary, prepared):
        composition = original(primary, prepared)
        prompts = tuple(reversed(composition.prompts))
        selection_digest = reference_mcp_prompt_selection_digest(
            scope=prepared.scope,
            receiver=composition.resource,
            policy_epoch=composition.prompt_policy_epoch,
            prompts=tuple((item.node_id, item.resource, item.source_revision, item.parameter_schema_json, item.policy_digest) for item in prompts),
        )
        return composition.model_copy(update={"prompts": prompts, "prompt_selection_digest": selection_digest})

    monkeypatch.setattr(execution, "_composition", reordered)
    with pytest.raises(ValueError, match="reference_mcp_prompt_composition_changed"):
        await claim_reference_mcp_prompt_primary_for_worker(
            index=admission.mcp_index,
            access=admission.mcp_access,
            scope=selection.scope.model_copy(update={"session_id": source.session_id}),
            turn_id=turn_id,
            owner_id="prompt-worker",
            expected_generation=1,
        )
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute("SELECT status FROM turn_agent_execution WHERE turn_id=?", (turn_id,)).fetchone() == ("admitted",)
        assert db.execute("SELECT state,owner_id FROM chat_turn_spawn_dispatch WHERE turn_id=?", (turn_id,)).fetchone() == ("claimed", None)
        assert db.execute("SELECT COUNT(*) FROM chat_reference_resolutions WHERE turn_id=?", (turn_id,)).fetchone() == (0,)


@pytest.mark.parametrize("mutation", ["source_hash", "task_hash", "capability_fingerprint", "baseline"])
async def test_prompt_composition_rejects_changed_primary_base_atomically(tmp_path, monkeypatch, mutation):
    turn_id = f"t_reference_mcp_prompt_claim_base_{mutation}"
    case, _agent, _prompt, admission, source = await _admitted_prompt_worker_case(tmp_path, turn_id=turn_id)
    selection = admission.mcp_prompt_selection
    assert selection is not None
    import bowhead_core.reference_mcp_prompt_execution as execution

    original = execution._composition

    def malformed(primary, prepared):
        composition = original(primary, prepared)
        if mutation == "baseline":
            baseline = composition.baseline.model_copy(update={"behavior_fingerprint": "sha256:" + "f" * 64})
            return composition.model_copy(update={"baseline": baseline})
        value = "sha256:" + "f" * 64 if mutation != "capability_fingerprint" else "forged-capability"
        return composition.model_copy(update={mutation: value})

    monkeypatch.setattr(execution, "_composition", malformed)
    with pytest.raises(ValueError, match="reference_mcp_prompt_composition_changed"):
        await claim_reference_mcp_prompt_primary_for_worker(
            index=admission.mcp_index,
            access=admission.mcp_access,
            scope=selection.scope.model_copy(update={"session_id": source.session_id}),
            turn_id=turn_id,
            owner_id="prompt-worker",
            expected_generation=1,
        )
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute("SELECT status FROM turn_agent_execution WHERE turn_id=?", (turn_id,)).fetchone() == ("admitted",)
        assert db.execute("SELECT state,owner_id FROM chat_turn_spawn_dispatch WHERE turn_id=?", (turn_id,)).fetchone() == ("claimed", None)
        assert db.execute("SELECT COUNT(*) FROM chat_reference_resolutions WHERE turn_id=?", (turn_id,)).fetchone() == (0,)


async def _claimed_runtime(tmp_path, *, turn_id: str, fail_remote: bool = False, fixed_year: int = 2025):
    case, scope, agent, index, source_metadata, read, write, access = await _case(tmp_path)
    document = _document(
        agent,
        (source_metadata.resource, "Finance source", {}),
        (read.resource, "Read margin", {"year": fixed_year}),
    )
    prepared = await prepare_reference_team_start_admission(
        case.fixture.db_path,
        input_document=document,
        scope=scope,
        mcp_index=index,
        mcp_access=access,
    )
    session_id = f"s_{turn_id}"
    sessions = SessionStore(case.fixture.db_path)
    session = await sessions.ensure_session(
        session_id=session_id,
        workspace_id=case.fixture.workspace_id,
        user_id=case.fixture.member_id,
    )
    await sessions.record_turn(turn_id=turn_id, session_id=session.id, user_text=document.display_text())
    source = ChatTurnUserSource(
        turn_id=turn_id,
        session_id=session.id,
        user_id=case.fixture.member_id,
        workspace_id=case.fixture.workspace_id,
        user_text=document.display_text(),
        attachment_ids=(),
        attachment_refs=(),
        parts=None,
        input_document=document,
    )
    now = datetime.now(UTC)
    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _insert_chat_turn_user_source_in_tx(db, source=source, created_at=now.isoformat())
        await admit_reference_start_in_connection(
            db,
            admission=prepared,
            source=source,
            team_id=case.fixture.team_id,
            bindings=AgentExecutionBindingStore(case.fixture.db_path),
            expires_at=(now + timedelta(days=1)).isoformat(),
        )
        await db.execute(
            "INSERT INTO chat_turn_spawn_dispatch(turn_id,session_id,dispatch_kind,state,lease_expires_at,created_at,updated_at) "
            "VALUES(?,?,'start','claimed',?,?,?)",
            (turn_id, session_id, (now + timedelta(minutes=5)).isoformat(), now.isoformat(), now.isoformat()),
        )
        await db.commit()
    request_scope = scope.model_copy(update={"session_id": session_id})
    claim = await claim_reference_mcp_primary_for_worker(
        index=index,
        access=access,
        scope=request_scope,
        turn_id=turn_id,
        owner_id="mcp-worker",
        expected_generation=1,
    )
    assert claim.outcome == "claimed"
    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN")
        epoch = await require_reference_request_scope(db, request_scope)
        await db.rollback()
    runtime_scope = ReferenceScope(**request_scope.model_dump(), policy_epoch=epoch)
    cipher = ReferenceCipher(Fernet.generate_key().decode())
    runtime = ReferencePrimaryRuntime(
        scope=runtime_scope,
        turn_id=turn_id,
        owner_id="mcp-worker",
        bindings=AgentExecutionBindingStore(case.fixture.db_path),
        cipher=cipher,
        mcp_index=index,
        mcp_access=access,
    )
    entries = tuple(openapi_entry("ots_finance", operation) for operation in parse_openapi_ops(_SPEC))
    client = _McpResultClient()
    view = ToolRegistry().clone_with_overlay(build_team_specs(case.fixture.team_id, entries, client))
    if fail_remote:
        selected = view.get(read.id)

        async def disconnected(arguments, _ctx):
            client.calls.append((read.id, arguments))
            raise RuntimeError("remote disconnected")

        view = view.clone_with_spec_override(replace(selected, handler=disconnected))
    ctx = ToolExecutionContext(
        **runtime_scope.identity(),
        turn_id=turn_id,
        member_uid=case.fixture.member_id,
        datarc=None,
        tool_view=view,
    )
    return case, runtime, ctx, document, read, write, client


@pytest.mark.parametrize("invoke_required", [True, False])
async def test_primary_runtime_scopes_and_completes_exact_mcp_tool(tmp_path, invoke_required):
    turn_id = "t_reference_mcp_runtime_ok" if invoke_required else "t_reference_mcp_runtime_missing"
    case, runtime, ctx, document, read, write, client = await _claimed_runtime(tmp_path, turn_id=turn_id)

    async def run(**kwargs):
        assert read.id not in kwargs["tool_deny"]
        assert write.id in kwargs["tool_deny"]
        assert f'"required_operations":["{read.id}"]' in kwargs["extra_instructions"]
        assert '"year":2025' not in kwargs["extra_instructions"]
        assert kwargs["parent_ctx"].tool_view.get(read.id).schema.input_schema == {
            "type": "object",
            "properties": {"year": {"const": 2025}},
            "required": ["year"],
            "additionalProperties": False,
        }
        if invoke_required:
            result = await kwargs["parent_ctx"].tool_view.get(read.id).handler({"year": 2025}, kwargs["parent_ctx"])
            assert result == {"ok": True, "data": {"margin": 42}}
        return SubagentResult(
            profile=kwargs["profile_name"],
            subagent_id=kwargs["pre_minted_subagent_id"],
            ok=True,
            final_answer="Margin analyzed.",
        )

    result = await runtime.run(runner=SimpleNamespace(run=run), parent_ctx=ctx, parent_bus=TurnEventBus())
    assert result.ok is invoke_required
    assert result.error == (None if invoke_required else "reference_mcp_required_tool_incomplete")
    assert client.calls == ([(read.id, {"year": 2025})] if invoke_required else [])
    with sqlite3.connect(case.fixture.db_path) as db:
        execution = db.execute("SELECT status,failure_code FROM turn_agent_execution WHERE turn_id=?", (turn_id,)).fetchone()
        calls = db.execute("SELECT status,result_ok FROM chat_reference_mcp_calls WHERE turn_id=?", (turn_id,)).fetchall()
    assert execution == (("completed", None) if invoke_required else ("failed", "reference_mcp_required_tool_incomplete"))
    assert calls == ([("completed", 1)] if invoke_required else [])
    projection = await read_reference_projection(
        case.fixture.db_path,
        scope=runtime.scope,
        turn_id=turn_id,
        visible_resource_identities=frozenset(
            reference_resource_identity(node.resource) for node in document.nodes if isinstance(node, ReferenceNode)
        ),
        visibility_complete=True,
    )
    tool = next(item for item in projection["references"] if item["resource_kind"] == "mcp_tool")
    assert tool["preparation"] == "ready"
    assert tool["outcome"] == ("fulfilled" if invoke_required else "failed")
    if invoke_required:
        assert len(tool["mcp_calls"]) == 1
        call = tool["mcp_calls"][0]
        assert call["call_id"].startswith("mcp_") and call["result_hash"].startswith("sha256:")
        assert {key: call[key] for key in ("call_seq", "operation_name", "status", "result_ok", "failure_code")} == {
            "call_seq": 1,
            "operation_name": read.id,
            "status": "completed",
            "result_ok": True,
            "failure_code": None,
        }
    else:
        assert tool["mcp_calls"] == []


async def test_native_model_loop_sees_and_calls_only_exact_mcp_reference(tmp_path):
    turn_id = "t_reference_mcp_native_model"
    case, runtime, ctx, _document, read, write, client = await _claimed_runtime(
        tmp_path,
        turn_id=turn_id,
        fixed_year=2001,
    )
    adapter = adapter_for("openai")
    runner = SubagentRunner(
        router=router_for(adapter),
        tool_registry=ToolRegistry(),
        team_subagent_store=case.fixture.users,
    )
    sent: list[dict] = []

    # Providers only accept ^[a-zA-Z0-9_-]{1,64}$ names: the dotted reference goes out in
    # wire form, and the model's call on that wire name must dispatch the canonical reference.
    read_wire, write_wire = to_wire_tool_name(read.id), to_wire_tool_name(write.id)

    def respond(request):
        body = json.loads(request.content)
        sent.append(body)
        tools = {item["function"]["name"]: item["function"] for item in body["tools"]}
        assert read_wire in tools and write_wire not in tools
        assert tools[read_wire]["parameters"] == {
            "type": "object",
            "properties": {"year": {"const": 2001}},
            "required": ["year"],
            "additionalProperties": False,
        }
        if len(sent) == 1:
            return tool_response(
                read_wire,
                {"year": 2001},
                stream=body.get("stream", False),
            )
        return success(
            "openai",
            "stream" if body.get("stream", False) else "invoke",
        )

    try:
        with respx.mock(assert_all_called=False) as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            result = await runtime.run(
                runner=runner,
                parent_ctx=ctx,
                parent_bus=TurnEventBus(),
            )
        assert route.call_count == 2
    finally:
        await adapter.close()

    assert result.ok and result.final_answer == "OK", result.error
    assert client.calls == [(read.id, {"year": 2001})]
    system_text = "\n".join(str(message.get("content") or "") for message in sent[0]["messages"] if message["role"] == "system")
    assert "2001" not in system_text
    tool_messages = [message for message in sent[1]["messages"] if message["role"] == "tool"]
    assert len(tool_messages) == 1 and '"margin":42' in tool_messages[0]["content"].replace(" ", "")
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute(
            "SELECT operation_name,status,result_ok FROM chat_reference_mcp_calls WHERE turn_id=?",
            (turn_id,),
        ).fetchall() == [(read.id, "completed", 1)]


async def test_mcp_unknown_remote_result_recovers_without_reinvocation(tmp_path):
    turn_id = "t_reference_mcp_runtime_unknown"
    case, runtime, ctx, _document, read, _write, client = await _claimed_runtime(
        tmp_path,
        turn_id=turn_id,
        fail_remote=True,
    )

    async def run(**kwargs):
        await kwargs["parent_ctx"].tool_view.get(read.id).handler({"year": 2025}, kwargs["parent_ctx"])

    runner = SimpleNamespace(run=run)
    with pytest.raises(RuntimeError, match="remote disconnected"):
        await runtime.run(runner=runner, parent_ctx=ctx, parent_bus=TurnEventBus())
    assert len(client.calls) == 1
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute(
            "UPDATE chat_turn_spawn_dispatch SET lease_expires_at=? WHERE turn_id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), turn_id),
        )
        assert db.execute("SELECT status,failure_code FROM chat_reference_mcp_calls WHERE turn_id=?", (turn_id,)).fetchone() == (
            "interrupted",
            "handler_failed",
        )
    assert await SessionStore(case.fixture.db_path).recover_expired_chat_spawn_dispatches() == [turn_id]
    with sqlite3.connect(case.fixture.db_path) as db:
        assert db.execute("SELECT status FROM turn_agent_execution WHERE turn_id=?", (turn_id,)).fetchone() == ("failed",)
    with pytest.raises(TerminalDurabilityAbort):
        await runtime.run(runner=runner, parent_ctx=ctx, parent_bus=TurnEventBus())
    assert len(client.calls) == 1


async def test_gateway_start_supplies_exact_mcp_dependencies(tmp_path, monkeypatch):
    from bowhead_core import chat_reference_catalog
    from bowhead_gateway import chat_reference_start

    case, scope, agent, index, _source, read, _write, access = await _case(tmp_path)
    document = _document(agent, (read.resource, "Read margin", {"year": 2025}))
    existing_session = await SessionStore(case.fixture.db_path).ensure_session(
        session_id="s_reference_mcp_http",
        workspace_id=scope.workspace_id,
        user_id=scope.user_id,
    )

    async def unused_access():
        raise AssertionError("skill access must not be resolved for an MCP-only document")

    orchestrator = SimpleNamespace(
        session_store=case.store,
        reference_skill_sources_factory=lambda _scope: SkillReadSourceResolver(
            case.fixture.db_path,
            access_factory=unused_access,
            builtin_index=lambda: None,
        ),
        subagent_runner=SimpleNamespace(),
    )
    state = SimpleNamespace(
        orchestrator=orchestrator,
        session_store=case.store,
        access_control_store=access,
        reference_mcp_metadata=SimpleNamespace(current=lambda: index),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_PRIMARY_RUNTIME", "1")
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_LEAD_DELEGATION", "0")
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_MCP", "1")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", Fernet.generate_key().decode())
    original_support = chat_reference_catalog.reference_entry_support

    def mcp_support(mode):
        return [
            cell.model_copy(
                update={
                    "adapter_id": "initial-mcp-read",
                    "adapter_version": 1,
                    "supported": True,
                    "reason_code": None,
                }
            )
            if mode == "ordinary" and cell.resource_kind in {"mcp_source", "mcp_tool"}
            else cell
            for cell in original_support(mode)
        ]

    monkeypatch.setattr(chat_reference_catalog, "reference_entry_support", mcp_support)
    assert chat_reference_start.reference_mcp_writer_ready(request)
    assert chat_reference_start.reference_initial_writer_ready(request, document)
    prepared = await chat_reference_start.prepare_reference_http_start(
        request,
        document,
        team_id=scope.team_id,
        user_id=scope.user_id,
        workspace_id=scope.workspace_id,
        requested_session_id=existing_session.id,
        preferred_provider=None,
        preferred_model=None,
    )
    assert prepared.mcp_selection is not None
    assert prepared.mcp_index is index and prepared.mcp_access is access
    raw_mixed = document.model_dump(mode="python")
    raw_mixed["nodes"].insert(
        -1,
        {
            "type": "reference",
            "node_id": "00000000-0000-4000-8000-000000000099",
            "resource": {"kind": "capability", "id": "cap_visualize", "version": 1},
            "label": "Visualize",
            "parameters": {},
        },
    )
    mixed = InputDocument.model_validate(raw_mixed)
    assert not chat_reference_start.initial_reference_entry_supported(mixed)
    state.reference_mcp_metadata = SimpleNamespace(current=lambda: replace(index, ready=False))
    assert not chat_reference_start.reference_mcp_writer_ready(request)
    assert not chat_reference_start.reference_initial_writer_ready(request, document)
    state.reference_mcp_metadata = SimpleNamespace(current=lambda: index)
    state.access_control_store = AccessControlStore(tmp_path / "wrong.sqlite")
    assert not chat_reference_start.reference_mcp_writer_ready(request)
    state.access_control_store = access
    del state.reference_mcp_metadata
    assert not chat_reference_start.reference_mcp_writer_ready(request)
    assert not chat_reference_start.reference_initial_writer_ready(request, document)


async def test_prepares_source_and_tool_as_one_exact_network_free_dependency(tmp_path):
    case, scope, agent, index, source, read, _write, access = await _case(tmp_path)
    document = _document(
        agent,
        (source.resource, "Finance source", {}),
        (read.resource, "Read margin", {"year": 2025}),
    )
    prepared = await prepare_reference_mcp_selection(
        case.fixture.db_path,
        input_document=document,
        scope=scope,
        receiver=agent,
        index=index,
        access=access,
    )
    assert prepared.scope == scope and prepared.receiver == agent
    assert [item.operation for item in prepared.admissions] == ["provide", "invoke"]
    assert [item.expanded_operation_names for item in prepared.admissions] == [(read.id,), (read.id,)]
    assert [item.resource for item in prepared.operations] == [read.resource]
    assert prepared.selection_digest.startswith("sha256:")

    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await revalidate_reference_mcp_selection_in_connection(
            db,
            db_path=case.fixture.db_path,
            prepared=prepared,
            input_document=document,
            index=index,
            access=access,
        )
        admissions = await project_reference_mcp_admissions_in_connection(
            db,
            db_path=case.fixture.db_path,
            prepared=prepared,
            input_document=document,
            index=index,
            access=access,
        )
        assert [(item.node_id, item.operation, item.policy_digest) for item in admissions] == [
            (item.node_id, item.operation, item.policy_digest) for item in prepared.admissions
        ]
        assert db.in_transaction
        await db.rollback()


async def test_preparation_keeps_skill_and_capability_for_their_own_exact_resolvers(tmp_path):
    case, scope, agent, index, _source, read, _write, access = await _case(tmp_path)
    document = _document(agent, (read.resource, "Read margin", {"year": 2025}))
    document = InputDocument.model_validate(
        {
            "schema_version": 1,
            "nodes": [
                *[node.model_dump(mode="python") for node in document.nodes[:-1]],
                {
                    "type": "reference",
                    "node_id": "00000000-0000-4000-8000-000000000020",
                    "resource": {"kind": "skill", "source_kind": "builtin", "id": "method", "version": "sha256:" + "1" * 64},
                    "label": "Method",
                    "parameters": {},
                },
                {
                    "type": "reference",
                    "node_id": "00000000-0000-4000-8000-000000000021",
                    "resource": {"kind": "capability", "id": "cap_visualize", "version": 1},
                    "label": "Visualize",
                    "parameters": {"prompt": "margin"},
                },
                document.nodes[-1].model_dump(mode="python"),
            ],
        }
    )
    prepared = await prepare_reference_mcp_selection(
        case.fixture.db_path,
        input_document=document,
        scope=scope,
        receiver=agent,
        index=index,
        access=access,
    )
    assert [item.resource for item in prepared.admissions] == [read.resource]


async def test_exact_mcp_admission_revalidates_and_persists_plan_atomically(tmp_path):
    case, scope, agent, index, source_metadata, read, _write, access = await _case(tmp_path)
    document = _document(
        agent,
        (source_metadata.resource, "Finance source", {}),
        (read.resource, "Read margin", {"year": 2025}),
    )
    prepared = await prepare_reference_team_start_admission(
        case.fixture.db_path,
        input_document=document,
        scope=scope,
        mcp_index=index,
        mcp_access=access,
    )
    assert prepared.mcp_selection is not None and prepared.snapshot() == prepared
    turn_id = "t_reference_mcp_admission"
    sessions = SessionStore(case.fixture.db_path)
    session = await sessions.ensure_session(
        session_id="s_reference_mcp_admission",
        workspace_id=case.fixture.workspace_id,
        user_id=case.fixture.member_id,
    )
    await sessions.record_turn(turn_id=turn_id, session_id=session.id, user_text=document.display_text())
    source = ChatTurnUserSource(
        turn_id=turn_id,
        session_id=session.id,
        user_id=case.fixture.member_id,
        workspace_id=case.fixture.workspace_id,
        user_text=document.display_text(),
        attachment_ids=(),
        attachment_refs=(),
        parts=None,
        input_document=document,
    )
    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _insert_chat_turn_user_source_in_tx(db, source=source, created_at=datetime.now(UTC).isoformat())
        await admit_reference_start_in_connection(
            db,
            admission=prepared,
            source=source,
            team_id=case.fixture.team_id,
            bindings=AgentExecutionBindingStore(case.fixture.db_path),
            expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        )
        await db.commit()
    with sqlite3.connect(case.fixture.db_path) as db:
        plan = json.loads(db.execute("SELECT plan_json FROM chat_reference_plans WHERE turn_id=?", (turn_id,)).fetchone()[0])
        assert [item["operation"] for item in plan["admissions"]] == ["delegate", "provide", "invoke"]
        assert all(item["policy_digest"].startswith("sha256:") for item in plan["admissions"])
        assert db.execute("SELECT COUNT(*) FROM turn_agent_execution WHERE turn_id=?", (turn_id,)).fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM turn_agent_binding WHERE turn_id=?", (turn_id,)).fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM chat_reference_resolutions WHERE turn_id=?", (turn_id,)).fetchone() == (0,)


@pytest.mark.parametrize("composition_mutation", [None, "expanded_operation", "authority_after_prepare"])
async def test_exact_mcp_worker_claim_persists_network_free_composition_atomically(tmp_path, monkeypatch, composition_mutation):
    case, scope, agent, index, source_metadata, read, _write, access = await _case(tmp_path)
    document = _document(
        agent,
        (source_metadata.resource, "Finance source", {}),
        (read.resource, "Read margin", {"year": 2025}),
    )
    prepared = await prepare_reference_team_start_admission(
        case.fixture.db_path,
        input_document=document,
        scope=scope,
        mcp_index=index,
        mcp_access=access,
    )
    turn_id, session_id = "t_reference_mcp_claim", "s_reference_mcp_claim"
    sessions = SessionStore(case.fixture.db_path)
    session = await sessions.ensure_session(
        session_id=session_id,
        workspace_id=case.fixture.workspace_id,
        user_id=case.fixture.member_id,
    )
    await sessions.record_turn(turn_id=turn_id, session_id=session.id, user_text=document.display_text())
    source = ChatTurnUserSource(
        turn_id=turn_id,
        session_id=session.id,
        user_id=case.fixture.member_id,
        workspace_id=case.fixture.workspace_id,
        user_text=document.display_text(),
        attachment_ids=(),
        attachment_refs=(),
        parts=None,
        input_document=document,
    )
    now = datetime.now(UTC)
    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _insert_chat_turn_user_source_in_tx(db, source=source, created_at=now.isoformat())
        await admit_reference_start_in_connection(
            db,
            admission=prepared,
            source=source,
            team_id=case.fixture.team_id,
            bindings=AgentExecutionBindingStore(case.fixture.db_path),
            expires_at=(now + timedelta(days=1)).isoformat(),
        )
        await db.execute(
            "INSERT INTO chat_turn_spawn_dispatch(turn_id,session_id,dispatch_kind,state,lease_expires_at,created_at,updated_at) "
            "VALUES(?,?,'start','claimed',?,?,?)",
            (turn_id, session_id, (now + timedelta(minutes=5)).isoformat(), now.isoformat(), now.isoformat()),
        )
        await db.commit()
    runtime_scope = scope.model_copy(update={"session_id": session_id})
    if composition_mutation == "authority_after_prepare":
        import bowhead_core.reference_mcp_execution as execution

        stale = await prepare_reference_mcp_selection(
            case.fixture.db_path,
            input_document=document,
            scope=runtime_scope,
            receiver=agent,
            index=index,
            access=access,
        )
        with sqlite3.connect(case.fixture.db_path) as db:
            db.execute("UPDATE team_tool_source SET updated_at='2026-09-16T00:01:00+00:00' WHERE id='ots_finance'")

        async def stale_preparation(*args, **kwargs):
            return stale

        monkeypatch.setattr(execution, "prepare_reference_mcp_selection", stale_preparation)
        with pytest.raises(ReferenceMcpPreparationError):
            await claim_reference_mcp_primary_for_worker(
                index=index,
                access=access,
                scope=runtime_scope,
                turn_id=turn_id,
                owner_id="mcp-worker",
                expected_generation=1,
            )
        with sqlite3.connect(case.fixture.db_path) as db:
            assert db.execute("SELECT status FROM turn_agent_execution WHERE turn_id=?", (turn_id,)).fetchone() == ("admitted",)
            assert db.execute("SELECT state,owner_id FROM chat_turn_spawn_dispatch WHERE turn_id=?", (turn_id,)).fetchone() == (
                "claimed",
                None,
            )
            assert db.execute("SELECT COUNT(*) FROM chat_reference_resolutions WHERE turn_id=?", (turn_id,)).fetchone() == (0,)
        return
    if composition_mutation is not None:
        import bowhead_core.reference_mcp_execution as execution

        original = execution._composition

        def malformed(primary, current):
            composition = original(primary, current)
            first = composition.mcp_admissions[0].model_copy(update={"expanded_operation_names": ("teammcp.unknown.read",)})
            return composition.model_copy(update={"mcp_admissions": (first, *composition.mcp_admissions[1:])})

        monkeypatch.setattr(execution, "_composition", malformed)
        with pytest.raises(sqlite3.IntegrityError, match="reference MCP composition mismatch"):
            await claim_reference_mcp_primary_for_worker(
                index=index,
                access=access,
                scope=runtime_scope,
                turn_id=turn_id,
                owner_id="mcp-worker",
                expected_generation=1,
            )
        with sqlite3.connect(case.fixture.db_path) as db:
            assert db.execute("SELECT status FROM turn_agent_execution WHERE turn_id=?", (turn_id,)).fetchone() == ("admitted",)
            assert db.execute("SELECT state,owner_id FROM chat_turn_spawn_dispatch WHERE turn_id=?", (turn_id,)).fetchone() == (
                "claimed",
                None,
            )
            assert db.execute("SELECT COUNT(*) FROM chat_reference_resolutions WHERE turn_id=?", (turn_id,)).fetchone() == (0,)
        return
    first = await claim_reference_mcp_primary_for_worker(
        index=index,
        access=access,
        scope=runtime_scope,
        turn_id=turn_id,
        owner_id="mcp-worker",
        expected_generation=1,
    )
    assert first.outcome == "claimed" and first.primary.binding.status == "running"
    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN")
        epoch = await require_reference_request_scope(db, runtime_scope)
        composition = await restore_reference_mcp_composition_in_connection(
            db,
            primary=first.primary,
            scope=ReferenceScope(**runtime_scope.model_dump(), policy_epoch=epoch),
            generation=1,
        )
        await db.rollback()
    assert composition.runtime_policy == "initial-primary-mcp-read.v1"
    assert [item.operation for item in composition.mcp_admissions] == ["provide", "invoke"]
    assert [item.resource.operation_name for item in composition.mcp_operations] == [read.id]
    assert composition.mcp_admissions[1].policy_digest == first.primary.plan.admissions[2].policy_digest
    entry = next(openapi_entry("ots_finance", op) for op in parse_openapi_ops(_SPEC) if op.operation_id == "getMargin")
    client = _McpResultClient()
    view = ToolRegistry().clone_with_overlay(build_team_specs(case.fixture.team_id, [entry], client))
    handler_scope = await ReferenceMcpHandlerScope.bind(
        scope=ReferenceScope(**runtime_scope.model_dump(), policy_epoch=epoch),
        turn_id=turn_id,
        owner_id="mcp-worker",
        generation=1,
        index=index,
        access=access,
        bindings=AgentExecutionBindingStore(case.fixture.db_path),
        view=view,
    )
    assert [spec.schema.name for spec in handler_scope.handlers] == [read.id]
    assert handler_scope.handlers[0].team_tool_binding == TeamToolExecutionBinding(
        team_id=case.fixture.team_id,
        source_id="ots_finance",
        operation_id="getMargin",
        advertised_name=read.id,
    )
    wrong = replace(
        handler_scope.handlers[0],
        team_tool_binding=replace(handler_scope.handlers[0].team_tool_binding, source_id="other_source"),
    )
    changed_schema = replace(
        handler_scope.handlers[0],
        schema=ToolSchema(name=read.id, description="changed", input_schema={"type": "object", "additionalProperties": False}),
    )
    missing_overlay = ToolRegistry()
    missing_overlay.register(handler_scope.handlers[0])
    for changed_view in (view.clone_with_spec_override(wrong), view.clone_with_spec_override(changed_schema), missing_overlay):
        with pytest.raises(ValueError, match="reference_mcp_handler_scope_changed"):
            await ReferenceMcpHandlerScope.bind(
                scope=ReferenceScope(**runtime_scope.model_dump(), policy_epoch=epoch),
                turn_id=turn_id,
                owner_id="mcp-worker",
                generation=1,
                index=index,
                access=access,
                bindings=AgentExecutionBindingStore(case.fixture.db_path),
                view=changed_view,
            )
    cipher = ReferenceCipher(Fernet.generate_key().decode())
    wrapped = handler_scope.wrap_view(view, cipher=cipher)
    ctx = SimpleNamespace(
        team_id=case.fixture.team_id,
        user_id=case.fixture.member_id,
        workspace_id=case.fixture.workspace_id,
        session_id=session_id,
        turn_id=turn_id,
        skill_instance_store=None,
        access_control_store=None,
        access_context=None,
    )
    with pytest.raises(ValueError, match="reference_mcp_required_tool_incomplete"):
        await handler_scope.require_explicit_tools_completed()
    assert (await wrapped.get(read.id).handler({"year": "2025"}, ctx))["error_kind"] == "reference_mcp_parameters_invalid"
    assert client.calls == []
    assert await wrapped.get(read.id).handler({"year": 2025}, ctx) == {"ok": True, "data": {"margin": 42}}
    assert await wrapped.get(read.id).handler({"year": 2025}, ctx) == {"ok": True, "data": {"margin": 42}}
    assert client.calls == [("teammcp.ots_finance.getMargin", {"year": 2025})]
    await handler_scope.require_explicit_tools_completed()
    with sqlite3.connect(case.fixture.db_path) as db:
        db.row_factory = sqlite3.Row
        calls = db.execute("SELECT * FROM chat_reference_mcp_calls WHERE turn_id=?", (turn_id,)).fetchall()
    assert len(calls) == 1
    assert calls[0]["node_id"] == composition.mcp_admissions[1].node_id
    assert calls[0]["status"] == "completed" and calls[0]["result_ok"] == 1
    assert "margin" not in calls[0]["result_ciphertext"]
    with sqlite3.connect(case.fixture.db_path) as db:
        before = {
            table: db.execute(f"SELECT * FROM {table} WHERE turn_id=?", (turn_id,)).fetchall()
            for table in ("chat_turn_spawn_dispatch", "turn_agent_execution", "chat_reference_resolutions")
        }
    retry = await claim_reference_mcp_primary_for_worker(
        index=index,
        access=access,
        scope=runtime_scope,
        turn_id=turn_id,
        owner_id="other-worker",
        expected_generation=1,
    )
    assert retry.outcome == "already_running"
    with sqlite3.connect(case.fixture.db_path) as db:
        after = {table: db.execute(f"SELECT * FROM {table} WHERE turn_id=?", (turn_id,)).fetchall() for table in before}
    assert after == before
    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN")
        assert await handler_scope.revalidate_in_connection(db, view=view) == retry.primary
        await db.rollback()
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("UPDATE team_tool_source SET updated_at='2026-09-16T00:02:00+00:00' WHERE id='ots_finance'")
    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN")
        with pytest.raises(ReferenceMcpPreparationError, match="reference_mcp_source_unavailable"):
            await handler_scope.revalidate_in_connection(db, view=view)
        await db.rollback()


async def test_mcp_authority_change_rolls_back_the_whole_admission(tmp_path):
    case, scope, agent, index, _source_metadata, read, _write, access = await _case(tmp_path)
    document = _document(agent, (read.resource, "Read margin", {"year": 2025}))
    prepared = await prepare_reference_team_start_admission(
        case.fixture.db_path,
        input_document=document,
        scope=scope,
        mcp_index=index,
        mcp_access=access,
    )
    with sqlite3.connect(case.fixture.db_path) as db:
        db.execute("UPDATE team_tool_source SET updated_at='2026-09-16T00:01:00+00:00' WHERE id='ots_finance'")
    turn_id = "t_reference_mcp_rollback"
    sessions = SessionStore(case.fixture.db_path)
    session = await sessions.ensure_session(
        session_id="s_reference_mcp_rollback",
        workspace_id=case.fixture.workspace_id,
        user_id=case.fixture.member_id,
    )
    await sessions.record_turn(turn_id=turn_id, session_id=session.id, user_text=document.display_text())
    source = ChatTurnUserSource(
        turn_id=turn_id,
        session_id=session.id,
        user_id=case.fixture.member_id,
        workspace_id=case.fixture.workspace_id,
        user_text=document.display_text(),
        attachment_ids=(),
        attachment_refs=(),
        parts=None,
        input_document=document,
    )
    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await _insert_chat_turn_user_source_in_tx(db, source=source, created_at=datetime.now(UTC).isoformat())
        with pytest.raises(ReferenceMcpPreparationError):
            await admit_reference_start_in_connection(
                db,
                admission=prepared,
                source=source,
                team_id=case.fixture.team_id,
                bindings=AgentExecutionBindingStore(case.fixture.db_path),
                expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
            )
        await db.rollback()
    with sqlite3.connect(case.fixture.db_path) as db:
        for table in ("chat_turn_user_sources", "chat_reference_plans", "turn_agent_execution", "turn_agent_binding"):
            assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE turn_id=?", (turn_id,)).fetchone() == (0,)


@pytest.mark.parametrize(
    "mutation,error",
    [
        ("parameters", "reference_mcp_parameters_invalid"),
        ("write", "reference_mcp_contract_unavailable"),
        ("binding", "reference_mcp_contract_changed"),
        ("system", "reference_mcp_selection_unsupported"),
        ("source_parameters", "reference_mcp_contract_changed"),
        ("agent_parameters", "reference_mcp_selection_unsupported"),
    ],
)
async def test_preparation_rejects_unreviewed_or_inexact_selection(tmp_path, mutation, error):
    case, scope, agent, index, source, read, write, access = await _case(tmp_path)
    selected = read.resource
    parameters = {"year": 2025}
    receiver = agent
    if mutation == "parameters":
        parameters = {"year": 1999}
    elif mutation == "write":
        selected = write.resource
        parameters = {}
    elif mutation == "binding":
        selected = selected.model_copy(update={"connection_subject_fingerprint": "sha256:" + "0" * 64})
    elif mutation == "system":
        receiver = AgentResourceRef(kind="agent", source_kind="system", id="system:compute", version="sar_1")
    document = (
        _document(agent, (source.resource, "Finance source", {"unexpected": True}))
        if mutation == "source_parameters"
        else _document(agent, (selected, "Operation", parameters))
    )
    if mutation == "agent_parameters":
        document = document.model_copy(
            update={
                "nodes": [
                    node.model_copy(update={"parameters": {"unexpected": True}}) if getattr(node, "resource", None) == agent else node
                    for node in document.nodes
                ]
            }
        )
    with pytest.raises(ReferenceMcpPreparationError, match=error):
        await prepare_reference_mcp_selection(
            case.fixture.db_path,
            input_document=document,
            scope=scope,
            receiver=receiver,
            index=index,
            access=access,
        )


@pytest.mark.parametrize("change", ["disable", "revision", "deny", "projection"])
async def test_revalidation_fails_closed_after_current_authority_changes(tmp_path, monkeypatch, change):
    case, scope, agent, index, _source, read, _write, access = await _case(tmp_path)
    document = _document(agent, (read.resource, "Read margin", {"year": 2025}))
    prepared = await prepare_reference_mcp_selection(
        case.fixture.db_path,
        input_document=document,
        scope=scope,
        receiver=agent,
        index=index,
        access=access,
    )
    if change == "deny":
        await access.create_or_update_resource_grant(
            team_id=case.fixture.team_id,
            resource_kind="team_tool_op",
            resource_id=read.id,
            principal_type="user",
            principal_id=case.fixture.member_id,
            permission="use",
            effect="deny",
            created_by=case.fixture.owner_id,
        )
        monkeypatch.setenv("BOWHEAD_RESOURCE_GRANTS_ENFORCE", "1")
    else:
        with sqlite3.connect(case.fixture.db_path) as db:
            if change == "disable":
                db.execute("UPDATE team_tool_source SET status='disabled' WHERE id='ots_finance'")
            elif change == "projection":
                db.execute("UPDATE agent_catalog_projection SET source_version='arv_stale' WHERE agent_id='osa_exact'")
            else:
                db.execute("UPDATE team_tool_source SET updated_at='2026-09-16T00:01:00+00:00' WHERE id='ots_finance'")
    async with open_shared_ctx(case.fixture.db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ReferenceMcpPreparationError):
            await revalidate_reference_mcp_selection_in_connection(
                db,
                db_path=case.fixture.db_path,
                prepared=prepared,
                input_document=document,
                index=index,
                access=access,
            )
        await db.rollback()


async def test_preparation_rejects_incomplete_or_cross_database_snapshot(tmp_path):
    case, scope, agent, index, _source, read, _write, access = await _case(tmp_path)
    document = _document(agent, (read.resource, "Read margin", {"year": 2025}))
    for changed in (
        index.__class__(index.db_path, index.items, True, frozenset({case.fixture.team_id})),
        index.__class__(tmp_path / "other.sqlite", index.items, True),
    ):
        with pytest.raises(ReferenceMcpPreparationError, match="reference_mcp_metadata_unavailable"):
            await prepare_reference_mcp_selection(
                case.fixture.db_path,
                input_document=document,
                scope=scope,
                receiver=agent,
                index=changed,
                access=access,
            )


@pytest.mark.parametrize("kind", ["mcp_tool", "mcp_prompt"])
async def test_mcp_selection_defers_attachment_task_validation_to_admission(tmp_path, kind):
    from bowhead_core.chat_reference_preparation import ReferenceStartPreparationError

    if kind == "mcp_prompt":
        case, scope, agent, index, prompt, access = await _prompt_case(tmp_path)
        document = _document(agent, (prompt.resource, "Profitability", {"period": "FY2026"}))
    else:
        case, scope, agent, index, _source, tool, _write, access = await _case(tmp_path)
        document = _document(agent, (tool.resource, "Read margin", {"year": 2026}))
    document = InputDocument.model_validate(
        {
            "schema_version": 1,
            "nodes": [node.model_dump(mode="python") for node in document.nodes if isinstance(node, ReferenceNode)],
        }
    )
    arguments = {"input_document": document, "scope": scope, "mcp_index": index, "mcp_access": access}
    with pytest.raises(ReferenceStartPreparationError):
        await prepare_reference_team_start_admission(case.fixture.db_path, **arguments, has_attachments=False)
    prepared = await prepare_reference_team_start_admission(case.fixture.db_path, **arguments, has_attachments=True)
    assert prepared.has_attachments and prepared.input_document == document
    assert (prepared.mcp_prompt_selection is not None) == (kind == "mcp_prompt")
    assert (prepared.mcp_selection is not None) == (kind == "mcp_tool")
