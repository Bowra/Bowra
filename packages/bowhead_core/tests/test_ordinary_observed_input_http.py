"""Resume actual template work independently of directory size or Team save epochs."""

import pytest
from bowhead_router.router import Router
from test_ordinary_lead_question_http import call, response
from test_ordinary_session_context_http import test_existing_session_retains_original_context_through_question as run_case
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
]


@pytest.mark.parametrize("receiver_case", ["admission_office_sandbox_unused_invoke"], indirect=True)
@pytest.mark.parametrize(
    "boundary,projection,prefix",
    [
        *[
            (boundary, projection, True)
            for boundary in ("projection_capacity", "directory_metadata", "model_config", "directory_deleted", "canvas_only", "deleted")
            for projection in (True, False)
        ],
        ("model_config", True, False),
        ("model_config", False, False),
        ("actual_capacity", False, True),
        ("directory_race", False, True),
        ("bytes", False, True),
        ("missing_proof", False, True),
    ],
)
async def test_original_template_question_resumes_with_observed_input_semantics(http_start, monkeypatch, boundary, projection, prefix):
    monkeypatch.setattr(Router, "supports_context_maintenance", lambda *args, **kwargs: projection)
    monkeypatch.setenv("BOWHEAD_CTX_PREFIX_ORDER", "1" if prefix else "0")
    kind = "canvas_only" if boundary == "canvas_only" else "upload"
    checkpoint = await run_case(http_start, monkeypatch, "retain", "custom", f"sandbox:{kind}:" + boundary, observed_inputs=True)
    if checkpoint is not None:
        assert checkpoint.context_projection_enabled is projection


@pytest.mark.parametrize("receiver_case", ["admission"], indirect=True)
async def test_basic_team_helper_question_keeps_its_original_writer_when_extension_is_off(http_start, monkeypatch):
    import json
    from dataclasses import replace

    import respx
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.agents.clarification_tool import register_clarification_tool
    from bowhead_core.artifact_store import ArtifactStore, LocalBackend
    from bowhead_core.ordinary_lead_continuation import read_ordinary_card_in_tx, reopen_ordinary_checkpoint
    from bowhead_core.subagent import PROFILES
    from bowhead_gateway.routes import clarifications
    from test_reference_http_start import enable_lead_writer, rows, settled

    from bowhead_core import events

    case = http_start
    case.client.headers["Content-Type"] = "application/json"
    monkeypatch.setenv("FEATURE_CHAT_MULTI_HELPER_QUESTION", "0")
    monkeypatch.setenv("FEATURE_KNOWLEDGE_PLUGIN_DIRECT_USE", "0")
    enable_lead_writer(case, monkeypatch)
    monkeypatch.setitem(PROFILES, "lead", replace(PROFILES["lead"], tool_whitelist=("subagent_spawn", "clarification_ask")))
    orch = case.app.state.orchestrator
    register_clarification_tool(orch.tool_registry)
    case.app.include_router(clarifications.router, prefix="/api/v1/clarifications")
    monkeypatch.setattr(events, "_default_registry", orch.event_registry)
    artifacts = ArtifactStore(str(case.path), LocalBackend(case.path.parent / "basic-inputs"))
    await artifacts.initialize()
    case.app.state.artifact_store = orch.artifact_store = artifacts
    calls = {"lead": 0, "helper": 0}

    def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if "subagent_spawn" in names:
            calls["lead"] += 1
            if calls["lead"] == 1:
                message = call(
                    "inspection",
                    "subagent_spawn",
                    {
                        "profile": case.fixture.case.args["reference_start"].profile_name,
                        "task": "Inspect the request.",
                        "delegation_role": "helper",
                        "required_source_urls": [],
                    },
                )
            elif calls["lead"] == 2:
                message = call("audience", "clarification_ask", {"questions": [{"question": "Which audience?", "type": "free_text", "options": []}]})
            else:
                assert "Finance" in json.dumps(body)
                assert calls["helper"] == 1
                return response(body, {"role": "assistant", "content": "Finance outline: context, findings, decision."}, "stop")
        elif "subagent_return" in names:
            calls["helper"] += 1
            message = call(
                "inspected",
                "subagent_return",
                {"payload": {"delivery_surface": "chat", "exec_summary": "Inspection complete; audience still needed."}},
            )
        else:
            return response(body, {"role": "assistant", "content": "Context"}, "stop")
        return response(body, message, "tool_calls")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        sent = await case.client.post(
            "/api/v1/chat/send",
            json={
                "workspace_id": case.fixture.scope.workspace_id,
                "message": "Inspect the request, ask for the audience, then give a short outline.",
            },
        )
        assert sent.status_code == 200, sent.text
        turn_id = sent.json()["turn_id"]
        await settled(turn_id)
        cards = [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
        assert len(cards) == 1
        async with open_shared_ctx(case.path) as db:
            await db.execute("BEGIN")
            checkpoint = reopen_ordinary_checkpoint(await read_ordinary_card_in_tx(db, card_id=cards[0]["request_id"]))
        assert checkpoint.contract in {"ordinary-lead-loop.v1", "ordinary-lead-loop.v2", "ordinary-lead-loop.v3", "ordinary-lead-loop.v9"}
        resolved = await case.client.post(
            f"/api/v1/clarifications/{cards[0]['request_id']}/resolve",
            json={"answers": [{"question": "Which audience?", "answer": "Finance", "source": "custom"}]},
        )
        assert resolved.status_code == 200, resolved.text
        await settled(turn_id)
        turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
        assert (turn["status"], turn["execution_generation"]) == ("completed", 2)
        assert calls["helper"] == 1
