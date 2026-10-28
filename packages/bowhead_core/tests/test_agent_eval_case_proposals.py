"""Independent evaluation coverage changes never mutate an Agent definition."""

import sqlite3
from importlib import import_module
from types import SimpleNamespace

import pytest
from bowhead_cli.migrate import TARGET_FILES
from bowhead_core.agent_change_proposal_store import AgentChangeProposalError, AgentChangeProposalStore
from bowhead_core.agent_eval_case_proposals import AgentEvalCaseProposals
from bowhead_core.agent_eval_store import AgentEvalStore, AgentEvalStoreError, EvalCaseDraft
from test_agent_revision_store import _db, _seed
from test_chat_reference_storage import _upgrade
from test_reference_model_context_migration import snapshot
from test_skill_invocation_migration import schema

pytestmark = pytest.mark.unit
MIGRATION = import_module("bowhead_core.migrations.versions.0142_case_only_proposals")


@pytest.mark.parametrize("target", list(TARGET_FILES))
def test_case_baseline_migration_preserves_prior_rows(tmp_path, target):
    path = tmp_path / TARGET_FILES[target]
    _upgrade(path, MIGRATION.down_revision, target)
    if target == "core":
        _seed(path)
        with sqlite3.connect(path) as db:
            db.execute(
                """INSERT INTO agent_change_proposal(
                    id,team_id,agent_id,source_type,source_ref,base_document_json,base_document_hash,
                    patch_json,patch_hash,atomic_groups_json,suggested_cases_json,rationale,provenance_json,
                    status,row_version,created_by_kind,created_by_id,created_at
                ) VALUES('acp_historical','team_release','osa_release','coach','historical','{}',?,
                         '[]',?,'[]','[]','Historical suggestion','{}','open',1,'user','user_owner','2026-10-01')""",
                ("sha256:" + "1" * 64, "sha256:" + "2" * 64),
            )
    before = snapshot(path)
    _upgrade(path, MIGRATION.revision, target)
    after = snapshot(path)
    for name, rows in before.items():
        if name == "agent_change_proposal":
            assert [row[:-1] for row in after[name]] == rows
        else:
            assert after[name] == rows
    shape = schema(path)
    _upgrade(path, MIGRATION.revision, target)
    assert schema(path) == shape
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == MIGRATION.revision


def test_partial_case_baseline_migration_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "core.db"
    _upgrade(path, MIGRATION.down_revision, "core")
    before = schema(path)
    monkeypatch.setattr(MIGRATION, "_DDL", (*MIGRATION._DDL[:2], "invalid sql"))
    with sqlite3.connect(path) as db:
        monkeypatch.setattr(
            MIGRATION,
            "context",
            SimpleNamespace(
                config=SimpleNamespace(attributes={"bowhead_target": "core"}),
                get_bind=lambda: SimpleNamespace(exec_driver_sql=db.execute),
            ),
        )
        with pytest.raises(sqlite3.OperationalError):
            MIGRATION.upgrade()
    assert schema(path) == before


def case(key="empty_result"):
    return {
        "id": key,
        "task": "Explain an empty result without inventing data.",
        "failure_mode": "Invented records",
        "case_spec": {
            "case_key": key,
            "fixture_ref": {},
            "expected_contract": {"failure_mode": "Invented records"},
            "scorer_config": {},
            "effect_policy": {"mode": "no_effects"},
            "valid_until": None,
        },
    }


async def setup_case(tmp_path):
    path = _db(tmp_path)
    _seed(path)
    store = AgentEvalStore(str(path))
    suite = await store.create_suite(
        team_id="team_release",
        agent_id="osa_release",
        agent_source_kind="team",
        suite_kind="owner_quality",
        name="Quality",
        visibility="owner",
        actor_user_id="user_owner",
    )
    revision = await store.create_suite_revision(
        suite_id=suite.suite_id,
        definition={"schema_version": 1},
        scorer_bundle_ref="evalkit.core@1",
        judge_policy={"enabled": False, "rubric_ref": None},
        cases=(EvalCaseDraft(input={"task": "Existing check"}, **case("existing")["case_spec"]),),
        actor_user_id="user_owner",
    )

    async def authorize(db):
        pass

    intent = {
        "team_id": "team_release",
        "agent_id": "osa_release",
        "actor_user_id": "user_owner",
        "suite_id": suite.suite_id,
        "revision_id": revision.suite_revision_id,
        "source_ref": "coach-cases:t_original:one",
        "suggested_cases": [case()],
        "rationale": "Add empty-result coverage",
        "provenance": {"source_turn_id": "t_original"},
        "authorize": authorize,
    }
    return path, store, suite, revision, intent


async def test_case_only_proposal_acceptance_and_replay_do_not_create_a_working_copy(tmp_path):
    path, store, _, revision, intent = await setup_case(tmp_path)
    with sqlite3.connect(path) as db:
        original_revisions = db.execute("SELECT * FROM team_subagent_revision").fetchall()
    proposals = AgentEvalCaseProposals(str(path))
    first = await proposals.create(**intent)
    assert not first.patch and not first.atomic_groups and first.base_working_copy_id is None
    assert first.base_suite_revision_id == revision.suite_revision_id
    decisions = AgentChangeProposalStore(str(path))
    result = await decisions.accept_suggested_cases(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        proposal_id=first.id,
        expected_proposal_row_version=first.row_version,
        expected_suite_revision_id=revision.suite_revision_id,
        cases=(("empty_result", EvalCaseDraft(input={"task": "Owner-reviewed task"}, **case()["case_spec"])),),
    )
    updated = await store.read_suite_revision(result["suite_revision_id"])
    assert updated.case_count == 2
    recovered = await proposals.create(**intent)
    assert recovered.id == first.id and recovered.status == "accepted"
    assert recovered.suggested_cases[0]["decision_status"] == "accepted"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM team_subagent_working_copy").fetchone()[0] == 0
        assert db.execute("SELECT * FROM team_subagent_revision").fetchall() == original_revisions
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE agent_change_proposal SET base_suite_revision_id=NULL WHERE id=?", (first.id,))
    with pytest.raises(AgentChangeProposalError, match="idempotency_conflict"):
        await proposals.create(**{**intent, "rationale": "Changed goal"})
    with pytest.raises(AgentChangeProposalError, match="suite_version_conflict"):
        await proposals.create(**{**intent, "source_ref": "coach-cases:t_original:two"})


async def test_case_acceptance_cannot_retarget_a_newer_suite(tmp_path):
    path, store, suite, revision, intent = await setup_case(tmp_path)
    first = await AgentEvalCaseProposals(str(path)).create(**intent)
    newer = await store.create_suite_revision(
        suite_id=suite.suite_id,
        definition={"schema_version": 1},
        scorer_bundle_ref="evalkit.core@1",
        judge_policy={"enabled": False, "rubric_ref": None},
        cases=(EvalCaseDraft(input={"task": "Newer check"}, **case("newer")["case_spec"]),),
        actor_user_id="user_owner",
    )
    for target in (revision.suite_revision_id, newer.suite_revision_id):
        with pytest.raises((AgentChangeProposalError, AgentEvalStoreError), match="suite_version_conflict"):
            await AgentChangeProposalStore(str(path)).accept_suggested_cases(
                team_id="team_release",
                agent_id="osa_release",
                actor_user_id="user_owner",
                proposal_id=first.id,
                expected_proposal_row_version=first.row_version,
                expected_suite_revision_id=target,
                cases=(("empty_result", EvalCaseDraft(input={"task": "Suggested check"}, **case()["case_spec"])),),
            )
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM agent_change_proposal_decision").fetchone()[0] == 0


async def test_partial_adoption_follows_only_this_proposals_append_receipts(tmp_path):
    path, store, _, revision, intent = await setup_case(tmp_path)
    intent["suggested_cases"] = [case("first"), case("second"), case("third")]
    creator = AgentEvalCaseProposals(str(path))
    first = await creator.create(**intent)
    decisions = AgentChangeProposalStore(str(path))
    current, expected_suite = first, revision.suite_revision_id
    for key in ("first", "second"):
        accepted = await decisions.accept_suggested_cases(
            team_id="team_release",
            agent_id="osa_release",
            actor_user_id="user_owner",
            proposal_id=first.id,
            expected_proposal_row_version=current.row_version,
            expected_suite_revision_id=expected_suite,
            cases=((key, EvalCaseDraft(input={"task": "Reviewed check"}, **case(key)["case_spec"])),),
        )
        current = await creator.create(**intent)
        expected_suite = accepted["suite_revision_id"]
        assert current.status == "open" and current.case_target_suite_revision_id == expected_suite
        assert current.base_suite_revision_id == revision.suite_revision_id
    await decisions.dismiss_suggested_cases(
        team_id="team_release",
        agent_id="osa_release",
        actor_user_id="user_owner",
        proposal_id=first.id,
        expected_proposal_row_version=current.row_version,
        suggested_case_ids=["third"],
    )
    settled = await creator.create(**intent)
    assert settled.status == "accepted" and settled.row_version == 2
    assert [item["decision_status"] for item in settled.suggested_cases] == ["accepted", "accepted", "dismissed"]
    assert (await store.read_suite_revision(expected_suite)).case_count == 3


async def test_case_proposal_cannot_name_another_suite_revision(tmp_path):
    path, _, _, revision, intent = await setup_case(tmp_path)
    with pytest.raises(AgentChangeProposalError, match="not_editable"):
        await AgentEvalCaseProposals(str(path)).create(**{**intent, "suite_id": "aes_other"})
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM agent_change_proposal").fetchone()[0] == 0
