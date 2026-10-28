"""The alignment-loop report (technical design §7–§8): before → after under the loop's pinned judge.

Both sides of one comparison are measured; held-out cases appear only as counts; a check graded
under a different judge doesn't count and says so (A7, R13).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_eval_alignment import AlignmentLoop, build_report_conn
from bowhead_core.agent_eval_judge import POINTS_PROMPT_VERSION
from test_agent_eval_policy_plan_store import _seed, _upgrade
from test_agent_eval_run_insights import _complete, _start

pytestmark = pytest.mark.integration

PIN = "sha256:" + "a" * 64
POINT = {"id": "p_aaaa1111", "kind": "judgment", "text": "States the period", "source": "coach_alignment", "state": "confirmed"}


def _loop(*, fingerprint: str = PIN) -> AlignmentLoop:
    return AlignmentLoop(
        id="aeal_" + "1" * 32,
        team_id="team_p",
        agent_id="osa_policy",
        suite_id="aes_owner",
        owner_id="user_owner",
        session_id="s_coach",
        owner_words="It never says which period.",
        words_by="user_owner",
        origin_turn_id=None,
        judge_prompt_version=POINTS_PROMPT_VERSION,
        judge_model_fingerprint=fingerprint,
        pinned_at="2026-01-01T00:00:00+00:00",
        state="fixing",
        baseline_comparison_id=None,
        points=({"id": POINT["id"], "kind": "judgment", "text": POINT["text"], "status": "confirmed"},),
        report=None,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def _graded(db_path: Path, *, baseline_met: bool, candidate_met: bool, prompt_version: str = POINTS_PROMPT_VERSION) -> None:
    """Give the finished check a pinned judge and point verdicts; results are immutable, so lift the guards briefly."""
    with sqlite3.connect(db_path) as db:
        triggers = db.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name IN ('agent_eval_run','agent_eval_case_result')"
        ).fetchall()
        for name, _sql in triggers:
            db.execute(f"DROP TRIGGER {name}")
        db.execute("UPDATE agent_eval_run SET judge_role_ref='judge',judge_model_fingerprint=?", (PIN,))
        for side, met in (("baseline", baseline_met), ("candidate", candidate_met)):
            judge = {"prompt_version": prompt_version, "model_fingerprint": PIN, "points": [{"id": POINT["id"], "met": met, "blocking": False}]}
            db.execute(
                """UPDATE agent_eval_case_result SET judge_json=? WHERE case_revision_id='aec_aes_owner'
                   AND run_id IN (SELECT run_id FROM agent_eval_comparison_run WHERE subject_side=?)""",
                (json.dumps(judge), side),
            )
        for _name, sql in triggers:
            db.execute(sql)


async def _report(db_path: Path, started, loop: AlignmentLoop) -> dict:
    async with open_shared_ctx(str(db_path)) as db:
        db.row_factory = sqlite3.Row
        return await build_report_conn(db, loop, execution_id=started.id)


async def _check(tmp_path: Path, *, held_out: bool):
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path, owner_expected_contract={"points": [POINT], **({"held_out": True} if held_out else {})})
    _plan, comparison, started = await _start(db_path)
    await _complete(db_path, comparison, started, candidate_status="failed")
    return db_path, started


async def test_the_report_counts_the_target_point_before_and_after_and_names_regressions(tmp_path: Path) -> None:
    db_path, started = await _check(tmp_path, held_out=False)
    _graded(db_path, baseline_met=False, candidate_met=True)

    report = await _report(db_path, started, _loop())

    assert report["status"] == "measured"
    [target] = report["target_points"]
    # One case: counts only, never a rate (R12).
    assert target["before"] == {"met": 0, "cases": 1} and target["after"] == {"met": 1, "cases": 1}
    # The point has no held-out case, so the change couldn't be checked independently.
    assert target["independent_check"] is False
    assert report["regressions"] == ["case-aes_owner"]
    assert report["held_out"] == {"cases": 0, "before": {"passing": 0, "cases": 0}, "after": {"passing": 0, "cases": 0}, "regressions": 0}
    assert [item["case_key"] for item in report["to_read"]] == ["case-aes_owner"]
    assert len(report["to_read"][0]["excerpt"]) <= 280


async def test_a_held_out_case_appears_only_as_counts(tmp_path: Path) -> None:
    db_path, started = await _check(tmp_path, held_out=True)
    _graded(db_path, baseline_met=False, candidate_met=True)

    report = await _report(db_path, started, _loop())

    assert report["held_out"] == {"cases": 1, "before": {"passing": 1, "cases": 1}, "after": {"passing": 0, "cases": 1}, "regressions": 1}
    assert report["target_points"][0]["independent_check"] is True
    assert report["target_points"][0]["after"] == {"met": 0, "cases": 0}
    # Nothing about the held-out case beyond counts: no name, no answer.
    text = json.dumps(report)
    assert "case-aes_owner" not in text and "Candidate answer" not in text


@pytest.mark.parametrize("change", ["model", "prompt", "no_judge"])
async def test_a_check_under_another_judge_does_not_count(tmp_path: Path, change: str) -> None:
    db_path, started = await _check(tmp_path, held_out=False)
    if change != "no_judge":
        _graded(db_path, baseline_met=False, candidate_met=True, prompt_version="agent-eval-judge@1" if change == "prompt" else POINTS_PROMPT_VERSION)
    loop = _loop(fingerprint="sha256:" + "b" * 64) if change == "model" else _loop()

    report = await _report(db_path, started, loop)

    expected = "judge_unavailable" if change == "no_judge" else "judge_changed"
    assert report["status"] == expected
    assert "target_points" not in report and "held_out" not in report and "regressions" not in report


async def test_only_live_attempts_on_owner_cases_become_loop_candidates(tmp_path: Path) -> None:
    """A Live attempt on the Owner's own case is a candidate; one on the platform contract case never is,
    whether picked by the server or named by Coach. Decided by the case's suite, not its task text."""
    from bowhead_core.agent_eval_alignment import AgentEvalAlignmentError, AgentEvalAlignmentStore, AlignmentService
    from bowhead_core.agent_eval_point_calibration import attempt_answer_ref
    from bowhead_core.agent_eval_product import AgentEvalProductStore
    from test_agent_eval_point_drafting import _set_task

    db_path, started = await _check(tmp_path, held_out=False)
    _set_task(db_path, "aec_aes_owner", "Sales last month?")
    _set_task(db_path, "aec_aes_platform", "Explain briefly what result you can produce for a typical request.")
    with sqlite3.connect(db_path) as db:
        # Give the Live release's platform contract attempts an answer, as a real run would.
        db.execute(
            """UPDATE turns SET final_answer='I can produce a short summary.' WHERE id IN (
                 SELECT scratch.turn_id FROM agent_eval_scratch_turn scratch WHERE scratch.case_revision_id='aec_aes_platform')"""
        )
        platform_run = db.execute(
            """SELECT run.id FROM agent_eval_run run JOIN agent_eval_case_result result ON result.run_id=run.id
               WHERE result.case_revision_id='aec_aes_platform' AND run.candidate_id IS NULL LIMIT 1"""
        ).fetchone()[0]
    owner = {"team_id": "team_p", "agent_id": "osa_policy", "actor_user_id": "user_owner"}
    product = AgentEvalProductStore(str(db_path))
    live = await product.live_answers(**owner, limit=20, exclude_held_out=True)
    assert {item["suite_kind"] for item in live} == {"owner_quality", "platform_contract"}

    async def live_answers():
        return live

    async def pin():
        return PIN

    service = AlignmentService(
        store=AgentEvalAlignmentStore(str(db_path)), judge=None, writer=None, current_judge_fingerprint=pin, live_answers=live_answers
    )
    refs = [candidate.answer_ref for candidate in await service._candidates(_loop(), answer_refs=(), exclude=set())]
    assert refs and all(":aec_aes_owner:" in ref for ref in refs)
    platform_ref = attempt_answer_ref(run_id=platform_run, case_revision_id="aec_aes_platform", attempt=1)
    async with open_shared_ctx(str(db_path)) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(AgentEvalAlignmentError) as error:
            await service._explicit_candidates(db, _loop(), [platform_ref])
    assert error.value.code == "agent_eval_alignment_answer_not_owner_case"
    assert error.value.details["suite_kind"] == "platform_contract" and error.value.details["action"]
    assert started


async def test_a_held_out_attempt_is_never_a_loop_answer(tmp_path: Path) -> None:
    from bowhead_core.agent_eval_alignment import AgentEvalAlignmentError, AgentEvalAlignmentStore, AlignmentService
    from bowhead_core.agent_eval_product import AgentEvalProductStore
    from test_agent_eval_point_drafting import _set_task

    db_path, _started = await _check(tmp_path, held_out=True)
    _set_task(db_path, "aec_aes_owner", "Sales last month?")
    owner = {"team_id": "team_p", "agent_id": "osa_policy", "actor_user_id": "user_owner"}
    everything = await AgentEvalProductStore(str(db_path)).live_answers(**owner, limit=20)
    held_ref = next(item["answer_ref"] for item in everything if item["case_revision_id"] == "aec_aes_owner" and item["held_out"])

    async def pin():
        return PIN

    async def live():
        return everything

    service = AlignmentService(store=AgentEvalAlignmentStore(str(db_path)), judge=None, writer=None, current_judge_fingerprint=pin, live_answers=live)
    candidates = await service._candidates(_loop(), answer_refs=(), exclude=set())
    assert not [candidate for candidate in candidates if ":aec_aes_owner:" in candidate.answer_ref]
    async with open_shared_ctx(str(db_path)) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(AgentEvalAlignmentError) as error:
            await service._explicit_candidates(db, _loop(), [held_ref])
    assert error.value.code == "agent_eval_case_held_out"
