"""A helper that writes to the Session Canvas before Lead asks keeps the pause.

A helper Canvas write had no retention record, so Lead's question became the
nonterminal capture refusal and the user got a plain-text question (116,
``add_chart``). The retained Canvas projection already proves the write: the
same Turn resumes after the answer without rerunning the helper or its write,
and a Canvas changed during the pause refuses the retained continuation.
"""

import json
from types import SimpleNamespace

import pytest
import respx
from bowhead_core.artifact_store import ArtifactStore, LocalBackend
from bowhead_core.canvas_store import CanvasStore
from bowhead_core.canvas_tools import register_canvas_tools
from bowhead_core.ordinary_lead_continuation import OrdinaryContinuationUnavailable
from test_ordinary_helper_question_http import QUESTION, _prepare
from test_ordinary_lead_question_http import call, response, review_round
from test_reference_http_start import http_start as http_start
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import rows, settled
from test_reference_http_start import start_case as start_case

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission_canvas_block"], indirect=True),
]

BLOCK = {
    "format": "graph",
    "title": "Sales path",
    "nodes": [{"id": "orders", "label": "Orders"}, {"id": "sales", "label": "Sales"}],
    "edges": [{"from": "orders", "to": "sales"}],
}


@pytest.mark.parametrize("boundary", ["resume", "canvas_changed"])
async def test_a_helper_canvas_write_before_lead_asks_keeps_the_pause(http_start, monkeypatch, boundary):
    case = http_start
    orch = _prepare(case, monkeypatch)
    artifacts = ArtifactStore(str(case.path), LocalBackend(case.path.parent / "canvas-question-uploads"))
    canvas_store = CanvasStore(str(case.path))
    await artifacts.initialize()
    await canvas_store.initialize()
    case.app.state.artifact_store = orch.artifact_store = artifacts
    orch.canvas_store = canvas_store
    if not orch.tool_registry.has("canvas_add_spec_block"):
        register_canvas_tools(orch.tool_registry)
    helper_profile = case.fixture.case.args["reference_start"].profile_name
    lead_calls, helper_calls = [], []

    def respond(request):
        body = json.loads(request.content)
        names = {item["function"]["name"] for item in body.get("tools", [])}
        if review_round(body):
            return response(body, {"role": "assistant", "content": body["messages"][-2]["content"]}, "stop")
        if "subagent_spawn" in names:
            lead_calls.append(body)
            if len(lead_calls) == 1:
                spawn = {"profile": helper_profile, "task": "Draw the sales path.", "delegation_role": "helper", "required_source_urls": []}
                return response(body, call("helper-native", "subagent_spawn", spawn), "tool_calls")
            if len(lead_calls) == 2:
                return response(body, call("question-native", "clarification_ask", {"questions": [QUESTION]}), "tool_calls")
            if len(lead_calls) == 4:
                return response(body, {"role": "assistant", "content": "Gross sales follow the same path."}, "stop")
            return response(body, {"role": "assistant", "content": "Net sales were selected."}, "stop")
        if "subagent_return" in names:
            helper_calls.append(body)
            if len(helper_calls) == 1:
                return response(body, call("helper-block", "canvas_add_spec_block", BLOCK), "tool_calls")
            summary = {"delivery_surface": "chat", "exec_summary": "The sales path is on the Canvas. Which sales metric?"}
            return response(body, call("helper-return", "subagent_return", {"payload": summary}), "tool_calls")
        return response(body, {"role": "assistant", "content": "Sales"}, "stop")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        accepted = await case.client.post("/api/v1/chat/send", json={"workspace_id": case.fixture.scope.workspace_id, "message": "Show sales."})
        assert accepted.status_code == 200, accepted.text
        turn_id = accepted.json()["turn_id"]
        await settled(turn_id)

        block = json.loads(next(m for m in helper_calls[1]["messages"] if m.get("tool_call_id") == "helper-block")["content"])
        assert block["kind"] == "spec_block", block
        turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
        cards = [row for row in rows(case, "clarification_requests") if row["turn_id"] == turn_id]
        assert turn["status"] == "clarification_pending", turn["error"]
        assert len(cards) == 1

        from bowhead_core._db import open_shared_ctx
        from bowhead_core.ordinary_lead_continuation import read_ordinary_card_in_tx, reopen_ordinary_checkpoint
        from bowhead_core.ordinary_lead_runtime import validate_ordinary_retained_materials
        from bowhead_core.subagent import SubagentRunner

        async with open_shared_ctx(case.path) as db:
            await db.execute("BEGIN")
            checkpoint = reopen_ordinary_checkpoint(await read_ordinary_card_in_tx(db, card_id=cards[0]["request_id"]))
        [helper] = checkpoint.helper_materials()
        assert [(item["call_id"], item["kind"]) for item in helper["tools"]] == [("helper-block", "canvas_result")]
        sections = await canvas_store.list_sections(block["canvas_id"], include_hidden=True)
        assert [section.id for section in sections] == [block["section_id"]]

        if boundary == "canvas_changed":
            # Another writer edits the Canvas during the pause: the retained write is no longer proven.
            await canvas_store.append_section(
                canvas_id=block["canvas_id"], kind="markdown", payload={"text": "Edited elsewhere"}, by_turn_id="t_competing"
            )
            ctx = SimpleNamespace(
                subagent_runner=SubagentRunner(router=None, tool_registry=None),
                session_store=orch.session_store,
                artifact_store=artifacts,
                canvas_store=canvas_store,
                skill_catalog=getattr(orch, "skill_catalog", None),
                team_id=case.fixture.scope.team_id,
                user_id=case.fixture.scope.user_id,
                workspace_id=case.fixture.scope.workspace_id,
                session_id=cards[0]["session_id"],
                turn_id=turn_id,
            )
            with pytest.raises(OrdinaryContinuationUnavailable):
                await validate_ordinary_retained_materials(ctx, checkpoint)
            return

        answered = await case.client.post(
            f"/api/v1/clarifications/{cards[0]['request_id']}/resolve",
            json={"answers": [{"question": QUESTION["question"], "source": "candidate", "answer": "Net sales", "option_index": 0}]},
        )
        assert answered.status_code == 200, answered.text
        await settled(turn_id)

        turn = next(row for row in rows(case, "turns") if row["id"] == turn_id)
        assert turn["status"] == "completed", turn["error"]
        # Resumed in place: the helper and its Canvas write did not run again.
        assert len(helper_calls) == 2
        assert len(lead_calls) == 3
        assert [section.id for section in await canvas_store.list_sections(block["canvas_id"], include_hidden=True)] == [block["section_id"]]
        answer = next(m for m in lead_calls[2]["messages"] if m.get("tool_call_id") == "question-native")
        assert "Net sales" in answer["content"]

        # The next Turn loads the published history, whose material dependencies include the Canvas record.
        follow = await case.client.post(
            "/api/v1/chat/send",
            json={"workspace_id": case.fixture.scope.workspace_id, "session_id": cards[0]["session_id"], "message": "And gross sales?"},
        )
        assert follow.status_code == 200, follow.text
        await settled(follow.json()["turn_id"])
    later = next(row for row in rows(case, "turns") if row["id"] == follow.json()["turn_id"])
    assert later["status"] == "completed", later["error"]
    assert len(lead_calls) == 4
