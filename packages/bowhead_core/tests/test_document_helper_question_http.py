"""Actual upload, completed helper, original answer and native document attempts.

Provider and executor transports are controlled. Upload authorization, native
handlers, question persistence, worker dispatch and document compilation are real.
"""

import hashlib
import json
import os
from dataclasses import replace
from types import SimpleNamespace

import httpx2
import pytest
import respx
from bowhead_core._db import open_shared_ctx
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.clarification_store import ClarificationStore
from bowhead_core.document_chat_continuation import DocumentTurnOwner
from bowhead_core.documents.executor import PROFILE
from bowhead_core.documents.planning_instruction import clarification_evidence_sources_in_tx, document_instruction_in_tx
from bowhead_core.documents.pptx_text import inventory_pptx_text
from bowhead_core.events import EventBusRegistry
from bowhead_core.orchestrator import Orchestrator
from bowhead_core.session_store import SessionStore
from bowhead_core.skill_bridge import register_skill_tools
from bowhead_core.subagent import PROFILES, SubagentRunner
from bowhead_core.turn_log_store import OrderedTurnLogSink
from bowhead_gateway.routes import documents, files
from test_document_pptx_text import _deck, _parts
from test_document_preflight_question_http import prepare_document_http
from test_document_runner import IMAGE
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import rows, settled
from test_reference_http_start import start_case as start_case

from bowhead_gateway import turn_service
from bowhead_skill import SkillCatalog, SkillRuntime

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.slow,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
]


def native_response(body: dict, message: dict, reason: str) -> httpx2.Response:
    usage = {"prompt_tokens": 17, "completion_tokens": 9}
    if not body.get("stream"):
        return httpx2.Response(200, json={"choices": [{"message": message, "finish_reason": reason}], "usage": usage})
    chunks = [{"choices": [{"delta": message, "finish_reason": None}]}, {"choices": [{"delta": {}, "finish_reason": reason}], "usage": usage}]
    return httpx2.Response(
        200,
        text="".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n",
        headers={"content-type": "text/event-stream"},
    )


def native_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}],
    }


async def prepare_retained_document_materials(case, monkeypatch):
    """Use native Skill/material writers with a controlled settled executor."""
    monkeypatch.setenv("BOWHEAD_SANDBOX_BACKEND", "executor")
    monkeypatch.setattr("bowhead_core.assigned_skill_execution.builtin_catalog_dir", lambda: case.fixture.root)
    monkeypatch.setattr("bowhead_core.ordinary_lead_material.builtin_catalog_dir", lambda: case.fixture.root)
    orchestrator = case.app.state.orchestrator
    scope = case.fixture.scope
    artifacts = orchestrator.artifact_store
    canvas_store = CanvasStore(str(case.path))
    await canvas_store.initialize()
    orchestrator.canvas_store = canvas_store
    orchestrator.skill_instance_store = case.fixture.sources.instances
    orchestrator.skill_catalog = SkillCatalog(roots=[case.fixture.root])
    runtime = SkillRuntime()
    control = SimpleNamespace(executions=[], outputs=[], sections=[], legacy_scope=None)

    async def run(spec, *, entry_script, input_data, extra_args):
        assert spec.name == "python_sandbox" and entry_script == "runner.py"
        control.executions.append(input_data["code"])
        inputs = []
        for key, item in input_data["inputs"].items():
            artifact = await artifacts.get(item["artifact_id"])
            data = await artifacts.backend.read_bounded(artifact.path, limit=artifact.size)
            inputs.append({"key": key, "artifact_id": artifact.id, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        assert len(inputs) == 1
        value = str(41 + len(control.executions))
        from test_document_legacy_inspection_token import legacy_inspection_token

        token = input_data["_artifact_fetch_token"]
        query = {"source": "python_sandbox", "session_id": input_data["_session_id"], "created_by_turn": input_data["_turn_id"]}
        headers = {"Authorization": "Bearer " + token, "X-Space-Id": scope.workspace_id}
        if control.legacy_scope is not None:
            restriction = control.legacy_scope
            token = legacy_inspection_token(case.app.state.artifact_fetch_secret, scope.user_id, scope=restriction)
            headers["Authorization"] = "Bearer " + token
            before = len(rows(case, "artifacts"))
            for filename, content, mime in [
                ("unconfirmed.pptx", _deck(), "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
                ("unconfirmed.pdf", b"%PDF-1.4\n", "application/pdf"),
            ]:
                blocked = await case.client.post("/api/v1/files/upload", params=query, files={"file": (filename, content, mime)}, headers=headers)
                assert blocked.status_code == 403, blocked.text
                assert blocked.json().get("error", blocked.json().get("detail", {}).get("error", {})).get("code") == "document_workflow_required", (
                    blocked.text
                )
            assert len(rows(case, "artifacts")) == before, "Inspection must not expose downloadable document files before native review"
            for params, overrides in [
                ({**query, "session_id": "s_other"}, {}),
                ({**query, "created_by_turn": "t_other"}, {}),
                ({**query, "kind": "deliverable"}, {}),
                (query, {"X-Bowhead-Upload-Source": "upload"}),
                (query, {"X-Workspace-Id": "ws_other"}),
                (
                    query,
                    {
                        "Authorization": "Bearer "
                        + legacy_inspection_token(
                            case.app.state.artifact_fetch_secret,
                            scope.user_id,
                            scope={**restriction, "execution_generation": restriction["execution_generation"] + 1},
                        )
                    },
                ),
            ]:
                blocked = await case.client.post(
                    "/api/v1/files/upload",
                    params=params,
                    files={"file": ("sandbox-stdout.txt", b"inspection", "text/plain")},
                    headers={**headers, **overrides},
                )
                assert (
                    blocked.status_code == 403
                    and blocked.json().get("error", blocked.json().get("detail", {}).get("error", {})).get("code") == "document_workflow_required"
                ), blocked.text
            assert len(rows(case, "artifacts")) == before
        uploaded = await case.client.post(
            "/api/v1/files/upload", params=query, files={"file": ("sandbox-stdout.txt", (value + "\n").encode(), "text/plain")}, headers=headers
        )
        assert uploaded.status_code == 200, uploaded.text
        output = await artifacts.get(uploaded.json()["file_id"])
        control.outputs.append(output)
        canvas = await canvas_store.ensure_canvas(session_id=input_data["_session_id"], workspace_id=scope.workspace_id, user_id=scope.user_id)
        control.sections.append(
            await canvas_store.append_section(
                canvas_id=canvas.id,
                kind="markdown",
                payload={"text": f"Synthetic template inspection from {output.id}"},
                by_turn_id=input_data["_turn_id"],
            )
        )
        return SimpleNamespace(
            ok=True,
            exit_code=0,
            stdout=value,
            stderr="",
            outputs=[],
            workdir=None,
            duration_ms=1,
            error=None,
            result_json={
                "ok": True,
                "input_artifacts": inputs,
                "execution": {
                    "contract": "sandbox-execution.v1",
                    "id": "bowhead-sandbox-" + f"{len(control.executions):024x}",
                    "image_id": "sha256:" + "a" * 64,
                    "runtime": "runsc",
                    "network": "none",
                    "settled": True,
                },
                "outputs": [{"artifact_id": output.id, "name": output.filename, "mime": output.mime, "size": output.size, "data_role": "stdout"}],
            },
        )

    monkeypatch.setattr(runtime, "run", run)
    register_skill_tools(orchestrator.tool_registry, orchestrator.skill_catalog, runtime)
    return control


def recreate_document_runtime(case) -> None:
    """Discard run-local orchestration state while retaining the real database."""
    old = case.app.state.orchestrator
    store = SessionStore(case.path)
    clarification_store = ClarificationStore(case.path)
    orchestrator = Orchestrator(
        router=old.router,
        datarc=None,
        session_store=store,
        tool_registry=old.tool_registry,
        event_registry=EventBusRegistry(default_log_sink=OrderedTurnLogSink(case.app.state.turn_log_store)),
        user_store=old.user_store,
        clarification_store=clarification_store,
        artifact_store=old.artifact_store,
    )
    orchestrator.dispatch_owner_id = "recreated-document-helper-http-worker"
    orchestrator.subagent_runner = SubagentRunner(router=old.router, tool_registry=old.tool_registry, team_subagent_store=old.user_store)
    orchestrator.reference_skill_sources_factory = old.reference_skill_sources_factory
    for name in ("skill_instance_store", "skill_catalog"):
        if getattr(old, name, None) is not None:
            setattr(orchestrator, name, getattr(old, name))
    if getattr(old, "canvas_store", None) is not None:
        orchestrator.canvas_store = CanvasStore(str(case.path))
    orchestrator.access_control_store = case.app.state.access_control_store
    orchestrator.document_service = case.app.state.document_service
    case.app.state.orchestrator = orchestrator
    case.app.state.session_store = store
    case.app.state.clarification_store = clarification_store


@pytest.mark.parametrize(
    "boundary,receiver_case",
    [
        ("ordinary", "admission"),
        ("omitted_role", "admission"),
        ("recreated", "admission"),
        ("historical_routing", "admission"),
        ("preworker_retry", "admission"),
        ("revoked_upload", "admission"),
        ("admission_refusal", "admission"),
        ("materials", "admission_sandbox_unused_invoke"),
        ("legacy_inspection", "admission_sandbox_unused_invoke"),
        ("return_optional", "admission"),
        ("attachment_read", "admission_office_unused_sop"),
    ],
    indirect=["receiver_case"],
)
@pytest.mark.parametrize(
    "ending",
    [
        "incomplete",
        pytest.param(
            "review",
            marks=[
                pytest.mark.skipif(os.environ.get("BOWHEAD_DOCUMENT_NATIVE_SMOKE") != "1", reason="Requires native LibreOffice/Poppler"),
                pytest.mark.timeout(240),
            ],
        ),
    ],
)
async def test_helper_answers_reach_two_attempts_of_one_native_document(http_start, monkeypatch, tmp_path, ending, boundary) -> None:
    case = http_start
    service, worker, _ = await prepare_document_http(case, monkeypatch, upload=False)
    case.app.state.orchestrator.artifact_fetch_secret = "document-inspection-test-secret-" * 2
    case.app.state.artifact_fetch_secret = case.app.state.orchestrator.artifact_fetch_secret
    monkeypatch.setenv("FEATURE_CHAT_MULTI_HELPER_QUESTION", "1")
    monkeypatch.setitem(
        PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("subagent_spawn", "clarification_ask", "template_document_submit"))
    )
    scope = case.fixture.scope
    materials = await prepare_retained_document_materials(case, monkeypatch) if boundary in {"materials", "legacy_inspection"} else None
    attachment_parses = []
    if boundary == "attachment_read":
        from bowhead_core import attachment_read

        extract = attachment_read.extract_attachment_bounded

        async def parse(*args):
            attachment_parses.append(args[0].id)
            return await extract(*args)

        monkeypatch.setattr(attachment_read, "extract_attachment_bounded", parse)
    case.app.include_router(documents.router, prefix="/api/v1")
    case.app.include_router(files.router, prefix="/api/v1/files")
    case.client.headers.pop("Content-Type")
    template = _deck()
    uploaded = await case.client.post(
        "/api/v1/files/upload",
        files={"file": ("template.pptx", template, "application/vnd.openxmlformats-officedocument.presentationml.presentation")},
        headers={"X-Space-Id": scope.workspace_id},
    )
    assert uploaded.status_code == 200, uploaded.text
    attachment = uploaded.json()
    original = "Use the attached PPTX as the template. Inspect it, ask which audience to use, then prepare a draft and PDF."
    if boundary == "historical_routing":
        from bowhead_core.chat_turn_source import chat_turn_user_source_projection
        from bowhead_core.documents.routing import DocumentRoutingSnapshot
        from bowhead_core.ordinary_lead_continuation import content_hash

        from bowhead_core import ordinary_lead_runtime

        capture = ordinary_lead_runtime.capture_ordinary_lead_loop

        async def historical_capture(**kwargs):
            checkpoint = await capture(**kwargs)
            ctx = kwargs["ctx"]
            source = await ctx.session_store.get_chat_turn_user_source(turn_id=ctx.turn_id, user_id=ctx.user_id, workspace_id=ctx.workspace_id)
            # Reproduce the old encrypted envelope through the original card writer.
            # New production capture does not create this historical field.
            return checkpoint.model_copy(
                update={
                    "document_routing": DocumentRoutingSnapshot(
                        source_hash=content_hash(chat_turn_user_source_projection(source)),
                        mode="template",
                        evidence="Use the attached PPTX as the template.",
                        inspection_quote="Inspect it",
                        question_quote="ask which audience to use",
                    )
                }
            )

        monkeypatch.setattr(ordinary_lead_runtime, "capture_ordinary_lead_loop", historical_capture)
    ordinary_question, ordinary_answer = "Which audience?", "Board members"
    domain_question, domain_answer = "Which period?", "Quarter two"
    lead_calls, helper_calls, document_calls = [], [], []
    from bowhead_core.documents.turn_policy import NATIVE_DOCUMENT_GUIDANCE

    expected_lead_calls = 3
    lead_results = []
    original_run = SubagentRunner.run

    async def observe_run(self, **kwargs):
        if boundary == "legacy_inspection" and kwargs["profile_name"] != "lead":
            materials.legacy_scope = {
                key: getattr(kwargs["parent_ctx"], key)
                for key in ("team_id", "user_id", "workspace_id", "session_id", "turn_id", "execution_generation", "execution_owner_id")
            }
        result = await original_run(self, **kwargs)
        if kwargs["profile_name"] == "lead":
            lead_results.append(
                (result.ok, result.error, [(inv.tool_name, inv.error, (inv.result or {}).get("error")) for inv in result.tool_invocations])
            )
        return result

    monkeypatch.setattr(SubagentRunner, "run", observe_run)

    async def respond(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if "template_document_submit" in names:
            lead_calls.append(body)
            index = len(lead_calls)
            if boundary == "recreated":
                system_text = "\n".join(item.get("content", "") for item in body["messages"] if item["role"] == "system")
                assert system_text.count(NATIVE_DOCUMENT_GUIDANCE) == 1
                assert "Fresh document guidance after restart" not in system_text
            if index == 1:
                inspection_args = {
                    "profile": case.fixture.case.args["reference_start"].profile_name,
                    "task": "Inspect template structure.",
                }
                if boundary != "omitted_role":
                    inspection_args["delegation_role"] = "helper"
                message = native_call(
                    "inspect-template",
                    "subagent_spawn",
                    inspection_args,
                )
            elif index == 2:
                message = native_call(
                    "ask-audience", "clarification_ask", {"questions": [{"question": ordinary_question, "type": "free_text", "options": []}]}
                )
            else:
                assert index == 3, "Native planning must not restart Lead or replay its helper"
                if boundary == "admission_refusal":
                    from bowhead_core import ordinary_lead_continuation

                    async def refuse_admission(*_args, **_kwargs):
                        raise ordinary_lead_continuation.OrdinaryContinuationUnavailable()

                    # Exercise atomic consumer refusal after the real shared worker has started.
                    monkeypatch.setattr(ordinary_lead_continuation, "validate_ordinary_document_admission_in_tx", refuse_admission)
                message = native_call(
                    "generate-template",
                    "template_document_submit",
                    {
                        "action": "generate",
                        "evidence": {"text": original},
                        "inputs": [
                            {"artifact_id": attachment["file_id"], "role": "template", "evidence": {"text": "Use the attached PPTX as the template."}}
                        ],
                        "requested_outputs": ["source", "pdf"],
                    },
                )
            return native_response(body, message, "tool_calls")
        if "subagent_return" in names:
            helper_calls.append(body)
            index = len(helper_calls)
            if boundary == "attachment_read":
                assert index <= 2, "The original native attachment read must not replay after clarification"
                if index == 1:
                    assert "attachment_read" in names
                    message = native_call("read-original-template", "attachment_read", {"artifact_id": attachment["file_id"]})
                else:
                    read = next(
                        json.loads(message["content"])
                        for message in body["messages"]
                        if message.get("role") == "tool" and message.get("tool_call_id") == "read-original-template"
                    )
                    assert read["ok"] is True and read["truncated"] is False
                    assert '["North", "42"]' in read["text"] and "Stable grouped caption" in read["text"]
                    message = native_call(
                        "return-native-inspection",
                        "subagent_return",
                        {
                            "payload": {
                                "delivery_surface": "chat",
                                "exec_summary": "The template contains the quarter heading, region table and grouped caption.",
                            }
                        },
                    )
                return native_response(body, message, "tool_calls")
            if boundary == "return_optional":
                assert index == 1, "Inspection must not restart after the original answer"
                payload = {
                    "delivery_surface": "chat",
                    "exec_summary": "The template has a quarter heading and a region table.",
                    "kpis": [{"label": "Slides", "value": "2"}],
                    "caveats": ["Only template structure was inspected."],
                }
                return native_response(body, native_call("return-inspection", "subagent_return", {"payload": payload}), "tool_calls")
            if materials is not None:
                if index == 1:
                    message = native_call("read-inspection-method", "skill_read", {"name": "method"})
                elif index in {2, 3}:
                    message = native_call(
                        f"inspect-template-{index}",
                        "skill_invoke",
                        {
                            "name": "python_sandbox",
                            "script": "runner.py",
                            "input": {
                                "code": f"print({40 + index})",
                                "inputs": {"template": {"artifact_id": attachment["file_id"] if index == 2 else materials.outputs[0].id}},
                            },
                        },
                    )
                else:
                    assert index == 4, "Captured Skill/sandbox inspection must not execute again"
                    return native_response(body, {"role": "assistant", "content": "Template inspection completed."}, "stop")
                return native_response(body, message, "tool_calls")
            assert index == 1, "The completed helper must never execute again after the original answer"
            return native_response(
                body,
                native_call(
                    "return-template-inspection",
                    "subagent_return",
                    {"payload": {"delivery_surface": "chat", "exec_summary": "The template contains a quarter heading and a region table."}},
                ),
                "tool_calls",
            )
        try:
            evidence = json.loads(body["messages"][-1]["content"])
        except (ValueError, TypeError):
            evidence = None
        if not isinstance(evidence, dict) or "instruction" not in evidence:
            return native_response(body, {"role": "assistant", "content": "Template draft"}, "stop")
        document_calls.append(body)
        if len(document_calls) == 1 or ending == "incomplete":
            result = {"action": "ask", "reason": "comparison", "slots": ["s0"], "question": domain_question}
        else:
            slot = next(item["id"] for item in evidence["slots"] if item["text"] == "Quarter one")
            fact = next(item["id"] for item in evidence["facts"] if item["path"] == "/clarification/answer")
            result = {
                "action": "propose",
                "updates": [{"slot": slot, "parts": [{"ref": fact}]}],
                "keep": [item["id"] for item in evidence["slots"] if item["id"] != slot],
            }
        return native_response(body, {"role": "assistant", "content": json.dumps(result)}, "stop")

    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://document-executor/document-runs/capabilities").respond(
            200, json={"profile": PROFILE, "image_id": IMAGE, "planner_profiles": ["pptx-grounded-plan-v5"]}
        )
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
            headers={"Content-Type": "application/json"},
        )
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)
        expected_helper_calls = 4 if materials is not None else 2 if boundary == "attachment_read" else 1
        assert len(helper_calls) == expected_helper_calls and len(lead_calls) == 2
        if materials is not None:
            assert materials.executions == ["print(42)", "print(43)"]
            assert len(materials.outputs) == len(materials.sections) == 2
            original_sections = rows(case, "canvas_sections")
            original_revisions = rows(case, "canvas_revisions")
        assert rows(case, "document_jobs") == [] and not document_calls
        cards = [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
        assert len(cards) == 1
        original_card = cards[0]
        snapshot = json.loads(original_card["resume_context_json"])
        assert snapshot["__schema_version"] == 4, {
            "actual_version": snapshot["__schema_version"],
            "helper_calls": len(helper_calls),
            "lead_calls": len(lead_calls),
            "document_calls": len(document_calls),
            "document_jobs": len(rows(case, "document_jobs")),
        }
        if materials is not None or boundary == "attachment_read":
            from bowhead_core.ordinary_lead_continuation import read_ordinary_card_in_tx, reopen_ordinary_checkpoint

            async with open_shared_ctx(case.path) as db:
                await db.execute("BEGIN")
                checkpoint = reopen_ordinary_checkpoint(await read_ordinary_card_in_tx(db, card_id=original_card["request_id"]))
            if boundary == "attachment_read":
                assert checkpoint.contract == ("ordinary-lead-loop.v9" if checkpoint.collaboration is not None else "ordinary-lead-loop.v8")
                assert len(checkpoint.helper_materials()) == 1
                proof = checkpoint.helper_materials()[0]["tools"]
                assert len(proof) == 1 and proof[0]["kind"] == "attachment_read"
                assert proof[0]["attachment"]["artifact_id"] == attachment["file_id"]
                assert attachment_parses == [attachment["file_id"]]
            else:
                assert checkpoint.contract == ("ordinary-lead-loop.v9" if checkpoint.collaboration is not None else "ordinary-lead-loop.v3")
                assert len(checkpoint.helper_materials()) == 1
                material = checkpoint.helper_materials()[0]
                assert [(item["call_id"], item["kind"]) for item in material["tools"]] == [
                    ("read-inspection-method", "skill_read"),
                    ("inspect-template-2", "sandbox"),
                    ("inspect-template-3", "sandbox"),
                ]
                assert json.loads(checkpoint.material_json)["canvas"] is not None
        answer_payload = {"answers": [{"question": ordinary_question, "source": "custom", "answer": ordinary_answer}]}
        if boundary == "recreated":
            monkeypatch.setattr("bowhead_core.documents.turn_policy.NATIVE_DOCUMENT_GUIDANCE", "Fresh document guidance after restart")
        if boundary in {"recreated", "historical_routing", "materials", "legacy_inspection", "return_optional", "attachment_read"}:
            recreate_document_runtime(case)
        elif boundary == "preworker_retry":

            async def fail_before_worker(*_args, **_kwargs) -> None:
                raise ValueError("injected_preworker_failure")

            with monkeypatch.context() as failure:
                failure.setattr(turn_service, "spawn_turn", fail_before_worker)
                refused = await case.client.post(f"/api/v1/clarifications/{original_card['request_id']}/resolve", json=answer_payload)
            assert refused.status_code == 409, refused.text
            assert len(helper_calls) == 1 and len(lead_calls) == 2 and not document_calls
            assert rows(case, "document_jobs") == []
            saved = await case.app.state.clarification_store.get(original_card["request_id"])
            assert saved.status == "resolved" and saved.answers[0]["answer"] == ordinary_answer
        elif boundary == "revoked_upload":
            assert await case.app.state.artifact_store.soft_delete(attachment["file_id"], user_id=scope.user_id)
        resolved = await case.client.post(
            f"/api/v1/clarifications/{original_card['request_id']}/resolve",
            json=answer_payload,
        )
        if boundary == "revoked_upload":
            assert resolved.status_code == 409, resolved.text
            assert len(helper_calls) == 1 and len(lead_calls) == 2 and not document_calls
            assert rows(case, "document_jobs") == [] and not worker.runs
            return
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["resume_turn_id"] == turn_id
        if boundary == "historical_routing":
            saved_card = await case.app.state.clarification_store.get(original_card["request_id"])
            assert saved_card.resume_context == snapshot, "Resume must retain the original encrypted historical envelope"
        await settled(turn_id)
        if boundary == "admission_refusal":
            assert len(helper_calls) == 1 and len(lead_calls) == 3 and not document_calls
            assert rows(case, "document_jobs") == rows(case, "document_turn_dispatches") == rows(case, "document_dispatches") == []
            assert rows(case, "document_planning_attempts") == [] and not worker.runs
            saved = await case.app.state.clarification_store.get(original_card["request_id"])
            assert saved.answers[0]["answer"] == ordinary_answer
            return
        jobs = rows(case, "document_jobs")
        assert len(jobs) == 1 and jobs[0]["origin_turn_id"] == turn_id, (len(lead_calls), len(helper_calls), lead_results)
        job_id = jobs[0]["id"]
        assert len(document_calls) == 1 and len(helper_calls) == expected_helper_calls and len(lead_calls) == expected_lead_calls
        planned = json.loads(document_calls[0]["messages"][-1]["content"])
        assert json.loads(planned["instruction"].rsplit("\n", 1)[1]) == {"decisions": [{"question": ordinary_question, "answer": ordinary_answer}]}
        cards = [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
        domain_card = next(row for row in cards if row["request_id"] != original_card["request_id"])
        assert json.loads(domain_card["resume_context_json"])["__schema_version"] == 3
        dispatch = rows(case, "document_turn_dispatches")[0]
        creation_worker = DocumentTurnOwner(
            team_id=scope.team_id,
            workspace_id=scope.workspace_id,
            user_id=scope.user_id,
            session_id=accepted.json()["session_id"],
            turn_id=turn_id,
            generation=dispatch["creation_generation"],
            owner_id=dispatch["owner_id"],
        )
        # The original worker is settled; durable reconstruction must not need its live lease.
        async with open_shared_ctx(case.path) as db:
            await db.execute("BEGIN")
            instruction = await document_instruction_in_tx(db, worker=creation_worker, job_id=job_id)
            clarification_sources = await clarification_evidence_sources_in_tx(db, job=jobs[0])
            assert clarification_sources == [
                {
                    "source_name": instruction.history.name,
                    "source_sha256": hashlib.sha256(instruction.history.content).hexdigest(),
                }
            ]
        assert json.loads(instruction.text.rsplit("\n", 1)[1]) == {"decisions": [{"question": ordinary_question, "answer": ordinary_answer}]}
        if boundary in {"recreated", "materials"}:
            recreate_document_runtime(case)
        resumed = await case.client.post(
            f"/api/v1/clarifications/{domain_card['request_id']}/resolve",
            json={"answers": [{"question": domain_question, "source": "custom", "answer": domain_answer}]},
        )
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["resume_turn_id"] == turn_id
        await settled(turn_id, timeout=120 if ending == "review" else 30)
        assert len(rows(case, "document_jobs")) == len(rows(case, "document_turn_dispatches")) == 1
        attempts = rows(case, "document_planning_attempts")
        assert {(item["job_id"], item["attempt_no"]) for item in attempts} == {(job_id, 1), (job_id, 2)}
        assert next(item for item in attempts if item["attempt_no"] == 2)["owner_id"] != creation_worker.owner_id
        assert len(helper_calls) == expected_helper_calls and len(lead_calls) == expected_lead_calls and len(document_calls) == 2
        if boundary == "attachment_read":
            assert attachment_parses == [attachment["file_id"]]
        if materials is not None:
            assert materials.executions == ["print(42)", "print(43)"]
            assert len(materials.outputs) == len(materials.sections) == 2
            assert rows(case, "canvas_revisions") == original_revisions
            assert {row["id"] for row in rows(case, "canvas_sections")} == {row["id"] for row in original_sections}
        assert worker.runs.count("inventory") == 1
        source = next(row for row in rows(case, "chat_turn_user_sources") if row["turn_id"] == turn_id)
        assert source["user_text"] == original
        retained = await case.app.state.clarification_store.get(original_card["request_id"])
        assert retained.answers[0]["answer"] == ordinary_answer
        turn = await case.fixture.case.store.get_turn(turn_id)
        if ending == "incomplete":
            assert turn.status == "failed" and turn.error_kind == "document_information_incomplete"
        else:
            from bowhead_core.documents.request import DocumentScope

            document_scope = DocumentScope(team_id=scope.team_id, workspace_id=scope.workspace_id, user_id=scope.user_id)
            draft = await service.delivery.get(scope=document_scope, job_id=job_id)
            assert turn.status == "completed" and draft["state"] == "review"
            assert draft["draft_digest"]
            assert draft["clarification_evidence_sources"] == clarification_sources
            assert (
                hashlib.sha256(json.dumps(draft["draft"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                == draft["draft_digest"]
            )
            exact_url = f"/api/v1/sessions/{accepted.json()['session_id']}/documents/{job_id}"
            base = f"/api/v1/documents/{job_id}"
            exact = await case.client.get(exact_url)
            assert exact.status_code == 200 and exact.json() == draft, exact.text
            assert exact.headers["cache-control"] == "no-store, private"
            for name in ("candidate.pptx", "candidate.pdf"):
                assert (await case.client.get(base + "/download/" + name)).status_code == 404
            preview = draft["draft"]["preview"]
            assert preview["page_count"] == len(preview["pages"]) == 2
            for page in preview["pages"]:
                image = await case.client.get(base + "/preview/" + page["name"])
                assert image.status_code == 200 and image.content.startswith(b"\x89PNG\r\n\x1a\n"), image.text
                assert hashlib.sha256(image.content).hexdigest() == page["sha256"]
                (tmp_path / page["name"]).write_bytes(image.content)
            original_runs = list(worker.runs)
            command = {"command_id": "confirm-reviewed-helper-document", "draft_digest": draft["draft_digest"]}
            published = await case.client.post(base + "/confirm", json=command)
            assert published.status_code == 200 and published.json()["state"] == "published", published.text
            replayed = await case.client.post(base + "/confirm", json=command)
            assert replayed.status_code == 200 and replayed.json()["draft_digest"] == draft["draft_digest"], replayed.text
            assert replayed.json()["draft"]["files"] == draft["draft"]["files"]
            assert replayed.json()["clarification_evidence_sources"] == clarification_sources
            assert len(rows(case, "document_confirmations")) == 1
            restored = await case.client.get(exact_url)
            assert restored.status_code == 200 and restored.json()["state"] == "published", restored.text
            assert restored.json()["origin_turn_id"] == turn_id
            assert restored.json()["draft_digest"] == draft["draft_digest"]
            downloaded_files = {}
            for name in ("candidate.pptx", "candidate.pdf"):
                file = draft["draft"]["files"][name]
                downloaded = await case.client.get(base + "/download/" + name)
                assert downloaded.status_code == 200, downloaded.text
                assert downloaded.headers["cache-control"] == "no-store, private"
                assert downloaded.headers["content-disposition"].startswith("attachment;")
                assert downloaded.headers["content-disposition"].endswith(".pdf" if name.endswith(".pdf") else ".pptx")
                assert hashlib.sha256(downloaded.content).hexdigest() == file["sha256"]
                generic = await case.client.get(f"/api/v1/files/{file['artifact_id']}")
                assert generic.status_code == 200 and generic.content == downloaded.content, generic.text
                downloaded_files[name] = downloaded.content
                (tmp_path / name).write_bytes(downloaded.content)
            source_file = draft["draft"]["files"]["candidate.pptx"]
            pdf_file = draft["draft"]["files"]["candidate.pdf"]
            assert preview["source_sha256"] == source_file["sha256"]
            assert preview["pdf_sha256"] == pdf_file["sha256"]
            pdf_artifact = next(row for row in rows(case, "artifacts") if row["id"] == pdf_file["artifact_id"])
            assert pdf_artifact["derived_from"] == source_file["artifact_id"]
            assert {slot.text for slot in inventory_pptx_text(downloaded_files["candidate.pptx"]).slots} == {
                "Quarter two",
                "Stable grouped caption",
                "Region",
                "Open cases",
                "North",
                "42",
                "South",
                "17",
            }
            before, after = _parts(template), _parts(downloaded_files["candidate.pptx"])
            assert before.keys() == after.keys()
            assert {name for name in before if before[name] != after[name]} == {"ppt/slides/slide1.xml"}
            assert worker.runs == original_runs
            assert len(helper_calls) == expected_helper_calls and len(lead_calls) == expected_lead_calls and len(document_calls) == 2
            if materials is not None:
                assert materials.executions == ["print(42)", "print(43)"]
                assert rows(case, "canvas_revisions") == original_revisions
