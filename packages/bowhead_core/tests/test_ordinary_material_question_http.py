"""Actual ordinary HTTP pause/resume with native retained material writers."""

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import respx
from bowhead_core.agents.clarification_tool import register_clarification_tool
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.skill_bridge import register_skill_tools
from bowhead_core.subagent import PROFILES
from bowhead_gateway.routes import clarifications, files
from test_ordinary_lead_question_http import call, response
from test_reference_http_start import enable_lead_writer, rows, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case

from bowhead_core import events
from bowhead_router import AgentBinding
from bowhead_skill import SkillCatalog, SkillRuntime

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission_sandbox_unused_invoke"], indirect=True),
]


@pytest.mark.parametrize(
    "boundary",
    [
        "resume",
        "settled_failure",
        "settled_failure_recreated",
        "mixed_failure",
        "script_recovery",
        "receipt_recovery",
        "script_recovery_question",
        "receipt_recovery_question",
        "recreated",
        "bundle_rejection",
        "classification",
        "classification_race",
        "skill_changed",
        "output_deleted",
        "canvas_changed",
        "output_race",
        "canvas_race",
        "budget",
    ],
)
@pytest.mark.parametrize("helper_count", [1, 2, 3])
async def test_native_materials_survive_original_question_without_reexecuting_helper(http_start, monkeypatch, boundary, helper_count):
    asks_after_recovery = boundary.endswith("_recovery_question")
    if asks_after_recovery:
        boundary = boundary.removesuffix("_question")
    case = http_start
    case.client.headers["Content-Type"] = "application/json"
    scope = case.fixture.scope
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setenv("FEATURE_CHAT_MULTI_HELPER_QUESTION", "1")
    monkeypatch.setenv("BOWHEAD_PI_K_ENABLED", "0")
    monkeypatch.setenv("BOWHEAD_SANDBOX_BACKEND", "executor")
    monkeypatch.setattr("bowhead_core.assigned_skill_execution.builtin_catalog_dir", lambda: case.fixture.root)
    monkeypatch.setattr("bowhead_core.ordinary_lead_material.builtin_catalog_dir", lambda: case.fixture.root)
    monkeypatch.setitem(
        PROFILES,
        "lead",
        replace(
            PROFILES["lead"],
            tool_whitelist=("subagent_spawn", "clarification_ask", "skill_list"),
            max_iterations=helper_count + 1 if boundary == "budget" else 8,
        ),
    )
    if boundary in {"mixed_failure", "script_recovery"}:
        from test_system_reference_baseline import configure

        _, system_profile = await configure(case.fixture, monkeypatch)
        monkeypatch.setitem(
            PROFILES,
            "compute",
            replace(
                system_profile,
                tool_whitelist=(*system_profile.tool_whitelist, "subagent_return"),
            ),
        )
    orch = case.app.state.orchestrator
    orch.router.bind(AgentBinding(agent_type="echo", primary=("primary", "large")))
    register_clarification_tool(orch.tool_registry)
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    monkeypatch.setattr(events, "_default_registry", orch.event_registry)
    artifacts = ArtifactStore(str(case.path), LocalBackend(case.path.parent / "ordinary-material-outputs"))
    canvas_store = CanvasStore(str(case.path))
    await artifacts.initialize()
    await canvas_store.initialize()
    orch.artifact_store = case.app.state.artifact_store = artifacts
    orch.canvas_store = canvas_store
    orch.skill_instance_store = case.fixture.sources.instances
    orch.skill_catalog = SkillCatalog(roots=[case.fixture.root])
    runtime = SkillRuntime()
    executions, outputs, sections = [], [], []
    case.app.include_router(files.router, prefix="/api/v1/files")
    case.client.headers.pop("Content-Type")
    uploaded = await case.client.post(
        "/api/v1/files/upload",
        files={"file": ("synthetic.txt", b"Synthetic input: 42", "text/plain")},
        headers={"X-Space-Id": scope.workspace_id},
    )
    assert uploaded.status_code == 200, uploaded.text
    attachment = uploaded.json()
    case.client.headers["Content-Type"] = "application/json"

    async def run(spec, *, entry_script, input_data, extra_args):
        executions.append(input_data["code"])
        assert spec.name == "python_sandbox" and entry_script == "runner.py"
        input_evidence = []
        for key, item in input_data["inputs"].items():
            art = await artifacts.get(item["artifact_id"])
            data = await artifacts.backend.read_bounded(art.path, limit=art.size)
            input_evidence.append({"key": key, "artifact_id": art.id, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        assert len(input_evidence) == 1
        output = await artifacts.put_bytes(
            content=b"42\n",
            filename=f"calculation-{len(executions)}.txt",
            mime="text/plain",
            source="python_sandbox",
            created_by=scope.user_id,
            workspace_id=scope.workspace_id,
            created_by_turn=input_data["_turn_id"],
            session_id=input_data["_session_id"],
        )
        outputs.append(output)
        canvas = await canvas_store.ensure_canvas(session_id=input_data["_session_id"], workspace_id=scope.workspace_id, user_id=scope.user_id)
        sections.append(
            await canvas_store.append_section(
                canvas_id=canvas.id, kind="markdown", payload={"text": f"Synthetic calculation from {output.id}"}, by_turn_id=input_data["_turn_id"]
            )
        )
        if boundary == "recreated" and len(executions) == 2 * helper_count:
            revised = await canvas_store.patch_section(
                canvas_id=canvas.id,
                section_id=sections[0].id,
                expected_rev=sections[0].rev,
                patch={"text": sections[0].payload["text"] + " (checked by the final helper)"},
                by_turn_id=input_data["_turn_id"],
            )
            assert revised is not None
            sections[0] = revised
        failed = boundary in {"script_recovery", "receipt_recovery"} and len(executions) == 1
        error = "KeyError: missing column" if boundary == "script_recovery" else "Executor reply unavailable"
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
                "ok": not failed,
                "exit_code": 1 if failed else 0,
                "error": error if failed else None,
                "stderr": error if failed else "",
                "input_artifacts": input_evidence,
                "execution": {
                    "contract": "sandbox-execution.v1",
                    "id": "bowhead-sandbox-" + f"{len(executions):024x}",
                    "image_id": "sha256:" + "a" * 64,
                    "runtime": "runsc",
                    "network": "none",
                    "settled": True,
                }
                if boundary != "receipt_recovery" or not failed
                else None,
                "outputs": [{"artifact_id": output.id, "name": output.filename, "mime": output.mime, "size": output.size, "data_role": "stdout"}],
            },
        )

    monkeypatch.setattr(runtime, "run", run)
    register_skill_tools(orch.tool_registry, orch.skill_catalog, runtime)
    lead_calls, helper_calls, closeout_calls = [], [], []
    question = {"question": "Which audience?", "options": [], "type": "free_text"}

    async def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls.append(body)
            index = len(lead_calls)
            if index <= helper_count:
                message = call(
                    f"helper-original-{index}",
                    "subagent_spawn",
                    {
                        "profile": "compute"
                        if (boundary == "mixed_failure" and index == helper_count) or boundary == "script_recovery"
                        else case.fixture.case.args["reference_start"].profile_name,
                        "task": f"Read the method and calculate twice for inspection {index}.",
                        "delegation_role": "helper",
                    },
                )
            elif index == helper_count + 1:
                if boundary in {"script_recovery", "receipt_recovery"} and not asks_after_recovery:
                    return response(body, {"role": "assistant", "content": "The corrected calculation is complete."}, "stop")
                if boundary == "classification":
                    # The real asynchronous classifier updates these fields
                    # after the native tool observer has retained its output.
                    await artifacts.set_category(outputs[0].id, category="dataset", source="llm", retention_hint="one_off")
                message = call("question-original", "clarification_ask", {"questions": [question]})
            elif index == helper_count + 2:
                message = call("list-after-answer", "skill_list", {})
            else:
                assert index == helper_count + 3
                answer = (
                    "The calculation is 42; the audience is still unspecified."
                    if asks_after_recovery
                    else "The retained calculation is 42 for the selected audience."
                )
                return response(body, {"role": "assistant", "content": answer}, "stop")
            return response(body, message, "tool_calls")
        if names == {"subagent_return"}:
            # 5261ea640: an empty peer answer after successful tools gets one return-only
            # closeout. These variants keep the helper result empty, so it still settles as failed.
            closeout_calls.append(body)
            return response(body, {"role": "assistant", "content": ""}, "stop")
        if "subagent_return" in names:
            helper_calls.append(body)
            index = (len(helper_calls) - 1) % 5 + 1
            if index == 1:
                message = call(
                    "denied-original",
                    "skill_read",
                    {"name": "method", "file": "synthetic.txt"} if boundary == "bundle_rejection" else {"name": "outside-original-grant"},
                )
            elif index == 2:
                message = call("read-original", "skill_read", {"name": "method"})
            elif index in {3, 4}:
                message = call(
                    f"sandbox-original-{index}",
                    "skill_invoke",
                    {
                        "name": "python_sandbox",
                        "script": "runner.py",
                        "input": {
                            "code": f"print({40 + index})",
                            "inputs": {"source": {"artifact_id": outputs[-1].id if outputs else attachment["file_id"]}},
                        },
                    },
                )
            else:
                assert index == 5
                return response(
                    body,
                    {
                        "role": "assistant",
                        "content": ""
                        if boundary in {"settled_failure", "settled_failure_recreated", "mixed_failure"}
                        else "Both calculations finished; choose the audience.",
                    },
                    "stop",
                )
            return response(body, message, "tool_calls")
        return response(body, {"role": "assistant", "content": "Synthetic inspection"}, "stop")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": scope.workspace_id,
                "message": "Inspect and calculate, then ask for the audience.",
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
        if boundary in {"script_recovery", "receipt_recovery"}:
            turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
            assert turn["status"] == "completed", turn["error"]
            if asks_after_recovery:
                assert len(lead_calls) == helper_count + 3
                refusal = next(message for message in lead_calls[helper_count + 1]["messages"] if message.get("tool_call_id") == "question-original")
                assert "clarification_capture_unavailable" in refusal["content"]
                assert not [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
                persisted_questions = [
                    json.loads(row["payload_json"])
                    for row in rows(case, "turn_events")
                    if row["turn_id"] == turn_id
                    and row["event_type"] == "tool_result"
                    and json.loads(row["payload_json"]).get("call_id") == "question-original"
                ]
                assert len(persisted_questions) == 1
                assert "_clarification_pending" not in json.dumps(persisted_questions)
                assert "clarification_capture_unavailable" in json.dumps(persisted_questions)
            else:
                assert "corrected calculation" in turn["final_answer"]
            assert len(executions) == 2 * helper_count
            assert len(helper_calls) == 5 * helper_count
            failed_result = next(message for message in helper_calls[3]["messages"] if message.get("tool_call_id") == "sandbox-original-3")
            assert ("KeyError: missing column" if boundary == "script_recovery" else "Executor reply unavailable") in failed_result["content"]
            assert outputs[0].id in json.dumps(helper_calls[3])
            persisted = await case.app.state.turn_log_store.list_events(turn_id, event_types=("tool_result", "step_done"))
            attempts = [
                event.payload for event in persisted if event.event_type == "tool_result" and event.payload.get("tool_name") == "python_sandbox"
            ]
            assert len(attempts) == 2 * helper_count and attempts[0]["ok"] is False
            assert all(attempt["ok"] is True for attempt in attempts[1:])
            return
        cards = [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
        assert len(cards) == 1, [row for row in rows(case, "turns") if row["id"] == turn_id]
        card = cards[0]
        assert json.loads(card["resume_context_json"])["__schema_version"] == 4
        assert len(executions) == 2 * helper_count
        assert len(helper_calls) == 5 * helper_count
        empty_answer = boundary in {"settled_failure", "settled_failure_recreated", "mixed_failure"}
        assert len(closeout_calls) == (helper_count if empty_answer else 0)
        from bowhead_core._db import open_shared_ctx
        from bowhead_core.ordinary_lead_continuation import read_ordinary_card_in_tx, reopen_ordinary_checkpoint
        from bowhead_core.ordinary_lead_runtime import validate_ordinary_retained_materials

        async with open_shared_ctx(case.path) as db:
            await db.execute("BEGIN")
            checkpoint = reopen_ordinary_checkpoint(await read_ordinary_card_in_tx(db, card_id=card["request_id"]))
        # A native bundle rejection is a settled child call. An Agent permission
        # guard rejects before invocation and supplies only model feedback.
        denied = [message for message in helper_calls[1]["messages"] if message.get("tool_call_id") == "denied-original"]
        assert len(denied) == 1
        assert ("skill_file_kind_invalid" if boundary == "bundle_rejection" else "isn't included in this Agent") in denied[0]["content"]
        helpers = checkpoint.helper_materials()
        assert checkpoint.contract == "ordinary-lead-loop.v9"
        if boundary in {"settled_failure", "settled_failure_recreated", "mixed_failure"}:
            assert all(helper["outcome"]["status"] == "failed" for helper in helpers)
            assert all(item.result["ok"] is False for item in checkpoint.invocations())
        assert len(helpers) == helper_count
        assert len({helper["execution_id"] for helper in helpers}) == helper_count
        for helper in helpers:
            assert [(item["call_id"], item["kind"]) for item in helper["tools"]] == (
                [("denied-original", "rejected")] if boundary == "bundle_rejection" else []
            ) + [
                ("read-original", "skill_read"),
                ("sandbox-original-3", "sandbox"),
                ("sandbox-original-4", "sandbox"),
            ]
        assert ("denied-original" in checkpoint.retained_call_ids()) == (boundary == "bundle_rejection")
        from bowhead_core.subagent import SubagentRunner

        await validate_ordinary_retained_materials(
            SimpleNamespace(
                subagent_runner=SubagentRunner(router=None, tool_registry=None),
                session_store=orch.session_store,
                artifact_store=artifacts,
                canvas_store=canvas_store,
                skill_catalog=orch.skill_catalog,
                team_id=scope.team_id,
                user_id=scope.user_id,
                workspace_id=scope.workspace_id,
                session_id=card["session_id"],
                turn_id=turn_id,
            ),
            checkpoint,
        )
        if boundary == "classification":
            await artifacts.set_category(outputs[1].id, category="document", source="user", retention_hint="long_term")
        elif boundary == "skill_changed":
            (case.fixture.root / "method/method.md").write_text("CHANGED after pause")
        elif boundary == "output_deleted":
            await artifacts.soft_delete(outputs[0].id, user_id=scope.user_id)
        elif boundary == "canvas_changed":
            await canvas_store.reorder_sections(
                canvas_id=sections[0].canvas_id, ordered_ids=[item.id for item in reversed(sections)], by_turn_id="t_competing"
            )
        elif boundary in {"output_race", "canvas_race", "classification_race"}:
            from bowhead_core import ordinary_lead_runtime

            original_validate = ordinary_lead_runtime.validate_ordinary_retained_materials

            async def change_after_worker_preflight(ctx, checkpoint):
                await original_validate(ctx, checkpoint)
                if getattr(ctx, "ordinary_lead_state", None) is not None:
                    if boundary == "output_race":
                        await artifacts.soft_delete(outputs[0].id, user_id=scope.user_id)
                    elif boundary == "canvas_race":
                        await canvas_store.delete_section(canvas_id=sections[0].canvas_id, section_id=sections[0].id, by_turn_id="t_competing")
                    else:
                        await artifacts.set_category(outputs[0].id, category="dataset", source="llm", retention_hint="one_off")

            monkeypatch.setattr(ordinary_lead_runtime, "validate_ordinary_retained_materials", change_after_worker_preflight)
        if boundary in {"recreated", "settled_failure_recreated", "mixed_failure"}:
            from bowhead_core.clarification_store import ClarificationStore
            from bowhead_core.events import EventBusRegistry
            from bowhead_core.orchestrator import Orchestrator
            from bowhead_core.session_store import SessionStore
            from bowhead_core.subagent import SubagentRunner
            from bowhead_core.turn_log_store import OrderedTurnLogSink

            old = orch
            orch = Orchestrator(
                router=old.router,
                datarc=None,
                session_store=SessionStore(case.path),
                tool_registry=old.tool_registry,
                event_registry=EventBusRegistry(default_log_sink=OrderedTurnLogSink(case.app.state.turn_log_store)),
                user_store=old.user_store,
                clarification_store=ClarificationStore(case.path),
                artifact_store=artifacts,
            )
            orch.dispatch_owner_id = "recreated-ordinary-multi-helper-worker"
            orch.subagent_runner = SubagentRunner(router=old.router, tool_registry=old.tool_registry, team_subagent_store=old.user_store)
            orch.reference_skill_sources_factory = old.reference_skill_sources_factory
            orch.skill_instance_store = old.skill_instance_store
            orch.skill_catalog = old.skill_catalog
            orch.access_control_store = case.app.state.access_control_store
            case.app.state.orchestrator = orch
            case.app.state.session_store = orch.session_store
            case.app.state.clarification_store = orch.clarification_store
            monkeypatch.setattr(events, "_default_registry", orch.event_registry)
        # Reopen with fresh stores: no in-memory observation survives the pause.
        orch.canvas_store = CanvasStore(str(case.path))
        orch.artifact_store = ArtifactStore(str(case.path), artifacts.backend)
        # Creation gates cannot strand an already captured checkpoint.
        monkeypatch.setenv("FEATURE_CHAT_MULTI_HELPER_QUESTION", "0")
        answered = await case.client.post(
            f"/api/v1/clarifications/{card['request_id']}/resolve",
            json={"answers": [{"question": question["question"], "source": "custom", "answer": "Board members"}]},
        )
        accepted_resume = boundary in {
            "mixed_failure",
            "settled_failure",
            "settled_failure_recreated",
            "resume",
            "recreated",
            "bundle_rejection",
            "classification",
            "classification_race",
            "output_race",
            "canvas_race",
            "budget",
        }
        assert answered.status_code == (200 if accepted_resume else 409), answered.text
        if accepted_resume:
            await settled(turn_id)
        else:
            retained = next(row for row in rows(case, "clarification_requests") if row["request_id"] == card["request_id"])
            # First-answer persistence precedes resume preflight. Material loss
            # prevents dispatch without discarding or replacing that answer.
            assert retained["status"] == "resolved"
            assert retained["resume_context_json"] == card["resume_context_json"]
            # A fresh history read must retain the unavailable continuation,
            # even though the question is answered and is no longer pending.
            replay = await case.client.get(f"/api/v1/clarifications/turn/{turn_id}")
            assert replay.status_code == 200, replay.text
            assert replay.json()["items"] == []
            failure = replay.json().get("resume_failure")
            assert failure is not None
            assert failure["request_id"] == card["request_id"]
            assert failure["turn_id"] == turn_id
            assert failure["execution_generation"] == 1
            assert failure["code"] == "clarification_resume_unavailable"
            assert failure["recovery_action"] == "copy_diagnostics"
            assert failure["diagnostics"]["retryable"] is False
            assert failure["diagnostics"]["safe_detail"] == "phase=clarification_resume_preflight; failure_class=state"
            assert answered.json()["detail"]["error"]["diagnostics"]["safe_detail"] == failure["diagnostics"]["safe_detail"]
            assert retained["injected_at"] is None and retained["continuation_generation"] is None
            paused = next(row for row in rows(case, "turns") if row["id"] == turn_id)
            assert paused["status"] == "clarification_pending" and paused["execution_generation"] == 1
            assert next(row for row in rows(case, "clarification_requests") if row["request_id"] == card["request_id"]) == retained
        assert len(executions) == 2 * helper_count
        assert len(helper_calls) == 5 * helper_count
        assert len(lead_calls) == helper_count + (
            3
            if boundary
            in {
                "resume",
                "settled_failure",
                "settled_failure_recreated",
                "mixed_failure",
                "recreated",
                "bundle_rejection",
                "classification",
                "classification_race",
            }
            else 2
            if boundary == "budget"
            else 1
        )
        if boundary == "budget":
            turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
            assert turn["status"] == "failed"
        if boundary in {"output_race", "canvas_race"}:
            retained = next(row for row in rows(case, "clarification_requests") if row["request_id"] == card["request_id"])
            assert retained["continuation_generation"] is None
            turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
            assert turn["status"] == "failed"
        if boundary in {
            "resume",
            "settled_failure",
            "settled_failure_recreated",
            "mixed_failure",
            "recreated",
            "bundle_rejection",
            "classification",
            "classification_race",
        }:
            turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
            assert turn["status"] == "completed"
            result_messages = [item for item in lead_calls[helper_count + 1]["messages"] if item.get("tool_call_id") == "question-original"]
            assert len(result_messages) == 1 and "Board members" in result_messages[0]["content"]
            before = [item for item in lead_calls[helper_count]["messages"] if item.get("name") == "subagent_spawn"]
            after = [item for item in lead_calls[helper_count + 1]["messages"] if item.get("name") == "subagent_spawn"]
            assert len(before) == helper_count and after == before
            retained_sections = [
                item
                for item in await orch.canvas_store.list_sections(sections[0].canvas_id, include_hidden=True)
                if item.payload.get("text", "").startswith("Synthetic calculation from ")
            ]
            assert [(item.id, item.payload) for item in retained_sections] == [(item.id, item.payload) for item in sections]
