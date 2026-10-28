"""Capability source -> exact implementation ledger -> verified image output."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import httpx2
import pytest
import respx
from bowhead_core.chat_reference_preparation import prepare_reference_team_start_admission
from bowhead_core.chat_reference_projection import read_reference_projection, reference_resource_identity
from bowhead_core.chat_reference_runtime import ReferencePrimaryRuntime
from bowhead_core.chat_reference_storage import ReferenceCipher, ReferenceScope
from bowhead_core.chat_references import AgentResourceRef, InputDocument
from bowhead_core.events import TurnEventBus
from bowhead_core.skill_reference_execution import claim_reference_skill_primary_for_worker
from bowhead_core.subagent import SubagentRunner
from bowhead_core.tool_registry import ToolExecutionContext, ToolSpec, build_default_registry
from cryptography.fernet import Fernet
from test_reference_start_preparation import activate
from test_skill_model_invocation import adapter_for, router_for, success
from test_skill_read_receiver_integration import receiver_case as _receiver_case
from test_skill_read_receiver_integration import start_case as _start_case

from bowhead_router import ToolSchema

receiver_case, start_case = _receiver_case, _start_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission_capability", "admission_capability_skill"], indirect=True),
]


def _rows(case: SimpleNamespace, table: str) -> list[dict]:
    with sqlite3.connect(case.path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE turn_id='t_reference'")]


@pytest.fixture(params=["explicit", "automatic"])
async def capability_case(receiver_case, monkeypatch, request):
    fixture = receiver_case
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_CAPABILITY", "1")
    activate(fixture)
    original = fixture.case.args["reference_start"].input_document
    original_nodes = [node.model_dump(mode="python") for node in original.nodes]
    agent_node = next(node for node in original_nodes if node.get("type") == "reference")
    nodes = original_nodes if request.param == "explicit" else [node for node in original_nodes if node is not agent_node]
    nodes.insert(
        0,
        {
            "type": "reference",
            "node_id": str(uuid4()),
            "resource": {"kind": "capability", "id": "cap_visualize", "version": 1},
            "label": "Create visuals",
            "parameters": {},
        },
    )
    if fixture.mode == "admission_capability_skill":
        nodes.insert(
            1,
            {
                "type": "reference",
                "node_id": str(uuid4()),
                "resource": fixture.selected.model_dump(mode="python"),
                "label": "Financial method",
                "parameters": {},
            },
        )
    document = InputDocument.model_validate({"schema_version": 1, "nodes": nodes})
    prepared = await prepare_reference_team_start_admission(
        fixture.case.fixture.db_path,
        input_document=document,
        scope=fixture.scope,
        sources=fixture.sources,
        automatic_receiver=AgentResourceRef.model_validate(agent_node["resource"]) if request.param == "automatic" else None,
    )
    assert prepared.capability is not None
    assert prepared.capability.implementation_ref == "capi_image_generation_native@1"
    fixture.case.args.update(reference_start=prepared, user_text=document.display_text())
    await fixture.case.store.finalize_started_chat_submission(**fixture.case.args)
    scope = fixture.scope.model_copy(update={"session_id": "s_reference"})
    cipher = ReferenceCipher(Fernet.generate_key().decode())
    claim = await claim_reference_skill_primary_for_worker(
        sources=fixture.sources,
        cipher=cipher,
        scope=scope,
        turn_id="t_reference",
        owner_id="capability-worker",
        expected_generation=1,
    )
    runtime_scope = ReferenceScope(**scope.model_dump(), policy_epoch=prepared.policy_epoch)
    runtime = ReferencePrimaryRuntime(
        scope=runtime_scope,
        turn_id="t_reference",
        owner_id="capability-worker",
        bindings=fixture.case.bindings,
        cipher=cipher,
        skill_sources=fixture.sources,
    )
    context = ToolExecutionContext(
        **runtime_scope.identity(),
        turn_id="t_reference",
        member_uid=runtime_scope.user_id,
        datarc=None,
        session_store=fixture.case.store,
    )
    return SimpleNamespace(
        fixture=fixture,
        path=fixture.case.fixture.db_path,
        prepared=prepared,
        claim=claim,
        runtime=runtime,
        context=context,
        origin=request.param,
    )


def _image_tool_response() -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "capability-image-call",
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "image-call",
                                "type": "function",
                                "function": {
                                    "name": "image_generate",
                                    "arguments": json.dumps({"prompt": "Create a launch image"}),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


@pytest.mark.parametrize("verified_output", [True, False])
async def test_capability_runtime_settles_from_actual_image_artifact(capability_case, verified_output, monkeypatch):
    case = capability_case
    assert case.claim.outcome == "claimed"
    selected = _rows(case, "turn_capability_execution")
    assert len(selected) == 1
    assert selected[0]["status"] == "running"
    assert selected[0]["implementation_id"] == "capi_image_generation_native"
    assert selected[0]["output_contract"] == "visual.artifact.v1"
    # Closing the admission gate stops new claims but must not strand an exact
    # execution that already crossed the irreversible worker boundary.
    monkeypatch.setenv("FEATURE_CHAT_REFERENCE_CAPABILITY", "0")
    composition = json.loads(_rows(case, "chat_reference_resolutions")[0]["composition_json"])
    if case.fixture.mode == "admission_capability_skill":
        assert composition["members"]
        assert composition["capability"]["resource"]["id"] == "cap_visualize"
    else:
        assert composition["members"] == []
    assert composition.get("receiver_source") == ("lead" if case.origin == "automatic" else None)
    agent_admission = next(
        (item for item in case.claim.primary.plan.admissions if item.resource.kind == "agent"),
        None,
    )
    if case.origin == "automatic":
        assert composition.get("node_id") is None
    else:
        assert agent_admission is not None
        assert composition.get("node_id") == agent_admission.node_id

    async def image_generate(_args, _ctx):
        return {
            "is_deliverable": True,
            "images": [{"artifact_id": "art_capability_image", "is_deliverable": True}],
        }

    class Artifacts:
        async def get(self, artifact_id, **_kwargs):
            if not verified_output or artifact_id != "art_capability_image":
                return None
            return SimpleNamespace(
                kind="deliverable",
                producer_kind="image_generate",
                created_by_turn="t_reference",
                status="ready",
            )

    case.context.artifact_store = Artifacts()

    registry = build_default_registry()
    registry.register(
        ToolSpec(
            schema=ToolSchema(
                name="image_generate",
                description="Create an image",
                input_schema={"type": "object", "properties": {"prompt": {"type": "string"}}},
            ),
            handler=image_generate,
        )
    )
    adapter = adapter_for("openai")
    runner = SubagentRunner(
        router=router_for(adapter),
        tool_registry=registry,
        team_subagent_store=case.fixture.case.fixture.users,
    )
    responses = 0

    def respond(_request):
        nonlocal responses
        responses += 1
        return _image_tool_response() if responses == 1 else success("openai", "stream")

    try:
        with respx.mock() as mock:
            route = mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
            result = await case.runtime.run(
                runner=runner,
                parent_ctx=case.context,
                parent_bus=TurnEventBus(),
            )
            assert route.call_count == 2
    finally:
        await adapter.close()

    capability = _rows(case, "turn_capability_execution")[0]
    parent = _rows(case, "turn_agent_execution")[0]
    if verified_output:
        assert result.ok
        assert result.payload == {"deliverables": [{"kind": "image", "artifact_id": "art_capability_image"}]}
        assert capability["status"] == parent["status"] == "completed"
        assert capability["failure_code"] is None
    else:
        assert not result.ok and result.error == "capability_output_contract_failed"
        assert capability["status"] == parent["status"] == "failed"
        assert capability["failure_code"] == parent["failure_code"] == "capability_output_contract_failed"
    assert [row["kind"] for row in _rows(case, "chat_reference_evidence")] == ["delegate_started", "output_received"]
    projected = await read_reference_projection(
        case.path,
        scope=case.runtime.scope,
        turn_id="t_reference",
        visible_resource_identities=frozenset({reference_resource_identity(case.prepared.capability.resource)}),
        visibility_complete=True,
    )
    capability_fact = next(item for item in projected["references"] if item["resource_kind"] == "capability")
    assert capability_fact["capability_execution"] == {
        "execution_id": capability["id"],
        "attempt_generation": 1,
        "implementation": {"id": "capi_image_generation_native", "version": 1},
        "output_contract": "visual.artifact.v1",
        "status": "completed" if verified_output else "failed",
        "failure_code": None if verified_output else "capability_output_contract_failed",
    }
    assert capability_fact["outcome"] == ("fulfilled" if verified_output else "failed")
