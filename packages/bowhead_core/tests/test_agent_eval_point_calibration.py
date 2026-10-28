"""Calibration and Judged wrongly for expected points (evaluation-credibility technical design §7)."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from bowhead_core.agent_eval_insights import read_unmet_points
from bowhead_core.agent_eval_judge import EvalPointsGrading, EvalPointVerdict, TeamRouterEvalJudge
from bowhead_core.agent_eval_point_calibration import (
    TRANSIENT_RETRY_AFTER,
    AgentEvalPointStore,
    PointCalibrationRow,
    PointCalibrator,
    PointExample,
    attempt_answer_ref,
    considered_examples,
    is_minimal_edit,
    point_status,
    point_text_hash,
)
from bowhead_core.agent_eval_service import EvalAttemptOutput
from bowhead_core.agent_eval_store import AgentEvalStoreError, EvalCaseDraft
from test_agent_eval_execution import _HASH_A, _fixture, _registry, _service
from test_agent_eval_points import _JUDGMENT, _Router

from bowhead_core import agent_eval_point_calibration as calibration
from bowhead_core import agent_eval_points

pytestmark = pytest.mark.unit
_HASH_B = "sha256:" + "b" * 64
_PV = "agent-eval-judge@2"
_TEXT_HASH = point_text_hash(_JUDGMENT["text"])


def _example(
    kind: str,
    *,
    met: bool | None,
    expected: bool | None = None,
    fingerprint: str = _HASH_A,
    created: str = "2026-10-18T00:00:00",
    example_id: str = "",
) -> PointExample:
    return PointExample(
        id=example_id or f"aepx_{kind}_{created}",
        kind=kind,  # type: ignore[arg-type]
        answer_ref="coach:x",
        answer_text="answer",
        expected_met=(kind == "good") if expected is None else expected,
        graded_text_hash=_TEXT_HASH,
        graded_prompt_version=_PV,
        graded_model_fingerprint=fingerprint,
        graded_met=met,
        created_at=created,
    )


def _row(state: str = "calibrated", *, fingerprint: str = _HASH_A, reason: str | None = None, rewording: str | None = None) -> PointCalibrationRow:
    return PointCalibrationRow(
        text_hash=_TEXT_HASH,
        prompt_version=_PV,
        model_fingerprint=fingerprint,
        state=state,
        failure_reason=reason,
        rewording=rewording,  # type: ignore[arg-type]
    )


_AGREEING = [_example("good", met=True), _example("bad", met=False)]


@pytest.mark.parametrize(
    ("row", "examples", "fingerprint", "reason"),
    [
        (None, [], _HASH_A, "calibrating"),
        (_row(), _AGREEING, None, "judge_unavailable"),
        (_row(fingerprint=_HASH_B), _AGREEING, _HASH_A, "judge_changed"),
        (_row("failed", reason="good_judged_unmet", rewording="Gives the year-on-year change"), _AGREEING, _HASH_A, "calibration_failed"),
        # A failure that says nothing about the point is retried, not reported.
        (_row("failed", reason="judge_unavailable"), _AGREEING, _HASH_A, "calibrating"),
        # A new Judged-wrongly example has not been graded yet.
        (_row(), [*_AGREEING, _example("judged_wrongly", met=None, expected=True)], _HASH_A, "calibrating"),
        # Calibrated, but the dry run hasn't admitted judgment criteria yet.
        (_row(), _AGREEING, _HASH_A, "kind_not_validated"),
    ],
)
def test_point_status_says_why_a_confirmed_point_does_not_count(row, examples, fingerprint, reason) -> None:
    status = point_status(_JUDGMENT, row, examples, prompt_version=_PV, model_fingerprint=fingerprint)

    assert status["counts"] is False and status["reason"] == reason
    if reason == "calibration_failed":
        assert status["rewording"] == "Gives the year-on-year change"


def test_a_calibrated_point_of_a_blocking_kind_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(calibration, "BLOCKING_POINT_KINDS", frozenset({"required_action", "judgment"}))

    status = point_status(_JUDGMENT, _row(), _AGREEING, prompt_version=_PV, model_fingerprint=_HASH_A)
    edited = point_status({**_JUDGMENT, "text": "Compares with last year"}, _row(), _AGREEING, prompt_version=_PV, model_fingerprint=_HASH_A)

    assert status == {"counts": True, "reason": None, "rewording": None}
    # Editing the text makes the point informational until it is recalibrated (R11).
    assert edited["reason"] == "calibrating"


def test_a_required_action_counts_without_calibration() -> None:
    action = {"id": "p_0a3b", "kind": "required_action", "text": "Queries sales", "state": "confirmed", "action": {"tool": "bi_query"}}

    assert point_status(action, None, [], prompt_version=_PV, model_fingerprint=None) == {"counts": True, "reason": None, "rewording": None}


def test_calibration_considers_only_the_most_recent_judged_wrongly_examples() -> None:
    wrongly = [_example("judged_wrongly", met=None, created=f"2026-10-{day:02d}", example_id=f"aepx_{day}") for day in range(1, 13)]

    considered = considered_examples([*_AGREEING, *wrongly])

    assert {example.id for example in considered if example.kind == "judged_wrongly"} == {f"aepx_{day}" for day in range(3, 13)}


def test_a_bad_example_must_be_a_small_edit_of_the_good_one() -> None:
    good = "Revenue rose 12% on the same month last year, driven by Shanghai."

    assert is_minimal_edit(good, "Revenue rose 12% this month, driven by Shanghai.")
    assert not is_minimal_edit(good, good)
    assert not is_minimal_edit(good, "I can't help with that.")


# ── store and calibrator against a real database ─────────────────────────


async def _graded_run(tmp_path: Path, *, verdict: str = "false") -> dict[str, Any]:
    db_path, users, team, owner, workspace_id, eval_store, original, previous = await _fixture(tmp_path)
    revision = await eval_store.create_suite_revision(
        suite_id=original.suite_id,
        definition={},
        scorer_bundle_ref="evalkit.core@1",
        judge_policy={"enabled": False},
        cases=(
            EvalCaseDraft(
                case_key="points.judgment",
                input={"task": "How did East China do? Do not call Tools."},
                fixture_ref={},
                expected_contract={"points": [_JUDGMENT]},
                scorer_config={"max_tool_calls": 0},
                effect_policy={"mode": "no_effects"},
            ),
        ),
        actor_user_id=owner.id,
    )
    run = await eval_store.create_run(
        subject=previous.subject,
        suite_revision_id=revision.suite_revision_id,
        trigger_kind="manual",
        idempotency_key="points.calibration",
        actor_user_id=owner.id,
        judge_role_ref="judge",
        judge_model_fingerprint=_HASH_A,
    )

    class Executor:
        async def execute(self, request: Any) -> EvalAttemptOutput:
            return EvalAttemptOutput(output="Revenue was flat.", terminal_status="completed", duration_ms=10, cost_microusd=100)

    service = _service(db_path=db_path, eval_store=eval_store, executor=Executor(), registry=_registry([]))
    service.judge = TeamRouterEvalJudge(router=_Router(f'{{"points":[{{"id":"p_0a1b","met":{verdict},"quote":"Revenue was flat."}}]}}'))
    await service.execute_run(run_id=run.run_id, workspace_id=workspace_id, worker_id="eval.calibration")
    with sqlite3.connect(db_path) as db:
        case_revision_id, attempt = db.execute(
            "SELECT case_revision_id,attempt FROM agent_eval_case_result WHERE run_id=? ORDER BY attempt LIMIT 1", (run.run_id,)
        ).fetchone()
        agent_id = db.execute("SELECT agent_id FROM agent_eval_run WHERE id=?", (run.run_id,)).fetchone()[0]
    return {
        "db_path": str(db_path),
        "users": users,
        "team": team,
        "owner": owner,
        "agent_id": agent_id,
        "suite_id": original.suite_id,
        "run_id": run.run_id,
        "case_revision_id": case_revision_id,
        "attempt": attempt,
    }


@pytest.mark.anyio
async def test_judged_wrongly_records_the_opposite_verdict_once_and_only_for_the_owner(tmp_path: Path) -> None:
    ctx = await _graded_run(tmp_path)
    store = AgentEvalPointStore(ctx["db_path"])
    arguments = {
        "team_id": ctx["team"].id,
        "agent_id": ctx["agent_id"],
        "point_id": "p_0a1b",
        "run_id": ctx["run_id"],
        "case_revision_id": ctx["case_revision_id"],
        "attempt": ctx["attempt"],
    }

    first = await store.record_judged_wrongly(actor_user_id=ctx["owner"].id, **arguments)
    again = await store.record_judged_wrongly(actor_user_id=ctx["owner"].id, **arguments)

    assert first["disputed_met"] is False and first["answer"] == "Revenue was flat."
    assert first["point"]["text"] == _JUDGMENT["text"]
    assert again["example_id"] == first["example_id"]
    with sqlite3.connect(ctx["db_path"]) as db:
        assert db.execute("SELECT kind,expected_met,answer_ref FROM agent_eval_point_example").fetchall() == [
            ("judged_wrongly", 1, attempt_answer_ref(run_id=ctx["run_id"], case_revision_id=ctx["case_revision_id"], attempt=ctx["attempt"]))
        ]
    member = await ctx["users"].create_user(team_id=ctx["team"].id, email="member@example.com")
    with pytest.raises(AgentEvalStoreError) as denied:
        await store.record_judged_wrongly(actor_user_id=member.id, **arguments)
    assert denied.value.code == "agent_eval_action_denied"
    with pytest.raises(AgentEvalStoreError) as unknown:
        await store.record_judged_wrongly(actor_user_id=ctx["owner"].id, **{**arguments, "attempt": 99})
    assert unknown.value.code == "agent_eval_point_answer_not_found"


@pytest.mark.anyio
async def test_a_good_answer_from_another_agent_is_rejected(tmp_path: Path) -> None:
    ctx = await _graded_run(tmp_path)
    store = AgentEvalPointStore(ctx["db_path"])

    with pytest.raises(AgentEvalStoreError) as rejected:
        await store.mark_good_answer(
            team_id=ctx["team"].id,
            agent_id=ctx["agent_id"],
            actor_user_id=ctx["owner"].id,
            point_id="p_0a1b",
            case_revision_id=ctx["case_revision_id"],
            answer_ref=attempt_answer_ref(run_id="aer_elsewhere", case_revision_id=ctx["case_revision_id"], attempt=1),
        )
    assert rejected.value.code == "agent_eval_point_answer_not_found"


class _Writer:
    def __init__(self, *, bad: str | None = None) -> None:
        self.bad = bad
        self.rewordings: list[str] = []

    async def good_answer(self, *, task: str, point: Mapping[str, Any]) -> str | None:
        return "Revenue rose 12% on the same month last year."

    async def bad_answer(self, *, task: str, point: Mapping[str, Any], good_answer: str) -> str | None:
        return self.bad or "Revenue rose 12% this month."

    async def rewording(self, *, task: str, point: Mapping[str, Any], problem: str, answer: str | None = None) -> str | None:
        self.rewordings.append(problem)
        return "Gives the change against the same month last year"


class _Judge:
    """Meets the point only when the answer mentions last year, unless told to get it wrong."""

    def __init__(self, *, invert: bool = False, unavailable: bool = False) -> None:
        self.invert = invert
        self.unavailable = unavailable
        self.answers: list[str] = []

    async def grade_points(
        self, *, task: str, output: Any, points: Sequence[Mapping[str, Any]], role_ref: str, expected_model_fingerprint: str
    ) -> EvalPointsGrading:
        self.answers.append(str(output))
        if self.unavailable:
            return EvalPointsGrading(status="unknown", failure_code="agent_eval_judge_unavailable")
        met = ("last year" in str(output)) != self.invert
        return EvalPointsGrading(status="graded", verdicts={str(points[0]["id"]): EvalPointVerdict(met=met, quote=str(output))})


async def _status(ctx: dict[str, Any], fingerprint: str | None) -> dict[str, Any]:
    with sqlite3.connect(ctx["db_path"]) as db:
        suite_revision_id = db.execute("SELECT suite_revision_id FROM agent_eval_case_revision WHERE id=?", (ctx["case_revision_id"],)).fetchone()[0]
    statuses = await AgentEvalPointStore(ctx["db_path"]).suite_point_status(
        team_id=ctx["team"].id, agent_id=ctx["agent_id"], suite_revision_ids={ctx["suite_id"]: suite_revision_id}, model_fingerprint=fingerprint
    )
    return statuses[ctx["suite_id"]]["p_0a1b"]


async def _tasks(ctx: dict[str, Any], fingerprint: str | None = _HASH_A, *, now: datetime | None = None):
    return await AgentEvalPointStore(ctx["db_path"]).calibration_tasks(
        team_id=ctx["team"].id, agent_id=ctx["agent_id"], model_fingerprint=fingerprint, now=now
    )


@pytest.mark.anyio
async def test_calibration_writes_examples_grades_them_and_follows_the_judge(tmp_path: Path) -> None:
    ctx = await _graded_run(tmp_path)
    store = AgentEvalPointStore(ctx["db_path"])
    judge = _Judge()

    (task,) = await _tasks(ctx)
    outcome = await PointCalibrator(store=store, judge=judge, writer=_Writer()).calibrate(task, model_fingerprint=_HASH_A)

    assert outcome.state == "calibrated"
    assert len(judge.answers) == 2
    assert (await _status(ctx, _HASH_A))["reason"] == "kind_not_validated"
    assert await _tasks(ctx) == []
    calibrated = await store.calibrated_point_ids(case_revision_id=ctx["case_revision_id"], points=[_JUDGMENT], model_fingerprint=_HASH_A)
    assert calibrated == frozenset({"p_0a1b"})
    # Another evaluation model has never been checked against these examples (E9).
    assert await store.calibrated_point_ids(case_revision_id=ctx["case_revision_id"], points=[_JUDGMENT], model_fingerprint=_HASH_B) == frozenset()
    assert (await _status(ctx, _HASH_B))["reason"] == "judge_changed"
    assert len(await _tasks(ctx, _HASH_B)) == 1
    with sqlite3.connect(ctx["db_path"]) as db:
        rows = db.execute("SELECT kind,expected_met,graded_met,graded_model_fingerprint FROM agent_eval_point_example ORDER BY kind").fetchall()
    assert rows == [("bad", 0, 0, _HASH_A), ("good", 1, 1, _HASH_A)]


@pytest.mark.anyio
async def test_a_judged_wrongly_example_must_be_judged_right_before_the_point_is_calibrated_again(tmp_path: Path) -> None:
    ctx = await _graded_run(tmp_path)
    store = AgentEvalPointStore(ctx["db_path"])
    (task,) = await _tasks(ctx)
    await PointCalibrator(store=store, judge=_Judge(), writer=_Writer()).calibrate(task, model_fingerprint=_HASH_A)

    # The run judged "Revenue was flat." as not met; the Owner says it meets the point.
    await store.record_judged_wrongly(
        team_id=ctx["team"].id,
        agent_id=ctx["agent_id"],
        actor_user_id=ctx["owner"].id,
        point_id="p_0a1b",
        run_id=ctx["run_id"],
        case_revision_id=ctx["case_revision_id"],
        attempt=ctx["attempt"],
    )
    assert (await _status(ctx, _HASH_A))["reason"] == "calibrating"
    (task,) = await _tasks(ctx)
    writer = _Writer()
    outcome = await PointCalibrator(store=store, judge=_Judge(), writer=writer).calibrate(task, model_fingerprint=_HASH_A)

    assert (outcome.state, outcome.failure_reason) == ("failed", "judged_wrongly_repeated")
    status = await _status(ctx, _HASH_A)
    assert status == {"counts": False, "reason": "calibration_failed", "rewording": "Gives the change against the same month last year"}
    assert await store.calibrated_point_ids(case_revision_id=ctx["case_revision_id"], points=[_JUDGMENT], model_fingerprint=_HASH_A) == frozenset()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("judge", "writer", "state", "reason"),
    [
        (_Judge(invert=True), _Writer(), "failed", "good_judged_unmet"),
        (_Judge(), _Writer(bad="Something completely different that shares nothing."), "failed", "bad_example_not_minimal"),
        (_Judge(unavailable=True), _Writer(), "retry", "judge_unavailable"),
    ],
)
async def test_calibration_failures_are_reported_and_outages_are_retried(tmp_path: Path, judge, writer, state, reason) -> None:
    ctx = await _graded_run(tmp_path)
    (task,) = await _tasks(ctx)

    outcome = await PointCalibrator(store=AgentEvalPointStore(ctx["db_path"]), judge=judge, writer=writer).calibrate(task, model_fingerprint=_HASH_A)

    assert (outcome.state, outcome.failure_reason) == (state, reason)
    expected_reason = "calibrating" if state == "retry" else "calibration_failed"
    assert (await _status(ctx, _HASH_A))["reason"] == expected_reason
    # An outage is retried only after its back-off, so reading the eval manager repeatedly doesn't spend a
    # Coach and judge round each time; a reported failure waits for the Owner to reword the point.
    assert await _tasks(ctx) == []
    later = datetime.now(UTC) + TRANSIENT_RETRY_AFTER + timedelta(minutes=1)
    assert len(await _tasks(ctx, now=later)) == (1 if state == "retry" else 0)


@pytest.mark.anyio
async def test_without_an_evaluation_model_nothing_is_calibrated(tmp_path: Path) -> None:
    ctx = await _graded_run(tmp_path)

    # No Coach examples are written for a point that can't be graded anyway.
    assert await _tasks(ctx, None) == []
    assert (await _status(ctx, None))["reason"] == "judge_unavailable"


@pytest.mark.anyio
async def test_the_owners_good_answer_replaces_coach_examples(tmp_path: Path) -> None:
    ctx = await _graded_run(tmp_path, verdict="true")
    store = AgentEvalPointStore(ctx["db_path"])
    (task,) = await _tasks(ctx)
    await PointCalibrator(store=store, judge=_Judge(), writer=_Writer()).calibrate(task, model_fingerprint=_HASH_A)

    await store.mark_good_answer(
        team_id=ctx["team"].id,
        agent_id=ctx["agent_id"],
        actor_user_id=ctx["owner"].id,
        point_id="p_0a1b",
        case_revision_id=ctx["case_revision_id"],
        answer_ref=attempt_answer_ref(run_id=ctx["run_id"], case_revision_id=ctx["case_revision_id"], attempt=ctx["attempt"]),
    )

    with sqlite3.connect(ctx["db_path"]) as db:
        assert db.execute("SELECT kind,answer_ref LIKE 'attempt:%' FROM agent_eval_point_example").fetchall() == [("good", 1)]
    assert (await _status(ctx, _HASH_A))["reason"] == "calibrating"


@pytest.mark.anyio
async def test_a_calibrated_point_of_a_blocking_kind_fails_the_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = await _graded_run(tmp_path)
    store = AgentEvalPointStore(ctx["db_path"])
    (task,) = await _tasks(ctx)
    await PointCalibrator(store=store, judge=_Judge(), writer=_Writer()).calibrate(task, model_fingerprint=_HASH_A)
    monkeypatch.setattr(agent_eval_points, "BLOCKING_POINT_KINDS", frozenset({"required_action", "judgment"}))

    calibrated = await store.calibrated_point_ids(case_revision_id=ctx["case_revision_id"], points=[_JUDGMENT], model_fingerprint=_HASH_A)
    rows = agent_eval_points.graded_point_rows(
        [_JUDGMENT],
        EvalPointsGrading(status="graded", verdicts={"p_0a1b": EvalPointVerdict(met=False, quote="Revenue was flat.")}),
        data_read=False,
        authorized_numbers=(),
        calibrated_point_ids=calibrated,
    )
    uncalibrated = agent_eval_points.graded_point_rows(
        [_JUDGMENT],
        EvalPointsGrading(status="graded", verdicts={"p_0a1b": EvalPointVerdict(met=False, quote="Revenue was flat.")}),
        data_read=False,
        authorized_numbers=(),
    )

    assert agent_eval_points.settle_points(rule_passed=True, graded=rows) == "failed"
    assert agent_eval_points.settle_points(rule_passed=True, graded=uncalibrated) == "passed"


@pytest.mark.anyio
async def test_results_show_every_graded_point_and_the_risk_view_lists_blocking_unmet_ones(tmp_path: Path) -> None:
    from bowhead_core.agent_eval_insights import read_case_answers
    from bowhead_core.agent_eval_job_store import AgentEvalJobStore
    from bowhead_core.agent_eval_store import AgentEvalStore
    from test_agent_eval_policy_plan_store import _seed, _upgrade
    from test_agent_eval_run_insights import _OWNER, _runs, _start

    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    _plan, comparison, started = await _start(db_path)
    runs = _runs(started)
    candidate_run = runs[("aev_owner", "candidate")]
    with sqlite3.connect(db_path) as db:
        # The fixture plan predates points, so its runs pinned no judge; pin one the way a comparison
        # with points does (slice 3 tests that path). Only the identity guard is lifted, and only for
        # this update: the result-scope trigger still checks every stored judge document below.
        guards = db.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name='agent_eval_run' AND sql LIKE '%identity is immutable%'"
        ).fetchall()
        for name, _sql in guards:
            db.execute(f"DROP TRIGGER {name}")
        db.execute("UPDATE agent_eval_run SET judge_role_ref='judge',judge_model_fingerprint=? WHERE id=?", (_HASH_A, candidate_run))
        for _name, sql in guards:
            db.execute(sql)
    await comparison.mark_running(started.id)
    for run_id in runs.values():
        await AgentEvalStore(db_path).mark_run_running(run_id)
    jobs = AgentEvalJobStore(db_path)
    verdicts = [
        {"id": "p_0a1b", "met": False, "quote": "Revenue was flat.", "applicable": True, "blocking": True},
        {"id": "p_0c1d", "met": True, "quote": "Up on last year.", "applicable": True, "blocking": False},
    ]
    for _ in range(2):
        claim = await jobs.claim_next_job(run_id=candidate_run, worker_id="points.test")
        assert claim is not None
        turn_id = f"t_eval_{claim.job.job_id[4:28]}"
        await jobs.start_scratch_turn(
            job_id=claim.job.job_id,
            lease_token=claim.lease_token,
            session_id=f"s_eval_{claim.job.job_id[4:28]}",
            turn_id=turn_id,
            user_text="task",
        )
        await jobs.settle_job(
            job_id=claim.job.job_id,
            lease_token=claim.lease_token,
            status="failed",
            deterministic={"passed": True},
            judge={"points": verdicts, "prompt_version": _PV, "schema_version": 2},
            trace_evidence_ref=f"turn:{turn_id}",
            duration_ms=10,
            subject_provider_ref="model-config",
            subject_provider_hash=f"sha256:{'6' * 64}",
            judge_provider_ref="judge",
            judge_provider_hash=_HASH_A,
            reproducibility="exact",
        )
    args = {**_OWNER, "execution_id": started.id, "actor_user_id": "user_owner"}

    unmet = await read_unmet_points(str(db_path), **args)
    answers = await read_case_answers(str(db_path), **args, case_revision_id="aec_aes_owner")

    assert [(item["point_id"], item["quote"], item["attempts"]) for item in unmet["items"]] == [("p_0a1b", "Revenue was flat.", 2)]
    attempt = answers["candidate"]["attempts"][0]
    assert attempt["run_id"] == candidate_run
    # Met points are listed too, so a wrong pass can be reported (R26).
    assert [(point["id"], point["met"]) for point in attempt["points"]] == [("p_0a1b", False), ("p_0c1d", True)]
    with pytest.raises(Exception, match="agent_owner_required"):
        await read_unmet_points(str(db_path), **(args | {"actor_user_id": "user_other"}))


# ── the counterexample writer (116 dogfood, 1.5.84) ──────────────────────


class _WriterRouter:
    """Replies in order; a reply of None never answers (a reasoning model past the time limit)."""

    def __init__(self, *replies: str | None) -> None:
        self.replies = list(replies)
        self.requests: list[Any] = []

    async def invoke(self, role: str, request: Any) -> Any:
        import anyio

        from bowhead_router import LLMResponse

        self.requests.append(request)
        reply = self.replies.pop(0)
        if reply is None:
            await anyio.sleep(10)
        return LLMResponse(content=reply, provider="fixture-provider", model="fixture-model")


_LONG_GOOD = " ".join(f"Store {index} sold {index * 3} units this week, up from {index * 2} last week." for index in range(1, 160))


async def test_a_counterexample_is_one_edit_applied_to_the_answer_however_long_it_is() -> None:
    import json

    router = _WriterRouter(json.dumps({"find": "Store 7 sold 21 units this week", "replace": "Store 7 sold 21 units in March 2030"}))
    writer = calibration.TeamRouterCalibrationWriter(router=router)

    bad = await writer.bad_answer(task="Summarize the week", point=_JUDGMENT, good_answer=_LONG_GOOD)

    # The model returned only the edit; the server applied it to the whole answer.
    assert len(_LONG_GOOD) > 9_000 and bad == _LONG_GOOD.replace("Store 7 sold 21 units this week", "Store 7 sold 21 units in March 2030", 1)
    assert is_minimal_edit(_LONG_GOOD, bad)
    assert len(router.requests) == 1


async def test_a_rejected_edit_is_retried_once_with_what_was_wrong(caplog) -> None:
    import json

    good = "Sales grew 4% this week."
    router = _WriterRouter(json.dumps({"find": "Sales rose", "replace": "x"}), json.dumps({"find": "this week", "replace": "in March 2030"}))
    writer = calibration.TeamRouterCalibrationWriter(router=router)

    assert await writer.bad_answer(task="t", point=_JUDGMENT, good_answer=good) == "Sales grew 4% in March 2030."
    assert "does not appear in the answer" in router.requests[1].messages[0].content
    assert "agent_eval_writer.bad_answer_rejected reason=edit_not_found" in caplog.text


async def test_an_unchanged_edit_is_still_returned_for_the_minimal_edit_check() -> None:
    import json

    good = "Sales grew 4% this week."
    unchanged = json.dumps({"find": "this week", "replace": "this week"})
    writer = calibration.TeamRouterCalibrationWriter(router=_WriterRouter(unchanged, unchanged))

    # Calibration then records a lasting not-minimal failure rather than retrying as an outage.
    bad = await writer.bad_answer(task="t", point=_JUDGMENT, good_answer=good)
    assert bad == good and not is_minimal_edit(good, bad)


async def test_a_writer_timeout_is_logged_with_its_reason(caplog) -> None:
    router = _WriterRouter(None)
    writer = calibration.TeamRouterCalibrationWriter(router=router, timeout_seconds=0.05)

    assert await writer.bad_answer(task="t", point=_JUDGMENT, good_answer="Sales grew 4% this week.") is None
    # One call only: a retry after a timeout would wait as long again.
    assert len(router.requests) == 1
    assert "agent_eval_writer.call_failed op=bad_answer reason=timeout" in caplog.text
