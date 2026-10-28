"""Optional retained input follows the original first workspace clarification."""

import json
from types import SimpleNamespace

import pytest
import respx
from bowhead_core._db import open_shared_ctx
from bowhead_core.chat_reference_execution import read_reference_skill_primary_in_connection
from bowhead_core.chat_reference_scope import require_reference_request_scope
from bowhead_core.chat_reference_storage import ReferenceScope
from bowhead_gateway.turn_service import spawn_resume_turn
from test_reference_bi_continuation import answer_response, pause, resolve_request, settle_worker, workspace_question
from test_reference_bi_continuation import continuation_case as _continuation_case
from test_reference_bi_continuation import test_gateway_resumes_the_original_agent_without_replaying_its_first_call as original_resume_contract
from test_reference_bi_continuation import test_revoked_original_skill_keeps_answer_without_starting_another_model as original_revocation_contract
from test_skill_extension_runtime import publish_policy
from test_skill_reference_claim import claimed_case as _claimed_case
from test_skill_reference_claim import receiver_case as _receiver_case
from test_skill_reference_claim import start_case as _start_case
from test_skill_reference_runtime import rows

start_case, receiver_case, claimed_case, continuation_case = _start_case, _receiver_case, _claimed_case, _continuation_case
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("receiver_case", ["admission_extension_unused_invoke"], indirect=True),
    pytest.mark.parametrize("claimed_case", ["extension"], indirect=True),
]


async def test_same_optional_composition_and_policy_evidence_follow_native_resume(continuation_case):
    case = continuation_case
    await original_resume_contract(case)
    composition = json.loads(rows(case, "chat_reference_resolutions")[0]["composition_json"])
    assert composition["runtime_policy"] == "initial-primary-skill-extension.v1"
    checks = rows(case, "chat_reference_extension_checks")
    assert sorted(row["created_generation"] for row in checks) == [1, 2]
    assert all(row["composition_digest"] == rows(case, "chat_reference_resolutions")[0]["composition_digest"] for row in checks)
    assert len({json.loads(row["policy_check_json"])["original_extension_digest"] for row in checks}) == 1


async def test_revoked_original_dependency_still_blocks_optional_resume(continuation_case):
    await original_revocation_contract(continuation_case)
    checks = rows(continuation_case, "chat_reference_extension_checks")
    assert sorted(row["created_generation"] for row in checks) == [1, 3]


@pytest.mark.parametrize("point,enabled", [("before_pause", True), ("after_pause", True), ("after_pause", False)])
async def test_current_publication_constrains_original_gateway_continuation(continuation_case, point, enabled):
    case = continuation_case
    async with open_shared_ctx(case.path) as db:
        await db.execute("BEGIN")
        scope = ReferenceScope(**case.base.scope.model_dump(), policy_epoch=await require_reference_request_scope(db, case.base.scope))
        primary = await read_reference_skill_primary_in_connection(db, scope=scope, turn_id="t_reference", bindings=case.base.fixture.case.bindings)
    publication = SimpleNamespace(base=case.base, path=case.path, primary=primary)
    bodies = []

    async def respond(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1 and point == "before_pause":
            await publish_policy(publication, enabled=enabled)
        return workspace_question(request) if len(bodies) == 1 else answer_response(request)

    with respx.mock() as mock:
        route = mock.post(url__regex=r"https://api\..*").mock(side_effect=respond)
        question = await pause(case)
        before = rows(case, "chat_reference_resolutions")
        if point == "after_pause":
            await publish_policy(publication, enabled=enabled)
        request = await resolve_request(case, question)
        kwargs = {"initial_seq": max(row["seq"] for row in rows(case, "turn_events")), "resume_notice": {"clarification_id": question.request_id}}
        if enabled:
            spawned = await spawn_resume_turn(case.orchestrator, request, team_id=request.team_id, **kwargs)
            await settle_worker(spawned.task)
            assert route.call_count == 2
            turn = await case.case.store.get_turn("t_reference")
            assert (turn.status, turn.final_answer, turn.tokens_input, turn.tokens_output) == ("completed", "OK", 113, 23)
            assert "A new role for future executions." not in json.dumps(bodies)
            assert "BODY_optional_method" in json.dumps(bodies[1])
            assert sum(m.get("tool_call_id") == "select-context" for m in bodies[1]["messages"]) == 1
        else:
            with pytest.raises(ValueError):
                await spawn_resume_turn(case.orchestrator, request, team_id=request.team_id, **kwargs)
            assert route.call_count == 1
            assert (await case.case.store.get_turn("t_reference")).status == "clarification_pending"
            assert (await case.clarifications.get(question.request_id)).status == "resolved"
        assert rows(case, "chat_reference_resolutions") == before
        checks = sorted(rows(case, "chat_reference_extension_checks"), key=lambda row: row["created_generation"])
        assert [row["created_generation"] for row in checks] == ([1, 2] if enabled else [1])
        if enabled and point == "after_pause":
            assert (
                json.loads(checks[1]["policy_check_json"])["current_access_epoch"]
                > json.loads(checks[0]["policy_check_json"])["current_access_epoch"]
            )
