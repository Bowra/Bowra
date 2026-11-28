"""Owner draft trial through ``/chat/send``, the readers and the exclusions (slice 2).

docs/sprints/agent-page-redesign-2026-11-24/trial-tech-design.md §4–§6. Runs the original reference HTTP
harness on an actually migrated domain layout: real auth, submission claims, Turn admission, the native
worker with a mocked model, and the trial guards with their registered predicates.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import respx
import test_reference_http_start as http_flows
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_behavior_contract import AgentBehaviorDescriptor, canonical_fingerprint
from bowhead_core.agent_owner_trial import list_recent_owner_trials, read_owner_trial
from bowhead_core.chat_references import InputDocument
from bowhead_core.sqlite_domain_runtime import connect_sync
from bowhead_core.sqlite_domains import domain_database_path
from fastapi import HTTPException
from test_reference_bi_continuation import answer_response
from test_reference_http_start import http_start as http_start
from test_reference_http_start import prohibit_unmocked_network as prohibit_unmocked_network
from test_reference_http_start import start_case as start_case
from test_reference_split_domains import legacy_receiver_case as legacy_receiver_case
from test_reference_split_domains import receiver_case as receiver_case

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.parametrize("start_case", ["team"], indirect=True),
    pytest.mark.parametrize("legacy_receiver_case", ["admission"], indirect=True),
]

AGENT = "osa_exact"
LIVE_REVISION = "arv_exact_2"
DRAFT_REVISION = "arv_exact_3"
CANDIDATE = "arc_trial_3"
DRAFT_PROMPT = "DRAFT_TRIAL_PROMPT answer from the saved draft."
_NOW = datetime.now(UTC).isoformat()


def _hash(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _scope(case):
    return case.fixture.scope


def _prepare_draft(case, *, owner: bool = True, state: str = "prepared") -> None:
    """Freeze a draft (revision 3, its Safety report and behavior) as a release candidate."""
    scope = _scope(case)
    with closing(connect_sync(str(case.path))) as db:
        db.row_factory = sqlite3.Row
        live = db.execute("SELECT * FROM team_subagent_revision WHERE id=?", (LIVE_REVISION,)).fetchone()
        definition = json.loads(live["definition_json"])
        definition["system_prompt"] = DRAFT_PROMPT
        listing = json.loads(live["listing_json"])
        definition_json = _canonical(definition)
        db.execute(
            "INSERT INTO team_subagent_revision(id,team_id,subagent_id,sequence,revision_kind,definition_json,listing_json,definition_hash,"
            "dependency_fingerprint,capability_fingerprint,risk_compiler_version,risk_json,created_by,created_at) "
            "VALUES (?,?,?,3,'authored',?,?,?,?,?,?,?,?,?)",
            (
                DRAFT_REVISION,
                scope.team_id,
                AGENT,
                definition_json,
                live["listing_json"],
                _hash(definition_json),
                live["dependency_fingerprint"],
                live["capability_fingerprint"],
                live["risk_compiler_version"],
                live["risk_json"],
                live["created_by"],
                _NOW,
            ),
        ).close()
        db.execute(
            "INSERT INTO agent_safety_report(id,team_id,agent_id,subject_kind,subject_ref,content_hash,dependency_fingerprint,compiler_version,"
            "semantic_scanner_version,status,deterministic_json,semantic_json,safe_summary_json,created_by,created_at) "
            "VALUES ('asr_trial_3',?,?,'revision',?,?,?,'safety-v1','none','pass','{}','{}','{}',?,?)",
            (
                scope.team_id,
                AGENT,
                DRAFT_REVISION,
                _hash(_canonical({"definition": definition, "listing": listing})),
                live["dependency_fingerprint"],
                live["created_by"],
                _NOW,
            ),
        ).close()
        behavior = db.execute("SELECT * FROM team_subagent_revision_behavior WHERE revision_id=?", (LIVE_REVISION,)).fetchone()
        payload = AgentBehaviorDescriptor.model_validate_json(behavior["descriptor_json"]).model_dump(mode="json")
        payload["source_definition_hash"] = _hash(definition_json)
        payload.pop("behavior_fingerprint")
        payload["behavior_fingerprint"] = canonical_fingerprint(payload)
        descriptor = AgentBehaviorDescriptor.model_validate(payload)
        db.execute(
            "INSERT INTO team_subagent_revision_behavior(revision_id,team_id,subagent_id,source_definition_hash,descriptor_json,"
            "behavior_fingerprint,compiler_version,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                DRAFT_REVISION,
                scope.team_id,
                AGENT,
                descriptor.source_definition_hash,
                descriptor.model_dump_json(),
                descriptor.behavior_fingerprint,
                descriptor.compiler_version,
                _NOW,
            ),
        ).close()
        db.execute(
            "INSERT INTO team_subagent_release_candidate(id,team_id,subagent_id,revision_id,base_live_release_id,target_audience_json,"
            "target_audience_hash,capability_fingerprint,dependency_fingerprint,risk_compiler_version,risk_json,safety_report_id,"
            "candidate_hash,state,submitted_by,submitted_at,updated_at) "
            "VALUES (?,?,?,?,'arl_exact_2','{}','sha256:aud',?,?,?,?,'asr_trial_3',?,?,?,?,?)",
            (
                CANDIDATE,
                scope.team_id,
                AGENT,
                DRAFT_REVISION,
                live["capability_fingerprint"],
                live["dependency_fingerprint"],
                live["risk_compiler_version"],
                live["risk_json"],
                _hash(CANDIDATE),
                state,
                scope.user_id,
                _NOW,
                _NOW,
            ),
        ).close()
        if owner:
            db.execute("UPDATE team_subagent SET owner_user_id=?,row_version=row_version+1 WHERE id=?", (scope.user_id, AGENT)).close()
        db.commit()


def _trial_payload(case, *, version: str = DRAFT_REVISION, session_id: str | None = None, skills: bool = False) -> dict:
    body = http_flows.payload(case, skills=skills)
    agent = next(node for node in body["input_document"]["nodes"] if node.get("resource", {}).get("kind") == "agent")
    agent["resource"] = {"kind": "agent", "source_kind": "team", "id": AGENT, "version": version, "trial_candidate_id": CANDIDATE}
    body["message"] = InputDocument.model_validate(body["input_document"]).display_text()
    if session_id is not None:
        body["session_id"] = session_id
    return body


def _rows(case, table: str) -> list[dict]:
    return http_flows.rows(case, table)


def _turn_count(case) -> int:
    return len(_rows(case, "turns"))


async def _refused(case, body: dict) -> str:
    response = await case.client.post("/api/v1/chat/send", json=body)
    assert response.status_code == 409, response.text
    error = response.json()["detail"]["error"]
    assert error.get("admission") == "not_started", error
    return error["code"]


async def _send(case, body: dict) -> dict:
    response = await case.client.post("/api/v1/chat/send", json=body)
    assert response.status_code == 200, response.text
    await http_flows.settled(response.json()["turn_id"])
    return response.json()


async def test_a_trial_message_runs_the_draft_and_continues_in_its_trial_session(http_start, monkeypatch):
    case = http_start
    monkeypatch.setenv("FEATURE_AGENT_DRAFT_TRIAL_V1", "1")
    _prepare_draft(case)
    scope = _scope(case)
    with respx.mock() as mock:
        route = mock.post(url__regex=r"https://api\..*").mock(side_effect=answer_response)
        first = await _send(case, _trial_payload(case))
        assert "agent_trial_notice" not in first
        turn = await case.fixture.case.store.get_turn(first["turn_id"])
        assert turn.status == "completed", turn
        assert route.call_count == 1
        # The model received the saved draft, not the released instructions.
        assert DRAFT_PROMPT in json.dumps(json.loads(route.calls[0].request.content), ensure_ascii=False)
        second = await _send(case, _trial_payload(case, session_id=first["session_id"]))
        assert second["session_id"] == first["session_id"]
        assert (await case.fixture.case.store.get_turn(second["turn_id"])).status == "completed"

    session = next(row for row in _rows(case, "sessions") if row["id"] == first["session_id"])
    assert session["origin"] == "agent_trial"
    executions = [row for row in _rows(case, "turn_agent_execution") if row["turn_id"] in {first["turn_id"], second["turn_id"]}]
    assert {(row["revision_ref"], row["release_id"]) for row in executions} == {(DRAFT_REVISION, None)}
    trial = await read_owner_trial(case.path, turn_id=first["turn_id"])
    assert (trial.candidate_id, trial.candidate_kind, trial.owner_user_id, trial.session_id) == (
        CANDIDATE,
        "draft",
        scope.user_id,
        first["session_id"],
    )
    expected = {"candidate_id": CANDIDATE, "candidate_kind": "draft", "frozen_at": trial.frozen_at}

    # Live and persisted: every attributed event carries the same draft.
    attributed = [
        json.loads(row["payload_json"])
        for row in _rows(case, "turn_events")
        if row["turn_id"] == first["turn_id"] and "agent_execution_id" in json.loads(row["payload_json"])
    ]
    assert attributed and all(item.get("trial") == expected for item in attributed)
    # Stored Turn detail.
    detail = await case.client.get(f"/api/v1/turns/{first['turn_id']}/agent-binding")
    assert detail.status_code == 200, detail.text
    assert [item.get("trial") for item in detail.json()["executions"]] == [expected]
    assert detail.json()["root_binding"].get("agent_release_id") is None
    # Replay shows the trial receiver to its Owner.
    evidence = await case.client.get(f"/api/v1/turns/{first['turn_id']}/reference-evidence")
    assert evidence.status_code == 200, evidence.text
    assert evidence.json()["composition"]["receiver_label"] == "Exact Agent"

    # The Owner's trial list; nobody else gets one.
    trials = await list_recent_owner_trials(case.path, team_id=scope.team_id, agent_id=AGENT, owner_user_id=scope.user_id)
    assert [(item["session_id"], item["turn_count"], item["released_as"]) for item in trials] == [(first["session_id"], 2, None)]
    assert trials[0]["drafts_used"] == [{**expected, "released_as": None}]
    assert "用户原文" in trials[0]["first_question"]
    assert await list_recent_owner_trials(case.path, team_id=scope.team_id, agent_id=AGENT, owner_user_id=case.fixture.case.fixture.owner_id) == []

    # Cases from the Owner's own trial Turns; ordinary readers never see them.
    from bowhead_core.agent_eval_conversation_cases import list_conversation_candidates, read_conversation_turns

    core = str(case.path)
    with_trials = await list_conversation_candidates(core, team_id=scope.team_id, agent_id=AGENT, user_id=scope.user_id, include_owner_trials=True)
    assert {item["turn_id"] for item in with_trials} >= {first["turn_id"], second["turn_id"]}
    ordinary = await list_conversation_candidates(core, team_id=scope.team_id, agent_id=AGENT, user_id=scope.user_id)
    assert not {item["turn_id"] for item in ordinary} & {first["turn_id"], second["turn_id"]}
    owner_view = await list_conversation_candidates(
        core, team_id=scope.team_id, agent_id=AGENT, user_id=case.fixture.case.fixture.owner_id, include_owner_trials=True
    )
    assert not {item["turn_id"] for item in owner_view} & {first["turn_id"], second["turn_id"]}
    picked = await read_conversation_turns(
        core, team_id=scope.team_id, agent_id=AGENT, user_id=scope.user_id, turn_ids=[first["turn_id"]], include_owner_trials=True
    )
    assert picked[0]["turn_id"] == first["turn_id"] and picked[0]["answer"]

    # Sidebar search (TD2) and the session DTO after a reload.
    sessions = await case.fixture.case.store.list_sessions(user_id=scope.user_id, workspace_id=scope.workspace_id, q="用户原文")
    assert first["session_id"] not in {item.id for item in sessions}
    from bowhead_gateway.routes import sessions as session_routes

    case.app.include_router(session_routes.router, prefix="/api/v1/sessions")
    reloaded = await case.client.get(f"/api/v1/sessions/{first['session_id']}")
    assert reloaded.status_code == 200, reloaded.text
    assert (reloaded.json()["session"]["origin"], reloaded.json()["session"]["trial_agent_id"]) == ("agent_trial", AGENT)

    await _assert_exclusions(case, trial_turn_id=first["turn_id"], trial_session_id=first["session_id"])


async def _assert_exclusions(case, *, trial_turn_id: str, trial_session_id: str) -> None:
    scope = _scope(case)
    # /satisfaction refuses a trial Turn.
    case.app.state.user_memory_store = SimpleNamespace(upsert_operational_memory=_never_called)
    satisfaction = await case.client.post(f"/api/v1/turns/{trial_turn_id}/satisfaction", json={"score": 1})
    assert satisfaction.status_code == 409 and satisfaction.json()["detail"]["error"]["code"] == "agent_trial_feedback_unavailable"
    # Workflow solidify treats a trial as no source.
    from bowhead_gateway.routes.workflow_solidify import _session_digest

    with pytest.raises(HTTPException) as hidden:
        await _session_digest(case.fixture.case.store, None, session_id=trial_session_id, user_id=scope.user_id)
    assert hidden.value.status_code == 404
    # Scheduling from the trial conversation.
    from bowhead_gateway.routes.scheduled import _refuse_trial_anchor

    request = SimpleNamespace(app=case.app)
    with pytest.raises(HTTPException) as scheduled:
        await _refuse_trial_anchor(request, trial_session_id)
    assert scheduled.value.detail == {"code": "agent_trial_schedule_unavailable"}
    # Sharing a file produced in the trial (TD3); a file from elsewhere is unaffected.
    from bowhead_gateway.routes.files import _refuse_trial_output_share

    with pytest.raises(HTTPException) as shared:
        await _refuse_trial_output_share(request, SimpleNamespace(session_id=None, created_by_turn=trial_turn_id))
    assert shared.value.detail == {"error": {"code": "agent_trial_file_share_unavailable"}}
    await _refuse_trial_output_share(request, SimpleNamespace(session_id="s_other", created_by_turn="t_other"))
    # Resource-usage history and the operations report's token count leave trials out.
    from bowhead_core.resource_usage_projection import _team_executions
    from bowhead_core.team_operations_report import get_team_operations_report

    async with open_shared_ctx(domain_database_path(case.path, "execution")) as db:
        db.row_factory = sqlite3.Row
        assert trial_turn_id not in {row["turn_id"] for row in await _team_executions(db, team_id=scope.team_id)}
    yesterday = (datetime.now(UTC) - timedelta(days=1)).replace(hour=12).isoformat()
    with closing(connect_sync(domain_database_path(case.path, "execution"))) as db:
        for turn_id, tokens in ((trial_turn_id, 7000), (None, 11)):
            db.execute(
                "INSERT INTO llm_usage_events(id,team_id,user_id,workspace_id,turn_id,reason,agent_type,provider,model,tokens_input,"
                "tokens_output,tokens_cached,streamed,created_at) VALUES (?,?,?,?,?,'turn','lead','mock','mock',?,0,0,0,?)",
                (f"lue_{uuid4().hex}", scope.team_id, scope.user_id, scope.workspace_id, turn_id, tokens, yesterday),
            ).close()
        db.commit()
    with closing(connect_sync(str(case.path))) as db:
        db.execute("UPDATE teams SET report_timezone='UTC' WHERE id=?", (scope.team_id,)).close()
        db.commit()
    report = await get_team_operations_report(str(case.path), team_id=scope.team_id, days=7)
    assert sum(day["input_tokens"] for day in report["days"]) == 11


async def _never_called(**_kwargs):
    raise AssertionError("a trial Turn must not record satisfaction")


async def test_trial_messages_are_refused_with_their_own_codes(http_start, monkeypatch):
    case = http_start
    monkeypatch.setenv("BOWHEAD_PLANNING_MODE", "1")
    _prepare_draft(case, owner=False)
    before = _turn_count(case)
    monkeypatch.delenv("FEATURE_AGENT_DRAFT_TRIAL_V1", raising=False)
    assert await _refused(case, _trial_payload(case)) == "agent_trial_disabled"
    monkeypatch.setenv("FEATURE_AGENT_DRAFT_TRIAL_V1", "1")
    # Only the Agent's Owner tries its draft.
    assert await _refused(case, _trial_payload(case)) == "agent_trial_owner_required"
    with closing(connect_sync(str(case.path))) as db:
        db.execute("UPDATE team_subagent SET owner_user_id=?,row_version=row_version+1 WHERE id=?", (_scope(case).user_id, AGENT)).close()
        db.commit()
    assert await _refused(case, _trial_payload(case, skills=True)) == "agent_trial_extra_references"
    assert await _refused(case, {**_trial_payload(case), "planning_mode": "planning"}) == "agent_trial_plan_unavailable"
    assert await _refused(case, _trial_payload(case, version=LIVE_REVISION)) == "agent_trial_candidate_changed"
    # An ordinary session is not a trial session.
    await case.fixture.case.store.ensure_session(session_id="s_ordinary", workspace_id=_scope(case).workspace_id, user_id=_scope(case).user_id)
    assert await _refused(case, _trial_payload(case, session_id="s_ordinary")) == "agent_trial_session_mismatch"
    with closing(connect_sync(str(case.path))) as db:
        db.execute("UPDATE team_subagent_release_candidate SET state='withdrawn',row_version=row_version+1 WHERE id=?", (CANDIDATE,)).close()
        db.commit()
    assert await _refused(case, _trial_payload(case)) == "agent_trial_candidate_not_prepared"
    # Nothing was admitted for any refused message.
    assert _turn_count(case) == before
    assert not _rows(case, "agent_owner_trial_turn")


async def test_a_trial_session_takes_only_trial_messages(http_start, monkeypatch):
    case = http_start
    monkeypatch.setenv("FEATURE_AGENT_DRAFT_TRIAL_V1", "1")
    _prepare_draft(case)
    with respx.mock() as mock:
        mock.post(url__regex=r"https://api\..*").mock(side_effect=answer_response)
        first = await _send(case, _trial_payload(case))
    steer = _trial_payload(case, session_id=first["session_id"])
    steer.update(submission_kind="steer", expected_parent_turn_id=first["turn_id"], expected_execution_generation=1)
    assert await _refused(case, steer) == "agent_trial_steer_unavailable"
    plain = http_flows.payload(case, skills=False)
    plain["session_id"] = first["session_id"]
    assert await _refused(case, plain) == "agent_trial_reference_required"
    assert len([row for row in _rows(case, "turns") if row["session_id"] == first["session_id"]]) == 1

    # A queued follow-up is never re-prepared against the released version.
    from bowhead_core.chat_reference_storage import QueuedReferenceInput

    from bowhead_gateway import turn_service

    document = _trial_payload(case)["input_document"]
    queued = QueuedReferenceInput.model_validate(
        {"schema_version": 1, "input_document": document, "attachment_ids": [], "attachment_refs": [], "automatic_receiver": None}
    )

    async def head(**_kwargs):
        return "sub_queued", queued, 1

    monkeypatch.setattr(case.fixture.case.store, "read_reference_steer_head", head)
    orchestrator = case.app.state.orchestrator
    with pytest.raises(ValueError, match="agent_trial_steer_unavailable"):
        await turn_service.materialize_reference_steer_head(
            orchestrator,
            session_id=first["session_id"],
            current_turn_id=first["turn_id"],
            next_turn_id="t_next",
            team_id=_scope(case).team_id,
            user_id=_scope(case).user_id,
            workspace_id=_scope(case).workspace_id,
        )
