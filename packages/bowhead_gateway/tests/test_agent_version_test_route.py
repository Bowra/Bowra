"""HTTP boundary of "Test this change": one Owner action from the saved draft to a running release comparison."""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock

import pytest
from bowhead_core.agent_eval_service import AgentEvalServiceError
from bowhead_gateway.routes import agents
from test_agents_routes import _enable_revisioned_agent, _PassingSemanticScanner
from test_agents_routes import app_client as app_client

pytestmark = pytest.mark.unit

_TESTS = "/api/v1/agents/osa_1/tests"
_HASH = "sha256:" + "6" * 64


def _seed_space(db_path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO workspaces(id,team_id,name,status,created_at,updated_at) VALUES('ws_eval','team_1','Evaluation','active',?,?)",
            ("2026-10-10T00:00:00+00:00", "2026-10-10T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO workspace_members(workspace_id,user_id,role,status,joined_at) VALUES('ws_eval','user_1','admin','active',?)",
            ("2026-10-10T00:00:00+00:00",),
        )


def _stub_evaluation(monkeypatch) -> tuple[AsyncMock, object]:
    """The evaluation runtime and model routes are outside this boundary; scheduling is recorded."""
    monkeypatch.setattr(agents, "RouterAgentSemanticSafetyScanner", _PassingSemanticScanner)
    monkeypatch.setattr(agents, "_require_eval_runtime", lambda _request: object())

    async def route_hashes(*, db_path, candidate_id, **_kwargs):
        # One hash per revision the comparison runs: the released version and the candidate.
        with sqlite3.connect(db_path) as connection:
            revisions = connection.execute(
                "SELECT s.live_revision_id,c.revision_id FROM team_subagent_release_candidate c "
                "JOIN team_subagent s ON s.id=c.subagent_id WHERE c.id=?",
                (candidate_id,),
            ).fetchone()
        return dict.fromkeys(revisions, _HASH), None

    hashes = AsyncMock(side_effect=route_hashes)
    monkeypatch.setattr(agents, "_comparison_model_hashes", hashes)
    # The preflight resolves the Team's models through the runtime; these tests stub the runtime itself.
    monkeypatch.setattr(agents, "_require_eval_subject_models", AsyncMock())
    monkeypatch.setattr(agents, "_schedule_comparison", AsyncMock())
    return hashes, route_hashes


async def _save_draft(client, prompt: str, *, audience: str = "private") -> dict:
    current = (await client.get("/api/v1/agents/osa_1/version-workbench")).json()["next_draft_identity"]
    saved = await client.patch(
        "/api/v1/agents/osa_1/working-copy",
        json={
            "definition": {
                "max_iterations": 8,
                "model": None,
                "output_schema": None,
                "skill_grants": [],
                "system_prompt": prompt,
                "tool_refs": [],
            },
            "listing": {"description": "Monthly finance analysis", "display_name": "Finance Helper"},
            "audience": {"schema_version": 1, "effective_mode": audience, "user_ids": [], "group_ids": []},
            "expected_row_version": current["row_version"] if current else None,
        },
    )
    assert saved.status_code == 200, saved.text
    return saved.json()


def _candidates(db_path) -> list[tuple[str, str]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute("SELECT id,state FROM team_subagent_release_candidate WHERE subagent_id='osa_1' ORDER BY rowid").fetchall()


def _executions(db_path) -> list[str]:
    with sqlite3.connect(db_path) as connection:
        return [row[0] for row in connection.execute("SELECT id FROM agent_eval_comparison_execution ORDER BY rowid")]


async def _workbench(client) -> dict:
    return (await client.get("/api/v1/agents/osa_1/version-workbench")).json()


@pytest.mark.asyncio
async def test_one_press_prepares_the_saved_draft_and_starts_the_comparison(app_client, monkeypatch) -> None:
    client, db_path = app_client
    _enable_revisioned_agent(db_path)
    _seed_space(db_path)
    _stub_evaluation(monkeypatch)
    await _save_draft(client, "Answer with the monthly number and its share.")

    started = await client.post(_TESTS, json={"workspace_id": "ws_eval"}, headers={"Idempotency-Key": "test-1"})

    assert started.status_code == 202, started.text
    payload = started.json()
    assert payload["kind"] == "agent_version_test" and payload["step"] == "started"
    candidate_id = payload["preparation"]["candidate_id"]
    assert payload["comparison"]["candidate_id"] == candidate_id
    assert _candidates(db_path) == [(candidate_id, "prepared")]
    after = await _workbench(client)
    # No prepared-but-untested state is left: the comparison is already the workbench's state.
    assert after["candidate_identity"]["candidate_id"] == candidate_id
    assert after["active_execution_summary"]["status"] in {"queued", "running"}
    assert after["primary_action"]["kind"] == "view_comparison"
    assert after["active_execution_summary"]["origin_kind"] == "user"

    # Resending the same request recovers the same run; nothing is prepared or started again.
    replay = await client.post(_TESTS, json={"workspace_id": "ws_eval"}, headers={"Idempotency-Key": "test-1"})
    assert replay.status_code == 202, replay.text
    assert replay.json()["step"] == "running"
    assert replay.json()["comparison"]["execution_id"] == payload["comparison"]["execution_id"]
    assert _candidates(db_path) == [(candidate_id, "prepared")]
    assert len(_executions(db_path)) == 1

    # Pressing again while it runs starts nothing.
    again = await client.post(_TESTS, json={"workspace_id": "ws_eval"}, headers={"Idempotency-Key": "test-2"})
    assert again.status_code == 202 and again.json()["step"] == "running"
    assert len(_executions(db_path)) == 1


@pytest.mark.asyncio
async def test_no_usable_model_refuses_before_anything_is_prepared(app_client, monkeypatch) -> None:
    client, db_path = app_client
    _enable_revisioned_agent(db_path)
    _seed_space(db_path)
    hashes, _route_hashes = _stub_evaluation(monkeypatch)
    preflight = AsyncMock(
        side_effect=AgentEvalServiceError("agent_eval_model_unavailable", details={"model_role": "analyst", "model_reason": "role_unassigned"})
    )
    monkeypatch.setattr(agents, "_require_eval_subject_models", preflight)
    await _save_draft(client, "Answer with the monthly number and its share.")

    refused = await client.post(_TESTS, json={"workspace_id": "ws_eval"}, headers={"Idempotency-Key": "test-no-model"})

    # The role without a model and why, so the page names what to set; nothing is prepared or started.
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["error"] == {
        "code": "agent_eval_model_unavailable",
        "details": {"model_role": "analyst", "model_reason": "role_unassigned"},
    }
    assert _candidates(db_path) == [] and _executions(db_path) == []
    hashes.assert_not_called()


@pytest.mark.asyncio
async def test_a_failed_comparison_start_is_finished_by_the_next_press_without_another_candidate(app_client, monkeypatch) -> None:
    client, db_path = app_client
    _enable_revisioned_agent(db_path)
    _seed_space(db_path)
    hashes, route_hashes = _stub_evaluation(monkeypatch)
    await _save_draft(client, "Answer with the monthly number and its share.")
    # The team has no model the comparison can run with: an evaluation service condition, not a crash.
    hashes.side_effect = AgentEvalServiceError("team_model_unconfigured")

    failed = await client.post(_TESTS, json={"workspace_id": "ws_eval"}, headers={"Idempotency-Key": "test-fail"})

    # The specific condition comes back, so the page can say what to fix.
    assert failed.status_code == 422, failed.text
    assert failed.json()["detail"]["error"]["code"] == "team_model_unconfigured"
    [(candidate_id, state)] = _candidates(db_path)
    assert state == "prepared" and _executions(db_path) == []

    hashes.side_effect = route_hashes
    retried = await client.post(_TESTS, json={"workspace_id": "ws_eval"}, headers={"Idempotency-Key": "test-retry"})

    assert retried.status_code == 202, retried.text
    assert retried.json()["step"] == "started"
    assert retried.json()["preparation"] is None
    assert retried.json()["comparison"]["candidate_id"] == candidate_id
    assert _candidates(db_path) == [(candidate_id, "prepared")]
    assert len(_executions(db_path)) == 1


@pytest.mark.asyncio
async def test_a_draft_saved_after_the_test_replaces_the_candidate_in_one_step(app_client, monkeypatch) -> None:
    client, db_path = app_client
    _enable_revisioned_agent(db_path)
    _seed_space(db_path)
    _stub_evaluation(monkeypatch)
    await _save_draft(client, "Answer with the monthly number.")
    first = await client.post(_TESTS, json={"workspace_id": "ws_eval"}, headers={"Idempotency-Key": "test-first"})
    assert first.status_code == 202, first.text
    old_candidate = first.json()["preparation"]["candidate_id"]
    execution = first.json()["comparison"]
    cancelled = await client.post(
        f"/api/v1/agents/osa_1/release-candidates/{old_candidate}/comparisons/{execution['execution_id']}/cancel",
        json={"expected_execution_row_version": execution["row_version"]},
    )
    assert cancelled.status_code == 200, cancelled.text
    await _save_draft(client, "Answer with the monthly number and its share.")
    assert (await _workbench(client))["draft_ahead_of_candidate"] is True

    retested = await client.post(_TESTS, json={"workspace_id": "ws_eval"}, headers={"Idempotency-Key": "test-second"})

    assert retested.status_code == 202, retested.text
    new_candidate = retested.json()["preparation"]["candidate_id"]
    assert new_candidate != old_candidate
    assert dict(_candidates(db_path)) == {old_candidate: "withdrawn", new_candidate: "prepared"}
    assert retested.json()["comparison"]["candidate_id"] == new_candidate


@pytest.mark.asyncio
async def test_nothing_to_test_and_unavailable_evaluation_are_refused_before_anything_is_prepared(app_client, monkeypatch) -> None:
    client, db_path = app_client
    _enable_revisioned_agent(db_path)
    _seed_space(db_path)

    unavailable = await client.post(_TESTS, json={"workspace_id": "ws_eval"}, headers={"Idempotency-Key": "test-off"})
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["error"]["code"] == "agent_eval_unavailable"

    _stub_evaluation(monkeypatch)
    no_draft = await client.post(_TESTS, json={"workspace_id": "ws_eval"}, headers={"Idempotency-Key": "test-empty"})
    assert no_draft.status_code == 409
    assert no_draft.json()["detail"]["error"]["code"] == "agent_version_test_no_draft"
    assert _candidates(db_path) == []


def test_the_step_rule_follows_the_workbench() -> None:
    prepared = {"candidate_id": "arc_1", "state": "prepared", "row_version": 1, "candidate_hash": _HASH}
    base = {
        "candidate_identity": prepared,
        "active_execution_summary": None,
        "draft_rebase_state": "current",
        "display_state": "prepared",
        "next_draft_identity": {"id": "awc_1", "row_version": 2},
        "readiness": "missing",
        "evaluation_plan_behind": False,
        "draft_ahead_of_candidate": False,
    }
    assert agents.agent_version_test_step(base) == "reuse"
    assert agents.agent_version_test_step({**base, "draft_ahead_of_candidate": True}) == "replace"
    assert agents.agent_version_test_step({**base, "display_state": "candidate_stale"}) == "prepare"
    assert agents.agent_version_test_step({**base, "candidate_identity": None}) == "prepare"
    assert agents.agent_version_test_step({**base, "readiness": "ready"}) == "current"
    assert agents.agent_version_test_step({**base, "readiness": "ready", "evaluation_plan_behind": True}) == "reuse"
    assert agents.agent_version_test_step({**base, "readiness": "unknown"}) == "reuse"
    assert agents.agent_version_test_step({**base, "active_execution_summary": {"status": "running"}}) == "running"
    for state, code in (("pending", "agent_version_test_in_review"),):
        with pytest.raises(agents.AgentVersionError) as refused:
            agents.agent_version_test_step({**base, "candidate_identity": {**prepared, "state": state}})
        assert refused.value.code == code
    with pytest.raises(agents.AgentVersionError) as rebase:
        agents.agent_version_test_step({**base, "draft_rebase_state": "required"})
    assert rebase.value.code == "agent_version_test_rebase_required"
    with pytest.raises(agents.AgentVersionError) as empty:
        agents.agent_version_test_step({**base, "candidate_identity": None, "next_draft_identity": None})
    assert empty.value.code == "agent_version_test_no_draft"


_RELEASES = "/api/v1/agents/osa_1/releases"


def _release_body(workbench: dict, state: str, **extra) -> dict:
    draft = workbench["next_draft_identity"]
    return {
        "workspace_id": "ws_eval",
        "working_copy_id": draft["id"] if draft else None,
        "working_copy_row_version": draft["row_version"] if draft else None,
        "expected_test_state": state,
        **extra,
    }


def _release_test(db_path, release_id: str) -> dict:
    import json

    with sqlite3.connect(db_path) as connection:
        return json.loads(connection.execute("SELECT eval_evidence_json FROM team_subagent_release WHERE id=?", (release_id,)).fetchone()[0])[
            "release_test"
        ]


@pytest.mark.asyncio
async def test_a_private_agent_releases_its_untested_draft_and_records_that(app_client, monkeypatch) -> None:
    client, db_path = app_client
    _enable_revisioned_agent(db_path)
    _seed_space(db_path)
    _stub_evaluation(monkeypatch)
    await _save_draft(client, "Answer with the monthly number and its share.")
    workbench = await _workbench(client)
    offered = workbench["private_release"]
    assert offered["available"] is True and offered["test_state"] == "not_tested" and offered["warning"] is False
    assert offered["candidate_covers_draft"] is False

    # The dialog showed an earlier state: refused with the current one, nothing prepared.
    changed = await client.post(_RELEASES, json=_release_body(workbench, "passed"), headers={"Idempotency-Key": "rel-0"})
    assert changed.status_code == 409, changed.text
    assert changed.json()["detail"]["error"]["code"] == "agent_release_test_state_changed"
    # A newer save than the dialog showed is refused too.
    stale = await client.post(
        _RELEASES, json={**_release_body(workbench, "not_tested"), "working_copy_row_version": 99}, headers={"Idempotency-Key": "rel-1"}
    )
    assert stale.status_code == 409 and stale.json()["detail"]["error"]["code"] == "agent_release_draft_changed"

    released = await client.post(
        _RELEASES, json=_release_body(workbench, "not_tested", release_note="Adds the share."), headers={"Idempotency-Key": "rel-2"}
    )

    assert released.status_code == 200, released.text
    payload = released.json()
    assert payload["state"] == "activated"
    assert _release_test(db_path, payload["release_id"]) == {"reason_codes": [], "state": "not_tested"}
    history = (await client.get("/api/v1/agents/osa_1/releases")).json()
    items = history["items"] if isinstance(history, dict) else history
    assert items[0]["release_test"] == {"reason_codes": [], "state": "not_tested"}
    assert items[0]["release_note"] == "Adds the share."


@pytest.mark.asyncio
async def test_releasing_waits_for_a_running_test_and_shared_agents_keep_the_review_flow(app_client, monkeypatch) -> None:
    client, db_path = app_client
    _enable_revisioned_agent(db_path)
    _seed_space(db_path)
    _stub_evaluation(monkeypatch)
    await _save_draft(client, "Answer with the monthly number and its share.")
    started = await client.post(_TESTS, json={"workspace_id": "ws_eval"}, headers={"Idempotency-Key": "test-run"})
    assert started.status_code == 202, started.text
    workbench = await _workbench(client)
    assert workbench["private_release"]["available"] is False
    assert workbench["private_release"]["blocked_reason_code"] == "agent_release_test_running"

    running = await client.post(_RELEASES, json=_release_body(workbench, "not_tested"), headers={"Idempotency-Key": "rel-run"})
    assert running.status_code == 423, running.text
    assert running.json()["detail"]["error"]["code"] == "agent_release_test_running"

    execution = started.json()["comparison"]
    candidate_id = started.json()["preparation"]["candidate_id"]
    await client.post(
        f"/api/v1/agents/osa_1/release-candidates/{candidate_id}/comparisons/{execution['execution_id']}/cancel",
        json={"expected_execution_row_version": execution["row_version"]},
    )
    await _save_draft(client, "Answer with the monthly number and its share.", audience="teamwide")
    shared = await _workbench(client)
    assert shared["private_release"] is None
    refused = await client.post(_RELEASES, json=_release_body(shared, "not_tested"), headers={"Idempotency-Key": "rel-shared"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"]["code"] == "agent_release_not_private"
