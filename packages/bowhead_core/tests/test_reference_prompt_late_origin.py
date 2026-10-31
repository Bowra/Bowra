"""Two authenticated Lead questions precede one retained Prompt expansion."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest
import respx
from test_reference_http_start import answer_response, settled
from test_reference_http_start import http_start as http_start
from test_reference_http_start import prohibit_unmocked_network as prohibit_unmocked_network
from test_reference_http_start import receiver_case as receiver_case
from test_reference_http_start import start_case as start_case
from test_reference_lead_prompt import lead_prompt as lead_prompt
from test_reference_prompt_http import prompt_http as prompt_http
from test_skill_extension_runtime import tool_response

from bowhead_core import reference_mcp_prompt_execution as prompt_runtime

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission_unused_sop"], indirect=True),
]


async def test_two_lead_answers_fetch_once_and_reuse_after_primary_answer(lead_prompt, monkeypatch):
    p, lead_calls, primary_calls = lead_prompt, [], []
    case = p.case
    questions = ["Which comparison?", "Which currency?", "Which scope?"]
    answers = ["Regional comparison", "USD", "All teams"]
    original_materialize = prompt_runtime.materialize_reference_mcp_prompt_results
    boundary_checks = []

    async def checked_materialize(**kwargs):
        generation = kwargs["expected_generation"]
        if generation == 3:
            for change, override in (
                ("foreign-scope", {"scope": kwargs["scope"].model_copy(update={"user_id": "foreign-user"})}),
                ("stale-generation", {"expected_generation": 2}),
                ("foreign-owner", {"owner_id": "foreign-owner"}),
            ):
                with pytest.raises(ValueError):
                    await original_materialize(**{**kwargs, **override})
                boundary_checks.append(change)
                assert p.calls == []

            def replace_notice(payload=None):
                with sqlite3.connect(case.path) as db:
                    row = db.execute(
                        "SELECT seq,payload_json FROM turn_events WHERE turn_id=? AND event_type='turn.resumed' "
                        "AND json_extract(payload_json,'$.execution_generation')=3",
                        (kwargs["turn_id"],),
                    ).fetchone()
                    assert row is not None
                    changed = json.loads(row[1])
                    changed["request_id"] = "unrelated-request"
                    db.execute(
                        "UPDATE turn_events SET payload_json=? WHERE turn_id=? AND seq=?",
                        (payload if payload is not None else json.dumps(changed), kwargs["turn_id"], row[0]),
                    )
                    return row[1]

            retained = await asyncio.to_thread(replace_notice)
            try:
                with pytest.raises(ValueError):
                    await original_materialize(**kwargs)
                assert p.calls == []
                boundary_checks.append("missing-consumed-proof")
            finally:
                await asyncio.to_thread(replace_notice, retained)
        elif generation == 4:
            # A missing retained result after dispatch cannot trigger a fresh
            # remote expansion. Restore is fault-injected; authority is real.
            with monkeypatch.context() as missing:
                missing.setattr(prompt_runtime, "_restore_reference_mcp_prompt_results_in_connection", AsyncMock(return_value=None))
                with pytest.raises(ValueError, match="reference_mcp_prompt_result_unavailable"):
                    await original_materialize(**kwargs)
            assert len(p.calls) == 1
            boundary_checks.append("already-dispatched")
        return await original_materialize(**kwargs)

    monkeypatch.setattr(prompt_runtime, "materialize_reference_mcp_prompt_results", checked_materialize)

    def respond(request):
        body = json.loads(request.content)
        names = {tool["function"]["name"] for tool in body.get("tools", [])}
        if "subagent_spawn" in names:
            lead_calls.append(body)
            if len(lead_calls) <= 2:
                return tool_response(
                    "clarification_ask",
                    {"questions": [{"question": questions[len(lead_calls) - 1], "type": "free_text", "options": []}]},
                    stream=body.get("stream", False),
                )
            return tool_response(
                "subagent_spawn",
                {"agent_id": p.agent.id, "task": "Analyze the selected period using the retained Prompt.", "delegation_role": "primary"},
                stream=body.get("stream", False),
            )
        primary_calls.append(body)
        assert "REMOTE_PROFITABILITY_METHOD" in json.dumps(body)
        if len(primary_calls) == 1:
            return tool_response(
                "subagent_return",
                {
                    "payload": {
                        "error": {
                            "kind": "needs_user_input",
                            "message": "Choose scope.",
                            "clarification_payload": {"questions": [{"question": questions[2], "type": "free_text", "options": []}]},
                        }
                    }
                },
                stream=body.get("stream", False),
            )
        assert answers[2] in json.dumps(body)
        return answer_response(request)

    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        sent = await case.client.post("/api/v1/chat/send", json=p.body)
        assert sent.status_code == 200, sent.text
        turn_id = sent.json()["turn_id"]
        cards = []
        for index in range(3):
            await settled(turn_id)
            assert (await case.fixture.case.store.get_turn(turn_id)).status == "clarification_pending"
            with sqlite3.connect(case.path) as db:
                card = db.execute(
                    "SELECT request_id,resume_context_json FROM clarification_requests WHERE turn_id=? "
                    "ORDER BY created_at DESC,request_id DESC LIMIT 1",
                    (turn_id,),
                ).fetchone()
                assert json.loads(card[1])["question_round"]["round"] == index + 1
                if index < 2:
                    assert (
                        db.execute("SELECT COUNT(*) FROM chat_reference_resolutions WHERE turn_id=? AND resolution_seq=2", (turn_id,)).fetchone()[0]
                        == 0
                    )
                    assert p.calls == []
                else:
                    assert db.execute(
                        "SELECT created_generation FROM chat_reference_resolutions WHERE turn_id=? "
                        "AND idempotency_key='reference:primary:mcp-prompt-results'",
                        (turn_id,),
                    ).fetchone() == (3,)
                    assert len(p.calls) == 1
            cards.append(card[0])
            resolved = await case.client.post(
                f"/api/v1/clarifications/{card[0]}/resolve",
                json={"chosen_values": [answers[index]], "answers": [{"question": questions[index], "source": "custom", "answer": answers[index]}]},
            )
            assert resolved.status_code == 200, resolved.text
        await settled(turn_id)
        turn = await case.fixture.case.store.get_turn(turn_id)
        assert turn.status == "completed" and turn.execution_generation == 4
        duplicate = await case.client.post(f"/api/v1/clarifications/{cards[-1]}/resume_turn")
        assert duplicate.status_code == 200, duplicate.text
    assert p.calls == [("profitability", {"period": "FY2026"})]
    assert len(lead_calls) == 3 and len(primary_calls) == 2
    assert boundary_checks == ["foreign-scope", "stale-generation", "foreign-owner", "missing-consumed-proof", "already-dispatched"]
    with sqlite3.connect(case.path) as db:
        assert db.execute("SELECT COUNT(*) FROM turn_agent_execution WHERE turn_id=?", (turn_id,)).fetchone()[0] == 1
        assert db.execute(
            "SELECT DISTINCT created_generation FROM chat_reference_content_snapshots WHERE turn_id=? "
            "AND json_extract(descriptor_json,'$.content_kind')='prompt_result'",
            (turn_id,),
        ).fetchall() == [(3,)]
        assert db.execute(
            "SELECT created_generation FROM chat_reference_evidence WHERE turn_id=? AND kind='output_received'", (turn_id,)
        ).fetchone() == (4,)
