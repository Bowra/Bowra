from __future__ import annotations

import sqlite3

import pytest
from bowhead_core.agent_change_proposal_store import AgentChangeProposalError, AgentChangeProposalStore, AgentProposalBaseline
from bowhead_core.agent_eval_store import AgentEvalStore, EvalCaseDraft
from bowhead_core.agent_revision_store import AgentAudience, AgentRevisionStore
from test_agent_revision_store import _db, _definition, _listing, _seed, _Validator


async def _working(db_path):
    return await AgentRevisionStore(str(db_path), validator=_Validator()).save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition("Base prompt."),
        listing=_listing("Base Agent"),
        audience=AgentAudience("private"),
        expected_row_version=None,
    )


@pytest.mark.parametrize(
    "identities,duplicate_keys,error,rule",
    [
        (("same", "same"), False, "agent_change_suggested_case_invalid", "duplicate_case_id"),
        (("case_2", None), False, "agent_change_suggested_case_invalid", "duplicate_case_id"),
        (("", "second"), False, "agent_change_suggested_case_invalid", "id_invalid"),
        (("first", "second"), True, "agent_eval_case_conflict", None),
    ],
)
async def test_ambiguous_case_identities_reject_the_whole_proposal(tmp_path, identities, duplicate_keys, error, rule):
    db_path = _db(tmp_path)
    _seed(db_path)
    await _working(db_path)
    cases = [
        {
            "task": f"Review scenario {index}",
            "failure_mode": "Unreviewed result",
            "covers_group_ids": ["behavior"],
            **({"id": identity} if identity is not None else {}),
            **(
                {
                    "case_spec": {
                        "case_key": "quality.duplicate",
                        "fixture_ref": {},
                        "expected_contract": {},
                        "scorer_config": {},
                        "effect_policy": {"mode": "no_effects"},
                    }
                }
                if duplicate_keys
                else {}
            ),
        }
        for index, identity in enumerate(identities)
    ]
    with pytest.raises(AgentChangeProposalError, match=error) as rejected:
        await AgentChangeProposalStore(str(db_path)).create(
            team_id="team_release",
            agent_id="osa_release",
            effective_owner_user_id="user_owner",
            source_type="coach",
            source_ref="coach:case-collision",
            operations=[{"op": "replace", "path": "/definition/system_prompt", "value": "Review the result."}],
            atomic_groups=[{"id": "behavior", "operation_indexes": [0]}],
            suggested_cases=cases,
        )
    if rule is not None:
        # The refusal names the failed rule so Coach can correct the case instead of retrying blindly.
        assert rejected.value.details["rule"] == rule
        assert rejected.value.details["field"] == "id"
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM agent_change_proposal").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM agent_change_proposal_decision").fetchone()[0] == 0


async def test_coach_exact_replay_precedes_new_baseline_validation_and_preserves_intent(tmp_path):
    db_path = _db(tmp_path)
    _seed(db_path)
    working = await _working(db_path)
    revisions = AgentRevisionStore(str(db_path), validator=_Validator())
    store = AgentChangeProposalStore(str(db_path), revision_store=revisions)
    baseline = AgentProposalBaseline(working.id, working.row_version, working.base_release_id)
    intent = {
        "team_id": "team_release",
        "agent_id": "osa_release",
        "effective_owner_user_id": "user_owner",
        "source_type": "coach",
        "source_ref": "coach:t_original:one",
        "strict_replay": True,
        "expected_baseline": baseline,
        "provenance": {"source_turn_id": "t_original"},
        "operations": [{"op": "replace", "path": "/definition/system_prompt", "value": "Suggested instructions."}],
        "atomic_groups": [{"id": "behavior", "operation_indexes": [0]}],
        "rationale": "Improve the instructions.",
    }
    preview = await store.preview(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        expected_baseline=baseline,
        operations=intent["operations"],
        atomic_groups=intent["atomic_groups"],
    )
    assert preview["validation_kind"] == "proposal_structure" and not preview["persisted"] and not preview["executed"]
    assert preview["changed_paths"] == ["/definition/system_prompt"]
    first = await store.create(**intent)
    await revisions.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition("Newer human instructions."),
        listing=_listing("Base Agent"),
        audience=AgentAudience("private"),
        expected_row_version=working.row_version,
    )
    recovered = await store.create(**intent)
    assert recovered.id == first.id and recovered.base_working_copy_version == working.row_version
    with pytest.raises(AgentChangeProposalError, match="version_conflict"):
        await store.create(**{**intent, "source_ref": "coach:t_original:two"})
    for change in (
        {"rationale": "Different intent"},
        {"provenance": {"source_turn_id": "t_other"}},
        {"atomic_groups": [{"id": "changed-group", "operation_indexes": [0]}]},
    ):
        with pytest.raises(AgentChangeProposalError, match="idempotency_conflict"):
            await store.create(**{**intent, **change})
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM agent_change_proposal").fetchone()[0] == 1
        assert "Newer human instructions." in db.execute("SELECT definition_json FROM team_subagent_working_copy").fetchone()[0]


@pytest.mark.asyncio
async def test_proposal_applies_selected_atomic_groups_and_keeps_audit(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    working = await _working(db_path)
    store = AgentChangeProposalStore(str(db_path), revision_store=AgentRevisionStore(str(db_path), validator=_Validator()))
    proposal = await store.create(
        team_id="team_release",
        agent_id="osa_release",
        effective_owner_user_id="user_owner",
        source_type="coach",
        source_ref="feedback:fc_1",
        operations=[
            {"op": "replace", "path": "/definition/system_prompt", "value": "Coach prompt."},
            {"op": "replace", "path": "/listing/description", "value": "Coach listing."},
        ],
        atomic_groups=[
            {"id": "behavior", "operation_indexes": [0], "depends_on": []},
            {"id": "listing", "operation_indexes": [1], "depends_on": []},
        ],
        suggested_cases=[{"task": "Check answer", "covers_group_ids": ["behavior"], "failure_mode": "missed instruction"}],
        rationale="Apply only what the Owner selects.",
    )

    result = await store.apply(
        team_id="team_release",
        agent_id="osa_release",
        proposal_id=proposal.id,
        actor_user_id="user_owner",
        selected_group_ids=["behavior"],
        expected_proposal_row_version=proposal.row_version,
        expected_working_copy_row_version=working.row_version,
    )
    assert result["working_copy"].definition["system_prompt"] == "Coach prompt."
    assert result["working_copy"].listing["description"] == "A useful specialist."
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT status,row_version FROM agent_change_proposal WHERE id=?", (proposal.id,)).fetchone() == ("accepted", 2)
        assert db.execute("SELECT decision_kind FROM agent_change_proposal_decision WHERE proposal_id=?", (proposal.id,)).fetchone() == (
            "patch_accept",
        )


@pytest.mark.asyncio
async def test_proposal_three_way_conflict_writes_nothing(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    working = await _working(db_path)
    revision_store = AgentRevisionStore(str(db_path), validator=_Validator())
    store = AgentChangeProposalStore(str(db_path), revision_store=revision_store)
    proposal = await store.create(
        team_id="team_release",
        agent_id="osa_release",
        effective_owner_user_id="user_owner",
        source_type="git",
        source_ref="repo@abc123",
        operations=[{"op": "replace", "path": "/definition/system_prompt", "value": "Proposed."}],
        atomic_groups=[{"id": "behavior", "operation_indexes": [0], "depends_on": []}],
    )
    current = await revision_store.save_working_copy(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        definition=_definition("Owner edit."),
        listing=_listing("Base Agent"),
        audience=AgentAudience("private"),
        expected_row_version=working.row_version,
    )

    with pytest.raises(AgentChangeProposalError, match="agent_change_merge_conflict") as raised:
        await store.apply(
            team_id="team_release",
            agent_id="osa_release",
            proposal_id=proposal.id,
            actor_user_id="user_owner",
            selected_group_ids=["behavior"],
            expected_proposal_row_version=proposal.row_version,
            expected_working_copy_row_version=current.row_version,
        )
    assert raised.value.conflicts[0]["field_path"] == "/definition/system_prompt"
    assert (await store.get(team_id="team_release", agent_id="osa_release", proposal_id=proposal.id, actor_user_id="user_owner")).status == "open"


@pytest.mark.asyncio
async def test_git_source_ref_is_an_idempotency_boundary(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    await _working(db_path)
    store = AgentChangeProposalStore(str(db_path), revision_store=AgentRevisionStore(str(db_path), validator=_Validator()))
    first = await store.create(
        team_id="team_release",
        agent_id="osa_release",
        effective_owner_user_id="user_owner",
        source_type="git",
        source_ref="git:idempotency:abc",
        operations=[{"op": "replace", "path": "/listing/description", "value": "First"}],
        atomic_groups=[{"id": "listing", "operation_indexes": [0], "depends_on": []}],
    )
    replay = await store.create(
        team_id="team_release",
        agent_id="osa_release",
        effective_owner_user_id="user_owner",
        source_type="git",
        source_ref="git:idempotency:abc",
        operations=[{"op": "replace", "path": "/listing/description", "value": "First"}],
        atomic_groups=[{"id": "listing", "operation_indexes": [0], "depends_on": []}],
    )
    assert replay.id == first.id

    with pytest.raises(AgentChangeProposalError, match="agent_change_proposal_idempotency_conflict"):
        await store.create(
            team_id="team_release",
            agent_id="osa_release",
            effective_owner_user_id="user_owner",
            source_type="git",
            source_ref="git:idempotency:abc",
            operations=[{"op": "replace", "path": "/listing/description", "value": "Different"}],
            atomic_groups=[{"id": "listing", "operation_indexes": [0], "depends_on": []}],
        )


@pytest.mark.asyncio
async def test_proposal_rejects_forbidden_paths_and_invalid_group_dag(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    store = AgentChangeProposalStore(str(db_path), revision_store=AgentRevisionStore(str(db_path), validator=_Validator()))

    await _working(db_path)

    async def create(path, groups):
        return await store.create(
            team_id="team_release",
            agent_id="osa_release",
            effective_owner_user_id="user_owner",
            source_type="coach",
            source_ref=path,
            operations=[{"op": "replace", "path": path, "value": "x"}],
            atomic_groups=groups,
        )

    with pytest.raises(AgentChangeProposalError, match="agent_change_proposal_path_forbidden"):
        await create("/owner_user_id", [{"id": "a", "operation_indexes": [0], "depends_on": []}])
    with pytest.raises(AgentChangeProposalError, match="agent_change_groups_invalid"):
        await create(
            "/definition/system_prompt",
            [{"id": "a", "operation_indexes": [0], "depends_on": ["a"]}],
        )


@pytest.mark.asyncio
async def test_suggested_cases_are_decided_separately_and_append_atomically(tmp_path) -> None:
    db_path = _db(tmp_path)
    _seed(db_path)
    await _working(db_path)
    eval_store = AgentEvalStore(str(db_path))
    suite = await eval_store.create_suite(
        team_id="team_release",
        agent_id="osa_release",
        agent_source_kind="team",
        suite_kind="owner_quality",
        name="Owner quality",
        visibility="owner",
        actor_user_id="user_owner",
    )
    original = await eval_store.create_suite_revision(
        suite_id=suite.suite_id,
        definition={"schema_version": 1},
        scorer_bundle_ref="evalkit.core@1",
        judge_policy={"enabled": False, "rubric_ref": None},
        cases=(
            EvalCaseDraft(
                case_key="existing",
                input={"task": "Existing check"},
                fixture_ref={},
                expected_contract={},
                scorer_config={},
                effect_policy={"mode": "no_effects"},
            ),
        ),
        actor_user_id="user_owner",
    )
    proposal_store = AgentChangeProposalStore(
        str(db_path),
        revision_store=AgentRevisionStore(str(db_path), validator=_Validator()),
    )
    proposal = await proposal_store.create(
        team_id="team_release",
        agent_id="osa_release",
        effective_owner_user_id="user_owner",
        source_type="coach",
        source_ref="owner:case-proposal",
        operations=[{"op": "replace", "path": "/definition/system_prompt", "value": "Proposed."}],
        atomic_groups=[{"id": "behavior", "operation_indexes": [0], "depends_on": []}],
        suggested_cases=[
            {"id": "case_behavior", "task": "Check the new behavior", "covers_group_ids": ["behavior"], "failure_mode": "ignored"},
        ],
    )
    dismissed = await proposal_store.dismiss(
        team_id="team_release",
        agent_id="osa_release",
        proposal_id=proposal.id,
        actor_user_id="user_owner",
        expected_proposal_row_version=proposal.row_version,
    )
    result = await proposal_store.accept_suggested_cases(
        team_id="team_release",
        agent_id="osa_release",
        proposal_id=proposal.id,
        actor_user_id="user_owner",
        expected_proposal_row_version=dismissed.row_version,
        expected_suite_revision_id=original.suite_revision_id,
        cases=(
            (
                "case_behavior",
                EvalCaseDraft(
                    case_key="coach.behavior",
                    input={"task": "Check the new behavior"},
                    fixture_ref={},
                    expected_contract={"failure_mode": "ignored"},
                    scorer_config={},
                    effect_policy={"mode": "no_effects"},
                ),
            ),
        ),
    )
    revision = await eval_store.read_suite_revision(result["suite_revision_id"])
    assert revision is not None
    assert [item.case_key for item in revision.cases] == ["coach.behavior", "existing"]
    projected = await proposal_store.get(
        team_id="team_release",
        agent_id="osa_release",
        proposal_id=proposal.id,
        actor_user_id="user_owner",
    )
    assert projected.status == "dismissed"
    assert projected.suggested_cases[0]["decision_status"] == "accepted"
    assert projected.suggested_cases[0]["result_suite_revision_id"] == revision.suite_revision_id

    with pytest.raises(AgentChangeProposalError, match="agent_change_suggested_case_conflict"):
        await proposal_store.dismiss_suggested_cases(
            team_id="team_release",
            agent_id="osa_release",
            proposal_id=proposal.id,
            actor_user_id="user_owner",
            expected_proposal_row_version=dismissed.row_version,
            suggested_case_ids=("case_behavior",),
        )
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_eval_suite_revision WHERE suite_id=?", (suite.suite_id,)).fetchone()[0] == 2


@pytest.mark.parametrize(
    ("groups", "operation_count", "expected"),
    [
        ([{"operation_indexes": [0]}], 1, {"rule": "group_id_required", "group_index": 0}),
        (
            [{"id": "a", "operation_indexes": [0]}, {"id": "a", "operation_indexes": [1]}],
            2,
            {"rule": "group_id_duplicate", "group_index": 1, "group_id": "a"},
        ),
        (
            [{"id": "a", "operation_indexes": [0, 3]}],
            2,
            {"rule": "operation_indexes_invalid", "group_id": "a", "operation_count": 2, "invalid_indexes": [3]},
        ),
        ([{"id": "a", "operation_indexes": []}], 1, {"rule": "operation_indexes_invalid", "group_id": "a", "invalid_indexes": []}),
        ([{"id": "a", "operation_indexes": [0], "depends_on": "b"}], 1, {"rule": "depends_on_invalid", "group_id": "a"}),
        ([{"id": "a", "operation_indexes": [0]}], 3, {"rule": "operation_not_grouped", "operation_indexes": [1, 2]}),
        (
            [{"id": "a", "operation_indexes": [0]}, {"id": "b", "operation_indexes": [0, 1]}],
            2,
            {"rule": "operation_in_several_groups", "operation_indexes": [0]},
        ),
        ([{"id": "a", "operation_indexes": [0], "depends_on": ["a"]}], 1, {"rule": "depends_on_self", "group_id": "a"}),
        ([{"id": "a", "operation_indexes": [0], "depends_on": ["b"]}], 1, {"rule": "depends_on_unknown_group", "group_id": "a", "depends_on": "b"}),
        (
            [{"id": "a", "operation_indexes": [0], "depends_on": ["b"]}, {"id": "b", "operation_indexes": [1], "depends_on": ["a"]}],
            2,
            {"rule": "depends_on_cycle", "group_id": "a"},
        ),
    ],
)
def test_invalid_groups_say_which_rule_failed_on_which_group(groups, operation_count, expected) -> None:
    from bowhead_core.agent_change_proposal_store import validate_proposal_payload

    operations = [{"op": "replace", "path": "/definition/system_prompt", "value": f"v{index}"} for index in range(operation_count)]
    with pytest.raises(AgentChangeProposalError) as error:
        validate_proposal_payload(operations, groups, [])
    assert error.value.code == "agent_change_groups_invalid"
    details = error.value.details
    assert {key: details.get(key) for key in expected} == expected
    # The message names the fix and the shape a group must have, so a retry can be right.
    assert '"operation_indexes"' in details["message"] and details["message"].startswith(("Group", "Operations"))


async def test_coach_source_changes_use_original_writer_and_cannot_remove_the_policy(tmp_path, monkeypatch):
    import json
    from unittest.mock import AsyncMock

    from bowhead_core.agent_knowledge_policy import KnowledgeAccessError

    monkeypatch.setenv("FEATURE_KNOWLEDGE_SOURCE_BINDINGS", "1")
    db_path = _db(tmp_path)
    _seed(db_path)
    working = await _working(db_path)
    authorize = AsyncMock(return_value=False)
    revisions = AgentRevisionStore(str(db_path), validator=_Validator(), knowledge_source_validator=authorize)
    store = AgentChangeProposalStore(str(db_path), revision_store=revisions)
    policy = {"schema_version": 1, "mode": "selected_files", "file_ids": ["kbf_allowed"]}
    args = {
        "team_id": "team_release",
        "agent_id": "osa_release",
        "effective_owner_user_id": "user_owner",
        "source_ref": "coach:sources",
        "operations": [{"op": "add", "path": "/definition/knowledge_access", "value": policy}],
        "atomic_groups": [{"id": "sources", "operation_indexes": [0]}],
        "expected_baseline": AgentProposalBaseline(working.id, working.row_version, working.base_release_id),
    }
    with pytest.raises(KnowledgeAccessError, match="source_unavailable"):
        await store.create_applied(**args)
    authorize.return_value = True
    _, version, written = await store.create_applied(**args)
    assert written
    with sqlite3.connect(db_path) as db:
        saved = json.loads(db.execute("SELECT definition_json FROM team_subagent_working_copy WHERE id=?", (working.id,)).fetchone()[0])
    assert saved["knowledge_access"] == policy
    with pytest.raises(KnowledgeAccessError, match="preservation_required"):
        await store.create_applied(
            **{
                **args,
                "source_ref": "coach:remove-sources",
                "expected_baseline": AgentProposalBaseline(working.id, version, working.base_release_id),
                "operations": [{"op": "remove", "path": "/definition/knowledge_access"}],
            }
        )
