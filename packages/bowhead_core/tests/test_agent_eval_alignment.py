"""The Coach alignment loop: answers picked and graded per point, the Owner's labels, confirmation.

Alignment-loop technical design §5–§6 and §11: only readable answers are offered, the server (never
Coach) confirms from the Owner's labels, the attention check needs an explicit look at a misjudged
counterexample, and labels seed calibration exactly as the calibrator would decide on the same data.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping

import pytest
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_assignment_store import AgentAssignmentStore
from bowhead_core.agent_eval_alignment import (
    AgentEvalAlignmentError,
    AgentEvalAlignmentStore,
    AlignmentQuestion,
    AlignmentService,
    alignment_outcome,
    seed_agrees,
)
from bowhead_core.agent_eval_conversation_cases import share_turn, withdraw_turn_share
from bowhead_core.agent_eval_judge import POINTS_PROMPT_VERSION, EvalPointsGrading, EvalPointVerdict
from bowhead_core.agent_eval_point_calibration import (
    AgentEvalPointStore,
    CalibrationTask,
    PointCalibrator,
    _calibration_row_conn,
    _examples_conn,
    is_calibrated,
    point_text_hash,
)
from bowhead_core.agent_eval_store import AgentEvalStore, AgentEvalStoreError, EvalCaseDraft
from test_agent_rankings import completed_use, ranking_db  # noqa: F401  (fixture)

pytestmark = pytest.mark.integration

PIN = "sha256:" + "a" * 64
OTHER_PIN = "sha256:" + "b" * 64
MEMBER = "usr_chart_1"
STRANGER = "usr_chart_2"
GOOD = "Sales grew 4% in January 2030 compared with December 2029."
# The writer's minimal edit: the same answer with the period removed.
COUNTER_MARK = " [x]"


def _q(
    index: int,
    *,
    label: str | None = None,
    met: bool | None = True,
    via: str = "row",
    reason: str = "recent",
    point_id: str = "p_aaaa",
    text_hash: str = "sha256:x",
) -> AlignmentQuestion:
    return AlignmentQuestion(
        id=f"aeaq_{index:032x}",
        point_id=point_id,
        point_text_hash=text_hash,
        card_seq=1,
        row_index=index,
        answer_ref=f"coach:aeaq_{index:032x}" if reason in {"counterexample", "generated"} else f"turn:t_{index}",
        turn_id=None,
        answer_text=None,
        task_text="q",
        answer_excerpt="a",
        judge_met=met,
        judge_quote=None,
        judge_reason=None,
        graded_prompt_version=POINTS_PROMPT_VERSION,
        graded_model_fingerprint=PIN,
        pick_reason=reason,
        replacement=False,
        owner_label=label,
        label_via=via if label else None,
        labelled_at="2030-02-01" if label else None,
        created_at="2030-02-01",
    )


# ── outcome rules ────────────────────────────────────────────────────────


def test_outcome_precedence_follows_the_design() -> None:
    right_met = [_q(1, label="right"), _q(2, label="right")]
    counter = _q(3, label="right", met=False, reason="counterexample")
    assert alignment_outcome([*right_met, counter], rewrites=0) == "confirm"
    # Judged wrong wins over everything, including three Can't tell.
    unclear = [_q(index, label="unclear") for index in range(4, 7)]
    assert alignment_outcome([*right_met, counter, _q(7, label="wrong"), *unclear], rewrites=0) == "rewrite"
    assert alignment_outcome([_q(7, label="wrong")], rewrites=2) == "split_or_informational"
    assert alignment_outcome(unclear, rewrites=0) == "unclear"
    # Three rights with one verdict only: ask for a counterexample.
    assert alignment_outcome([*right_met, _q(8, label="right")], rewrites=0) == "need_counterexample"
    # A "right" on an abstained verdict says nothing about met or unmet and doesn't count.
    assert alignment_outcome([*right_met, _q(9, label="right", met=None)], rewrites=0) == "continue"
    assert alignment_outcome([_q(1), _q(2)], rewrites=0) == "continue"


def test_the_attention_check_needs_an_explicit_look_at_a_misjudged_counterexample() -> None:
    rights = [_q(1, label="right"), _q(2, label="right", met=False), _q(4, label="right")]
    passed_by_rest = _q(3, label="right", met=True, via="rest_right", reason="counterexample")
    assert alignment_outcome([*rights, passed_by_rest], rewrites=0) == "recheck_row"
    # The same label given on the row itself is a normal label: the Owner says it really meets the point.
    explicit = _q(3, label="right", met=True, via="row", reason="counterexample")
    assert alignment_outcome([*rights, explicit], rewrites=0) == "confirm"
    # A real answer passed by "rest right" is not the attention check.
    assert alignment_outcome([*rights, _q(5, label="right", via="rest_right"), explicit], rewrites=0) == "confirm"
    assert alignment_outcome([*rights, _q(3, label="wrong", met=True, reason="counterexample")], rewrites=0) == "rewrite"


# ── fixtures ─────────────────────────────────────────────────────────────


class _Judge:
    """Grades by answer text; records every call so tests can see one answer per call."""

    def __init__(self, verdicts: Mapping[str, bool | None] | None = None, *, counter_met: bool | None = False) -> None:
        self.verdicts = dict(verdicts or {})
        self.counter_met = counter_met
        self.calls: list[str] = []

    async def grade_points(self, *, task, output, points, role_ref, expected_model_fingerprint) -> EvalPointsGrading:
        assert role_ref == "judge" and len(points) == 1
        self.calls.append(str(output))
        met = self.counter_met if str(output).endswith(COUNTER_MARK) else self.verdicts.get(str(output), True)
        quote = str(output)[:40] if met is not None else None
        return EvalPointsGrading(status="graded", verdicts={points[0]["id"]: EvalPointVerdict(met=met, quote=quote)})


class _Writer:
    async def good_answer(self, *, task, point):
        return GOOD

    async def bad_answer(self, *, task, point, good_answer):
        return good_answer + COUNTER_MARK

    async def rewording(self, *, task, point, problem, answer=None):
        return None


async def _loop_fixture(ranking_db, monkeypatch, *, judge: _Judge | None = None):  # noqa: F811
    fixture = ranking_db
    await AgentAssignmentStore(fixture.db_path).hire(
        team_id=fixture.team_id, user_id=fixture.owner_id, agent_id="osa_exact", agent_source_kind="team", live_access=True
    )
    store = AgentEvalStore(fixture.db_path)
    suite = await store.create_suite(
        team_id=fixture.team_id,
        agent_id="osa_exact",
        agent_source_kind="team",
        suite_kind="owner_quality",
        name="Quality",
        visibility="owner",
        actor_user_id=fixture.owner_id,
    )
    await store.create_suite_revision(
        suite_id=suite.suite_id,
        definition={"schema_version": 1},
        scorer_bundle_ref="evalkit.core@1",
        judge_policy={"enabled": False, "rubric_ref": None},
        cases=(
            EvalCaseDraft(
                case_key="existing",
                input={"task": "Summarize the report"},
                fixture_ref={},
                expected_contract={"allowed_terminal_statuses": ["completed"]},
                scorer_config={},
                effect_policy={"mode": "no_effects"},
            ),
        ),
        actor_user_id=fixture.owner_id,
    )
    own = await completed_use(fixture, monkeypatch, actor=fixture.owner_id, suffix="own")
    newer = await completed_use(fixture, monkeypatch, actor=fixture.owner_id, day="2030-01-31", suffix="newer")
    shared = await completed_use(fixture, monkeypatch, actor=MEMBER, suffix="shared")
    unshared = await completed_use(fixture, monkeypatch, actor=STRANGER, suffix="unshared")
    with sqlite3.connect(fixture.db_path) as db:
        for turn_id, answer in ((own, "Sales were 12 units."), (newer, GOOD), (shared, "Sales grew."), (unshared, "Private answer.")):
            db.execute("UPDATE turns SET final_answer=? WHERE id=?", (answer, turn_id))
        # The member's thumbs-down, with a comment.
        db.execute(
            """INSERT INTO agent_answer_feedback(turn_id,team_id,agent_id,user_id,score,reason_codes,comment,created_at,updated_at)
               VALUES(?,?,?,?,-1,'["missing_content"]','It never says which period.','2030-01-30','2030-01-30')""",
            (shared, fixture.team_id, "osa_exact", MEMBER),
        )
    await share_turn(fixture.db_path, team_id=fixture.team_id, user_id=MEMBER, turn_id=shared)
    judge = judge or _Judge()
    alignment = AgentEvalAlignmentStore(fixture.db_path)

    async def fingerprint():
        return PIN

    service = AlignmentService(store=alignment, judge=judge, writer=_Writer(), current_judge_fingerprint=fingerprint)
    owner = {"team_id": fixture.team_id, "agent_id": "osa_exact", "actor_user_id": fixture.owner_id}
    turns = {"own": own, "newer": newer, "shared": shared, "unshared": unshared}
    return fixture, alignment, service, owner, turns, judge


async def _start(alignment, owner, **kwargs):
    return await alignment.start(
        **owner,
        session_id="s_coach",
        words=kwargs.pop("words", "It never says which period the numbers cover."),
        origin_turn_id=kwargs.pop("origin_turn_id", None),
        words_from_feedback=kwargs.pop("words_from_feedback", False),
        judge_model_fingerprint=PIN,
    )


async def _ask(service, owner, loop_id, **kwargs):
    return await service.ask(
        **owner,
        loop_id=loop_id,
        point_id=kwargs.pop("point_id", None),
        kind="judgment",
        text=kwargs.pop("text", "States the period the figures cover"),
        **kwargs,
    )


# ── picking answers ──────────────────────────────────────────────────────


async def test_a_card_offers_thumbs_downs_first_and_never_an_unshared_turn(ranking_db, monkeypatch) -> None:  # noqa: F811
    fixture, alignment, service, owner, turns, judge = await _loop_fixture(ranking_db, monkeypatch)
    loop = await _start(alignment, owner)

    result = await _ask(service, owner, loop.id)

    refs = [row.answer_ref for row in result["rows"]]
    # The member's thumbs-down comes first; the Owner's own Turns fill; one per session.
    assert refs[0] == f"turn:{turns['shared']}" and result["rows"][0].pick_reason == "feedback_conflict"
    assert f"turn:{turns['unshared']}" not in refs
    assert len({row.task_text for row in result["rows"]}) >= 1
    # Two real answers plus the generated minimal-edit counterexample, each graded in its own call.
    assert [row.pick_reason for row in result["rows"]][-1] == "counterexample"
    assert len(result["rows"]) == 3 and result["card_seq"] == 1
    assert any(call.endswith(COUNTER_MARK) for call in judge.calls)
    # Coach never records a label.
    assert all(row.owner_label is None for row in result["rows"])

    with pytest.raises(AgentEvalAlignmentError) as error:
        await _ask(service, owner, loop.id, point_id=result["point"]["id"], answer_refs=[f"turn:{turns['unshared']}"], replace_unclear=False)
    # The open card is unanswered, so Coach waits for the Owner before asking again.
    assert error.value.code == "agent_eval_alignment_card_open" and error.value.status == 409
    assert error.value.details == {"point_id": result["point"]["id"], "card_seq": 1, "action": "wait_for_owner_labels"}


async def test_evaluation_scratch_turns_are_never_offered_as_answers(ranking_db, monkeypatch) -> None:  # noqa: F811
    fixture, alignment, service, owner, turns, _judge = await _loop_fixture(ranking_db, monkeypatch)
    # An evaluation, release-check or trial attempt: the same Agent, the same question, an agent_eval session.
    scratch = await completed_use(fixture, monkeypatch, actor=fixture.owner_id, day="2030-02-15", suffix="scratch", origin="agent_eval")
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("UPDATE turns SET final_answer='I can produce a short summary.' WHERE id=?", (scratch,))
    loop = await _start(alignment, owner)

    candidates = await service._candidates(loop, answer_refs=(), exclude=set())

    refs = {candidate.answer_ref for candidate in candidates}
    assert f"turn:{scratch}" not in refs
    assert refs == {f"turn:{turns['own']}", f"turn:{turns['newer']}", f"turn:{turns['shared']}"}


async def test_a_session_is_represented_by_the_turn_the_loop_started_from(ranking_db, monkeypatch) -> None:  # noqa: F811
    fixture, alignment, service, owner, turns, _judge = await _loop_fixture(ranking_db, monkeypatch)
    # The Owner's older Turn and a newer one in the same conversation; the loop starts from the older one.
    with sqlite3.connect(fixture.db_path) as db:
        session = db.execute("SELECT session_id FROM turns WHERE id=?", (turns["own"],)).fetchone()[0]
        db.execute("UPDATE turns SET session_id=?,seq=2 WHERE id=?", (session, turns["newer"]))
    loop = await _start(alignment, owner, origin_turn_id=turns["own"])

    result = await _ask(service, owner, loop.id)

    refs = [row.answer_ref for row in result["rows"]]
    assert refs[0] == f"turn:{turns['own']}"
    assert f"turn:{turns['newer']}" not in refs and f"turn:{turns['shared']}" in refs


async def test_naming_an_unshared_member_turn_is_refused_like_a_missing_one(ranking_db, monkeypatch) -> None:  # noqa: F811
    fixture, alignment, service, owner, turns, _judge = await _loop_fixture(ranking_db, monkeypatch)
    loop = await _start(alignment, owner)
    with pytest.raises(AgentEvalAlignmentError) as error:
        await _ask(service, owner, loop.id, answer_refs=[f"turn:{turns['unshared']}"])
    assert error.value.code == "agent_eval_conversation_unavailable" and error.value.status == 404
    with pytest.raises(AgentEvalAlignmentError) as error:
        await alignment.start(
            **owner, session_id="s", words=None, origin_turn_id=turns["unshared"], words_from_feedback=True, judge_model_fingerprint=PIN
        )
    assert error.value.code == "agent_eval_conversation_unavailable"


async def test_the_loop_starts_from_a_members_comment_or_asks_for_one(ranking_db, monkeypatch) -> None:  # noqa: F811
    fixture, alignment, _service, owner, turns, _judge = await _loop_fixture(ranking_db, monkeypatch)
    loop = await _start(alignment, owner, words=None, origin_turn_id=turns["shared"], words_from_feedback=True)
    # The member's comment is read live through the share; the loop stores neither it nor the member.
    assert (loop.words_source, loop.owner_words, loop.words_by) == ("member_feedback", None, None)
    assert await alignment.feedback_comment(loop) == "It never says which period."
    with pytest.raises(AgentEvalAlignmentError) as error:
        await _start(alignment, owner, words=None, origin_turn_id=turns["own"], words_from_feedback=True)
    assert error.value.code == "agent_eval_alignment_feedback_comment_missing"
    # A new loop abandons the previous open one.
    second = await _start(alignment, owner)
    with sqlite3.connect(fixture.db_path) as db:
        states = dict(db.execute("SELECT id,state FROM agent_eval_alignment_loop"))
    assert states == {loop.id: "abandoned", second.id: "aligning"}


async def test_cold_start_without_real_answers_drafts_from_the_definition(ranking_db, monkeypatch) -> None:  # noqa: F811
    fixture, alignment, service, owner, turns, _judge = await _loop_fixture(ranking_db, monkeypatch)
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("UPDATE turns SET status='failed'")
    loop = await _start(alignment, owner)
    result = await _ask(service, owner, loop.id)
    assert result["cold_start"] is True and result["rows"] == []


async def test_a_judge_change_stops_the_loop_until_it_is_repinned(ranking_db, monkeypatch) -> None:  # noqa: F811
    fixture, alignment, service, owner, _turns, _judge = await _loop_fixture(ranking_db, monkeypatch)
    loop = await _start(alignment, owner)

    async def changed():
        return OTHER_PIN

    service.current_judge_fingerprint = changed
    with pytest.raises(AgentEvalAlignmentError) as error:
        await _ask(service, owner, loop.id)
    assert error.value.code == "agent_eval_alignment_judge_changed" and error.value.status == 409
    await alignment.repin(**owner, loop_id=loop.id, judge_model_fingerprint=OTHER_PIN)
    assert (await _ask(service, owner, loop.id))["card_seq"] == 1


# ── labels and confirmation ──────────────────────────────────────────────


async def _labelled_card(ranking_db, monkeypatch, judge=None):  # noqa: F811
    fixture, alignment, service, owner, turns, judge = await _loop_fixture(ranking_db, monkeypatch, judge=judge)
    loop = await _start(alignment, owner, origin_turn_id=turns["shared"])
    asked = await _ask(service, owner, loop.id)
    return fixture, alignment, service, owner, turns, loop, asked


async def test_rest_right_confirms_the_point_and_seeds_calibration_the_calibrator_agrees_with(ranking_db, monkeypatch) -> None:  # noqa: F811
    judge = _Judge()
    fixture, alignment, _service, owner, turns, loop, asked = await _labelled_card(ranking_db, monkeypatch, judge)
    point_id = asked["point"]["id"]

    result = await alignment.label(**owner, loop_id=loop.id, labels=[], rest_right=True, card_seq=1)

    assert result["outcomes"] == {point_id: "confirmed"}
    confirmed = result["confirmed"][point_id]
    assert confirmed["calibration"] == "seeded" and confirmed["case_key"] == f"align.{point_id}"
    revision = await AgentEvalStore(fixture.db_path).read_suite_revision(confirmed["suite_revision_id"])
    case = next(case for case in revision.cases if case.case_key == confirmed["case_key"])
    [point] = case.expected_contract["points"]
    assert point["state"] == "confirmed" and point["source"] == "coach_alignment"
    assert point["origin"] == {"kind": "owner_words", "words": loop.owner_words, "by": fixture.owner_id, "at": loop.created_at, "loop_id": loop.id}
    # The case is the Turn the loop started from.
    assert case.input["task"] == "Summarize the report"
    async with open_shared_ctx(fixture.db_path) as db:
        examples = await _examples_conn(db, suite_id=revision.suite_id, point_id=point_id)
        row = await _calibration_row_conn(db, suite_id=revision.suite_id, point_id=point_id)
    assert {example.label_source for example in examples} == {"owner_alignment"}
    assert {example.kind for example in examples} == {"good", "bad"}
    turn_example = next(example for example in examples if example.answer_ref.startswith("turn:"))
    assert turn_example.task_text == "Summarize the report"
    # Seeding agrees with the stored-example rule …
    assert is_calibrated(point, row, examples, prompt_version=POINTS_PROMPT_VERSION, model_fingerprint=PIN)
    # … and with the calibrator itself, grading the same examples with the same judge.
    task = CalibrationTask(
        team_id=fixture.team_id,
        agent_id="osa_exact",
        owner_user_id=fixture.owner_id,
        suite_id=revision.suite_id,
        case_revision_id=case.case_revision_id,
        task="Summarize the report",
        point=dict(point),
    )
    outcome = await PointCalibrator(store=AgentEvalPointStore(str(fixture.db_path)), judge=judge, writer=_Writer()).calibrate(
        task, model_fingerprint=PIN
    )
    assert outcome.state == "calibrated"
    with sqlite3.connect(fixture.db_path) as db:
        assert db.execute("SELECT state FROM agent_eval_alignment_loop WHERE id=?", (loop.id,)).fetchone()[0] == "fixing"


def test_seeding_requires_both_verdicts_agreement_and_the_pinned_judge() -> None:
    args = {"prompt_version": POINTS_PROMPT_VERSION, "model_fingerprint": PIN}
    agree = [(True, True, POINTS_PROMPT_VERSION, PIN), (False, False, POINTS_PROMPT_VERSION, PIN)]
    assert seed_agrees(agree, **args)
    assert not seed_agrees(agree[:1], **args)
    assert not seed_agrees([*agree, (True, False, POINTS_PROMPT_VERSION, PIN)], **args)
    assert not seed_agrees([agree[0], (False, False, POINTS_PROMPT_VERSION, OTHER_PIN)], **args)


async def test_rest_right_on_a_misjudged_counterexample_asks_about_that_row(ranking_db, monkeypatch) -> None:  # noqa: F811
    # The judge says the counterexample meets the point: a pass-through "rest right" can't confirm.
    fixture, alignment, _service, owner, _turns, loop, asked = await _labelled_card(
        ranking_db, monkeypatch, _Judge({"Sales grew.": False}, counter_met=True)
    )
    point_id = asked["point"]["id"]
    counter = next(row for row in asked["rows"] if row.pick_reason == "counterexample")

    result = await alignment.label(**owner, loop_id=loop.id, labels=[], rest_right=True, card_seq=1)
    assert result["outcomes"] == {point_id: "recheck_row"} and result["recheck_question_ids"] == [counter.id]
    assert result["confirmed"] == {}
    # Looking at that row and saying "Judged right" there is a normal label.
    again = await alignment.label(**owner, loop_id=loop.id, labels=[{"question_id": counter.id, "label": "right"}], rest_right=False, card_seq=1)
    assert again["outcomes"] == {point_id: "confirmed"}


async def test_a_fully_labelled_card_that_cannot_confirm_tells_coach_to_ask_again(ranking_db, monkeypatch) -> None:  # noqa: F811
    from bowhead_core.agent_eval_alignment import loop_view

    # The judge abstains on the Owner's own answers, so a "Judged right" there says nothing.
    abstains = _Judge({"Sales were 12 units.": None, GOOD: None})
    fixture, alignment, service, owner, _turns, loop, asked = await _labelled_card(ranking_db, monkeypatch, abstains)
    point_id = asked["point"]["id"]

    async def step() -> str:
        current, questions = await alignment.read(**owner, loop_id=loop.id)
        return loop_view(current, questions, for_coach=True)["points"][0]["next_step"]

    assert await step() == "wait_for_labels"
    result = await alignment.label(**owner, loop_id=loop.id, labels=[], rest_right=True, card_seq=1)
    assert result["outcomes"] == {point_id: "continue"}
    # Nothing is left to label on the card, so waiting would never end: Coach asks another card.
    assert await step() == "ask_more_answers"
    again = await _ask(service, owner, loop.id, point_id=point_id)
    assert again["card_seq"] == 2
    assert await step() == "wait_for_labels"


async def test_a_reworded_point_makes_open_labels_stale(ranking_db, monkeypatch) -> None:  # noqa: F811
    fixture, alignment, _service, owner, _turns, loop, asked = await _labelled_card(ranking_db, monkeypatch)
    point_id, first = asked["point"]["id"], asked["rows"][0]
    await alignment.reword(**owner, loop_id=loop.id, point_id=point_id, text="Names the month the figures cover")
    with pytest.raises(AgentEvalAlignmentError) as error:
        await alignment.label(**owner, loop_id=loop.id, labels=[{"question_id": first.id, "label": "right"}], rest_right=False, card_seq=1)
    assert error.value.code == "agent_eval_alignment_point_changed" and error.value.status == 409
    assert error.value.details == {"question_ids": [first.id]}
    with sqlite3.connect(fixture.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_eval_alignment_question WHERE owner_label IS NOT NULL").fetchone()[0] == 0


async def test_only_the_loop_owner_labels(ranking_db, monkeypatch) -> None:  # noqa: F811
    fixture, alignment, _service, owner, _turns, loop, asked = await _labelled_card(ranking_db, monkeypatch)
    for actor in (MEMBER, STRANGER):
        with pytest.raises(AgentEvalStoreError) as error:
            await alignment.label(**(owner | {"actor_user_id": actor}), loop_id=loop.id, labels=[], rest_right=True, card_seq=1)
        assert error.value.code == "agent_eval_action_denied"
    with sqlite3.connect(fixture.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_eval_alignment_question WHERE owner_label IS NOT NULL").fetchone()[0] == 0


async def test_a_withdrawn_share_leaves_the_card_and_drops_its_example(ranking_db, monkeypatch) -> None:  # noqa: F811
    fixture, alignment, _service, owner, turns, loop, asked = await _labelled_card(ranking_db, monkeypatch, _Judge())
    point_id = asked["point"]["id"]
    confirmed = (await alignment.label(**owner, loop_id=loop.id, labels=[], rest_right=True, card_seq=1))["confirmed"][point_id]
    await withdraw_turn_share(fixture.db_path, team_id=fixture.team_id, user_id=MEMBER, turn_id=turns["shared"])

    points = AgentEvalPointStore(str(fixture.db_path))
    graded = await points.examples_with_answers(
        suite_id=confirmed["suite_id"], point_id=point_id, team_id=fixture.team_id, agent_id="osa_exact", owner_user_id=fixture.owner_id
    )
    assert f"turn:{turns['shared']}" not in {example.answer_ref for example, _answer in graded}
    async with open_shared_ctx(fixture.db_path) as db:
        examples = await _examples_conn(db, suite_id=confirmed["suite_id"], point_id=point_id)
    assert f"turn:{turns['shared']}" not in {example.answer_ref for example in examples}


async def test_withdrawing_a_share_deletes_its_rows_and_the_rows_generated_from_it_at_once(ranking_db, monkeypatch) -> None:  # noqa: F811
    fixture, alignment, _service, owner, turns, loop, asked = await _labelled_card(ranking_db, monkeypatch)
    shared_row = next(row for row in asked["rows"] if row.answer_ref == f"turn:{turns['shared']}")
    counter = next(row for row in asked["rows"] if row.pick_reason == "counterexample")
    # The counterexample is a minimal edit of the member's answer, so it is linked to that Turn.
    assert counter.source_turn_id == turns["shared"] and counter.turn_id is None

    await withdraw_turn_share(fixture.db_path, team_id=fixture.team_id, user_id=MEMBER, turn_id=turns["shared"])

    with sqlite3.connect(fixture.db_path) as db:
        left = {row[0] for row in db.execute("SELECT id FROM agent_eval_alignment_question")}
    assert shared_row.id not in left and counter.id not in left
    assert left == {row.id for row in asked["rows"]} - {shared_row.id, counter.id}


async def test_a_deleted_session_drops_its_rows_on_every_read_and_before_confirming(ranking_db, monkeypatch) -> None:  # noqa: F811
    fixture, alignment, _service, owner, turns, loop, asked = await _labelled_card(ranking_db, monkeypatch)
    shared_row = next(row for row in asked["rows"] if row.answer_ref == f"turn:{turns['shared']}")
    # Label everything first, then the member deletes the conversation (a soft delete: no cascade).
    others = [{"question_id": row.id, "label": "unclear"} for row in asked["rows"] if row.id != shared_row.id]
    await alignment.label(**owner, loop_id=loop.id, labels=[{"question_id": shared_row.id, "label": "right"}, *others], rest_right=False, card_seq=1)
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("UPDATE sessions SET deleted_at='2030-02-01' WHERE id=(SELECT session_id FROM turns WHERE id=?)", (turns["shared"],))

    linked = {shared_row.id} | {row.id for row in asked["rows"] if row.source_turn_id == turns["shared"]}
    # The next label call re-checks every Turn-backed row of the loop, not only the rows it labels.
    result = await alignment.label(**owner, loop_id=loop.id, labels=[], rest_right=True, card_seq=1)
    assert set(result["dropped_question_ids"]) == linked and result["confirmed"] == {}
    _loop, questions = await alignment.read(**owner, loop_id=loop.id)

    assert not linked & {q.id for q in questions}
    assert all(turns["shared"] not in (q.task_text, q.answer_ref) for q in questions)
    assert point_text_hash("x")  # the module helper stays importable for routes


def test_no_proposal_path_lets_coach_mark_a_case_held_out() -> None:
    from bowhead_core.agent_change_proposal_store import AgentChangeProposalError, validate_proposal_payload
    from bowhead_core.agent_eval_case_proposals import validate_case_suggestions

    spec = {
        "case_key": "variant.1",
        "fixture_ref": {},
        "expected_contract": {"allowed_terminal_statuses": ["completed"], "held_out": False},
        "scorer_config": {},
        "effect_policy": {"mode": "no_effects"},
        "valid_until": None,
    }
    with pytest.raises(AgentChangeProposalError):
        validate_case_suggestions([{"id": "v1", "task": "q", "failure_mode": "f", "case_spec": spec}])
    operations = [{"op": "replace", "path": "/definition/system_prompt", "value": "x"}]
    groups = [{"id": "g1", "operation_indexes": [0]}]
    with pytest.raises(AgentChangeProposalError):
        validate_proposal_payload(operations, groups, [{"id": "v1", "task": "q", "failure_mode": "f", "covers_group_ids": ["g1"], "case_spec": spec}])


# ── review fixes ─────────────────────────────────────────────────────────


async def test_a_point_from_a_members_comment_never_stores_the_comment_or_the_member(ranking_db, monkeypatch) -> None:  # noqa: F811
    fixture, alignment, service, owner, turns, _judge = await _loop_fixture(ranking_db, monkeypatch)
    loop = await _start(alignment, owner, words=None, origin_turn_id=turns["shared"], words_from_feedback=True)
    asked = await _ask(service, owner, loop.id)
    confirmed = (await alignment.label(**owner, loop_id=loop.id, labels=[], rest_right=True, card_seq=1))["confirmed"][asked["point"]["id"]]

    with sqlite3.connect(fixture.db_path) as db:
        loop_row = db.execute("SELECT owner_words,words_by,draft_points_json FROM agent_eval_alignment_loop WHERE id=?", (loop.id,)).fetchone()
        contracts = [row[0] for row in db.execute("SELECT expected_contract_json FROM agent_eval_case_revision")]
    assert loop_row[:2] == (None, None)
    for stored in (loop_row[2], *contracts):
        assert "It never says which period" not in stored and MEMBER not in stored
    revision = await AgentEvalStore(fixture.db_path).read_suite_revision(confirmed["suite_revision_id"])
    [point] = next(case for case in revision.cases if case.case_key == confirmed["case_key"]).expected_contract["points"]
    assert point["origin"] == {"kind": "member_feedback", "at": loop.created_at, "loop_id": loop.id}
    # The loop shows the comment only while it is readable.
    await withdraw_turn_share(fixture.db_path, team_id=fixture.team_id, user_id=MEMBER, turn_id=turns["shared"])
    assert await alignment.feedback_comment(loop) is None


def test_a_member_feedback_origin_carries_no_words_or_member() -> None:
    from bowhead_core.agent_eval_points import PointOrigin

    PointOrigin.model_validate({"kind": "member_feedback", "at": "2030-02-01"})
    for bad in (
        {"kind": "member_feedback", "at": "2030-02-01", "words": "x"},
        {"kind": "member_feedback", "at": "2030-02-01", "by": "usr_1"},
        {"kind": "owner_words", "at": "2030-02-01", "words": "x"},
    ):
        with pytest.raises(ValueError):
            PointOrigin.model_validate(bad)
    # Points saved before the kind existed still read as the Owner's words.
    assert PointOrigin.model_validate({"words": "x", "by": "usr_1", "at": "2030-02-01"}).kind == "owner_words"


async def test_a_seeded_point_stops_counting_when_its_turn_becomes_unreadable(ranking_db, monkeypatch) -> None:  # noqa: F811
    fixture, alignment, _service, owner, turns, loop, asked = await _labelled_card(ranking_db, monkeypatch)
    point_id = asked["point"]["id"]
    confirmed = (await alignment.label(**owner, loop_id=loop.id, labels=[], rest_right=True, card_seq=1))["confirmed"][point_id]
    revision = await AgentEvalStore(fixture.db_path).read_suite_revision(confirmed["suite_revision_id"])
    case = next(case for case in revision.cases if case.case_key == confirmed["case_key"])
    points = AgentEvalPointStore(str(fixture.db_path))
    calibrated = await points.calibrated_point_ids(
        case_revision_id=case.case_revision_id, points=case.expected_contract["points"], model_fingerprint=PIN
    )
    assert calibrated == {point_id}

    # The member deletes the conversation: a soft delete, so nothing cascades.
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("UPDATE sessions SET deleted_at='2030-02-01' WHERE id=(SELECT session_id FROM turns WHERE id=?)", (turns["shared"],))

    assert (
        await points.calibrated_point_ids(case_revision_id=case.case_revision_id, points=case.expected_contract["points"], model_fingerprint=PIN)
        == set()
    )
    # … and the point is queued for calibration again.
    tasks = await points.calibration_tasks(team_id=fixture.team_id, agent_id="osa_exact", model_fingerprint=PIN)
    assert point_id in {task.point["id"] for task in tasks}


async def test_coach_cannot_choose_a_new_points_id_and_confirmation_never_overwrites_another_point(ranking_db, monkeypatch) -> None:  # noqa: F811
    fixture, alignment, service, owner, _turns, _judge = await _loop_fixture(ranking_db, monkeypatch)
    loop = await _start(alignment, owner)
    with pytest.raises(AgentEvalAlignmentError) as error:
        await _ask(service, owner, loop.id, point_id="p_0a1b")
    assert error.value.code == "agent_eval_alignment_point_not_found"

    asked = await _ask(service, owner, loop.id)
    point_id = asked["point"]["id"]
    suite_id = loop.suite_id
    with sqlite3.connect(fixture.db_path) as db:
        # Another point of the suite already holds this id's calibration row.
        db.execute(
            """INSERT INTO agent_eval_point_calibration(suite_id,point_id,team_id,agent_id,text_hash,prompt_version,model_fingerprint,
                   state,failure_reason,rewording,updated_at) VALUES(?,?,?,'osa_exact','sha256:old','v','sha256:old','failed','bad_judged_met',NULL,'t')""",
            (suite_id, point_id, fixture.team_id),
        )
    with pytest.raises(AgentEvalAlignmentError) as error:
        await alignment.label(**owner, loop_id=loop.id, labels=[], rest_right=True, card_seq=1)
    assert error.value.code == "agent_eval_alignment_point_conflict"
    with sqlite3.connect(fixture.db_path) as db:
        assert db.execute("SELECT text_hash FROM agent_eval_point_calibration WHERE point_id=?", (point_id,)).fetchone()[0] == "sha256:old"
        assert db.execute("SELECT COUNT(*) FROM agent_eval_alignment_question WHERE owner_label IS NOT NULL").fetchone()[0] == 0


async def test_after_a_repin_only_verdicts_of_the_new_judge_count(ranking_db, monkeypatch) -> None:  # noqa: F811
    fixture, alignment, _service, owner, _turns, loop, asked = await _labelled_card(ranking_db, monkeypatch)
    real = [row for row in asked["rows"] if row.pick_reason != "counterexample"]
    await alignment.label(**owner, loop_id=loop.id, labels=[{"question_id": row.id, "label": "right"} for row in real], rest_right=False, card_seq=1)
    await alignment.repin(**owner, loop_id=loop.id, judge_model_fingerprint=OTHER_PIN)
    counter = next(row for row in asked["rows"] if row.pick_reason == "counterexample")

    # The old card is no longer the point's epoch: its rows can't complete a confirmation.
    loop_after, questions = await alignment.read(**owner, loop_id=loop.id)
    from bowhead_core.agent_eval_alignment import epoch_questions, point_outcome

    assert epoch_questions(questions, loop_after.point(asked["point"]["id"])) == []
    assert point_outcome(questions, loop_after.point(asked["point"]["id"])) == "continue"
    # A row of the old epoch is shown as stale and can't be labelled.
    with pytest.raises(AgentEvalAlignmentError) as error:
        await alignment.label(**owner, loop_id=loop.id, labels=[{"question_id": counter.id, "label": "right"}], rest_right=False, card_seq=1)
    assert error.value.code == "agent_eval_alignment_point_changed"
    from bowhead_core.agent_eval_alignment import loop_view

    assert all(row["stale"] for row in loop_view(loop_after, questions, for_coach=False)["questions"])


async def test_replacement_rows_take_the_lowest_free_positions(ranking_db, monkeypatch) -> None:  # noqa: F811
    fixture, alignment, _service, owner, turns, loop, asked = await _labelled_card(ranking_db, monkeypatch)
    point = asked["point"]
    dropped = asked["rows"][0]
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("DELETE FROM agent_eval_alignment_question WHERE id=?", (dropped.id,))
    rows = [
        {
            "answer_ref": f"coach:aeaq_{index:032x}",
            "answer_text": "x",
            "task_text": "q",
            "answer_excerpt": "x",
            "pick_reason": "generated",
            "id": f"aeaq_{index:032x}",
        }
        for index in (90, 91, 92)
    ]
    _card, stored = await alignment.add_rows(
        **owner, loop_id=loop.id, point_id=point["id"], text_hash=point_text_hash(point["text"]), rows=rows, replacement=True
    )
    assert sorted(row.row_index for row in stored) == [1, 2, 3, 4, 5]
    with pytest.raises(AgentEvalAlignmentError) as error:
        await alignment.add_rows(
            **owner, loop_id=loop.id, point_id=point["id"], text_hash=point_text_hash(point["text"]), rows=rows[:1], replacement=True
        )
    assert error.value.code == "agent_eval_alignment_card_full" and error.value.details == {"limit": 5}


async def test_reading_the_loop_never_shows_a_row_from_a_deleted_conversation(ranking_db, monkeypatch) -> None:  # noqa: F811
    fixture, alignment, _service, owner, turns, loop, asked = await _labelled_card(ranking_db, monkeypatch)
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("UPDATE sessions SET deleted_at='2030-02-01' WHERE id=(SELECT session_id FROM turns WHERE id=?)", (turns["shared"],))
    # The Owner's GET and Coach's alignment_get both read through `read`.
    _loop, questions = await alignment.read(**owner, loop_id=loop.id)
    assert turns["shared"] not in {q.linked_turn_id for q in questions}
    with sqlite3.connect(fixture.db_path) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM agent_eval_alignment_question WHERE turn_id=? OR source_turn_id=?", (turns["shared"],) * 2).fetchone()[0]
            == 0
        )


# ── card scope (frontend review) ─────────────────────────────────────────


async def _two_cards(ranking_db, monkeypatch):  # noqa: F811
    """Card 1 answered with Can't tell on every row, then a second card for the same point."""
    fixture, alignment, service, owner, turns, loop, asked = await _labelled_card(ranking_db, monkeypatch)
    point_id = asked["point"]["id"]
    labels = [{"question_id": row.id, "label": "unclear"} for row in asked["rows"]]
    assert (await alignment.label(**owner, loop_id=loop.id, labels=labels, rest_right=False, card_seq=1))["outcomes"] == {point_id: "unclear"}
    # Coach restates the point after three Can't tell; the next card is card 2.
    second = await _ask(service, owner, loop.id, point_id=point_id, text="Names the month the figures cover")
    assert second["card_seq"] == 2
    return fixture, alignment, service, owner, loop, asked, second


async def test_rest_right_applies_only_to_the_points_current_card(ranking_db, monkeypatch) -> None:  # noqa: F811
    fixture, alignment, _service, owner, loop, first, second = await _two_cards(ranking_db, monkeypatch)
    # Pressing "The rest are judged right" on the old card must not label card 2.
    with pytest.raises(AgentEvalAlignmentError) as error:
        await alignment.label(**owner, loop_id=loop.id, labels=[], rest_right=True, card_seq=1)
    assert (error.value.code, error.value.status, error.value.details) == ("agent_eval_alignment_card_stale", 409, {"card_seq": 2})
    # A row of another card can't ride along on the current one.
    with pytest.raises(AgentEvalAlignmentError) as error:
        await alignment.label(**owner, loop_id=loop.id, labels=[{"question_id": first["rows"][0].id, "label": "right"}], rest_right=False, card_seq=2)
    assert error.value.code == "agent_eval_alignment_card_stale" and error.value.details == {"card_seq": 2}
    with sqlite3.connect(fixture.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_eval_alignment_question WHERE card_seq=2 AND owner_label IS NOT NULL").fetchone()[0] == 0

    result = await alignment.label(**owner, loop_id=loop.id, labels=[], rest_right=True, card_seq=2)
    with sqlite3.connect(fixture.db_path) as db:
        labelled = dict(db.execute("SELECT card_seq,COUNT(*) FROM agent_eval_alignment_question WHERE label_via='rest_right' GROUP BY card_seq"))
    assert labelled == {2: len(second["rows"])}
    assert result["outcomes"]


async def test_a_question_deleted_with_its_turn_is_reported_as_dropped(ranking_db, monkeypatch) -> None:  # noqa: F811
    fixture, alignment, _service, owner, turns, loop, asked = await _labelled_card(ranking_db, monkeypatch)
    shared_row = next(row for row in asked["rows"] if row.answer_ref == f"turn:{turns['shared']}")
    other = next(row for row in asked["rows"] if row.id != shared_row.id and row.source_turn_id is None)
    await withdraw_turn_share(fixture.db_path, team_id=fixture.team_id, user_id=MEMBER, turn_id=turns["shared"])

    result = await alignment.label(
        **owner,
        loop_id=loop.id,
        labels=[{"question_id": shared_row.id, "label": "right"}, {"question_id": other.id, "label": "unclear"}],
        rest_right=False,
        card_seq=1,
    )

    assert shared_row.id in result["dropped_question_ids"]
    with sqlite3.connect(fixture.db_path) as db:
        assert db.execute("SELECT owner_label FROM agent_eval_alignment_question WHERE id=?", (other.id,)).fetchone()[0] == "unclear"


async def test_a_reworded_point_marks_its_rows_stale_in_every_view(ranking_db, monkeypatch) -> None:  # noqa: F811
    from bowhead_core.agent_eval_alignment import loop_view

    fixture, alignment, _service, owner, _turns, loop, asked = await _labelled_card(ranking_db, monkeypatch)
    _loop, questions = await alignment.read(**owner, loop_id=loop.id)
    assert not any(row["stale"] for row in loop_view(_loop, questions, for_coach=True)["questions"])
    await alignment.reword(**owner, loop_id=loop.id, point_id=asked["point"]["id"], text="Names the month the figures cover")
    _loop, questions = await alignment.read(**owner, loop_id=loop.id)
    for for_coach in (True, False):
        assert all(row["stale"] for row in loop_view(_loop, questions, for_coach=for_coach)["questions"])


async def test_rest_right_never_reaches_unlabelled_rows_of_an_older_card(ranking_db, monkeypatch) -> None:  # noqa: F811
    fixture, alignment, service, owner, _turns, loop, asked = await _labelled_card(ranking_db, monkeypatch)
    point_id = asked["point"]["id"]
    # The Owner rewords before answering card 1; card 2 asks about the new text.
    await alignment.reword(**owner, loop_id=loop.id, point_id=point_id, text="Names the month the figures cover")
    second = await _ask(service, owner, loop.id, point_id=point_id, text="Names the month the figures cover")
    assert second["card_seq"] == 2

    await alignment.label(**owner, loop_id=loop.id, labels=[], rest_right=True, card_seq=2)

    with sqlite3.connect(fixture.db_path) as db:
        labelled = dict(db.execute("SELECT card_seq,COUNT(owner_label) FROM agent_eval_alignment_question GROUP BY card_seq"))
    assert labelled == {1: 0, 2: len(second["rows"])}


async def test_the_owner_sees_who_shared_a_row_and_coach_never_does(ranking_db, monkeypatch) -> None:  # noqa: F811
    from bowhead_core.agent_eval_alignment import loop_view

    fixture, alignment, _service, owner, turns, loop, asked = await _labelled_card(ranking_db, monkeypatch)
    with sqlite3.connect(fixture.db_path) as db:
        db.execute("UPDATE users SET display_name='陈晓' WHERE id=?", (MEMBER,))
    current, questions = await alignment.read(**owner, loop_id=loop.id)
    sharers = await alignment.sharers(current, questions)
    shared_row = next(row for row in asked["rows"] if row.answer_ref == f"turn:{turns['shared']}")
    assert sharers == {shared_row.id: "陈晓"}  # the Owner's own Turns name no one

    owner_view = {row["question_id"]: row for row in loop_view(current, questions, for_coach=False, sharers=sharers)["questions"]}
    assert owner_view[shared_row.id]["shared_by"] == "陈晓"
    assert all(row["shared_by"] is None for key, row in owner_view.items() if key != shared_row.id)
    coach_view = loop_view(current, questions, for_coach=True, sharers=sharers)["questions"]
    assert all("shared_by" not in row for row in coach_view)

    # Read live: once the share is withdrawn the name is gone with the row.
    await withdraw_turn_share(fixture.db_path, team_id=fixture.team_id, user_id=MEMBER, turn_id=turns["shared"])
    current, questions = await alignment.read(**owner, loop_id=loop.id)
    assert await alignment.sharers(current, questions) == {}


async def test_the_calibration_view_shows_examples_judge_and_status(ranking_db, monkeypatch) -> None:  # noqa: F811
    fixture, alignment, _service, owner, turns, loop, asked = await _labelled_card(ranking_db, monkeypatch)
    point_id = asked["point"]["id"]
    confirmed = (await alignment.label(**owner, loop_id=loop.id, labels=[], rest_right=True, card_seq=1))["confirmed"][point_id]
    points = AgentEvalPointStore(str(fixture.db_path))

    view = await points.calibration_view(**owner, suite_id=confirmed["suite_id"], point_id=point_id, model_fingerprint=PIN)

    assert view["calibration"]["state"] == "calibrated" and view["calibration"]["current_text"] is True
    assert view["calibration"]["judge"] == {"prompt_version": POINTS_PROMPT_VERSION, "model_fingerprint": PIN}
    assert {(item["kind"], item["expected_met"], item["graded_met"], item["graded_current"]) for item in view["examples"]} >= {
        ("good", True, True, True),
        ("bad", False, False, True),
    }
    assert all(item["label_source"] == "owner_alignment" for item in view["examples"])
    for actor in (MEMBER, STRANGER):
        with pytest.raises(AgentEvalStoreError):
            await points.calibration_view(
                **(owner | {"actor_user_id": actor}), suite_id=confirmed["suite_id"], point_id=point_id, model_fingerprint=PIN
            )
    # A withdrawn share leaves its example out, as calibration does.
    await withdraw_turn_share(fixture.db_path, team_id=fixture.team_id, user_id=MEMBER, turn_id=turns["shared"])
    after = await points.calibration_view(**owner, suite_id=confirmed["suite_id"], point_id=point_id, model_fingerprint=PIN)
    assert len(after["examples"]) < len(view["examples"])


def test_point_review_v2_can_split_and_v1_replies_still_read() -> None:
    from bowhead_core.agent_eval_point_review import POINT_REVIEW_PROMPT_VERSION, SUITE_REVIEW_PROMPT_VERSION, parse_point_review

    assert POINT_REVIEW_PROMPT_VERSION == "agent-eval-point-review@3" and SUITE_REVIEW_PROMPT_VERSION == "agent-eval-point-review@1"
    point = {"id": "p_0001", "kind": "judgment", "text": "Sort by growth and say which measure"}
    split = '{"points":[{"id":"p_0001","problem":"multiple","reason":"two things","rewording":null,"split":["Sort by growth","Say which measure"]}]}'
    [item] = parse_point_review(split, points=[point], other_ids=[])
    assert item["split"] == ["Sort by growth", "Say which measure"]
    v1 = '{"points":[{"id":"p_0001","problem":"multiple","reason":"two things","rewording":"Sort by growth"}]}'
    [item] = parse_point_review(v1, points=[point], other_ids=[])
    assert item["problem"] == "multiple" and item["split"] is None and item["rewording"] == "Sort by growth"
    for bad in (["only one"], ["same", "same"], [point["text"], "Other"], ["a", 3]):
        [item] = parse_point_review(json.dumps({"points": [{"id": "p_0001", "problem": "multiple", "split": bad}]}), points=[point], other_ids=[])
        assert item["split"] is None
    vague = '{"points":[{"id":"p_0001","problem":"vague","rewording":"x y","split":["a","b"]}]}'
    assert parse_point_review(vague, points=[point], other_ids=[])[0]["split"] is None


def test_point_review_is_told_the_task_is_fixed_input_and_which_language_to_answer_in() -> None:
    from bowhead_core.agent_eval_point_review import POINT_REVIEW_SYSTEM, point_review_prompt

    prompt = point_review_prompt(
        task="华东 120 万", points=[{"id": "p_0001", "kind": "judgment", "text": "给出合计"}], other_points=[], ui_language="zh-CN"
    )

    assert json.loads(prompt.split("\n", 1)[1].split("\n\n", 1)[0])["ui_language"] == "zh-CN"
    assert "fixed input" in POINT_REVIEW_SYSTEM and "never copied_value" in POINT_REVIEW_SYSTEM
    assert "ui_language" in POINT_REVIEW_SYSTEM and "Never translate" in POINT_REVIEW_SYSTEM


def test_point_review_drops_a_copied_value_the_task_states_and_a_rewrite_in_another_language() -> None:
    from bowhead_core.agent_eval_point_review import parse_point_review

    task = "本周各区域销售额：华东 120 万，华南 80 万，华北 60 万。帮我写本周销售周报。"
    stated = {"id": "p_0001", "kind": "judgment", "text": "写出华东 120 万和华北 60 万"}
    derived = {"id": "p_0002", "kind": "judgment", "text": "给出本周合计 260 万"}
    reply = {
        "points": [
            {"id": "p_0001", "problem": "copied_value", "reason": "copied", "rewording": "写出各区域销售额"},
            # The novice walkthrough: an English rewrite offered for a Chinese point in a zh-CN interface.
            {"id": "p_0002", "problem": "vague", "reason": "unclear", "rewording": "Calculate the total sales"},
        ]
    }

    fixed, english = parse_point_review(json.dumps(reply), points=[stated, derived], other_ids=[], task=task)

    assert fixed["problem"] is None
    assert english["problem"] == "vague" and english["rewording"] is None
    split = {"points": [{"id": "p_0002", "problem": "multiple", "split": ["Give the total", "Give the change"]}]}
    assert parse_point_review(json.dumps(split), points=[derived], other_ids=[], task=task)[0]["split"] is None
    # Without a task (older callers) a copied value is still reported.
    assert parse_point_review(json.dumps(reply), points=[stated, derived], other_ids=[])[0]["problem"] == "copied_value"


# ── counterexamples (116 dogfood, 1.5.84) ────────────────────────────────


class _NoCounterexampleWriter(_Writer):
    """The writer timed out or returned no usable edit: no counterexample can be written."""

    def __init__(self) -> None:
        self.working = False

    async def bad_answer(self, *, task, point, good_answer):
        return good_answer + COUNTER_MARK if self.working else None


async def _two_cards_without_counterexample(ranking_db, monkeypatch):  # noqa: F811
    fixture, alignment, service, owner, turns, judge = await _loop_fixture(ranking_db, monkeypatch)
    writer = _NoCounterexampleWriter()
    service.writer = writer
    loop = await _start(alignment, owner)
    first = await _ask(service, owner, loop.id)
    point_id = first["point"]["id"]
    await alignment.label(**owner, loop_id=loop.id, labels=[], rest_right=True, card_seq=1)
    second = await _ask(service, owner, loop.id, point_id=point_id)
    await alignment.label(**owner, loop_id=loop.id, labels=[], rest_right=True, card_seq=second["card_seq"])
    return alignment, service, owner, judge, writer, loop, point_id, first, second


async def test_a_missing_counterexample_is_reported_and_the_next_card_asks_only_for_it(ranking_db, monkeypatch, caplog) -> None:  # noqa: F811
    from bowhead_core.agent_eval_alignment import loop_view

    alignment, service, owner, judge, writer, loop, point_id, first, second = await _two_cards_without_counterexample(ranking_db, monkeypatch)
    # Each card keeps its real answers; the missing counterexample is returned and logged, never dropped silently.
    assert first["counterexample"] == second["counterexample"] == "counterexample_not_written"
    assert "counterexample" not in {row.pick_reason for row in [*first["rows"], *second["rows"]]}
    assert "agent_eval_alignment.counterexample_missing" in caplog.text and "reason=counterexample_not_written" in caplog.text
    current, questions = await alignment.read(**owner, loop_id=loop.id)
    assert loop_view(current, questions, for_coach=True)["points"][0]["next_step"] == "ask_counterexample"

    # Asking again for the same point shows no real answer again: the card holds only the counterexample,
    # a minimal edit of an answer the Owner agreed meets the point.
    writer.working = True
    graded_before = len(judge.calls)
    third = await _ask(service, owner, loop.id, point_id=point_id)
    assert third["counterexample"] == "added"
    [row] = third["rows"]
    assert row.pick_reason == "counterexample" and row.answer_text is not None and row.answer_text.endswith(COUNTER_MARK)
    agreed = {q.answer_text or q.answer_excerpt for q in questions if q.owner_label == "right" and q.judge_met is True}
    assert row.answer_text.removesuffix(COUNTER_MARK) in agreed
    # Only the counterexample was graded; the agreed answer keeps its labelled verdict.
    assert len(judge.calls) == graded_before + 1
    labels = [{"question_id": row.id, "label": "right"}]
    result = await alignment.label(**owner, loop_id=loop.id, labels=labels, rest_right=False, card_seq=third["card_seq"])
    assert result["outcomes"] == {point_id: "confirmed"}


async def test_a_counterexample_only_card_that_cannot_be_written_is_refused_with_its_reason(ranking_db, monkeypatch) -> None:  # noqa: F811
    alignment, service, owner, _judge, _writer, loop, point_id, _first, second = await _two_cards_without_counterexample(ranking_db, monkeypatch)

    with pytest.raises(AgentEvalAlignmentError) as error:
        await _ask(service, owner, loop.id, point_id=point_id)
    assert error.value.code == "agent_eval_alignment_counterexample_unavailable" and error.value.status == 503
    assert error.value.details["reason"] == "counterexample_not_written" and error.value.details["point_id"] == point_id
    # Nothing was stored, so a later ask can still add the counterexample.
    _loop, questions = await alignment.read(**owner, loop_id=loop.id)
    assert max(q.card_seq for q in questions) == second["card_seq"]


def test_a_live_attempt_is_shown_as_its_chat_text_and_graded_as_stored() -> None:
    from bowhead_core.agent_eval_alignment import reader_answer

    payload = {"delivery_surface": "chat", "exec_summary": "Sales grew 4% this week."}
    assert reader_answer(json.dumps(payload)) == ("Sales grew 4% this week.", payload)
    # Plain answers, and JSON without a Chat text, are shown and graded as stored.
    assert reader_answer("Sales grew.") == ("Sales grew.", None)
    assert reader_answer('{"rows": [1, 2]}') == ('{"rows": [1, 2]}', None)
    assert reader_answer("{not json") == ("{not json", None)
