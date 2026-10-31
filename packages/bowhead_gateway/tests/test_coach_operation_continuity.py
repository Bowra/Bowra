"""Owner adoption followed by an ordinary no-ID Coach result request."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from httpx import AsyncClient

import pytest
from bowhead_core.agent_coach import AGENT_NAME, prepare_agent_coach, register_agent_coach_tools
from bowhead_core.agents._lead_context import reset_current_peer_name, set_current_peer_name
from bowhead_core.chat_reference_scope import ReferenceRequestScope
from bowhead_core.coach_operation_history import receipt_metadata, render_history
from bowhead_core.coach_operation_reference import with_operation_reference
from bowhead_core.events import TurnEventBus
from bowhead_core.session_store import SessionStore
from bowhead_core.tool_registry import ToolExecutionContext, build_default_registry
from bowhead_core.turn_log_store import TurnLogStore
from bowhead_core.types import ToolInvocation
from test_agent_coach_evaluations import app_client as app_client
from test_agent_coach_evaluations import case_spec, setup

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("intervening_read", [False, True])
async def test_adopted_original_is_read_and_opened_from_application_history(
    app_client: tuple[AsyncClient, Path], monkeypatch: pytest.MonkeyPatch, intervening_read: bool
) -> None:
    client, path = app_client
    evaluations = await setup(client, path, monkeypatch)
    sessions = SessionStore(path)
    await sessions.ensure_session(session_id="s_continuity", user_id="user_1", workspace_id="ws_1")
    await sessions.record_turn(turn_id="t_saved", session_id="s_continuity", user_text="Add one missing-input test, without running it.")
    resources = replace(evaluations.resources, session_id="s_continuity")
    evaluations = resources.evaluations
    response = await client.post(
        "/api/v1/agents/osa_1/eval-suites",
        json={"name": "Quality", "cases": [{**case_spec(), "case_key": "existing", "task": "Explain supplied facts."}]},
    )
    assert response.status_code == 200, response.text
    target = (await evaluations.suites(agent_id="osa_1", offset=0, limit=10))["items"][0]["target"]
    proposed = with_operation_reference(
        "agent_eval_case_propose",
        await evaluations.propose(
            target=target,
            cases=[{"id": "missing", "task": "Explain missing inputs.", "failure_mode": "Invented facts", "case_spec": case_spec()}],
            rationale="Cover missing data.",
            request_id="original",
            turn_id="t_saved",
        ),
    )
    original = proposed["proposal"]
    log = TurnLogStore(path)
    await log.record(turn_id="t_saved", event_type="agent_spawned", payload={"profile": AGENT_NAME, "subagent_id": "sub_saved"})
    meta = receipt_metadata([ToolInvocation("agent_eval_case_propose", {}, call_id="call_saved", result=proposed)])
    await log.record(
        turn_id="t_saved",
        event_type="tool_result",
        payload={
            "subagent_id": "sub_saved",
            "parent_agent_id": "sub_saved",
            "call_id": "call_saved",
            "tool_name": "agent_eval_case_propose",
            "result_inline": None,
            "result_meta": {"coach_operations": meta.model_dump(mode="json")},
        },
    )
    await sessions.mark_completed(
        turn_id="t_saved", intent=None, final_answer="The suggestion is ready for review.", tokens_input=0, tokens_output=0, cost_usd=0
    )
    await sessions.finalize_turn(session_id="s_continuity", current_turn_id="t_saved", next_turn_id=None)
    adopted = await client.post(
        f"/api/v1/agents/osa_1/change-proposals/{original['id']}/suggested-cases/accept",
        json={
            "expected_proposal_row_version": original["row_version"],
            "expected_active_suite_revision_id": target["revision_id"],
            "cases": [{"suggested_case_id": "missing", "edited_case": {**case_spec(), "task": "Explain missing inputs."}}],
        },
    )
    assert adopted.status_code == 200, adopted.text
    if intervening_read:
        await sessions.record_turn(turn_id="t_inspect", session_id="s_continuity", user_text="Read the suite only.")
        await log.record(turn_id="t_inspect", event_type="agent_spawned", payload={"profile": AGENT_NAME, "subagent_id": "sub_read"})
        await sessions.mark_completed(turn_id="t_inspect", intent=None, final_answer="Read only.", tokens_input=0, tokens_output=0, cost_usd=0)
        await sessions.finalize_turn(session_id="s_continuity", current_turn_id="t_inspect", next_turn_id=None)
    request = "Open the operation we just accepted; do not write or run anything."
    assert original["id"] not in request
    await sessions.record_turn(turn_id="t_reopen", session_id="s_continuity", user_text=request)
    scope = ReferenceRequestScope(user_id="user_1", team_id="team_1", workspace_id="ws_1", session_id="s_continuity")
    history = await sessions.load_coach_operation_history(scope, "t_reopen")
    ctx = ToolExecutionContext(
        user_id="user_1",
        team_id="team_1",
        workspace_id="ws_1",
        session_id="s_continuity",
        turn_id="t_reopen",
        role="lead",
        member_uid="m",
        datarc=None,
        agent_coach_resources=resources,
        coach_operation_history=history,
        parent_bus=TurnEventBus(),
    )
    # A controlled provider selects only from the actual application-produced block.
    block = render_history(ctx)
    body = json.loads(block[block.index("{") : block.rindex("}") + 1])
    chosen = body["entries"][0]["operation_ref"]
    assert chosen == proposed["operation_ref"]
    with sqlite3.connect(path) as db:
        before = tuple(
            db.execute(
                "SELECT (SELECT count(*) FROM agent_change_proposal),(SELECT count(*) FROM agent_eval_run),(SELECT count(*) FROM team_subagent_working_copy)"
            ).fetchone()
        )
    child, _ = await prepare_agent_coach(ctx)
    assert child is not None
    registry = build_default_registry()
    register_agent_coach_tools(registry)
    token = set_current_peer_name(AGENT_NAME)
    try:
        read = await registry.get("agent_operation_get").handler({"reference": chosen}, child)
        opened = await registry.get("agent_operation_open").handler({"reference": chosen}, child)
    finally:
        reset_current_peer_name(token)
    assert read["domain_result"]["proposal"]["id"] == original["id"]
    assert read["domain_result"]["proposal"]["status"] == "accepted"
    assert opened["view_delivery"] == "event_emitted"
    assert not read["effects_applied_by_this_read"]
    with sqlite3.connect(path) as db:
        after = tuple(
            db.execute(
                "SELECT (SELECT count(*) FROM agent_change_proposal),(SELECT count(*) FROM agent_eval_run),(SELECT count(*) FROM team_subagent_working_copy)"
            ).fetchone()
        )
    assert after == before
    # The retained pointer grants no current Owner authority after revocation.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE team_subagent SET owner_user_id='owner_1' WHERE id='osa_1'")
    token = set_current_peer_name(AGENT_NAME)
    try:
        denied = await registry.get("agent_operation_get").handler({"reference": chosen}, child)
    finally:
        reset_current_peer_name(token)
    assert denied["ok"] is False
    assert "proposal" not in denied
