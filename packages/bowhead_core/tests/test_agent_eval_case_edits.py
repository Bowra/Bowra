"""Existing-case suggestions preserve suite history and exact adoption scope."""

import copy
import json
import sqlite3

import pytest
from bowhead_core.agent_change_proposal_store import AgentChangeProposalError, AgentChangeProposalStore
from bowhead_core.agent_eval_case_proposals import AgentEvalCaseProposals
from bowhead_core.agent_eval_store import AgentEvalStoreError, EvalCaseDraft
from test_agent_eval_case_proposals import case, setup_case

pytestmark = pytest.mark.unit


async def setup_edits(tmp_path, *, held_out=False):
    path, store, suite, _, intent = await setup_case(tmp_path)
    point = {"id": "p_ab12", "kind": "judgment", "text": "Names the reporting period.", "source": "owner", "state": "confirmed"}
    expected = {"points": [point], **({"held_out": True} if held_out else {})}
    revision = await store.create_suite_revision(
        suite_id=suite.suite_id,
        definition={"schema_version": 1},
        scorer_bundle_ref="evalkit.core@1",
        judge_policy={"enabled": False, "rubric_ref": None},
        actor_user_id="user_owner",
        cases=(
            EvalCaseDraft(
                input={"task": "Original question", "retained_input": "Keep this"}, **{**case("existing")["case_spec"], "expected_contract": expected}
            ),
            EvalCaseDraft(input={"task": "Unrelated question"}, **case("other")["case_spec"]),
        ),
    )
    original = next(value for value in revision.cases if value.case_key == "existing")
    edit = {
        "operation": "edit",
        "id": "edit_one",
        "case_revision_id": original.case_revision_id,
        "case_hash": original.case_hash,
        "changes": {"task": "Clarify the requested period"},
        "failure_mode": "Ambiguous period",
    }
    return path, store, revision, original, {**intent, "revision_id": revision.suite_revision_id, "suggested_cases": [edit]}


async def adopt(path, proposal, suite_revision_id, identities=None):
    identities = identities or [case["id"] for case in proposal.suggested_cases]
    chosen = [case for case in proposal.suggested_cases if case["id"] in identities]
    return await AgentChangeProposalStore(str(path)).accept_suggested_cases(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        proposal_id=proposal.id,
        expected_proposal_row_version=proposal.row_version,
        expected_suite_revision_id=suite_revision_id,
        cases=tuple((case["id"], EvalCaseDraft(input={"task": case["task"]}, **case["case_spec"])) for case in chosen),
    )


async def test_edit_replaces_only_the_original_case_and_replays_after_adoption(tmp_path):
    path, store, revision, original, intent = await setup_edits(tmp_path)
    creator = AgentEvalCaseProposals(str(path))
    saved = await creator.create(**intent)
    assert saved.suggested_cases[0]["edit"]["changed_fields"] == ["task"]
    assert saved.suggested_cases[0]["edit"]["before"]["task"] == "Original question"
    assert (await store.read_suite_revision(revision.suite_revision_id)).cases == revision.cases
    result = await adopt(path, saved, revision.suite_revision_id)
    updated = await store.read_suite_revision(result["suite_revision_id"])
    assert updated.case_count == 2
    changed = next(case for case in updated.cases if case.case_key == original.case_key)
    assert changed.input == {"task": "Clarify the requested period", "retained_input": "Keep this"}
    assert changed.expected_contract == original.expected_contract
    assert (
        next(case for case in updated.cases if case.case_key == "other").case_hash
        == next(case for case in revision.cases if case.case_key == "other").case_hash
    )
    assert (await store.read_suite_revision(revision.suite_revision_id)).cases == revision.cases
    recovered = await creator.create(**intent)
    assert recovered.id == saved.id and recovered.status == "accepted"
    assert recovered.suggested_cases[0]["decision_status"] == "accepted"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM agent_change_proposal_decision WHERE proposal_id=?", (saved.id,)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM team_subagent_working_copy").fetchone()[0] == 0
    changed_intent = copy.deepcopy(intent)
    changed_intent["suggested_cases"][0]["changes"]["task"] = "Different intent"
    with pytest.raises(AgentChangeProposalError, match="idempotency_conflict"):
        await creator.create(**changed_intent)


async def test_partial_edit_and_addition_follow_only_their_own_revision_lineage(tmp_path):
    path, store, revision, _, intent = await setup_edits(tmp_path)
    intent["suggested_cases"].append(case("added"))
    creator = AgentEvalCaseProposals(str(path))
    proposal = await creator.create(**intent)
    first = await adopt(path, proposal, revision.suite_revision_id, ["added"])
    current = await creator.create(**intent)
    final = await adopt(path, current, first["suite_revision_id"], ["edit_one"])
    assert (await store.read_suite_revision(final["suite_revision_id"])).case_count == 3
    assert (await creator.create(**intent)).status == "accepted"


@pytest.mark.parametrize("fault", ["held_out", "hash", "wrong_revision", "unknown_field", "confirmed_point", "unchanged"])
async def test_invalid_or_hidden_edits_never_persist(tmp_path, fault):
    path, _, _, _, intent = await setup_edits(tmp_path, held_out=fault == "held_out")
    edit = intent["suggested_cases"][0]
    if fault == "hash":
        edit["case_hash"] = "sha256:" + "f" * 64
    if fault == "wrong_revision":
        edit["case_revision_id"] = "aec_missing"
    if fault == "unknown_field":
        edit["changes"] = {"case_key": "renamed"}
    if fault == "unchanged":
        edit["changes"] = {"task": "Original question"}
    if fault == "confirmed_point":
        edit["changes"] = {
            "expected_contract": {
                "points": [{"id": "p_ab12", "kind": "judgment", "text": "Changed without confirmation", "source": "owner", "state": "confirmed"}]
            }
        }
    with pytest.raises(AgentChangeProposalError):
        await AgentEvalCaseProposals(str(path)).create(**intent)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM agent_change_proposal").fetchone()[0] == 0


async def test_external_suite_edit_blocks_replacement_and_records_no_decision(tmp_path):
    path, store, revision, _, intent = await setup_edits(tmp_path)
    saved = await AgentEvalCaseProposals(str(path)).create(**intent)
    newer = await store.create_suite_revision(
        suite_id=revision.suite_id,
        definition=revision.definition,
        scorer_bundle_ref=revision.scorer_bundle_ref,
        judge_policy=revision.judge_policy,
        actor_user_id="user_owner",
        cases=(EvalCaseDraft(input={"task": "Owner's newer question"}, **case("existing")["case_spec"]),),
    )
    for target in (revision.suite_revision_id, newer.suite_revision_id):
        with pytest.raises((AgentChangeProposalError, AgentEvalStoreError), match="version_conflict"):
            await adopt(path, saved, target)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM agent_change_proposal_decision").fetchone()[0] == 0


async def test_owner_cannot_rename_an_edit_into_an_added_case(tmp_path):
    path, _, revision, _, intent = await setup_edits(tmp_path)
    saved = await AgentEvalCaseProposals(str(path)).create(**intent)
    spec = {**saved.suggested_cases[0]["case_spec"], "case_key": "different"}
    with pytest.raises(AgentChangeProposalError, match="suggested_case_invalid"):
        await AgentChangeProposalStore(str(path)).accept_suggested_cases(
            team_id="team_release",
            agent_id="osa_release",
            actor_user_id="user_owner",
            proposal_id=saved.id,
            expected_proposal_row_version=saved.row_version,
            expected_suite_revision_id=revision.suite_revision_id,
            cases=(("edit_one", EvalCaseDraft(input={"task": "Owner question"}, **spec)),),
        )


async def test_contract_edit_retains_owner_point_and_suggests_only_the_new_point(tmp_path):
    path, store, revision, original, intent = await setup_edits(tmp_path)
    suggested = {"id": "p_cd34", "kind": "judgment", "text": "Explains missing evidence.", "source": "coach_task", "state": "suggested"}
    intent["suggested_cases"][0]["changes"] = {
        "expected_contract": {"points": [*original.expected_contract["points"], suggested]},
        "scorer_config": {"max_tool_calls": 2},
        "valid_until": "2027-01-01T00:00:00Z",
    }
    saved = await AgentEvalCaseProposals(str(path)).create(**intent)
    result = await adopt(path, saved, revision.suite_revision_id)
    changed = next(case for case in (await store.read_suite_revision(result["suite_revision_id"])).cases if case.case_key == "existing")
    assert changed.input == original.input
    assert changed.expected_contract["points"] == [*original.expected_contract["points"], suggested]
    assert changed.scorer_config == {"max_tool_calls": 2} and changed.valid_until == "2027-01-01T00:00:00Z"


async def test_missing_migration_guard_refuses_edits_without_persistence(tmp_path):
    path, _, _, _, intent = await setup_edits(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("DROP TRIGGER trg_coach_case_edit_scope")
    with pytest.raises(AgentChangeProposalError, match="schema_required"):
        await AgentEvalCaseProposals(str(path)).create(**intent)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM agent_change_proposal").fetchone()[0] == 0


@pytest.mark.parametrize("fault", ["hash", "case_key", "original_revision", "intent"])
async def test_database_refuses_forged_edit_metadata(tmp_path, fault):
    path, _, _, _, intent = await setup_edits(tmp_path)
    saved = await AgentEvalCaseProposals(str(path)).create(**intent)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        row = dict(db.execute("SELECT * FROM agent_change_proposal WHERE id=?", (saved.id,)).fetchone())
        row.update(id="acp_forged", source_ref="forged")
        cases = json.loads(row["suggested_cases_json"])
        if fault == "hash":
            cases[0]["edit"]["case_hash"] = "sha256:" + "f" * 64
        if fault == "case_key":
            cases[0]["case_spec"]["case_key"] = "forged"
        if fault == "original_revision":
            cases[0]["edit"]["case_revision_id"] = "aec_missing"
        if fault == "intent":
            row["provenance_json"] = "{}"
        row["suggested_cases_json"] = json.dumps(cases)
        with pytest.raises(sqlite3.IntegrityError, match="Coach case edit requires"):
            db.execute(f"INSERT INTO agent_change_proposal({','.join(row)}) VALUES({','.join('?' for _ in row)})", tuple(row.values()))
        assert db.execute("SELECT count(*) FROM agent_change_proposal").fetchone()[0] == 1
