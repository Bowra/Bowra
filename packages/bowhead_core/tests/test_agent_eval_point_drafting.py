"""Coach drafts expected points and the Owner confirms them (evaluation credibility slice 4)."""

from __future__ import annotations

import itertools
import sqlite3
from pathlib import Path

import pytest
from bowhead_core.agent_change_proposal_store import AgentChangeProposalError
from bowhead_core.agent_eval_case_proposals import validate_case_suggestions
from bowhead_core.agent_eval_points import COACH_POINT_DRAFTING_RULES, drafted_points, validate_drafted_points
from bowhead_core.agent_eval_product import AgentEvalProductStore
from bowhead_core.agent_eval_store import AgentEvalStoreError
from bowhead_core.coach_drafting import _CHARTER_AGENT_DEFINITION_V2
from test_agent_eval_policy_plan_store import _seed, _upgrade
from test_agent_eval_run_insights import _complete, _start


def _spec(points: list[dict]) -> dict:
    return {
        "case_key": "quality.1",
        "fixture_ref": {},
        "expected_contract": {"points": points},
        "scorer_config": {},
        "effect_policy": {"mode": "no_effects"},
        "valid_until": None,
    }


def _ids():
    counter = itertools.count(1)
    return lambda: f"p_{next(counter):04x}"


def test_drafted_points_are_suggested_bounded_and_need_a_real_tool() -> None:
    raw = [
        {"kind": "judgment", "text": "  Compares with   the same month last year "},
        {"kind": "judgment", "text": "compares with the same month last year"},
        {"kind": "required_action", "text": "Queries sales data", "tool": "bi_query"},
        {"kind": "required_action", "text": "Searches the web", "tool": "invented_tool"},
        {"kind": "required_action", "text": "No tool named"},
        {"kind": "data_fact", "text": "States East China revenue for last quarter"},
        {"kind": "opinion", "text": "Unknown kind"},
        {"kind": "judgment", "text": ""},
        "not a point",
        *({"kind": "judgment", "text": f"Extra point {index}"} for index in range(10)),
    ]

    points = drafted_points(raw, source="coach_task", allowed_tools=["bi_query"], id_factory=_ids())

    assert len(points) == 6
    assert points[0] == {
        "id": "p_0001",
        "kind": "judgment",
        "text": "Compares with the same month last year",
        "source": "coach_task",
        "state": "suggested",
    }
    assert points[1]["action"] == {"tool": "bi_query"}
    assert [point["kind"] for point in points[:3]] == ["judgment", "required_action", "data_fact"]
    assert {point["state"] for point in points} == {"suggested"}
    assert drafted_points("not a list", source="coach_task", allowed_tools=[], id_factory=_ids()) == []


@pytest.mark.parametrize(
    "points",
    [
        [{"id": "p_0001", "kind": "judgment", "text": "Owner wrote this", "source": "owner", "state": "suggested"}],
        [{"id": "p_0001", "kind": "judgment", "text": "Already confirmed", "source": "coach_task", "state": "confirmed"}],
        [{"id": f"p_{index:04x}", "kind": "judgment", "text": f"Point {index}", "source": "coach_task", "state": "suggested"} for index in range(7)],
    ],
)
def test_coach_can_only_suggest_points(points) -> None:
    with pytest.raises(ValueError):
        validate_drafted_points({"points": points})
    case = {
        "id": "case_1",
        "task": "How did revenue change?",
        "failure_mode": "Misses the comparison",
        "case_spec": _spec(points),
    }
    with pytest.raises(AgentChangeProposalError, match="agent_change_suggested_case_invalid"):
        validate_case_suggestions([case])


def test_a_coach_case_with_suggested_points_is_accepted() -> None:
    point = {"id": "p_00a1", "kind": "judgment", "text": "Compares with last year", "source": "coach_live_answer", "state": "suggested"}
    [case] = validate_case_suggestions(
        [
            {
                "id": "case_1",
                "task": "How did revenue change?",
                "failure_mode": "Misses the comparison",
                "case_spec": _spec([point]),
            }
        ]
    )
    assert case["case_spec"]["expected_contract"]["points"] == [point]


def _case(**changes) -> dict:
    return {"id": "case_1", "task": "How did revenue change?", "failure_mode": "Misses the comparison", "case_spec": _spec([]), **changes}


@pytest.mark.parametrize(
    "cases,rule,facts",
    [
        ([], "case_count", {"case_count": 0}),
        ([{**_case(), "extra": 1}], "case_fields", {"case_index": 0, "case_id": "case_1", "unexpected_fields": ["extra"]}),
        ([_case(), _case()], "duplicate_case_id", {"case_index": 1, "case_id": "case_1", "field": "id"}),
        ([_case(task="  ")], "task_invalid", {"case_id": "case_1", "field": "task"}),
        ([_case(failure_mode="x" * 2001)], "failure_mode_invalid", {"case_id": "case_1", "field": "failure_mode"}),
        ([_case(case_spec={**_spec([]), "notes": "x"})], "case_spec_fields", {"case_id": "case_1", "unexpected_fields": ["notes"]}),
        ([_case(case_spec={**_spec([]), "case_key": "has space"})], "case_spec_invalid", {"case_id": "case_1", "field": "case_spec"}),
        (
            [_case(case_spec={**_spec([]), "expected_contract": {"held_out": True}})],
            "case_spec_invalid",
            {"case_id": "case_1", "field": "case_spec"},
        ),
    ],
)
def test_invalid_case_suggestions_name_the_rule_case_and_field(cases, rule, facts) -> None:
    with pytest.raises(AgentChangeProposalError, match="agent_change_suggested_case_invalid") as rejected:
        validate_case_suggestions(cases)
    details = rejected.value.details
    assert details["rule"] == rule
    assert {key: details.get(key) for key in facts} == facts
    assert "Each suggested case is" in details["message"]


def test_coach_drafting_prompt_carries_the_point_rules() -> None:
    assert COACH_POINT_DRAFTING_RULES in _CHARTER_AGENT_DEFINITION_V2
    assert "{point_rules}" not in _CHARTER_AGENT_DEFINITION_V2


def _set_task(db_path: Path, case_revision_id: str, task: str) -> None:
    # Fixture cases are seeded without a task. Case revisions are immutable, so lift the guard just
    # long enough to give this one a task, then restore the exact trigger.
    with sqlite3.connect(db_path) as db:
        [trigger_sql] = db.execute("SELECT sql FROM sqlite_master WHERE name='trg_agent_eval_case_immutable_update'").fetchone()
        db.execute("DROP TRIGGER trg_agent_eval_case_immutable_update")
        db.execute("UPDATE agent_eval_case_revision SET input_json=? WHERE id=?", (f'{{"task":"{task}"}}', case_revision_id))
        db.execute(trigger_sql)


async def test_the_live_answer_is_the_newest_live_attempt_at_exactly_this_task(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    _plan, comparison, started = await _start(db_path)
    store = AgentEvalProductStore(str(db_path))
    args = {"team_id": "team_p", "agent_id": "osa_policy", "actor_user_id": "user_owner"}

    await _complete(db_path, comparison, started, candidate_status="failed")
    assert await store.latest_live_answer(**args, task="How did revenue change?") is None
    _set_task(db_path, "aec_aes_owner", "How did revenue change?")
    answer = await store.latest_live_answer(**args, task="How did revenue change?")

    # The Live (baseline) side answered; the candidate's answer is never offered as the Live one.
    assert answer is not None
    assert answer["answer"] == "Baseline answer" and answer["answer_truncated"] is False
    assert answer["case_revision_id"] == "aec_aes_owner" and answer["attempt"] == 3
    # Mark as good answer stores this reference, so it must be the attempt form calibration accepts.
    assert answer["answer_ref"] == f"attempt:{answer['run_id']}:aec_aes_owner:3"
    assert await store.latest_live_answer(**args, task="A different question") is None
    with pytest.raises(AgentEvalStoreError, match="agent_eval_action_denied"):
        await store.latest_live_answer(**(args | {"actor_user_id": "user_other"}), task="How did revenue change?")
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE team_subagent SET live_release_id=NULL WHERE id='osa_policy'")
    assert await store.latest_live_answer(**args, task="How did revenue change?") is None
