"""Alignment loop at the HTTP and Coach boundaries (technical design §4, §6, §7, §9a, §11).

Labels, rewording and re-pinning are the Owner's own HTTP actions; no Coach Tool reaches them. Held-out
cases reach Coach's suite, evidence and CI reads only as counts, and Coach can't select one for a
diagnostic run. Point and suite review is advice only.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from types import SimpleNamespace

import pytest
from bowhead_core.agent_coach import _COACH_TOOL_EFFECTS, AlignmentAskInput, register_agent_coach_tools
from bowhead_core.agent_eval_alignment import AgentEvalAlignmentStore
from bowhead_core.agent_eval_judge import POINTS_PROMPT_VERSION
from bowhead_core.agent_eval_point_calibration import point_text_hash
from bowhead_core.agent_eval_store import AgentEvalStore
from bowhead_core.coach_ci_delegation import CoachCIError
from bowhead_core.tool_registry import ToolRegistry
from bowhead_core.user_store import User
from bowhead_gateway.routes import agents
from test_agent_coach_evaluations import case_spec, setup
from test_agents_routes import app_client as app_client

pytestmark = pytest.mark.unit

PIN = "sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def _alignment_switched_on(monkeypatch):
    # FEATURE_AGENT_EVAL_ALIGNMENT gates the loop's routes and Tools; flag-off behavior has its own tests.
    monkeypatch.setenv("FEATURE_AGENT_EVAL_ALIGNMENT", "1")


OWNER = {"team_id": "team_1", "agent_id": "osa_1", "actor_user_id": "user_1"}


async def _suite(client, *, held_out: bool = True) -> dict:
    cases = [
        {**case_spec(), "case_key": "visible", "task": "Explain the result"},
        {
            **case_spec(),
            "case_key": "hidden",
            "task": "Explain last quarter's secret plan",
            "expected_contract": {"allowed_terminal_statuses": ["completed"], **({"held_out": True} if held_out else {})},
        },
    ]
    response = await client.post("/api/v1/agents/osa_1/eval-suites", json={"name": "Quality", "cases": cases})
    assert response.status_code == 200, response.text
    return response.json()


async def _loop_with_card(db_path, *, counter_met: bool = False):
    alignment = AgentEvalAlignmentStore(str(db_path))
    loop = await alignment.start(
        **OWNER,
        session_id="s_coach",
        words="It never says which period.",
        origin_turn_id=None,
        words_from_feedback=False,
        judge_model_fingerprint=PIN,
    )
    point = await alignment.prepare_point(**OWNER, loop_id=loop.id, point_id=None, kind="judgment", text="States the period")
    rows = [
        {
            "answer_ref": f"coach:aeaq_{index:032x}",
            "answer_text": text,
            "task_text": "Sales last month?",
            "answer_excerpt": text,
            "judge_met": met,
            "judge_quote": text,
            "pick_reason": reason,
            "id": f"aeaq_{index:032x}",
        }
        for index, (text, met, reason) in enumerate(
            (
                ("Sales in January 2030 grew.", True, "generated"),
                ("January 2030 sales were 12.", True, "generated"),
                ("Sales grew.", counter_met, "counterexample"),
            ),
            start=1,
        )
    ]
    _card, stored = await alignment.add_rows(
        **OWNER, loop_id=loop.id, point_id=point["id"], text_hash=point_text_hash(point["text"]), rows=rows, replacement=False
    )
    return loop, point, stored


# ── Coach can't label, confirm or hold out ───────────────────────────────


def test_no_coach_tool_records_labels_confirms_or_holds_out() -> None:
    registry = ToolRegistry()
    register_agent_coach_tools(registry)
    names = set(_COACH_TOOL_EFFECTS)
    assert {
        "agent_eval_alignment_start",
        "agent_eval_alignment_ask",
        "agent_eval_alignment_get",
        "agent_eval_alignment_report",
        "agent_eval_turns_list",
    } <= names
    assert not any(word in name for name in names for word in ("label", "confirm", "held_out", "repin"))
    # The ask input has no field that could carry a label.
    with pytest.raises(ValueError):
        AlignmentAskInput.model_validate(
            {"agent_id": "osa_1", "loop_id": "aeal_" + "1" * 32, "point": {"kind": "judgment", "text": "x"}, "labels": [{"label": "right"}]}
        )


async def test_the_coach_alignment_adapter_has_no_label_operation(app_client, monkeypatch) -> None:
    client, db_path = app_client
    evaluations = await setup(client, db_path, monkeypatch)
    await _suite(client)
    loop, _point, stored = await _loop_with_card(db_path)
    alignment = evaluations.resources.alignment
    for operation in ("agent_eval_alignment_label", "agent_eval_alignment_confirm"):
        with pytest.raises(ValueError, match="agent_eval_alignment_operation_unsupported"):
            await alignment.call(operation, {"agent_id": "osa_1", "loop_id": loop.id, "labels": [], "rest_right": True}, turn_id="turn_owner")
    read = await alignment.call("agent_eval_alignment_get", {"agent_id": "osa_1", "loop_id": loop.id}, turn_id="turn_owner")
    assert read["points"][0]["next_step"] == "wait_for_labels"
    assert all(row["label"] is None for row in read["questions"])
    # Coach gets a question preview, never the label channel.
    assert "label_via" not in read["questions"][0]


async def test_a_card_tells_coach_to_end_its_turn_and_wait_for_the_owner(app_client, monkeypatch) -> None:
    from bowhead_gateway import agent_coach_alignment

    client, db_path = app_client
    evaluations = await setup(client, db_path, monkeypatch)
    await _suite(client)
    loop, point, stored = await _loop_with_card(db_path)

    class _Service:
        async def ask(self, **_kwargs):
            return {"cold_start": False, "point": point, "card_seq": 1, "rows": stored, "first_card": True}

    async def _service(*_args, **_kwargs):
        return _Service()

    monkeypatch.setattr(agent_coach_alignment, "alignment_service", _service)
    asked = await evaluations.resources.alignment.call(
        "agent_eval_alignment_ask",
        {
            "agent_id": "osa_1",
            "loop_id": loop.id,
            "point": {"id": point["id"], "kind": "judgment", "text": point["text"]},
            "answer_refs": [],
            "replace_unclear": False,
        },
        turn_id="turn_owner",
    )
    # Coach kept reading and asking after a card until it ran out of steps, failing the Turn under a valid card.
    assert asked["next_step"] == "wait_for_owner_labels" and asked["card"]["card_seq"] == 1
    assert "End this turn" in asked["message"] and "no more Tools" in asked["message"]


async def test_the_production_service_offers_owner_case_attempts_but_never_the_platform_contract(app_client, monkeypatch) -> None:
    from bowhead_core.agent_eval_product import AgentEvalProductStore
    from bowhead_gateway.agent_coach_alignment import alignment_service

    client, db_path = app_client
    evaluations = await setup(client, db_path, monkeypatch)
    await _suite(client)
    loop, _point, _stored = await _loop_with_card(db_path)

    async def live_answers(self, **_kwargs):
        # Settled Live-release attempts as the product store reads them, with their case's suite kind.
        common = {"answer": "I can produce a short summary.", "answered_at": "2030-02-01T00:00:00+00:00", "held_out": False}
        return [
            {
                **common,
                "answer_ref": "attempt:aer_live:aec_platform:1",
                "suite_kind": "platform_contract",
                "task": "Explain briefly what result you can produce.",
            },
            {**common, "answer_ref": "attempt:aer_live:aec_owner:1", "suite_kind": "owner_quality", "task": "Sales last month?"},
        ]

    monkeypatch.setattr(AgentEvalProductStore, "live_answers", live_answers)
    service = await alignment_service(evaluations.resources.request, team_id=loop.team_id, agent_id=loop.agent_id, actor_user_id=loop.owner_id)

    refs = {candidate.answer_ref for candidate in await service._candidates(loop, answer_refs=(), exclude=set())}

    assert "attempt:aer_live:aec_owner:1" in refs
    assert "attempt:aer_live:aec_platform:1" not in refs


async def test_coachs_reads_say_a_label_checks_the_verdict_not_the_answer(app_client, monkeypatch) -> None:
    from bowhead_core.agent_coach import register_agent_coach_tools
    from bowhead_core.agent_eval_alignment import LABEL_MEANING
    from bowhead_core.tool_registry import ToolRegistry

    from bowhead_gateway import agent_coach_alignment

    client, db_path = app_client
    evaluations = await setup(client, db_path, monkeypatch)
    await _suite(client)
    loop, point, stored = await _loop_with_card(db_path)

    class _Service:
        async def ask(self, **_kwargs):
            return {"cold_start": False, "point": point, "card_seq": 1, "rows": stored, "first_card": True}

    async def _service(*_args, **_kwargs):
        return _Service()

    monkeypatch.setattr(agent_coach_alignment, "alignment_service", _service)
    alignment = evaluations.resources.alignment
    asked = await alignment.call(
        "agent_eval_alignment_ask",
        {
            "agent_id": "osa_1",
            "loop_id": loop.id,
            "point": {"id": point["id"], "kind": "judgment", "text": point["text"]},
            "answer_refs": [],
            "replace_unclear": False,
        },
        turn_id="turn_owner",
    )
    read = await alignment.call("agent_eval_alignment_get", {"agent_id": "osa_1", "loop_id": loop.id}, turn_id="turn_owner")
    # Coach once told the Owner that a right label means the answer did what was asked.
    assert asked["labels_mean"] == read["labels_mean"] == LABEL_MEANING
    assert "verdict" in LABEL_MEANING and "not a rating of the answer" in LABEL_MEANING
    registry = ToolRegistry()
    register_agent_coach_tools(registry)
    for tool in ("agent_eval_alignment_ask", "agent_eval_alignment_get"):
        description = registry.get(tool).schema.description
        assert "whether the answer is good" in description and "verdict" in description
    # The Owner's own card explains the labels itself.
    owner_read = (await client.get(f"/api/v1/agents/osa_1/eval-alignments/{loop.id}")).json()
    assert "labels_mean" not in owner_read


async def test_a_later_coach_finds_the_open_loop_without_its_id(app_client, monkeypatch) -> None:
    from bowhead_core.agent_coach import AlignmentReadInput
    from bowhead_core.agent_eval_alignment import AgentEvalAlignmentError
    from bowhead_gateway.agent_coach_resources import AgentCoachResources

    client, db_path = app_client
    evaluations = await setup(client, db_path, monkeypatch)
    await _suite(client)
    loop, _point, _stored = await _loop_with_card(db_path)
    resources = evaluations.resources
    with sqlite3.connect(db_path) as db:
        for session in ("s_coach", "s_other"):
            db.execute(
                "INSERT INTO sessions(id,user_id,workspace_id,started_at,last_active_at) VALUES(?,'user_1','ws_1','2026-10-19','2026-10-19')",
                (session,),
            )
    in_loop_conversation = AgentCoachResources(resources.request, resources.user, "ws_1", "s_coach").alignment
    other_conversation = AgentCoachResources(resources.request, resources.user, "ws_1", "s_other").alignment
    # The Tool schema accepts a read with neither id.
    assert AlignmentReadInput.model_validate({}).model_dump() == {"agent_id": None, "loop_id": None}

    by_conversation = await in_loop_conversation.call("agent_eval_alignment_get", {"agent_id": None, "loop_id": None}, turn_id="turn_2")
    by_agent = await other_conversation.call("agent_eval_alignment_get", {"agent_id": "osa_1", "loop_id": None}, turn_id="turn_2")
    assert by_conversation["loop_id"] == by_agent["loop_id"] == loop.id
    assert by_conversation["points"][0]["next_step"] == "wait_for_labels"
    # Another conversation without an Agent has no loop to continue; the error says what to do.
    with pytest.raises(AgentEvalAlignmentError) as missing:
        await other_conversation.call("agent_eval_alignment_get", {"agent_id": None, "loop_id": None}, turn_id="turn_2")
    assert missing.value.code == "agent_eval_alignment_not_found"
    assert missing.value.details == {"agent_id": None, "scope": "conversation", "action": "agent_eval_alignment_start"}
    with pytest.raises(AgentEvalAlignmentError, match="agent_eval_alignment_agent_required"):
        await in_loop_conversation.call("agent_eval_alignment_get", {"agent_id": None, "loop_id": loop.id}, turn_id="turn_2")

    # Only the caller's own loops are found: another member's Coach in the same conversation finds nothing.
    stranger = AgentCoachResources(resources.request, dataclasses.replace(resources.user, id="user_2"), "ws_1", "s_coach").alignment
    with pytest.raises(AgentEvalAlignmentError, match="agent_eval_alignment_not_found"):
        await stranger.call("agent_eval_alignment_get", {"agent_id": None, "loop_id": None}, turn_id="turn_2")
    # An abandoned loop is not continued.
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE agent_eval_alignment_loop SET state='abandoned' WHERE id=?", (loop.id,))
    with pytest.raises(AgentEvalAlignmentError, match="agent_eval_alignment_not_found"):
        await in_loop_conversation.call("agent_eval_alignment_get", {"agent_id": None, "loop_id": None}, turn_id="turn_2")


async def test_a_point_quotes_the_owners_own_words_never_coachs_paraphrase(app_client, monkeypatch) -> None:
    from bowhead_core.agent_eval_alignment import AgentEvalAlignmentError
    from bowhead_gateway.agent_coach_resources import AgentCoachResources

    from bowhead_gateway import agent_coach_alignment

    client, db_path = app_client
    evaluations = await setup(client, db_path, monkeypatch)
    await _suite(client)

    async def pinned(_request, _team_id):
        return PIN

    monkeypatch.setattr(agent_coach_alignment, "judge_fingerprint", pinned)
    resources = evaluations.resources
    document = {
        "schema_version": 1,
        "nodes": [
            {
                "type": "reference",
                "node_id": "n1",
                "resource": {"kind": "agent", "source_kind": "system", "id": "system:agent-coach"},
                "label": "Coach",
            },
            {"type": "text", "text": " Sales answers never say which period they cover."},
        ],
    }
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO sessions(id,user_id,workspace_id,started_at,last_active_at) VALUES('s_coach','user_1','ws_1','2026-10-19','2026-10-19')"
        )
        db.execute("INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES('t_said','s_coach',1,'@Coach x','2026-10-19T10:00')")
        db.execute(
            "INSERT INTO chat_turn_user_sources(turn_id,session_id,user_id,workspace_id,user_text,attachment_ids_json,attachment_refs_json,"
            "created_at,input_document_json) VALUES('t_said','s_coach','user_1','ws_1','@Coach Sales answers never say which period they cover.',"
            "'[]','[]','2026-10-19T10:00',?)",
            (json.dumps(document),),
        )
        db.execute("INSERT INTO turns(id,session_id,seq,user_text,started_at) VALUES('t_yes','s_coach',2,'Yes, go ahead.','2026-10-19T10:05')")
    alignment = AgentCoachResources(resources.request, resources.user, "ws_1", "s_coach").alignment
    args = {"agent_id": "osa_1", "origin_turn_id": None, "words_from_feedback": False, "suite_id": None}

    # Words copied from an earlier message of the Owner stand, without the @ mention.
    copied = await alignment.call("agent_eval_alignment_start", {**args, "words": "never say which period"}, turn_id="t_yes")
    assert copied["owner_words"] == "never say which period"
    # Coach's own point text is not the Owner's statement. With several Owner messages, this Turn's message may
    # be a follow-up rather than the complaint, so the refusal lists what the Owner said to copy from.
    for turn_id in ("t_yes", "t_missing"):
        with pytest.raises(AgentEvalAlignmentError) as refused:
            await alignment.call("agent_eval_alignment_start", {**args, "words": "States the reporting period"}, turn_id=turn_id)
        assert refused.value.code == "agent_eval_alignment_words_not_owner"
        assert refused.value.details["owner_messages"] == ["Yes, go ahead.", "Sales answers never say which period they cover."]
    # When this Turn's message is the Owner's only one, it is quoted instead of the paraphrase.
    with sqlite3.connect(db_path) as db:
        db.execute("DELETE FROM turns WHERE id='t_yes'")
    paraphrase = await alignment.call("agent_eval_alignment_start", {**args, "words": "States the reporting period"}, turn_id="t_said")
    assert paraphrase["owner_words"] == "Sales answers never say which period they cover."
    loop, _questions = await AgentEvalAlignmentStore(str(db_path)).read(**OWNER, loop_id=paraphrase["loop_id"])
    assert loop.owner_words == "Sales answers never say which period they cover."


# ── labels over HTTP ─────────────────────────────────────────────────────


async def test_only_the_owner_labels_and_the_server_confirms(app_client, monkeypatch) -> None:
    client, db_path = app_client
    await setup(client, db_path, monkeypatch)
    await _suite(client)
    loop, point, stored = await _loop_with_card(db_path)
    path = f"/api/v1/agents/osa_1/eval-alignments/{loop.id}"
    app = client._transport.app
    app.dependency_overrides[agents._current_user] = lambda: User(id="owner_1", team_id="team_1", email="owner_1@example.com", status="active")

    denied = await client.post(f"{path}/labels", json={"labels": [], "rest_right": True, "card_seq": 1})
    assert denied.status_code == 404 and denied.json()["detail"]["error"]["code"] == "agent_eval_action_denied"
    assert (await client.get(path)).status_code == 404

    app.dependency_overrides[agents._current_user] = lambda: User(id="user_1", team_id="team_1", email="user_1@example.com", status="active")
    response = await client.post(f"{path}/labels", json={"labels": [], "rest_right": True, "card_seq": 1})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcomes"] == {point["id"]: "confirmed"}
    assert body["confirmed"][point["id"]]["calibration"] == "seeded"
    assert body["loop"]["points"][0]["status"] == "confirmed" and body["loop"]["state"] == "fixing"
    # Labelling a confirmed point again is refused, not silently re-applied.
    again = await client.post(f"{path}/labels", json={"labels": [{"question_id": stored[0].id, "label": "wrong"}], "card_seq": 1})
    assert again.status_code == 409 and again.json()["detail"]["error"]["code"] == "agent_eval_alignment_point_resolved"


async def test_a_stale_point_text_is_a_409_and_rewording_stores_no_suite_revision(app_client, monkeypatch) -> None:
    client, db_path = app_client
    await setup(client, db_path, monkeypatch)
    suite = await _suite(client)
    loop, point, stored = await _loop_with_card(db_path)
    path = f"/api/v1/agents/osa_1/eval-alignments/{loop.id}"
    with sqlite3.connect(db_path) as db:
        before = db.execute("SELECT COUNT(*) FROM agent_eval_suite_revision").fetchone()[0]

    reworded = await client.patch(f"{path}/points/{point['id']}", json={"text": "Names the month the figures cover"})
    assert reworded.status_code == 200, reworded.text
    assert reworded.json()["points"][0]["text"] == "Names the month the figures cover"
    stale = await client.post(f"{path}/labels", json={"labels": [{"question_id": stored[0].id, "label": "right"}], "card_seq": 1})

    assert stale.status_code == 409
    assert stale.json()["detail"]["error"] == {"code": "agent_eval_alignment_point_changed", "details": {"question_ids": [stored[0].id]}}
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_eval_suite_revision").fetchone()[0] == before
    assert suite


async def test_repin_moves_the_loop_to_the_current_judge(app_client, monkeypatch) -> None:
    client, db_path = app_client
    await setup(client, db_path, monkeypatch)
    await _suite(client)
    loop, _point, _stored = await _loop_with_card(db_path)

    async def judge_model_hash(*, team_id: str) -> str:
        return "sha256:" + "c" * 64

    monkeypatch.setattr(agents, "_require_eval_runtime", lambda _request: SimpleNamespace(judge_model_hash=judge_model_hash))
    response = await client.post(f"/api/v1/agents/osa_1/eval-alignments/{loop.id}/repin")
    assert response.status_code == 200, response.text
    assert response.json()["judge"]["model_fingerprint"] == "sha256:" + "c" * 64
    assert response.json()["next_action"] == "request_check"
    assert response.json()["judge"]["prompt_version"] == POINTS_PROMPT_VERSION


# ── held-out cases on Coach's read paths ─────────────────────────────────


async def test_suite_get_returns_a_held_out_case_without_its_content(app_client, monkeypatch) -> None:
    client, db_path = app_client
    evaluations = await setup(client, db_path, monkeypatch)
    await _suite(client)
    target = (await evaluations.suites(agent_id="osa_1", offset=0, limit=10))["items"][0]["target"]

    read = await evaluations.suite(target=target, offset=0, limit=10)

    assert {"case_key": "hidden", "held_out": True} in read["items"]
    assert read["held_out_case_count"] == 1
    assert "secret plan" not in json.dumps(read)
    visible = next(item for item in read["items"] if item["case_key"] == "visible")
    assert visible["input"] == {"task": "Explain the result"}


async def test_evidence_lists_held_out_results_only_as_counts(app_client, monkeypatch) -> None:
    from bowhead_core.agent_coach import EvalEvidenceInput
    from bowhead_core.agent_eval_product import AgentEvalProductStore

    client, db_path = app_client
    evaluations = await setup(client, db_path, monkeypatch)
    await _suite(client)
    suite = (await evaluations.suites(agent_id="osa_1", offset=0, limit=10))["items"][0]["target"]
    store = AgentEvalStore(str(db_path))
    subject = await AgentEvalProductStore(str(db_path)).resolve_team_subject(
        team_id="team_1", agent_id="osa_1", model_config_hash="sha256:" + "1" * 64
    )
    run = await store.create_run(
        subject=subject.subject, suite_revision_id=suite["revision_id"], trigger_kind="manual", idempotency_key="held", actor_user_id="user_1"
    )
    await store.mark_run_running(run.run_id)
    revision = await store.read_suite_revision(suite["revision_id"])
    for case in revision.cases:
        await store.record_case_result(
            run_id=run.run_id,
            case_revision_id=case.case_revision_id,
            attempt=1,
            status="passed",
            cost_microusd=10,
            deterministic={"passed": True},
            trace_evidence_ref="trace_x",
        )
    target = {"kind": "agent_eval_run", "agent_id": "osa_1", "run_id": run.run_id}
    result = await evaluations.call("agent_eval_evidence_get", EvalEvidenceInput(target=target, limit=10).model_dump(), turn_id="turn_owner")

    assert [item["case_key"] for item in result["items"]] == ["visible"]
    assert result["held_out"] == {"cases": 1, "attempts": 1, "passed_attempts": 1}
    hidden = next(case for case in revision.cases if case.case_key == "hidden")
    assert hidden.case_revision_id not in json.dumps(result)


async def test_coach_cannot_select_a_held_out_case_for_a_diagnostic_run(app_client, monkeypatch) -> None:
    client, db_path = app_client
    evaluations = await setup(client, db_path, monkeypatch)
    await _suite(client)
    target = (await evaluations.suites(agent_id="osa_1", offset=0, limit=10))["items"][0]["target"]
    revision = await AgentEvalStore(str(db_path)).read_suite_revision(target["revision_id"])
    by_key = {case.case_key: case.case_revision_id for case in revision.cases}
    ci = evaluations.resources.ci

    with pytest.raises(CoachCIError) as error:
        await ci._refuse_held_out([by_key["visible"], by_key["hidden"]])
    assert error.value.code == "agent_eval_case_held_out"
    await ci._refuse_held_out([by_key["visible"]])


async def test_coach_cannot_propose_a_held_out_case_but_a_loop_variant_is_held_out(app_client, monkeypatch) -> None:
    client, db_path = app_client
    evaluations = await setup(client, db_path, monkeypatch)
    await _suite(client, held_out=False)
    target = (await evaluations.suites(agent_id="osa_1", offset=0, limit=10))["items"][0]["target"]
    spec = {**case_spec(), "case_key": "variant.1", "expected_contract": {"allowed_terminal_statuses": ["completed"], "held_out": True}}
    cases = [{"id": "v1", "task": "Sales two months ago?", "failure_mode": "No period", "case_spec": spec}]
    from bowhead_core.agent_change_proposal_store import AgentChangeProposalError

    with pytest.raises(AgentChangeProposalError):
        await evaluations.propose(target=target, cases=cases, rationale="", request_id="held", turn_id="turn_owner")

    loop, point, _stored = await _loop_with_card(db_path)
    labelled = await client.post(f"/api/v1/agents/osa_1/eval-alignments/{loop.id}/labels", json={"rest_right": True, "card_seq": 1})
    assert labelled.status_code == 200, labelled.text
    target = (await evaluations.suites(agent_id="osa_1", offset=0, limit=10))["items"][0]["target"]
    plain = {**case_spec(), "case_key": "variant.1"}
    variants = [
        {"id": "v1", "task": "Sales two months ago?", "failure_mode": "No period", "case_spec": plain},
        {"id": "v2", "task": "Sales this quarter?", "failure_mode": "No period", "case_spec": {**plain, "case_key": "variant.2"}},
    ]
    proposed = await evaluations.propose(
        target=target,
        cases=variants,
        rationale="",
        request_id="variants",
        turn_id="turn_owner",
        alignment={"loop_id": loop.id, "point_id": point["id"]},
    )
    # Coach sees the held-out variant only as a key, in the receipt and in every later read.
    for proposal in (proposed["proposal"], (await evaluations.get(agent_id="osa_1", proposal_id=proposed["proposal"]["id"]))["proposal"]):
        held, visible = proposal["suggested_cases"]
        assert held == {"id": "v1", "case_key": "variant.1", "held_out": True}
        assert "Sales two months ago" not in json.dumps(proposal)
        assert visible["case_spec"]["expected_contract"]["points"][-1] == {
            **visible["case_spec"]["expected_contract"]["points"][-1],
            "id": point["id"],
            "state": "confirmed",
        }
    # The stored proposal the Owner reviews keeps both variants, the first held out.
    with sqlite3.connect(db_path) as db:
        stored = json.loads(
            db.execute("SELECT suggested_cases_json FROM agent_change_proposal WHERE id=?", (proposed["proposal"]["id"],)).fetchone()[0]
        )
    assert [case["case_spec"]["expected_contract"].get("held_out", False) for case in stored] == [True, False]


# ── point and suite review ───────────────────────────────────────────────


class _Router:
    def __init__(self, content) -> None:
        self.content = content
        self.calls = 0
        self.requests: list = []

    async def invoke(self, role, request):
        self.calls += 1
        self.requests.append(request)
        if isinstance(self.content, Exception):
            raise self.content
        return SimpleNamespace(content=self.content if isinstance(self.content, str) else json.dumps(self.content))


async def test_point_review_suggests_quietly_and_respects_keep_my_wording(app_client, monkeypatch) -> None:
    client, db_path = app_client
    await setup(client, db_path, monkeypatch)
    router = _Router(
        {
            "points": [
                {"id": "p_0001", "problem": "vague", "reason": "Can't be checked", "rewording": "Names the month"},
                {"id": "p_0002", "problem": "multiple", "reason": "Two things", "rewording": "States the total"},
            ]
        }
    )

    async def resolve(_state, _team, *, agent_type, site):
        assert (agent_type, site) == ("judge", "agent_eval_point_review")
        return router, None

    monkeypatch.setattr(agents, "resolve_team_router", resolve)
    body = {
        "task": "Sales last month?",
        "points": [
            {"id": "p_0001", "kind": "judgment", "text": "Good answer"},
            {
                "id": "p_0002",
                "kind": "judgment",
                "text": "States total and trend",
                "review_dismissed_hash": point_text_hash("States total and trend"),
            },
        ],
    }
    response = await client.post("/api/v1/agents/osa_1/eval-points/review", json=body)
    assert response.status_code == 200, response.text
    points = {item["id"]: item for item in response.json()["points"]}
    assert points["p_0001"]["problem"] == "vague" and points["p_0001"]["rewording"] == "Names the month"
    # "Keep my wording" at this exact text: nothing comes back.
    assert points["p_0002"]["problem"] is None
    # The reviewer is told the Owner's interface language for advice on points in an unclear language.
    review_data = json.loads(router.requests[-1].messages[0].content.split("\n", 1)[1].split("\n\n", 1)[0])
    assert review_data["ui_language"] in {"en-US", "zh-CN", "de-DE", "fr-FR"}
    assert review_data["task"] == "Sales last month?"

    router.content = "not json"
    quiet = await client.post("/api/v1/agents/osa_1/eval-points/review", json=body)
    assert quiet.status_code == 200 and quiet.json()["status"] == "unavailable" and quiet.json()["points"] == []
    router.content = RuntimeError("model down")
    assert (await client.post("/api/v1/agents/osa_1/eval-points/review", json=body)).json()["status"] == "unavailable"


async def test_slow_review_returns_pending_promptly_and_the_same_request_collects_it(app_client, monkeypatch) -> None:
    import asyncio

    from bowhead_gateway.agent_eval_review_jobs import ReviewJobs

    client, db_path = app_client
    evaluations = await setup(client, db_path, monkeypatch)
    await _suite(client, held_out=False)
    target = (await evaluations.suites(agent_id="osa_1", offset=0, limit=10))["items"][0]["target"]
    release = asyncio.Event()

    class SlowRouter(_Router):
        async def invoke(self, role, request):
            # A reasoning model taking longer than the request waits (well past the old 30 s cut).
            await release.wait()
            return await super().invoke(role, request)

    router = SlowRouter({"gaps": [{"text": "Refund questions", "suggested_task": "How do refunds work?"}], "duplicate_cases": []})

    async def resolve(_state, _team, *, agent_type, site):
        return router, None

    monkeypatch.setattr(agents, "resolve_team_router", resolve)
    client._transport.app.state.agent_eval_review_jobs = ReviewJobs(wait_seconds=0.05)
    path = f"/api/v1/agents/osa_1/eval-suites/{target['suite_id']}/review"
    pending = await client.post(path, json={"suite_revision_id": target["revision_id"]})
    assert pending.status_code == 200 and pending.json()["status"] == "pending"
    release.set()
    await asyncio.sleep(0.05)
    done = (await client.post(path, json={"suite_revision_id": target["revision_id"]})).json()
    assert done["status"] == "reviewed" and done["gaps"] == [{"text": "Refund questions", "suggested_task": "How do refunds work?"}]
    assert router.calls == 1


async def test_suite_review_is_cached_per_revision_and_failure_is_silent(app_client, monkeypatch) -> None:
    client, db_path = app_client
    evaluations = await setup(client, db_path, monkeypatch)
    await _suite(client, held_out=False)
    target = (await evaluations.suites(agent_id="osa_1", offset=0, limit=10))["items"][0]["target"]
    router = _Router(RuntimeError("model down"))

    async def resolve(_state, _team, *, agent_type, site):
        return router, None

    monkeypatch.setattr(agents, "resolve_team_router", resolve)
    path = f"/api/v1/agents/osa_1/eval-suites/{target['suite_id']}/review"
    failed = await client.post(path, json={"suite_revision_id": target["revision_id"]})
    assert failed.status_code == 200 and failed.json()["status"] == "unavailable" and failed.json()["gaps"] == []

    router.content = {
        "gaps": [{"text": "Refund questions", "suggested_task": "How do refunds work?"}],
        "duplicate_cases": [["visible", "hidden"], ["visible", "nope"]],
    }
    first = (await client.post(path, json={"suite_revision_id": target["revision_id"]})).json()
    second = (await client.post(path, json={"suite_revision_id": target["revision_id"]})).json()
    assert first["status"] == "reviewed" and first["cached"] is False
    assert first["gaps"] == [{"text": "Refund questions", "suggested_task": "How do refunds work?"}]
    assert first["duplicate_cases"] == [["hidden", "visible"]]
    assert second["cached"] is True and second["gaps"] == first["gaps"]
    assert router.calls == 2


async def test_labels_name_their_card_and_each_report_gets_a_new_id(app_client, monkeypatch) -> None:
    from bowhead_gateway import agent_coach_alignment

    client, db_path = app_client
    evaluations = await setup(client, db_path, monkeypatch)
    await _suite(client)
    loop, _point, _stored = await _loop_with_card(db_path)
    path = f"/api/v1/agents/osa_1/eval-alignments/{loop.id}"
    assert (await client.post(f"{path}/labels", json={"labels": [], "rest_right": True})).status_code == 422
    stale = await client.post(f"{path}/labels", json={"labels": [], "rest_right": True, "card_seq": 7})
    assert stale.status_code == 409 and stale.json()["detail"]["error"] == {"code": "agent_eval_alignment_card_stale", "details": {"card_seq": 1}}

    async def execution(_db, _loop):
        return "aex_1"

    async def build(_db, current, *, execution_id):
        return {"kind": "agent_eval_alignment_report", "schema_version": 1, "loop_id": current.id, "status": "measured"}

    monkeypatch.setattr(agent_coach_alignment, "latest_loop_execution_conn", execution)
    monkeypatch.setattr(agent_coach_alignment, "build_report_conn", build)
    alignment = evaluations.resources.alignment
    args = {"agent_id": "osa_1", "loop_id": loop.id}
    first = await alignment.call("agent_eval_alignment_report", args, turn_id="turn_owner")
    second = await alignment.call("agent_eval_alignment_report", args, turn_id="turn_owner")
    assert first["report_id"] != second["report_id"] and second["report_card"]["report_id"] == second["report_id"]
    stored = (await client.get(path)).json()["report"]
    # Only the current report is kept; an older card's pointer id no longer matches.
    assert stored["report_id"] == second["report_id"]


async def test_the_calibration_route_is_the_owners(app_client, monkeypatch) -> None:
    client, db_path = app_client
    await setup(client, db_path, monkeypatch)
    await _suite(client)
    loop, point, _stored = await _loop_with_card(db_path)
    confirmed = (await client.post(f"/api/v1/agents/osa_1/eval-alignments/{loop.id}/labels", json={"rest_right": True, "card_seq": 1})).json()
    suite_id = confirmed["confirmed"][point["id"]]["suite_id"]
    path = f"/api/v1/agents/osa_1/eval-suites/{suite_id}/points/{point['id']}/calibration"

    response = await client.get(path)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["calibration"]["state"] == "calibrated" and len(body["examples"]) == 3
    # The GET loop names no sharer here: these rows are generated, not from a member's Turn.
    loop_read = (await client.get(f"/api/v1/agents/osa_1/eval-alignments/{loop.id}")).json()
    assert all(row["shared_by"] is None for row in loop_read["questions"])

    app = client._transport.app
    app.dependency_overrides[agents._current_user] = lambda: User(id="owner_1", team_id="team_1", email="owner_1@example.com", status="active")
    assert (await client.get(path)).status_code == 404


async def test_the_calibration_route_names_the_evaluation_model_like_team_settings(app_client, monkeypatch) -> None:
    from bowhead_gateway.routes import team_subagents

    client, db_path = app_client
    await setup(client, db_path, monkeypatch)
    await _suite(client)
    loop, point, _stored = await _loop_with_card(db_path)
    suite_id = (await client.post(f"/api/v1/agents/osa_1/eval-alignments/{loop.id}/labels", json={"rest_right": True, "card_seq": 1})).json()[
        "confirmed"
    ][point["id"]]["suite_id"]
    path = f"/api/v1/agents/osa_1/eval-suites/{suite_id}/points/{point['id']}/calibration"

    async def names(_store, _team):
        return {"judge": "qwen-max", "analyst": "deepseek-v3"}

    monkeypatch.setattr(team_subagents, "_team_tier_model_names", names)
    assert (await client.get(path)).json()["judge_model_name"] == "qwen-max"

    async def none(_store, _team):
        return {}

    monkeypatch.setattr(team_subagents, "_team_tier_model_names", none)
    assert (await client.get(path)).json()["judge_model_name"] is None
