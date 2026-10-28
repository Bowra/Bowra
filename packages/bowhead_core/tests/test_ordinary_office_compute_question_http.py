"""Native Office read and settled system Compute resume through one original card."""

import json
from dataclasses import replace
from types import SimpleNamespace

import anyio
import pytest
import respx
from bowhead_core._db import open_shared_ctx
from bowhead_core.agents.clarification_tool import register_clarification_tool
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.documents.package_io import OFFICE_MIMES
from bowhead_core.documents.tool import register_document_tool
from bowhead_core.ordinary_lead_continuation import read_ordinary_card_in_tx, reopen_ordinary_checkpoint
from bowhead_core.skill_bridge import register_skill_tools
from bowhead_core.subagent import PROFILES, SubagentRunner
from bowhead_gateway.document_service import DocumentService
from bowhead_gateway.routes import clarifications, files
from test_ordinary_lead_question_http import call, response
from test_reference_http_start import enable_lead_writer, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case
from test_reference_office_continuation import office_bytes
from test_system_reference_baseline import configure

from bowhead_core import attachment_read, events
from bowhead_router import AgentBinding
from bowhead_skill import SkillCatalog, SkillRuntime

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission_office_sandbox_unused_invoke"], indirect=True),
]


@pytest.mark.parametrize(
    "answer_mode,completion_order,collaboration",
    [
        (mode, None, collaboration)
        for mode in ("skip", "candidate", "custom", "protocol_repair", "document_tool_available")
        for collaboration in (False, True)
    ]
    + [
        (mode, order, collaboration)
        for mode in ("skip", "candidate", "custom")
        for order, collaboration in (("office_first", False), ("office_first", True), ("compute_first", True))
    ],
)
async def test_office_and_system_calculation_resume_without_repeating_effects(http_start, monkeypatch, answer_mode, collaboration, completion_order):
    case, scope = http_start, http_start.fixture.scope
    protocol_repair = answer_mode == "protocol_repair"
    document_tool_available = answer_mode == "document_tool_available"
    if protocol_repair or document_tool_available:
        answer_mode = "custom"
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setenv("FEATURE_CHAT_MULTI_HELPER_QUESTION", "1")
    monkeypatch.setenv("BOWHEAD_PI_K_ENABLED", "0")
    monkeypatch.setenv("BOWHEAD_SANDBOX_BACKEND", "executor")
    monkeypatch.setattr("bowhead_core.assigned_skill_execution.builtin_catalog_dir", lambda: case.fixture.root)
    monkeypatch.setattr("bowhead_core.ordinary_lead_material.builtin_catalog_dir", lambda: case.fixture.root)
    lead_tools = ("subagent_spawn", "clarification_ask") + (("template_document_submit",) if document_tool_available else ())
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=lead_tools, max_iterations=8))
    _, compute = await configure(case.fixture, monkeypatch)
    monkeypatch.setitem(PROFILES, "compute", replace(compute, tool_whitelist=(*compute.tool_whitelist, "subagent_return")))
    orch = case.app.state.orchestrator
    if not collaboration:
        # Exercise the retained v8 writer as well as current Web admission.
        async def no_collaboration(*args, **kwargs):
            return None

        monkeypatch.setattr("bowhead_core.agent_collaboration_runtime.prepare_lead_collaboration", no_collaboration)
    orch.router.bind(AgentBinding(agent_type="echo", primary=("primary", "large")))
    register_clarification_tool(orch.tool_registry)
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    case.app.include_router(files.router, prefix="/api/v1/files")
    monkeypatch.setattr(events, "_default_registry", orch.event_registry)
    artifacts = ArtifactStore(str(case.path), LocalBackend(case.path.parent / "mixed-ordinary"))
    await artifacts.initialize()
    canvas_store = CanvasStore(str(case.path))
    await canvas_store.initialize()
    orch.artifact_store = case.app.state.artifact_store = artifacts
    if document_tool_available:
        service = DocumentService(case.app.state)
        case.app.state.document_service = orch.document_service = service
        register_document_tool(orch.tool_registry, service=lambda: service)
    orch.canvas_store = canvas_store
    orch.skill_instance_store = case.fixture.sources.instances
    orch.skill_catalog = SkillCatalog(roots=[case.fixture.root])
    runtime = SkillRuntime()
    parses, executions, lead_calls, office_calls, compute_calls, outputs = [], [], [], [], [], []
    extract = attachment_read.extract_attachment_bounded

    first_completed = anyio.Event()
    completion_ids = []
    execute_tool = SubagentRunner._execute_tool

    async def execute(self, tc, *args, **kwargs):
        result = await execute_tool(self, tc, *args, **kwargs)
        if completion_order and tc.id in {"helper-1", "helper-2"}:
            completion_ids.append(tc.id)
            first_completed.set()
        return result

    monkeypatch.setattr(SubagentRunner, "_execute_tool", execute)

    async def parse(*args):
        if completion_order == "compute_first":
            with anyio.fail_after(10):
                await first_completed.wait()
        parses.append(args[0].id)
        return await extract(*args)

    monkeypatch.setattr(attachment_read, "extract_attachment_bounded", parse)

    async def run(spec, *, entry_script, input_data, extra_args):
        if completion_order == "office_first":
            with anyio.fail_after(10):
                await first_completed.wait()
        assert spec.name == "python_sandbox" and entry_script == "runner.py"
        executions.append(input_data["code"])
        output = await artifacts.put_bytes(
            content=b"42\n",
            filename="sandbox-stdout.txt",
            mime="text/plain",
            source="python_sandbox",
            created_by=scope.user_id,
            workspace_id=scope.workspace_id,
            created_by_turn=input_data["_turn_id"],
            session_id=input_data["_session_id"],
        )
        outputs.append(output)
        return SimpleNamespace(
            ok=True,
            exit_code=0,
            stdout="42",
            stderr="",
            outputs=[],
            workdir=None,
            duration_ms=1,
            error=None,
            result_json={
                "ok": True,
                "input_artifacts": [],
                "execution": {
                    "contract": "sandbox-execution.v1",
                    "id": "bowhead-sandbox-" + "1" * 24,
                    "image_id": "sha256:" + "a" * 64,
                    "runtime": "runsc",
                    "network": "none",
                    "settled": True,
                },
                "outputs": [{"artifact_id": output.id, "name": output.filename, "mime": output.mime, "size": output.size, "data_role": "stdout"}],
            },
        )

    monkeypatch.setattr(runtime, "run", run)
    register_skill_tools(orch.tool_registry, orch.skill_catalog, runtime)
    case.client.headers.pop("Content-Type", None)
    uploaded = await case.client.post(
        "/api/v1/files/upload",
        headers={"X-Space-Id": scope.workspace_id},
        files={"file": ("profit.pptx", office_bytes("pptx"), OFFICE_MIMES[".pptx"])},
    )
    assert uploaded.status_code == 200, uploaded.text
    attachment = uploaded.json()
    case.client.headers["Content-Type"] = "application/json"
    question = {"question": "Which audience?", "type": "single_select", "options": ["Operations", "Board"]}
    original = "Read the Office file, calculate its profit, then ask for an audience."
    if completion_order:
        original = "Independently read the Office file and calculate 40 + 2, then ask for an audience."
    if document_tool_available:
        original += " Do not edit the PPTX or generate files."

    def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls.append(body)
            index = len(lead_calls)
            if protocol_repair and index == 3:
                from test_stray_clarification_ask import SELF_CLOSING_CALL

                return response(body, {"role": "assistant", "content": SELF_CLOSING_CALL}, "stop")
            if protocol_repair and index > 3:
                index -= 1
            if index == 2 and not completion_order:
                previous = [message for message in body["messages"] if message.get("tool_call_id") == "helper-1"]
                assert len(previous) == 1 and "Original Office profit is 42." in previous[0]["content"]
            if completion_order:
                if index > 1:
                    index += 1
                else:
                    message = call(
                        "helper-1",
                        "subagent_spawn",
                        {
                            "profile": case.fixture.case.args["reference_start"].profile_name,
                            "task": "Read the attached profit.",
                            "delegation_role": "helper",
                        },
                    )
                    message["tool_calls"].extend(
                        call(
                            "helper-2",
                            "subagent_spawn",
                            {
                                "profile": "compute",
                                "task": "Independently calculate 40 + 2.",
                                "delegation_role": "helper",
                            },
                        )["tool_calls"]
                    )
                    message["tool_calls"][1]["index"] = 1
                    return response(body, message, "tool_calls")
            if index <= 2:
                message = call(
                    f"helper-{index}",
                    "subagent_spawn",
                    {
                        "profile": case.fixture.case.args["reference_start"].profile_name if index == 1 else "compute",
                        "task": "Read the attached profit." if index == 1 else "Calculate 40 + 2 from the retained read result.",
                        "delegation_role": "helper",
                    },
                )
            elif index == 3:
                message = call("question", "clarification_ask", {"questions": [question]})
            else:
                return response(body, {"role": "assistant", "content": "The retained synthetic profit is 42."}, "stop")
        elif "attachment_read" in names:
            office_calls.append(body)
            message = (
                call("read-office", "attachment_read", {"artifact_id": attachment["file_id"]})
                if len(office_calls) == 1
                else call(
                    "return-office", "subagent_return", {"payload": {"delivery_surface": "chat", "exec_summary": "Original Office profit is 42."}}
                )
            )
        elif "skill_invoke" in names:
            compute_calls.append(body)
            if len(compute_calls) == 1:
                message = call("calculate", "skill_invoke", {"name": "python_sandbox", "script": "runner.py", "input": {"code": "print(40 + 2)"}})
            else:
                message = call(
                    "return-compute",
                    "subagent_return",
                    {
                        "payload": {
                            "delivery_surface": "chat",
                            "exec_summary": "The actual calculation is 42.",
                            "kpis": [{"label": "Profit", "value": "42", "sentiment": "neutral"}],
                            "insights": [
                                {
                                    "title": "Profit calculation",
                                    "thesis": "The actual calculation is 42.",
                                    "evidence": [
                                        {"kind": "computation", "computation_artifact_id": outputs[0].id, "caption": "Actual sandbox output"}
                                    ],
                                    "narrative": "The calculation retains the original Office read and completed sandbox output.",
                                    "confidence": "high",
                                }
                            ],
                        }
                    },
                )
        else:
            return response(body, {"role": "assistant", "content": "Synthetic inspection"}, "stop")
        return response(body, message, "tool_calls")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": scope.workspace_id,
                "message": original,
                "attachments": [attachment["file_id"]],
                "attachment_refs": [
                    {
                        "fileId": attachment["file_id"],
                        "filename": attachment["filename"],
                        "mime": attachment["mime"],
                        "size": attachment["size"],
                        "referenceKind": "upload",
                    }
                ],
            },
        )
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
        cards = [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
        assert len(cards) == 1, [(row["status"], row["error"]) for row in rows(case, "turns") if row["id"] == turn_id]
        card_id = cards[0]["request_id"]
        async with open_shared_ctx(case.path) as db:
            await db.execute("BEGIN")
            checkpoint = reopen_ordinary_checkpoint(await read_ordinary_card_in_tx(db, card_id=card_id))
        assert len(checkpoint.helper_materials()) == 2
        assert checkpoint.contract == (
            "ordinary-lead-loop.v11" if completion_order else "ordinary-lead-loop.v9" if collaboration else "ordinary-lead-loop.v8"
        )
        if completion_order:
            assert completion_ids == (["helper-1", "helper-2"] if completion_order == "office_first" else ["helper-2", "helper-1"])
            assert [helper["dispatch_key"] for helper in checkpoint.helper_materials()] == ["call:helper-1", "call:helper-2"]
            groups = [message for message in checkpoint.messages() if len(message.tool_calls or []) == 2]
            assert len(groups) == 1
            assert [call.id for call in groups[0].tool_calls] == ["helper-1", "helper-2"]
        assert json.loads(checkpoint.material_json)["canvas"] is not None
        if protocol_repair:
            paused = [
                json.loads(row["payload_json"])
                for row in rows(case, "turn_events")
                if row["turn_id"] == turn_id and row["event_type"] in {"clarification_required", "done"}
            ]
            assert len(paused) == 2 and all(event["narrative_superseded"] for event in paused)
        answer = (
            {"skip": True}
            if answer_mode == "skip"
            else {
                "answers": [
                    {
                        "question": question["question"],
                        "answer": "Board" if answer_mode == "candidate" else "Finance",
                        "source": answer_mode,
                        **({"option_index": 1} if answer_mode == "candidate" else {}),
                    }
                ]
            }
        )
        resolved = await case.client.post(f"/api/v1/clarifications/{card_id}/resolve", json=answer)
        assert resolved.status_code == 200, resolved.text
        await settled(turn_id)
        turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
        assert turn["status"] == "completed" and turn["execution_generation"] == 2, turn
        assert parses == [attachment["file_id"]] and executions == ["print(40 + 2)"]
        assert len(lead_calls) == (3 if completion_order else 5 if protocol_repair else 4) and len(office_calls) == len(compute_calls) == 2
        if document_tool_available:
            assert not rows(case, "document_jobs")
