"""FEATURE_AGENT_EVAL_ALIGNMENT switched off (`0`): the loop's Owner routes, review routes and loop variants are gone.

Held-out redaction, Turn readability rechecks and share-withdrawal deletes are not switched; their tests run
without the flag.
"""

from __future__ import annotations

import pytest
from bowhead_core.agent_change_proposal_store import AgentChangeProposalError
from bowhead_gateway.routes import auth
from test_agent_coach_evaluations import case_spec, setup
from test_agent_eval_alignment_routes import _loop_with_card, _suite
from test_agents_routes import app_client as app_client

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _alignment_switched_off(monkeypatch):
    monkeypatch.setenv("FEATURE_AGENT_EVAL_ALIGNMENT", "0")


async def test_every_alignment_review_and_calibration_route_is_404_when_switched_off(app_client, monkeypatch) -> None:
    client, db_path = app_client
    evaluations = await setup(client, db_path, monkeypatch)
    await _suite(client)
    loop, point, _stored = await _loop_with_card(db_path)
    target = (await evaluations.suites(agent_id="osa_1", offset=0, limit=10))["items"][0]["target"]
    base = f"/api/v1/agents/osa_1/eval-alignments/{loop.id}"
    requests = (
        ("get", base, None),
        ("post", f"{base}/labels", {"labels": [], "rest_right": True, "card_seq": 1}),
        ("patch", f"{base}/points/{point['id']}", {"text": "Names the month"}),
        ("post", f"{base}/repin", None),
        ("post", "/api/v1/agents/osa_1/eval-points/review", {"task": "q", "points": [{"id": "p_0001", "kind": "judgment", "text": "x"}]}),
        ("post", f"/api/v1/agents/osa_1/eval-suites/{target['suite_id']}/review", {"suite_revision_id": target["revision_id"]}),
        ("get", f"/api/v1/agents/osa_1/eval-suites/{target['suite_id']}/points/{point['id']}/calibration", None),
    )
    for method, path, body in requests:
        response = await client.request(method.upper(), path, json=body)
        assert response.status_code == 404, (path, response.text)
    # Nothing was labelled or reworded behind the 404.
    monkeypatch.setenv("FEATURE_AGENT_EVAL_ALIGNMENT", "1")
    read = (await client.get(base)).json()
    assert read["points"][0]["text"] == point["text"] and all(row["label"] is None for row in read["questions"])


async def test_a_case_proposal_with_a_loop_link_is_refused_when_switched_off(app_client, monkeypatch) -> None:
    client, db_path = app_client
    evaluations = await setup(client, db_path, monkeypatch)
    await _suite(client, held_out=False)
    loop, point, _stored = await _loop_with_card(db_path)
    target = (await evaluations.suites(agent_id="osa_1", offset=0, limit=10))["items"][0]["target"]
    variants = [{"id": "v1", "task": "Sales two months ago?", "failure_mode": "No period", "case_spec": {**case_spec(), "case_key": "variant.1"}}]
    with pytest.raises(AgentChangeProposalError) as error:
        await evaluations.propose(
            target=target,
            cases=variants,
            rationale="",
            request_id="variants",
            turn_id="turn_owner",
            alignment={"loop_id": loop.id, "point_id": point["id"]},
        )
    assert error.value.code == "agent_eval_alignment_unavailable"
    # Without the link, case proposals work as before.
    plain = await evaluations.propose(target=target, cases=variants, rationale="", request_id="plain", turn_id="turn_owner")
    assert plain["ok"]


@pytest.mark.parametrize(
    ("value", "expected"), [(None, True), ("1", True), ("true", True), ("0", False), ("false", False), ("off", False), ("no", False)]
)
def test_the_browser_learns_the_switch_from_me_capabilities(monkeypatch, value, expected) -> None:
    if value is None:
        monkeypatch.delenv("FEATURE_AGENT_EVAL_ALIGNMENT", raising=False)
    else:
        monkeypatch.setenv("FEATURE_AGENT_EVAL_ALIGNMENT", value)
    assert auth._agent_eval_alignment_feature_on() is expected
