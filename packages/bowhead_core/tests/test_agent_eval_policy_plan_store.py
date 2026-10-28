from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from bowhead_core._db import open_shared_ctx
from bowhead_core.agent_eval_comparison import (
    RUN_SCHEDULING_SLACK_MS,
    AgentEvalComparisonError,
    AgentEvalComparisonStore,
    comparison_run_budget,
)
from bowhead_core.agent_eval_job_store import AgentEvalJobStore, AgentEvalJobStoreError
from bowhead_core.agent_eval_plan_store import AgentEvalPlanError, AgentEvalPlanStore
from bowhead_core.agent_eval_policy_store import (
    AgentEvalPolicyDraft,
    AgentEvalPolicyError,
    AgentEvalPolicyStore,
    AgentEvalPolicySuite,
)
from bowhead_core.agent_eval_service import _comparison_attempt_limit_ms
from bowhead_core.agent_eval_store import AgentEvalStore, EvalCaseDraft, append_eval_suite_cases_conn
from bowhead_core.agent_execution_binding import AgentExecutionBindingStore
from bowhead_core.agent_revision_store import AgentPublicationValidation, AgentReleaseStaleError, AgentRevisionStore
from bowhead_core.agent_version_automation import AgentVersionAutomationError
from bowhead_core.agent_version_service import AgentVersionError, AgentVersionService

from bowhead_core import migrations

pytestmark = pytest.mark.integration
NOW = "2026-09-14T10:00:00+00:00"


class _Clock:
    def now(self) -> datetime:
        return datetime.fromisoformat(NOW).astimezone(UTC)


def _hash_document(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _upgrade(path: Path) -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(migrations.__file__).resolve().parent))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    config.attributes["bowhead_target"] = "core"
    command.upgrade(config, "head")


def _seed(path: Path, *, owner_expected_contract: dict[str, Any] | None = None) -> None:
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("INSERT INTO teams(id,name,status,created_at) VALUES('team_p','Policy Team','active',?)", (NOW,))
        for user_id, email in (
            ("user_owner", "owner@example.com"),
            ("user_other", "other@example.com"),
        ):
            db.execute(
                "INSERT INTO users(id,team_id,email,status,created_at) VALUES(?, 'team_p',?,'active',?)",
                (user_id, email, NOW),
            )
            db.execute(
                """
                INSERT INTO team_members(id,user_id,team_id,role,status,created_at)
                VALUES(?,?,'team_p','member','active',?)
                """,
                (f"tm_{user_id}", user_id, NOW),
            )
        db.execute("UPDATE teams SET created_by_user_id='user_owner' WHERE id='team_p'")
        for workspace_id in ("ws_eval", "ws_denied"):
            db.execute(
                """
                INSERT INTO workspaces(id,team_id,name,status,revision,created_at,updated_at)
                VALUES(?,'team_p',?,'active',1,?,?)
                """,
                (workspace_id, workspace_id, NOW, NOW),
            )
        db.execute(
            """
            INSERT INTO workspace_members(
              workspace_id,user_id,role,invited_by,joined_at,last_active_at,status
            ) VALUES('ws_eval','user_owner','admin','user_owner',?,?,'active')
            """,
            (NOW, NOW),
        )
        db.execute(
            """
            INSERT INTO team_subagent(
              id,team_id,name,display_name,description,tool_refs_json,skills_json,
              visibility,created_by,owner_user_id,status,definition_mode,created_at,updated_at
            ) VALUES(
              'osa_policy','team_p','policy-agent','Policy Agent','','[]','[]',
              'private','user_owner','user_owner','enabled','revisioned',?,?
            )
            """,
            (NOW, NOW),
        )
        for sequence, revision_id, prompt in (
            (1, "arv_policy_base", "base"),
            (2, "arv_policy_candidate", "candidate"),
        ):
            definition_json = f'{{"system_prompt":"{prompt}"}}'
            db.execute(
                """
                INSERT INTO team_subagent_revision(
                  id,team_id,subagent_id,sequence,revision_kind,definition_json,
                  listing_json,definition_hash,dependency_fingerprint,
                  capability_fingerprint,risk_compiler_version,risk_json,
                  created_by,created_at
                ) VALUES(?, 'team_p','osa_policy',?,'authored',?,
                         '{"display_name":"Policy Agent"}',
                         ?,
                         'sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
                         'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
                         'risk-v1','{}','user_owner',?)
                """,
                (revision_id, sequence, definition_json, _hash_document(json.loads(definition_json)), NOW),
            )
        db.execute(
            """
            INSERT INTO agent_safety_report(
              id,team_id,agent_id,subject_kind,subject_ref,content_hash,
              dependency_fingerprint,compiler_version,semantic_scanner_version,status,
              deterministic_json,semantic_json,safe_summary_json,created_by,created_at
            ) VALUES(
              'asr_policy','team_p','osa_policy','revision','arv_policy_candidate',
              ?,
              'sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
              'safety-v1','none','pass',
              '{}','{}','{}','user_owner',?
            )
            """,
            (
                # The safety writer's formula: hash of {definition, listing}. A placeholder here
                # is refused by the eval binding (`agent_live_release_inconsistent`).
                _hash_document({"definition": {"system_prompt": "candidate"}, "listing": {"display_name": "Policy Agent"}}),
                NOW,
            ),
        )
        db.execute(
            """
            INSERT INTO team_subagent_release(
              id,team_id,subagent_id,release_sequence,revision_id,
              audience_snapshot_json,audience_hash,capability_fingerprint,
              dependency_fingerprint,risk_json,safety_report_id,activated_by,activated_at
            ) VALUES(
              'arl_policy_base','team_p','osa_policy',1,'arv_policy_base',
              '{"effective_mode":"private"}',
              'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
              'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
              'sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
              '{}','asr_policy','user_owner',?
            )
            """,
            (NOW,),
        )
        db.execute(
            """
            UPDATE team_subagent
            SET live_release_id='arl_policy_base',live_revision_id='arv_policy_base',release_sequence=1
            WHERE id='osa_policy'
            """,
        )
        db.execute(
            """
            INSERT INTO team_subagent_working_copy(
              id,team_id,subagent_id,base_release_id,definition_json,listing_json,
              audience_json,validation_json,row_version,updated_by,created_at,updated_at
            ) VALUES(
              'awc_policy','team_p','osa_policy','arl_policy_base',
              '{"system_prompt":"candidate"}','{"display_name":"Policy Agent"}',
              '{"effective_mode":"private"}','{}',1,'user_owner',?,?
            )
            """,
            (NOW, NOW),
        )
        _insert_suite(db, suite_id="aes_platform", revision_id="aev_platform", kind="platform_contract", managed=1)
        _insert_suite(
            db,
            suite_id="aes_owner",
            revision_id="aev_owner",
            kind="owner_quality",
            managed=0,
            expected_contract=owner_expected_contract or {},
        )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def _insert_suite(
    db: sqlite3.Connection,
    *,
    suite_id: str,
    revision_id: str,
    kind: str,
    managed: int,
    effect_mode: str = "no_effects",
    expected_contract: dict[str, Any] | None = None,
) -> None:
    visibility = "team_evidence" if managed else "owner"
    fixture_ref = {
        "classification": "internal",
        "content_hash": f"sha256:{'a' * 64}",
        "kind": "dataset",
        "ref": "fixture",
        "version": "1",
    }
    case_payload = {
        "case_key": f"case-{suite_id}",
        "effect_policy": {"mode": effect_mode},
        "expected_contract": expected_contract or {},
        "fixture_ref": fixture_ref,
        "input": {},
        "scorer_config": {},
        "valid_until": None,
    }
    case_hash = _hash_document(case_payload)
    case_set_hash = _hash_document([case_payload])
    definition_hash = _hash_document(
        {
            "cases": [case_payload],
            "definition": {},
            "judge_policy": {"enabled": False},
            "scorer_bundle_ref": "evalkit.core@1",
        },
    )
    db.execute(
        """
        INSERT INTO agent_eval_suite(
          id,team_id,agent_id,agent_source_kind,suite_kind,name,visibility,state,
          row_version,created_by,created_at,updated_at,platform_managed
        ) VALUES(?,'team_p','osa_policy','team',?,?,?,'active',1,'user_owner',?,?,?)
        """,
        (suite_id, kind, kind, visibility, NOW, NOW, managed),
    )
    db.execute(
        """
        INSERT INTO agent_eval_suite_revision(
          id,suite_id,sequence,definition_json,definition_hash,scorer_bundle_ref,
          judge_policy_json,case_count,case_set_hash,created_by,created_at
        ) VALUES(?,?,1,'{}',?,'evalkit.core@1','{"enabled":false}',1,?,
                 'user_owner',?)
        """,
        (revision_id, suite_id, definition_hash, case_set_hash, NOW),
    )
    db.execute(
        """
        INSERT INTO agent_eval_case_revision(
          id,suite_revision_id,case_key,case_hash,input_json,fixture_ref_json,
          expected_contract_json,scorer_config_json,effect_policy_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            f"aec_{suite_id}",
            revision_id,
            f"case-{suite_id}",
            case_hash,
            "{}",
            json.dumps(fixture_ref, sort_keys=True, separators=(",", ":")),
            json.dumps(expected_contract or {}, sort_keys=True, separators=(",", ":")),
            "{}",
            json.dumps({"mode": effect_mode}, sort_keys=True, separators=(",", ":")),
            NOW,
        ),
    )


def _insert_prepared(path: Path, *, audience_mode: str = "private") -> None:
    audience = {
        "effective_mode": audience_mode,
        "group_ids": [],
        "schema_version": 1,
        "user_ids": [],
    }
    audience_json = json.dumps(audience, sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(
            """
            INSERT INTO team_subagent_release_candidate(
              id,team_id,subagent_id,revision_id,base_live_release_id,
              target_audience_json,target_audience_hash,capability_fingerprint,
              dependency_fingerprint,risk_compiler_version,risk_json,safety_report_id,
              candidate_hash,state,submitted_by,submitted_at,updated_at
            ) VALUES(
              'arc_policy','team_p','osa_policy','arv_policy_candidate','arl_policy_base',
              ?,?,
              'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
              'sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
              'risk-v1','{}','asr_policy',
              'sha256:9999999999999999999999999999999999999999999999999999999999999999',
              'prepared',
              'user_owner',?,?
            )
            """,
            (audience_json, _hash_document(audience), NOW, NOW),
        )
        db.execute(
            """
            INSERT INTO agent_name_claim(
              team_id,locale,normalized_key,holder_kind,holder_id,agent_id,claimed_at
            ) VALUES('team_p','*','policy agent','agent','osa_policy','osa_policy',?)
            """,
            (NOW,),
        )
        db.execute(
            """
            INSERT INTO agent_release_candidate_name_claim(
              team_id,candidate_id,agent_id,locale,normalized_key,claim_kind,state,claimed_at
            ) VALUES('team_p','arc_policy','osa_policy','*','policy agent','inherited','reserved',?)
            """,
            (NOW,),
        )


def _draft(*, standard_threshold: float = 0.8) -> AgentEvalPolicyDraft:
    base = AgentEvalPolicyDraft.default(
        owner_suites=(
            AgentEvalPolicySuite(
                suite_revision_id="aev_owner",
                required=True,
                tier="standard",
                execution_scope="paired",
                attempts=3,
            ),
        ),
    )
    return replace(
        base,
        pass_thresholds={"critical": 1.0, "standard": standard_threshold},
    )


async def _refresh_behind_plan(db_path: Path, *, candidate_id: str = "arc_policy"):
    """Recompile the candidate's plan from the current policy, as the Owner's comparison start does."""
    async with open_shared_ctx(str(db_path)) as db:
        db.row_factory = sqlite3.Row
        await db.execute("BEGIN IMMEDIATE")
        candidate = await (await db.execute("SELECT * FROM team_subagent_release_candidate WHERE id=?", (candidate_id,))).fetchone()
        control = await (await db.execute("SELECT * FROM agent_eval_candidate_control WHERE candidate_id=?", (candidate_id,))).fetchone()
        plan = await (await db.execute("SELECT * FROM agent_eval_plan WHERE id=?", (control["active_plan_id"],))).fetchone()
        try:
            refreshed = await AgentEvalPlanStore(db_path, clock=_Clock()).refresh_behind_plan_conn(
                db, candidate=candidate, control=control, plan=plan, actor_user_id="user_owner", created_at=NOW
            )
        except BaseException:
            await db.rollback()
            raise
        await db.commit()
    return refreshed


@pytest.mark.asyncio
async def test_default_preview_is_read_only_and_server_injects_platform_suite(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    store = AgentEvalPolicyStore(db_path, clock=_Clock())

    preview = await store.default_preview(
        team_id="team_p",
        agent_id="osa_policy",
        actor_user_id="user_owner",
    )

    assert [item["suite_kind"] for item in preview["suite_items"]] == [
        "platform_contract",
        "owner_quality",
    ]
    assert preview["suite_items"][0]["suite_revision_id"] == "aev_platform"
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_eval_policy_revision").fetchone() == (0,)


@pytest.mark.asyncio
async def test_policy_revision_is_canonical_idempotent_and_owner_scoped(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    store = AgentEvalPolicyStore(db_path, clock=_Clock())

    created = await store.create_revision(
        team_id="team_p",
        agent_id="osa_policy",
        actor_user_id="user_owner",
        expected_revision_id=None,
        draft=_draft(),
    )
    duplicate = await store.create_revision(
        team_id="team_p",
        agent_id="osa_policy",
        actor_user_id="user_owner",
        expected_revision_id=created.id,
        draft=_draft(),
    )

    assert duplicate == created
    assert created.sequence == 1
    assert created.policy["suite_items"][0]["suite_kind"] == "platform_contract"
    with pytest.raises(AgentEvalPolicyError, match="agent_eval_policy_version_conflict"):
        await store.create_revision(
            team_id="team_p",
            agent_id="osa_policy",
            actor_user_id="user_owner",
            expected_revision_id=None,
            draft=_draft(standard_threshold=0.9),
        )
    with pytest.raises(AgentEvalPolicyError, match="agent_eval_policy_forbidden"):
        await store.current(
            team_id="team_p",
            agent_id="osa_policy",
            actor_user_id="user_other",
        )


@pytest.mark.asyncio
async def test_plan_freezes_exact_suite_fixture_and_workspace_scope_then_rotates_with_cas(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    policy_store = AgentEvalPolicyStore(db_path, clock=_Clock())
    plan_store = AgentEvalPlanStore(db_path, clock=_Clock())
    policy = await policy_store.create_revision(
        team_id="team_p",
        agent_id="osa_policy",
        actor_user_id="user_owner",
        expected_revision_id=None,
        draft=_draft(),
    )
    _insert_prepared(db_path)

    async with open_shared_ctx(str(db_path)) as db:
        db.row_factory = sqlite3.Row
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            "SELECT * FROM team_subagent_release_candidate WHERE id='arc_policy'",
        ) as cursor:
            candidate = await cursor.fetchone()
        assert candidate is not None
        first, control = await plan_store.create_initial_plan_conn(
            db,
            candidate=candidate,
            policy=policy,
            actor_user_id="user_owner",
            workspace_id="ws_eval",
            source_working_copy_id="awc_policy",
            source_working_copy_version=1,
            source_working_copy_hash="sha256:working-copy",
            created_at=NOW,
        )
        await db.commit()

    assert control.readiness == "missing"
    assert first.sequence == 1
    assert [suite.suite_revision_id for suite in first.suites] == ["aev_platform", "aev_owner"]
    assert first.suites[1].fixture_bundle["cases"][0]["fixture_ref"] == {
        "classification": "internal",
        "content_hash": f"sha256:{'a' * 64}",
        "kind": "dataset",
        "ref": "fixture",
        "version": "1",
    }
    loaded = await plan_store.get(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        plan_id=first.id,
        actor_user_id="user_owner",
    )
    assert loaded == first

    async with open_shared_ctx(str(db_path)) as db:
        db.row_factory = sqlite3.Row
        await db.execute("BEGIN IMMEDIATE")
        second_policy = await policy_store.create_revision_conn(
            db,
            team_id="team_p",
            agent_id="osa_policy",
            actor_user_id="user_owner",
            expected_revision_id=policy.id,
            draft=_draft(standard_threshold=0.9),
            created_at=NOW,
        )
        await db.commit()
    refreshed = await _refresh_behind_plan(db_path)
    assert refreshed is not None
    second, control_row_version = refreshed
    assert second.sequence == 2
    assert second.policy_revision_id == second_policy.id
    assert second.plan_hash != first.plan_hash
    assert second.execution_workspace_id == "ws_eval"
    assert control_row_version == 2
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT active_plan_id,row_version FROM agent_eval_candidate_control WHERE candidate_id='arc_policy'").fetchone() == (
            second.id,
            2,
        )

    # A plan on the current policy is left as it is.
    assert await _refresh_behind_plan(db_path) is None


@pytest.mark.asyncio
async def test_appended_owner_cases_reach_the_next_plan_but_not_a_prepared_one(tmp_path: Path) -> None:
    """The policy follows the newest Owner suite revision; a candidate's frozen plan keeps its own."""
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    policy_store = AgentEvalPolicyStore(db_path, clock=_Clock())
    plan_store = AgentEvalPlanStore(db_path, clock=_Clock())
    _insert_prepared(db_path)
    async with open_shared_ctx(str(db_path)) as db:
        db.row_factory = sqlite3.Row
        await db.execute("BEGIN IMMEDIATE")
        # The first preparation creates the default policy from the current suites.
        policy = await policy_store.resolve_for_prepare_conn(
            db, team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner", expected_revision_id=None, created_at=NOW
        )
        candidate = await (await db.execute("SELECT * FROM team_subagent_release_candidate WHERE id='arc_policy'")).fetchone()
        first, control = await plan_store.create_initial_plan_conn(
            db,
            candidate=candidate,
            policy=policy,
            actor_user_id="user_owner",
            workspace_id="ws_eval",
            source_working_copy_id="awc_policy",
            source_working_copy_version=1,
            source_working_copy_hash="sha256:working-copy",
            created_at=NOW,
        )
        await db.commit()
    assert [suite.suite_revision_id for suite in first.suites] == ["aev_platform", "aev_owner"]

    # An accepted Coach case proposal appends a case while the candidate is prepared.
    async with open_shared_ctx(str(db_path)) as db:
        db.row_factory = sqlite3.Row
        await db.execute("BEGIN IMMEDIATE")
        appended = await append_eval_suite_cases_conn(
            db,
            team_id="team_p",
            agent_id="osa_policy",
            expected_suite_revision_id="aev_owner",
            cases=(
                EvalCaseDraft(
                    case_key="coach.added",
                    input={"task": "The case Coach suggested"},
                    fixture_ref={},
                    expected_contract={"allowed_terminal_statuses": ["completed"]},
                    scorer_config={},
                    effect_policy={"mode": "no_effects"},
                ),
            ),
            actor_user_id="user_owner",
        )
        await db.commit()
    current = await policy_store.current(team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner")
    assert current is not None and current.sequence == 2
    assert [item["suite_revision_id"] for item in current.policy["suite_items"]] == ["aev_platform", appended.suite_revision_id]

    # The prepared candidate's plan is history: it still names the revision it was frozen with.
    kept = await plan_store.get(team_id="team_p", agent_id="osa_policy", candidate_id="arc_policy", plan_id=first.id, actor_user_id="user_owner")
    assert kept == first

    # A preparation holding the earlier policy is told which one is current.
    async with open_shared_ctx(str(db_path)) as db:
        with pytest.raises(AgentEvalPolicyError) as conflict:
            await policy_store.resolve_for_prepare_conn(
                db, team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner", expected_revision_id=policy.id, created_at=NOW
            )
    assert conflict.value.code == "agent_eval_policy_version_conflict"
    assert conflict.value.details == {"current_policy_revision_id": current.id}

    # The next plan runs the newest revision, appended case included.
    refreshed = await _refresh_behind_plan(db_path)
    assert refreshed is not None
    second = refreshed[0]
    owner_suite = second.suites[1]
    assert owner_suite.suite_revision_id == appended.suite_revision_id
    assert len(owner_suite.fixture_bundle["cases"]) == 2


@pytest.mark.asyncio
async def test_public_policy_write_refuses_to_orphan_a_prepared_candidate(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    store = AgentEvalPolicyStore(db_path, clock=_Clock())
    first = await store.create_revision(
        team_id="team_p",
        agent_id="osa_policy",
        actor_user_id="user_owner",
        expected_revision_id=None,
        draft=_draft(),
    )
    _insert_prepared(db_path)

    with pytest.raises(AgentEvalPolicyError, match="agent_eval_policy_plan_rotation_required"):
        await store.create_revision(
            team_id="team_p",
            agent_id="osa_policy",
            actor_user_id="user_owner",
            expected_revision_id=first.id,
            draft=_draft(standard_threshold=0.9),
        )
    with pytest.raises(AgentEvalPolicyError, match="agent_eval_policy_forbidden"):
        await store.create_revision(
            team_id="team_p",
            agent_id="osa_policy",
            actor_user_id="user_other",
            expected_revision_id=first.id,
            draft=_draft(standard_threshold=0.9),
        )


@pytest.mark.asyncio
async def test_policy_rejects_a_suite_effect_outside_its_allowlist(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        _insert_suite(
            db,
            suite_id="aes_sandbox",
            revision_id="aev_sandbox",
            kind="owner_quality",
            managed=0,
            effect_mode="sandbox",
        )
    draft = replace(
        AgentEvalPolicyDraft.default(
            owner_suites=(
                AgentEvalPolicySuite(
                    suite_revision_id="aev_sandbox",
                    required=True,
                    tier="standard",
                    execution_scope="paired",
                    attempts=3,
                ),
            ),
        ),
        allowed_effect_modes=("no_effects",),
    )

    with pytest.raises(AgentEvalPolicyError, match="agent_eval_effect_mode_not_allowed"):
        await AgentEvalPolicyStore(db_path, clock=_Clock()).create_revision(
            team_id="team_p",
            agent_id="osa_policy",
            actor_user_id="user_owner",
            expected_revision_id=None,
            draft=draft,
        )


async def _comparison_context(
    db_path: Path,
    *,
    audience_mode: str = "private",
    compatibility_verdict: str = "compatible",
    draft: AgentEvalPolicyDraft | None = None,
):
    policy = await AgentEvalPolicyStore(db_path, clock=_Clock()).create_revision(
        team_id="team_p",
        agent_id="osa_policy",
        actor_user_id="user_owner",
        expected_revision_id=None,
        draft=draft or _draft(),
    )
    _insert_prepared(db_path, audience_mode=audience_mode)
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute(
            """
            INSERT INTO agent_revision_compatibility(
              revision_id,team_id,agent_id,base_revision_id,compatibility_major,
              verdict,compiler_version,contract_hash,reason_codes_json,created_at
            ) VALUES(
              'arv_policy_candidate','team_p','osa_policy','arv_policy_base',1,
              ?,'compat-v1',
              'sha256:7777777777777777777777777777777777777777777777777777777777777777',
              '[]',?
            )
            """,
            (compatibility_verdict, NOW),
        )
    plan_store = AgentEvalPlanStore(db_path, clock=_Clock())
    async with open_shared_ctx(str(db_path)) as db:
        db.row_factory = sqlite3.Row
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            "SELECT * FROM team_subagent_release_candidate WHERE id='arc_policy'",
        ) as cursor:
            candidate = await cursor.fetchone()
        assert candidate is not None
        plan, control = await plan_store.create_initial_plan_conn(
            db,
            candidate=candidate,
            policy=policy,
            actor_user_id="user_owner",
            workspace_id="ws_eval",
            source_working_copy_id="awc_policy",
            source_working_copy_version=1,
            source_working_copy_hash="sha256:working-copy",
            created_at=NOW,
        )
        await db.commit()
    return plan, control


def _model_hashes() -> dict[str, str]:
    model_hash = f"sha256:{'6' * 64}"
    return {"arv_policy_base": model_hash, "arv_policy_candidate": model_hash}


async def _finish_passing_comparison(db_path: Path, *, plan, control, key: str):
    comparison = AgentEvalComparisonStore(db_path, clock=_Clock())
    started = await comparison.start_release(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        actor_user_id="user_owner",
        candidate_row_version=1,
        expected_control_row_version=control.row_version,
        candidate_hash="sha256:" + "9" * 64,
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        model_config_hashes=_model_hashes(),
        idempotency_key=key,
    )
    eval_store = AgentEvalStore(db_path)
    for link in started.runs:
        await eval_store.mark_run_running(link.run_id)
        revision = await eval_store.read_suite_revision(link.suite_revision_id)
        assert revision is not None
        # Owner suites and the paired platform contract both run 3 attempts.
        attempts = 3
        for case in revision.cases:
            for attempt in range(1, attempts + 1):
                await eval_store.record_case_result(
                    run_id=link.run_id,
                    case_revision_id=case.case_revision_id,
                    attempt=attempt,
                    status="passed",
                    deterministic={"passed": True},
                )
                with sqlite3.connect(db_path) as db:
                    db.execute(
                        """
                        INSERT INTO agent_eval_attempt_identity(
                          run_id,case_revision_id,attempt,subject_provider_ref,
                          subject_provider_hash,reproducibility,created_at
                        ) VALUES(?,?,?,'model-config',?,'exact',?)
                        """,
                        (link.run_id, case.case_revision_id, attempt, f"sha256:{'6' * 64}", NOW),
                    )
        await eval_store.mark_run_terminal(link.run_id, status="passed", summary={"schema_version": 1})
    return await comparison.finalize(started.id)


@pytest.mark.asyncio
async def test_release_comparison_materializes_exact_matrix_atomically_and_replays(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    plan, control = await _comparison_context(db_path)
    store = AgentEvalComparisonStore(db_path, clock=_Clock())

    started = await store.start_release(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        actor_user_id="user_owner",
        candidate_row_version=1,
        expected_control_row_version=control.row_version,
        candidate_hash="sha256:" + "9" * 64,
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        model_config_hashes=_model_hashes(),
        idempotency_key="comparison-one",
    )
    replay = await store.start_release(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        actor_user_id="user_owner",
        candidate_row_version=1,
        expected_control_row_version=control.row_version,
        candidate_hash="sha256:" + "9" * 64,
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        model_config_hashes=_model_hashes(),
        idempotency_key="comparison-one",
    )

    assert replay == started
    assert [(link.suite_revision_id, link.subject_side) for link in started.runs] == [
        ("aev_owner", "baseline"),
        ("aev_owner", "candidate"),
        # The platform contract also runs on Live, so a failure can be told apart from one Live has.
        ("aev_platform", "baseline"),
        ("aev_platform", "candidate"),
    ]
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_eval_comparison_execution").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM agent_eval_run").fetchone() == (4,)
        assert db.execute("SELECT COUNT(*) FROM agent_eval_run_execution").fetchone() == (4,)
        # Owner and platform suites: one case each, 3 attempts, on both Live and the candidate.
        assert db.execute("SELECT COUNT(*) FROM agent_eval_case_job").fetchone() == (12,)
        assert db.execute("SELECT readiness,active_execution_id,row_version FROM agent_eval_candidate_control").fetchone() == (
            "running",
            started.id,
            2,
        )


@pytest.mark.asyncio
async def test_release_comparison_failure_rolls_back_partial_runs_and_jobs(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    plan, control = await _comparison_context(db_path)

    with pytest.raises(AgentEvalComparisonError, match="agent_eval_model_config_unavailable"):
        await AgentEvalComparisonStore(db_path, clock=_Clock()).start_release(
            team_id="team_p",
            agent_id="osa_policy",
            candidate_id="arc_policy",
            actor_user_id="user_owner",
            candidate_row_version=1,
            expected_control_row_version=control.row_version,
            candidate_hash="sha256:" + "9" * 64,
            plan_id=plan.id,
            plan_hash=plan.plan_hash,
            model_config_hashes={"arv_policy_candidate": f"sha256:{'6' * 64}"},
            idempotency_key="comparison-partial-failure",
        )

    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_eval_comparison_execution").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM agent_eval_run").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM agent_eval_case_job").fetchone() == (0,)
        assert db.execute("SELECT readiness,active_execution_id,row_version FROM agent_eval_candidate_control").fetchone() == (
            "missing",
            None,
            1,
        )


@pytest.mark.asyncio
async def test_comparison_cancel_is_terminal_and_preserves_no_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    plan, control = await _comparison_context(db_path)
    store = AgentEvalComparisonStore(db_path, clock=_Clock())
    started = await store.start_release(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        actor_user_id="user_owner",
        candidate_row_version=1,
        expected_control_row_version=control.row_version,
        candidate_hash="sha256:" + "9" * 64,
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        model_config_hashes=_model_hashes(),
        idempotency_key="comparison-cancel",
    )

    cancelled = await store.cancel(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        execution_id=started.id,
        actor_user_id="user_owner",
        expected_execution_row_version=started.row_version,
    )
    replay = await store.cancel(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        execution_id=started.id,
        actor_user_id="user_owner",
        expected_execution_row_version=started.row_version,
    )

    assert replay == cancelled
    assert cancelled.status == "cancelled" and not cancelled.can_cancel
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_eval_snapshot").fetchone() == (0,)
        assert db.execute("SELECT readiness,active_execution_id FROM agent_eval_candidate_control").fetchone() == (
            "missing",
            None,
        )
        assert db.execute("SELECT COUNT(*) FROM agent_eval_run WHERE status='cancelled'").fetchone() == (4,)
        assert db.execute("SELECT COUNT(*) FROM agent_eval_case_job WHERE status='cancelled'").fetchone() == (12,)
        assert db.execute("SELECT COUNT(*) FROM agent_eval_case_result WHERE status='cancelled'").fetchone() == (12,)
        assert db.execute("SELECT COUNT(*) FROM agent_eval_attempt_identity").fetchone() == (12,)


@pytest.mark.asyncio
async def test_automation_can_only_cancel_a_comparison_from_the_same_grant(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    plan, control = await _comparison_context(db_path)
    from test_agent_automation_context import authority_for

    authority = await authority_for(db_path)
    store = AgentEvalComparisonStore(db_path, clock=_Clock())
    started = await store.start_release(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        actor_user_id="user_owner",
        candidate_row_version=1,
        expected_control_row_version=control.row_version,
        candidate_hash="sha256:" + "9" * 64,
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        model_config_hashes=_model_hashes(),
        idempotency_key="automation-comparison-cancel",
        actor_kind="automation",
        automation=authority,
        actor_ref=authority.principal_id,
        automation_grant_id=authority.grant_id,
    )

    with pytest.raises(AgentEvalComparisonError, match="agent_eval_comparison_not_cancellable"):
        await store.cancel(
            team_id="team_p",
            agent_id="osa_policy",
            candidate_id="arc_policy",
            execution_id=started.id,
            actor_user_id="user_owner",
            expected_execution_row_version=started.row_version,
            actor_kind="automation",
            automation=authority,
            automation_grant_id="avag_other",
        )
    cancelled = await store.cancel(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        execution_id=started.id,
        actor_user_id="user_owner",
        expected_execution_row_version=started.row_version,
        actor_kind="automation",
        automation=authority,
        automation_grant_id=authority.grant_id,
    )
    assert cancelled.status == "cancelled"


@pytest.mark.asyncio
async def test_complete_comparison_creates_one_immutable_ready_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    plan, control = await _comparison_context(db_path)
    store = AgentEvalComparisonStore(db_path, clock=_Clock())
    started = await store.start_release(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        actor_user_id="user_owner",
        candidate_row_version=1,
        expected_control_row_version=control.row_version,
        candidate_hash="sha256:" + "9" * 64,
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        model_config_hashes=_model_hashes(),
        idempotency_key="comparison-ready",
    )
    eval_store = AgentEvalStore(db_path)
    for link in started.runs:
        run = await eval_store.mark_run_running(link.run_id)
        revision = await eval_store.read_suite_revision(link.suite_revision_id)
        assert revision is not None
        # Owner suites and the paired platform contract both run 3 attempts.
        attempts = 3
        for case in revision.cases:
            for attempt in range(1, attempts + 1):
                await eval_store.record_case_result(
                    run_id=link.run_id,
                    case_revision_id=case.case_revision_id,
                    attempt=attempt,
                    status="passed",
                    deterministic={"passed": True},
                    duration_ms=100,
                    cost_microusd=200,
                )
                with sqlite3.connect(db_path) as db:
                    db.execute(
                        """
                        INSERT INTO agent_eval_attempt_identity(
                          run_id,case_revision_id,attempt,subject_provider_ref,
                          subject_provider_hash,reproducibility,created_at
                        ) VALUES(?,?,?,'model-config',?,'exact',?)
                        """,
                        (link.run_id, case.case_revision_id, attempt, f"sha256:{'6' * 64}", NOW),
                    )
        await eval_store.mark_run_terminal(run.run_id, status="passed", summary={"schema_version": 1})

    finalized = await store.finalize(started.id)
    replay = await store.finalize(started.id)

    assert finalized.execution.status == "completed"
    assert finalized.snapshot is not None and finalized.snapshot.readiness == "ready"
    assert finalized.decision is not None and finalized.decision.state == "ready"
    assert replay.snapshot == finalized.snapshot
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_eval_snapshot").fetchone() == (1,)
        assert db.execute("SELECT readiness,active_execution_id,active_snapshot_id FROM agent_eval_candidate_control").fetchone() == (
            "ready",
            None,
            finalized.snapshot.id,
        )
        assert db.execute("SELECT COUNT(*) FROM agent_version_notification_outbox").fetchone() == (2,)


class _UnusedValidator:
    async def validate(self, _db, **_kwargs):
        raise AssertionError("submit and projection must not recompile publication state")


class _ActivationValidator:
    async def validate(self, _db, **_kwargs):
        compilation = SimpleNamespace(
            capability_fingerprint=f"sha256:{'c' * 64}",
            dependency_fingerprint=f"sha256:{'e' * 64}",
            publishable=True,
            reason_codes=(),
            risk=SimpleNamespace(level="low"),
        )
        return AgentPublicationValidation(
            compilation=compilation,  # type: ignore[arg-type]
            safety_status="pass",
            deterministic_reason_codes=(),
            semantic_status="pass",
            semantic_reason_codes=(),
        )


async def _ready_candidate(
    db_path: Path,
    *,
    clock: Any,
    audience_mode: str = "teamwide",
    candidate_failures: int = 0,
    suite_kind: str | None = None,
) -> tuple[AgentVersionService, Any, Any]:
    """Drive a candidate through a real comparison to a snapshot (`ready` unless attempts fail).

    `candidate_failures` fails that many of each case's 3 attempts on the candidate side, in the suites of
    `suite_kind` (every suite when None). Returns the service plus the plan and snapshot that
    `submit_for_review` / `approve_shared` must quote. `db_path` must already be upgraded and seeded.
    """
    plan, control = await _comparison_context(db_path, audience_mode=audience_mode)
    comparison = AgentEvalComparisonStore(db_path, clock=clock)
    started = await comparison.start_release(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        actor_user_id="user_owner",
        candidate_row_version=1,
        expected_control_row_version=control.row_version,
        candidate_hash="sha256:" + "9" * 64,
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        model_config_hashes=_model_hashes(),
        idempotency_key="comparison-review",
    )
    eval_store = AgentEvalStore(db_path)
    for link in started.runs:
        await eval_store.mark_run_running(link.run_id)
        revision = await eval_store.read_suite_revision(link.suite_revision_id)
        assert revision is not None
        # Owner suites and the paired platform contract both run 3 attempts.
        attempts = 3
        with sqlite3.connect(db_path) as db:
            kind = db.execute(
                "SELECT suite.suite_kind FROM agent_eval_suite_revision revision JOIN agent_eval_suite suite ON suite.id=revision.suite_id WHERE revision.id=?",
                (link.suite_revision_id,),
            ).fetchone()[0]
        failing = candidate_failures if link.subject_side == "candidate" and suite_kind in {None, kind} else 0
        for case in revision.cases:
            for attempt in range(1, attempts + 1):
                failed = attempt > attempts - failing
                await eval_store.record_case_result(
                    run_id=link.run_id,
                    case_revision_id=case.case_revision_id,
                    attempt=attempt,
                    status="failed" if failed else "passed",
                    deterministic={"passed": not failed},
                )
                with sqlite3.connect(db_path) as db:
                    db.execute(
                        """
                        INSERT INTO agent_eval_attempt_identity(
                          run_id,case_revision_id,attempt,subject_provider_ref,
                          subject_provider_hash,reproducibility,created_at
                        ) VALUES(?,?,?,'model-config',?,'exact',?)
                        """,
                        (link.run_id, case.case_revision_id, attempt, f"sha256:{'6' * 64}", NOW),
                    )
        await eval_store.mark_run_terminal(link.run_id, status="failed" if failing else "passed", summary={"schema_version": 1})
    finalized = await comparison.finalize(started.id)
    assert finalized.snapshot is not None
    revision_store = AgentRevisionStore(str(db_path), validator=_ActivationValidator(), clock=clock)

    async def no_drift(*_args, **_kwargs):
        return ()

    revision_store._candidate_drift_reasons_conn = no_drift  # type: ignore[method-assign]
    service = AgentVersionService(db_path, revision_store=revision_store, clock=clock)
    return service, plan, finalized.snapshot


@pytest.mark.asyncio
async def test_owner_workbench_and_shared_submit_freeze_exact_review_evidence(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    service, plan, snapshot = await _ready_candidate(db_path, clock=_Clock())

    workbench = await service.workbench(team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner")
    assert workbench["display_state"] == "ready"
    assert workbench["current_eval_policy_revision_id"] == plan.policy_revision_id
    assert workbench["primary_action"] == {
        "disabled_reason_codes": [],
        "enabled": True,
        "kind": "submit_for_review",
    }

    result = await service.submit_for_review(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        actor_user_id="user_owner",
        candidate_row_version=1,
        candidate_hash="sha256:" + "9" * 64,
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        snapshot_id=snapshot.id,
        snapshot_hash=snapshot.snapshot_hash,
        override_id=None,
        override_hash=None,
        idempotency_key="submit-review",
    )
    replay = await service.submit_for_review(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        actor_user_id="user_owner",
        candidate_row_version=1,
        candidate_hash="sha256:" + "9" * 64,
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        snapshot_id=snapshot.id,
        snapshot_hash=snapshot.snapshot_hash,
        override_id=None,
        override_hash=None,
        idempotency_key="submit-review",
    )
    assert replay == result
    review = await service.reviewer_projection(
        team_id="team_p",
        candidate_id="arc_policy",
        actor_user_id="user_owner",
    )
    assert review["decision_availability"]["approve"] is True
    assert review["actor_overlap"] is True
    assert review["plan_hash"] == plan.plan_hash
    assert review["snapshot_hash"] == snapshot.snapshot_hash
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT state,row_version FROM team_subagent_release_candidate WHERE id='arc_policy'").fetchone() == (
            "pending",
            2,
        )
        assert db.execute("SELECT COUNT(*) FROM agent_version_notification_outbox WHERE event_type='review.submitted'").fetchone() == (1,)


@pytest.mark.asyncio
async def test_private_activation_writes_exact_release_evidence_and_v2_envelope(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    service, plan, snapshot = await _ready_candidate(db_path, clock=_Clock(), audience_mode="private")
    result = await service.activate_private(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        actor_user_id="user_owner",
        candidate_row_version=1,
        candidate_hash="sha256:" + "9" * 64,
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        snapshot_id=snapshot.id,
        snapshot_hash=snapshot.snapshot_hash,
        override_id=None,
        override_hash=None,
        idempotency_key="activate-private",
    )
    replay = await service.activate_private(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        actor_user_id="user_owner",
        candidate_row_version=1,
        candidate_hash="sha256:" + "9" * 64,
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        snapshot_id=snapshot.id,
        snapshot_hash=snapshot.snapshot_hash,
        override_id=None,
        override_hash=None,
        idempotency_key="activate-private",
    )
    assert replay == result
    assert result.release_id is not None and result.state == "activated"
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        evidence = db.execute("SELECT * FROM agent_release_eval_evidence WHERE release_id=?", (result.release_id,)).fetchone()
        release = db.execute("SELECT eval_evidence_json FROM team_subagent_release WHERE id=?", (result.release_id,)).fetchone()
        assert evidence is not None and evidence["snapshot_hash"] == snapshot.snapshot_hash
        assert release is not None and json.loads(release["eval_evidence_json"]) == {
            "readiness": "ready",
            "release_test": {"reason_codes": [], "state": "passed"},
            "schema_version": 2,
            "snapshot_hash": snapshot.snapshot_hash,
            "snapshot_id": snapshot.id,
        }
        live = db.execute("SELECT live_release_id,release_sequence FROM team_subagent WHERE id='osa_policy'").fetchone()
        assert live is not None and tuple(live) == (
            result.release_id,
            2,
        )


@pytest.mark.asyncio
async def test_workbench_candidate_previews_the_release_sequence_activation_assigns(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    service, plan, snapshot = await _ready_candidate(db_path, clock=_Clock(), audience_mode="private")

    workbench = await service.workbench(team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner")
    candidate = workbench["candidate_identity"]
    assert workbench["live_identity"]["release_sequence"] == 1
    assert candidate["release_sequence_if_activated"] == 2
    assert candidate["base_release_id"] == "arl_policy_base" == workbench["live_identity"]["release_id"]

    result = await service.activate_private(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        actor_user_id="user_owner",
        candidate_row_version=1,
        candidate_hash="sha256:" + "9" * 64,
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        snapshot_id=snapshot.id,
        snapshot_hash=snapshot.snapshot_hash,
        override_id=None,
        override_hash=None,
        idempotency_key="activate-preview",
    )
    with sqlite3.connect(db_path) as db:
        activated = db.execute("SELECT release_sequence FROM team_subagent_release WHERE id=?", (result.release_id,)).fetchone()
    # The previewed number is the number activation actually wrote.
    assert activated == (candidate["release_sequence_if_activated"],)


def _private_release_request(plan: Any, snapshot: Any | None, *, key: str, expected: str | None) -> dict[str, Any]:
    return {
        "team_id": "team_p",
        "agent_id": "osa_policy",
        "candidate_id": "arc_policy",
        "actor_user_id": "user_owner",
        "candidate_row_version": 1,
        "plan_id": plan.id,
        "plan_hash": plan.plan_hash,
        "snapshot_id": snapshot.id if snapshot is not None else None,
        "snapshot_hash": snapshot.snapshot_hash if snapshot is not None else None,
        "idempotency_key": key,
        "expected_test_state": expected,
        **_EVIDENCE_KW,
    }


def _release_test(db_path: Path, release_id: str) -> tuple[dict[str, Any], int]:
    with sqlite3.connect(db_path) as db:
        evidence = json.loads(db.execute("SELECT eval_evidence_json FROM team_subagent_release WHERE id=?", (release_id,)).fetchone()[0])
        rows = db.execute("SELECT COUNT(*) FROM agent_release_eval_evidence WHERE release_id=?", (release_id,)).fetchone()[0]
    return evidence["release_test"], rows


@pytest.mark.asyncio
async def test_a_private_agent_releases_a_change_whose_test_has_no_conclusion(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    # One of three attempts fails on the change: the case is unstable and the test has no conclusion.
    service, plan, snapshot = await _ready_candidate(db_path, clock=_Clock(), audience_mode="private", candidate_failures=1)
    assert snapshot.readiness == "unknown"

    workbench = await service.workbench(team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner")
    assert "flaky_limit_exceeded" in workbench["reason_codes"]
    release = workbench["private_release"]
    assert release["available"] is True and release["warning"] is False
    assert release["test_state"] == "no_conclusion" and release["candidate_covers_draft"] is True
    assert release["release_sequence"] == 2
    # Coach and the Team API read the same release outcome.
    from bowhead_core.agent_version_service import release_check_outcome

    assert release_check_outcome(workbench, failure_counts=None)["private_release"] == release

    result = await service.activate_private(**_private_release_request(plan, snapshot, key="release-unstable", expected="no_conclusion"))

    assert result.state == "activated"
    recorded, evidence_rows = _release_test(db_path, str(result.release_id))
    assert recorded["state"] == "no_conclusion" and "flaky_limit_exceeded" in recorded["reason_codes"]
    # The releasable-evidence table keeps passing results only.
    assert evidence_rows == 0
    from bowhead_core.agent_release_read_store import AgentReleaseReadStore

    history = await AgentReleaseReadStore(str(db_path)).releases(team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner")
    assert history[0]["release_sequence"] == 2 and history[0]["release_test"] == recorded


@pytest.mark.asyncio
@pytest.mark.parametrize(("suite_kind", "state"), [("owner_quality", "regressed"), ("platform_contract", "safety_failed")])
async def test_a_private_release_past_a_regression_or_contract_failure_is_a_warning(tmp_path: Path, suite_kind: str, state: str) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    # Every attempt fails on the change: a required owner case newly fails, or the contract case fails.
    service, plan, snapshot = await _ready_candidate(db_path, clock=_Clock(), audience_mode="private", candidate_failures=3, suite_kind=suite_kind)

    release = (await service.workbench(team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner"))["private_release"]
    assert release["test_state"] == state and release["warning"] is True and release["available"] is True

    result = await service.activate_private(**_private_release_request(plan, snapshot, key="release-anyway", expected=state))
    assert _release_test(db_path, str(result.release_id))[0]["state"] == state


@pytest.mark.asyncio
async def test_a_private_agent_releases_an_untested_candidate_but_not_during_its_test(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    plan, control = await _comparison_context(db_path, audience_mode="private")
    revision_store = AgentRevisionStore(str(db_path), validator=_ActivationValidator(), clock=_Clock())

    async def no_drift(*_args, **_kwargs):
        return ()

    revision_store._candidate_drift_reasons_conn = no_drift  # type: ignore[method-assign]
    service = AgentVersionService(db_path, revision_store=revision_store, clock=_Clock())
    assert (await service.workbench(team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner"))["private_release"][
        "test_state"
    ] == "not_tested"

    comparison = AgentEvalComparisonStore(db_path, clock=_Clock())
    started = await comparison.start_release(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        actor_user_id="user_owner",
        candidate_row_version=1,
        expected_control_row_version=control.row_version,
        candidate_hash="sha256:" + "9" * 64,
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        model_config_hashes=_model_hashes(),
        idempotency_key="comparison-running",
    )
    running = (await service.workbench(team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner"))["private_release"]
    assert running["available"] is False and running["blocked_reason_code"] == "agent_release_test_running"
    with pytest.raises(AgentVersionError) as refused:
        await service.activate_private(**_private_release_request(plan, None, key="release-running", expected="not_tested"))
    assert refused.value.code == "agent_eval_release_run_incomplete"

    await comparison.cancel(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        execution_id=started.id,
        actor_user_id="user_owner",
        expected_execution_row_version=started.row_version,
    )
    stopped = (await service.workbench(team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner"))["private_release"]
    # A stopped test measured nothing: the change is released as untested.
    assert stopped["available"] is True and stopped["test_state"] == "not_tested"

    result = await service.activate_private(**_private_release_request(plan, None, key="release-untested", expected="not_tested"))
    assert _release_test(db_path, str(result.release_id)) == ({"reason_codes": [], "state": "not_tested"}, 0)


@pytest.mark.asyncio
async def test_a_shared_agent_keeps_the_evidence_gate(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    service, plan, snapshot = await _ready_candidate(db_path, clock=_Clock(), audience_mode="teamwide", candidate_failures=1)

    workbench = await service.workbench(team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner")
    assert workbench["private_release"] is None
    with pytest.raises(AgentVersionError) as refused:
        await service.submit_for_review(
            team_id="team_p",
            agent_id="osa_policy",
            candidate_id="arc_policy",
            actor_user_id="user_owner",
            candidate_row_version=1,
            plan_id=plan.id,
            plan_hash=plan.plan_hash,
            snapshot_id=snapshot.id,
            snapshot_hash=snapshot.snapshot_hash,
            idempotency_key="submit-unstable",
            **_EVIDENCE_KW,
        )
    assert refused.value.code == "agent_eval_release_blocked"
    # Private activation of a shared candidate stays refused as well.
    with pytest.raises(AgentVersionError) as mismatch:
        await service.activate_private(**_private_release_request(plan, snapshot, key="activate-shared", expected=None))
    assert mismatch.value.code == "agent_release_audience_action_mismatch"


@pytest.mark.asyncio
async def test_a_release_note_given_at_release_goes_on_the_release_not_the_tested_candidate(tmp_path: Path) -> None:
    """ "Release as v2" takes the note in its dialog; the candidate hash the evidence pins never changes."""
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    service, plan, snapshot = await _ready_candidate(db_path, clock=_Clock(), audience_mode="private")
    request = {
        "team_id": "team_p",
        "agent_id": "osa_policy",
        "candidate_id": "arc_policy",
        "actor_user_id": "user_owner",
        "candidate_row_version": 1,
        "candidate_hash": "sha256:" + "9" * 64,
        "plan_id": plan.id,
        "plan_hash": plan.plan_hash,
        "snapshot_id": snapshot.id,
        "snapshot_hash": snapshot.snapshot_hash,
        "override_id": None,
        "override_hash": None,
        "idempotency_key": "activate-with-note",
    }
    result = await service.activate_private(**request, release_note="  Percentages keep one decimal place.  ")
    # The same key with another note is a different request, not a replay.
    with pytest.raises(Exception) as conflicting:
        await service.activate_private(**request, release_note="Something else")
    assert "conflict" in str(getattr(conflicting.value, "code", conflicting.value))
    with sqlite3.connect(db_path) as db:
        release_note = db.execute("SELECT release_note FROM team_subagent_release WHERE id=?", (result.release_id,)).fetchone()[0]
        candidate = db.execute("SELECT release_note,candidate_hash FROM team_subagent_release_candidate WHERE id='arc_policy'").fetchone()
    assert release_note == "Percentages keep one decimal place."
    assert candidate == (None, "sha256:" + "9" * 64)


@pytest.mark.asyncio
async def test_workbench_projects_outdated_candidate_as_stale_after_live_moves(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    service, _plan, _snapshot = await _ready_candidate(db_path, clock=_Clock(), audience_mode="private")
    with sqlite3.connect(db_path) as db:
        # Another release went live after the candidate was prepared (e.g. an audience contraction).
        db.execute(
            """
            INSERT INTO team_subagent_release(
              id,team_id,subagent_id,release_sequence,revision_id,predecessor_release_id,
              audience_snapshot_json,audience_hash,capability_fingerprint,
              dependency_fingerprint,risk_json,safety_report_id,activated_by,activated_at
            )
            SELECT 'arl_policy_next',team_id,subagent_id,2,revision_id,id,
                   audience_snapshot_json,audience_hash,capability_fingerprint,
                   dependency_fingerprint,risk_json,safety_report_id,activated_by,activated_at
            FROM team_subagent_release WHERE id='arl_policy_base'
            """
        )
        db.execute("UPDATE team_subagent SET live_release_id='arl_policy_next',release_sequence=2 WHERE id='osa_policy'")

    workbench = await service.workbench(team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner")
    candidate = workbench["candidate_identity"]
    assert workbench["live_identity"]["release_id"] == "arl_policy_next"
    assert candidate["base_release_id"] == "arl_policy_base"
    assert candidate["release_sequence_if_activated"] == 3
    # Not yet marked stale, but activation would reject it: no release primary, the stale card instead.
    assert candidate["state"] == "prepared"
    assert workbench["display_state"] == "candidate_stale"
    assert workbench["primary_action"]["kind"] in {"prepare_candidate", "rebase_draft"}
    assert workbench["primary_action"]["kind"] not in {"activate_candidate", "submit_for_review"}
    assert "base_release_changed" in workbench["reason_codes"]
    assert {"enabled": True, "kind": "withdraw_candidate"} in workbench["secondary_actions"]


@pytest.mark.asyncio
async def test_workbench_still_offers_release_when_candidate_base_is_live(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    service, _plan, _snapshot = await _ready_candidate(db_path, clock=_Clock(), audience_mode="private")
    workbench = await service.workbench(team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner")
    assert workbench["display_state"] == "ready"
    assert workbench["primary_action"]["kind"] == "activate_candidate"
    assert "base_release_changed" not in workbench["reason_codes"]


async def _append_owner_case(db_path: Path, *, case_key: str = "coach.added", valid_until: str | None = None) -> str:
    """Save a case into the Owner suite as Coach or the Evaluate tab would; the policy rotates onto it."""
    async with open_shared_ctx(str(db_path)) as db:
        db.row_factory = sqlite3.Row
        await db.execute("BEGIN IMMEDIATE")
        current = await (
            await db.execute("SELECT id FROM agent_eval_suite_revision WHERE suite_id='aes_owner' ORDER BY sequence DESC LIMIT 1")
        ).fetchone()
        appended = await append_eval_suite_cases_conn(
            db,
            team_id="team_p",
            agent_id="osa_policy",
            expected_suite_revision_id=str(current["id"]),
            cases=(
                EvalCaseDraft(
                    case_key=case_key,
                    input={"task": "The case Coach suggested"},
                    fixture_ref={},
                    expected_contract={"allowed_terminal_statuses": ["completed"]},
                    scorer_config={},
                    effect_policy={"mode": "no_effects"},
                    valid_until=valid_until,
                ),
            ),
            actor_user_id="user_owner",
        )
        await db.commit()
    return appended.suite_revision_id


async def _current_policy_id(db_path: Path) -> str:
    current = await AgentEvalPolicyStore(db_path, clock=_Clock()).current(team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner")
    assert current is not None
    return current.id


@pytest.mark.asyncio
async def test_workbench_reports_a_plan_behind_the_policy_and_offers_the_refreshing_run(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    plan, control = await _comparison_context(db_path)
    revision_store = AgentRevisionStore(str(db_path), validator=_ActivationValidator(), clock=_Clock())
    service = AgentVersionService(db_path, revision_store=revision_store, clock=_Clock())

    before = await service.workbench(team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner")
    assert before["evaluation_plan_behind"] is False
    assert before["active_plan_identity"]["policy_revision_id"] == plan.policy_revision_id == before["current_eval_policy_revision_id"]
    assert before["active_plan_identity"]["execution_workspace_id"] == "ws_eval"
    assert before["primary_action"] == {"disabled_reason_codes": [], "enabled": True, "kind": "run_release_comparison"}

    await _append_owner_case(db_path)
    behind = await service.workbench(team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner")
    assert behind["evaluation_plan_behind"] is True
    assert behind["current_eval_policy_revision_id"] != plan.policy_revision_id
    # The Owner's run refreshes the plan first, so running stays the one next step.
    assert behind["primary_action"] == {"disabled_reason_codes": [], "enabled": True, "kind": "run_release_comparison"}

    refreshed = await _refresh_behind_plan(db_path)
    assert refreshed is not None
    updated = refreshed[0]
    assert len(updated.suites[1].fixture_bundle["cases"]) == 2
    after = await service.workbench(team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner")
    assert after["evaluation_plan_behind"] is False
    assert after["active_plan_identity"]["id"] == updated.id
    assert after["primary_action"] == {"disabled_reason_codes": [], "enabled": True, "kind": "run_release_comparison"}
    # The earlier plan is history, not rewritten.
    kept = await AgentEvalPlanStore(db_path).get(
        team_id="team_p", agent_id="osa_policy", candidate_id="arc_policy", plan_id=plan.id, actor_user_id="user_owner"
    )
    assert kept.plan_hash == plan.plan_hash


@pytest.mark.asyncio
async def test_updating_a_behind_plan_after_a_comparison_offers_running_it_again(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    service, plan, snapshot = await _ready_candidate(db_path, clock=_Clock(), audience_mode="private")
    await _append_owner_case(db_path)

    behind = await service.workbench(team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner")
    assert behind["display_state"] == "ready"
    assert behind["evaluation_plan_behind"] is True
    # The frozen evidence describes the earlier plan without the saved case, so the next step is a new comparison.
    assert behind["primary_action"] == {"disabled_reason_codes": [], "enabled": True, "kind": "run_release_comparison"}
    # A private Agent may still release; the result is described as not covering the newer case.
    assert behind["private_release"]["test_state"] == "outdated"
    with pytest.raises(AgentVersionError) as refused:
        await service.activate_private(
            team_id="team_p",
            agent_id="osa_policy",
            candidate_id="arc_policy",
            actor_user_id="user_owner",
            candidate_row_version=1,
            plan_id=plan.id,
            plan_hash=plan.plan_hash,
            snapshot_id=snapshot.id,
            snapshot_hash=snapshot.snapshot_hash,
            idempotency_key="activate-behind",
            expected_test_state="passed",
            **_EVIDENCE_KW,
        )
    # The dialog showed an earlier state, so it must show the current one before releasing.
    assert refused.value.code == "agent_release_test_state_changed"
    assert refused.value.details == {"test_state": "outdated"}
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT state FROM team_subagent_release_candidate WHERE id='arc_policy'").fetchone() == ("prepared",)
        assert db.execute("SELECT COUNT(*) FROM agent_release_eval_evidence WHERE candidate_id='arc_policy'").fetchone() == (0,)

    assert await _refresh_behind_plan(db_path) is not None
    after = await service.workbench(team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner")
    assert after["display_state"] == "evidence_stale"
    assert after["reason_codes"] == ["evaluation_plan_changed"]
    # Running the comparison on the updated plan is the fix, so the stale evidence never disables it.
    assert after["primary_action"] == {"disabled_reason_codes": [], "enabled": True, "kind": "run_release_comparison"}
    assert after["evaluation_plan_behind"] is False
    assert after["active_snapshot_identity"] is None
    with sqlite3.connect(db_path) as db:
        # The earlier snapshot stays as history.
        assert db.execute("SELECT plan_id FROM agent_eval_snapshot WHERE id=?", (snapshot.id,)).fetchone() == (plan.id,)


@pytest.mark.asyncio
async def test_an_expired_case_names_itself_when_the_plan_is_compiled(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    _plan, control = await _comparison_context(db_path)
    suite_revision_id = await _append_owner_case(db_path, case_key="quality.7", valid_until="2026-01-01T00:00:00+00:00")

    with pytest.raises(AgentEvalPlanError) as expired:
        await _refresh_behind_plan(db_path)
    assert expired.value.code == "agent_eval_fixture_expired"
    assert expired.value.details["case_key"] == "quality.7"
    assert expired.value.details["suite_revision_id"] == suite_revision_id
    assert expired.value.details["valid_until"] == "2026-01-01T00:00:00+00:00"
    assert expired.value.details["case_revision_id"].startswith("aec")


@pytest.mark.asyncio
async def test_ready_with_risks_requires_complete_owner_override_bundle(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    plan, control = await _comparison_context(db_path, compatibility_verdict="unknown")
    finalized = await _finish_passing_comparison(db_path, plan=plan, control=control, key="comparison-risk")
    assert finalized.snapshot is not None and finalized.snapshot.readiness == "ready_with_risks"
    revision_store = AgentRevisionStore(str(db_path), validator=_UnusedValidator(), clock=_Clock())  # type: ignore[arg-type]
    service = AgentVersionService(db_path, revision_store=revision_store, clock=_Clock())
    with sqlite3.connect(db_path) as db:
        control_version = db.execute(
            "SELECT row_version FROM agent_eval_candidate_control WHERE candidate_id='arc_policy'",
        ).fetchone()[0]

    override = await service.create_override(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        actor_user_id="user_owner",
        candidate_row_version=1,
        expected_control_row_version=control_version,
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        snapshot_id=finalized.snapshot.id,
        snapshot_hash=finalized.snapshot.snapshot_hash,
        reason_overrides=[
            {
                "reason_code": "compatibility_unknown",
                "reason_note": "人工核对当前私有调用方的输出合同。",
            },
        ],
        team_evidence_selection={"regression_category_ids": [], "suite_revision_ids": []},
    )
    workbench = await service.workbench(team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner")

    assert override.reason_overrides[0]["reason_code"] == "compatibility_unknown"
    assert workbench["display_state"] == "ready_with_risks"
    assert workbench["primary_action"]["kind"] == "activate_candidate"
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT active_override_id,row_version FROM agent_eval_candidate_control WHERE candidate_id='arc_policy'",
        ).fetchone() == (override.id, control_version + 1)


@pytest.mark.asyncio
async def test_comparison_job_settlement_requires_attempt_identity_in_same_transaction(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    plan, control = await _comparison_context(db_path)
    comparison = AgentEvalComparisonStore(db_path, clock=_Clock())
    started = await comparison.start_release(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        actor_user_id="user_owner",
        candidate_row_version=1,
        expected_control_row_version=control.row_version,
        candidate_hash="sha256:" + "9" * 64,
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        model_config_hashes=_model_hashes(),
        idempotency_key="comparison-identity",
    )
    platform_run = next(link.run_id for link in started.runs if link.suite_revision_id == "aev_platform")
    await AgentEvalStore(db_path).mark_run_running(platform_run)
    await comparison.mark_running(started.id)
    jobs = AgentEvalJobStore(db_path)
    claim = await jobs.claim_next_job(run_id=platform_run, worker_id="worker-one")
    assert claim is not None
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE agent_eval_case_job SET status='running',started_at=? WHERE id=?",
            (NOW, claim.job.job_id),
        )

    with pytest.raises(AgentEvalJobStoreError, match="agent_eval_attempt_identity_required"):
        await jobs.settle_job(
            job_id=claim.job.job_id,
            lease_token=claim.lease_token,
            status="passed",
            deterministic={"passed": True},
        )
    settled = await jobs.settle_job(
        job_id=claim.job.job_id,
        lease_token=claim.lease_token,
        status="passed",
        deterministic={"passed": True},
        subject_provider_ref="model-config",
        subject_provider_hash=f"sha256:{'6' * 64}",
        reproducibility="exact",
    )

    assert settled.status == "completed"
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_eval_case_result WHERE run_id=?", (platform_run,)).fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM agent_eval_attempt_identity WHERE run_id=?", (platform_run,)).fetchone() == (1,)


@pytest.mark.asyncio
async def test_diagnostic_subset_never_replaces_release_readiness_or_creates_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    plan, control = await _comparison_context(db_path)
    store = AgentEvalComparisonStore(db_path, clock=_Clock())

    started = await store.start_diagnostic(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        actor_user_id="user_owner",
        candidate_row_version=1,
        expected_control_row_version=control.row_version,
        candidate_hash="sha256:" + "9" * 64,
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        model_config_hashes=_model_hashes(),
        selected_case_revision_ids=("aec_aes_owner",),
        idempotency_key="comparison-diagnostic",
    )

    assert started.run_kind == "diagnostic"
    assert len(started.runs) == 2
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_eval_case_job").fetchone() == (6,)
        assert db.execute("SELECT readiness,active_execution_id FROM agent_eval_candidate_control").fetchone() == (
            "missing",
            started.id,
        )
        for link in started.runs:
            db.execute(
                "UPDATE agent_eval_run SET status='running',started_at=? WHERE id=? AND status='queued'",
                (NOW, link.run_id),
            )
            db.execute(
                """
                UPDATE agent_eval_run
                SET status='incomplete',finished_at=?,summary_json='{"schema_version":1}'
                WHERE id=? AND status='running'
                """,
                (NOW, link.run_id),
            )

    finalized = await store.finalize(started.id)

    assert finalized.execution.status == "completed"
    assert finalized.snapshot is None and finalized.decision is None
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_eval_snapshot").fetchone() == (0,)
        assert db.execute("SELECT readiness,active_execution_id FROM agent_eval_candidate_control").fetchone() == (
            "missing",
            None,
        )


@pytest.mark.asyncio
async def test_comparison_attempt_can_bind_the_prepared_candidate_it_evaluates(tmp_path: Path) -> None:
    """The Owner comparison runs while the candidate is still `prepared`; its attempts must bind it.

    The eval binding's candidate lookup still admitted only `pending`/`approved` — the pre-M3 flow,
    where a candidate was created already submitted. 0073 widened the matching database triggers to
    `prepared` but not this query, so every attempt of every Owner comparison was refused before the
    Agent ran, settled as `infra_failed`, and the gate read `unknown` with no way forward. On 116 both
    candidate comparisons ever run ended that way. Earlier tests wrote case results straight into
    the store and never went through the binding.
    """
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    plan, control = await _comparison_context(db_path)
    comparison = AgentEvalComparisonStore(db_path, clock=_Clock())
    started = await comparison.start_release(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        actor_user_id="user_owner",
        candidate_row_version=1,
        expected_control_row_version=control.row_version,
        candidate_hash="sha256:" + "9" * 64,
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        model_config_hashes=_model_hashes(),
        idempotency_key="comparison-binding",
    )
    # The candidate side: the platform contract now also has a Live (baseline) run.
    run_id = next(link.run_id for link in started.runs if link.suite_revision_id == "aev_platform" and link.subject_side == "candidate")
    await AgentEvalStore(db_path).mark_run_running(run_id)
    await comparison.mark_running(started.id)
    jobs = AgentEvalJobStore(db_path)
    claim = await jobs.claim_next_job(run_id=run_id, worker_id="worker-binding")
    assert claim is not None
    scratch = await jobs.start_scratch_turn(
        job_id=claim.job.job_id,
        lease_token=claim.lease_token,
        session_id="s_eval_binding",
        turn_id="t_eval_binding",
        user_text="test",
    )
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT state FROM team_subagent_release_candidate WHERE id='arc_policy'").fetchone() == ("prepared",)

    bound = await AgentExecutionBindingStore(db_path).admit_eval_execution(
        eval_run_id=run_id,
        user_id="user_owner",
        turn_id=scratch.turn_id,
        dispatch_key="eval:binding",
    )

    assert bound.revision_ref == "arv_policy_candidate"
    assert bound.release_id is None


@pytest.mark.asyncio
async def test_private_activation_with_drift_marks_the_prepared_candidate_stale(tmp_path: Path) -> None:
    """Drift found at private activation must invalidate the candidate, not surface as a conflict.

    Private activation runs while the candidate is still `prepared`. `_mark_stale_conn` only matched
    `pending` (the legacy flow), so the update touched no row, raised a version conflict and rolled
    back: the Owner got a 409, the candidate stayed `prepared`, and nothing said "prepare again".
    """
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    service, plan, snapshot = await _ready_candidate(db_path, clock=_Clock(), audience_mode="private")

    async def base_moved(*_args, **_kwargs):
        return ("base_release_changed",)

    service._revision_store._candidate_drift_reasons_conn = base_moved  # type: ignore[method-assign]
    with pytest.raises(AgentReleaseStaleError):
        await service.activate_private(
            team_id="team_p",
            agent_id="osa_policy",
            candidate_id="arc_policy",
            actor_user_id="user_owner",
            candidate_row_version=1,
            candidate_hash="sha256:" + "9" * 64,
            plan_id=plan.id,
            plan_hash=plan.plan_hash,
            snapshot_id=snapshot.id,
            snapshot_hash=snapshot.snapshot_hash,
            override_id=None,
            override_hash=None,
            idempotency_key="activate-drift",
        )

    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT state,decision_reason_code FROM team_subagent_release_candidate WHERE id='arc_policy'").fetchone() == (
            "stale",
            "base_release_changed",
        )
        assert db.execute(
            "SELECT COUNT(*) FROM team_subagent_release_event WHERE candidate_id='arc_policy' AND event_type='candidate.stale'"
        ).fetchone() == (1,)


_EVIDENCE_KW = {"candidate_hash": "sha256:" + "9" * 64, "override_id": None, "override_hash": None}


async def _submitted_shared(db_path: Path) -> tuple[AgentVersionService, Any, Any]:
    service, plan, snapshot = await _ready_candidate(db_path, clock=_Clock())
    await service.submit_for_review(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        actor_user_id="user_owner",
        candidate_row_version=1,
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        snapshot_id=snapshot.id,
        snapshot_hash=snapshot.snapshot_hash,
        idempotency_key="submit-shared",
        **_EVIDENCE_KW,
    )
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE team_members SET role='team_admin' WHERE user_id='user_other'")
    return service, plan, snapshot


async def _approve(service: AgentVersionService, plan: Any, snapshot: Any) -> Any:
    return await service.approve_shared(
        team_id="team_p",
        candidate_id="arc_policy",
        actor_user_id="user_other",
        candidate_row_version=2,
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        snapshot_id=snapshot.id,
        snapshot_hash=snapshot.snapshot_hash,
        idempotency_key="approve-shared",
        **_EVIDENCE_KW,
    )


@pytest.mark.asyncio
async def test_a_note_submitted_for_review_reaches_the_reviewer_and_the_release_without_changing_the_candidate(tmp_path: Path) -> None:
    """Submit for review (as v2) takes the note; the tested candidate and its hash stay as they were."""
    from bowhead_core.agent_release_read_store import AgentReleaseReadStore

    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    service, plan, snapshot = await _ready_candidate(db_path, clock=_Clock())
    await service.submit_for_review(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        actor_user_id="user_owner",
        candidate_row_version=1,
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        snapshot_id=snapshot.id,
        snapshot_hash=snapshot.snapshot_hash,
        idempotency_key="submit-with-note",
        release_note=" Percentages keep one decimal place. ",
        **_EVIDENCE_KW,
    )
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE team_members SET role='team_admin' WHERE user_id='user_other'")
        candidate = db.execute("SELECT release_note,candidate_hash FROM team_subagent_release_candidate WHERE id='arc_policy'").fetchone()
        submitted = db.execute("SELECT review_release_note FROM agent_eval_candidate_control WHERE candidate_id='arc_policy'").fetchone()
    assert candidate == (None, "sha256:" + "9" * 64)
    assert submitted == ("Percentages keep one decimal place.",)
    detail = await AgentReleaseReadStore(db_path).candidate_detail(team_id="team_p", candidate_id="arc_policy", actor_user_id="user_other")
    assert detail["release_note"] == "Percentages keep one decimal place."

    released = await _approve(service, plan, snapshot)
    with sqlite3.connect(db_path) as db:
        note = db.execute("SELECT release_note FROM team_subagent_release WHERE id=?", (released.release_id,)).fetchone()
    assert note == ("Percentages keep one decimal place.",)


def _live_release(db_path: Path) -> tuple[Any, ...]:
    with sqlite3.connect(db_path) as db:
        return db.execute("SELECT audience_mode,live_release_id,lifecycle_state FROM team_subagent WHERE id='osa_policy'").fetchone()


@pytest.mark.asyncio
async def test_submitting_for_review_refuses_a_plan_behind_the_saved_cases(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    service, plan, snapshot = await _ready_candidate(db_path, clock=_Clock())
    await _append_owner_case(db_path)

    behind = await service.workbench(team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner")
    # Submitting waits for a comparison on the refreshed plan; the workbench offers that run instead.
    assert behind["primary_action"] == {"disabled_reason_codes": [], "enabled": True, "kind": "run_release_comparison"}
    with pytest.raises(AgentVersionError) as refused:
        await service.submit_for_review(
            team_id="team_p",
            agent_id="osa_policy",
            candidate_id="arc_policy",
            actor_user_id="user_owner",
            candidate_row_version=1,
            plan_id=plan.id,
            plan_hash=plan.plan_hash,
            snapshot_id=snapshot.id,
            snapshot_hash=snapshot.snapshot_hash,
            idempotency_key="submit-behind",
            **_EVIDENCE_KW,
        )
    assert refused.value.code == "agent_eval_plan_behind"
    assert refused.value.details["candidate_id"] == "arc_policy"
    assert refused.value.details["plan_policy_revision_id"] == plan.policy_revision_id
    with sqlite3.connect(db_path) as db:
        # Nothing reaches reviewers: still prepared, no review identity, no notification.
        assert db.execute("SELECT state FROM team_subagent_release_candidate WHERE id='arc_policy'").fetchone() == ("prepared",)
        assert db.execute("SELECT review_plan_id FROM agent_eval_candidate_control WHERE candidate_id='arc_policy'").fetchone() == (None,)
        assert db.execute("SELECT COUNT(*) FROM agent_version_notification_outbox WHERE event_type='review.submitted'").fetchone() == (0,)


@pytest.mark.asyncio
async def test_cases_saved_during_review_leave_the_submitted_plan_reviewable(tmp_path: Path) -> None:
    """Review freezes the plan the Owner submitted; cases saved meanwhile apply to the next candidate."""
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    service, plan, snapshot = await _submitted_shared(db_path)
    await _append_owner_case(db_path)
    assert await _current_policy_id(db_path) != plan.policy_revision_id

    pending = await service.workbench(team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner")
    assert pending["display_state"] == "pending_review"
    assert pending["evaluation_plan_behind"] is False
    review = await service.reviewer_projection(team_id="team_p", candidate_id="arc_policy", actor_user_id="user_other")
    assert review["decision_availability"]["approve"] is True

    result = await _approve(service, plan, snapshot)
    assert result.state == "activated" and result.release_id is not None


@pytest.mark.asyncio
async def test_comparisons_refuse_a_plan_behind_the_saved_cases(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    plan, control = await _comparison_context(db_path)
    await _append_owner_case(db_path)
    store = AgentEvalComparisonStore(db_path, clock=_Clock())
    common = {
        "team_id": "team_p",
        "agent_id": "osa_policy",
        "candidate_id": "arc_policy",
        "actor_user_id": "user_owner",
        "candidate_row_version": 1,
        "expected_control_row_version": control.row_version,
        "candidate_hash": "sha256:" + "9" * 64,
        "plan_id": plan.id,
        "plan_hash": plan.plan_hash,
        "model_config_hashes": _model_hashes(),
    }

    with pytest.raises(AgentEvalComparisonError) as release:
        await store.start_release(**common, idempotency_key="comparison-behind")
    assert release.value.code == "agent_eval_plan_behind"
    assert release.value.details == {
        "candidate_id": "arc_policy",
        "current_policy_revision_id": await _current_policy_id(db_path),
        "plan_id": plan.id,
        "plan_policy_revision_id": plan.policy_revision_id,
    }
    with pytest.raises(AgentEvalComparisonError) as diagnostic:
        await store.start_diagnostic(**common, selected_case_revision_ids=("aec_aes_owner",), idempotency_key="diagnostic-behind")
    assert diagnostic.value.code == "agent_eval_plan_behind"
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_eval_comparison_execution WHERE candidate_id='arc_policy'").fetchone() == (0,)

    # Only the Owner's own release run may refresh; CI and Coach keep getting the refusal above.
    with pytest.raises(AgentVersionAutomationError):
        await store.start_release(**common, idempotency_key="comparison-refresh-ci", refresh_behind_plan=True, actor_kind="automation")
    started = await store.start_release(**common, idempotency_key="comparison-refresh", refresh_behind_plan=True)
    assert started.plan_id != plan.id
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT policy_revision_id,created_by FROM agent_eval_plan WHERE id=?", (started.plan_id,)).fetchone() == (
            await _current_policy_id(db_path),
            "user_owner",
        )
        assert db.execute("SELECT active_plan_id FROM agent_eval_candidate_control WHERE candidate_id='arc_policy'").fetchone() == (started.plan_id,)


def _policy_rows(db_path: Path) -> list[tuple[int, str, str]]:
    with sqlite3.connect(db_path) as db:
        return db.execute("SELECT sequence,policy_json,policy_hash FROM agent_eval_policy_revision ORDER BY sequence").fetchall()


@pytest.mark.asyncio
async def test_new_policies_decide_standard_cases_by_majority_and_critical_cases_by_every_attempt(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    plan, _control = await _comparison_context(db_path)

    [(_sequence, policy_json, _hash)] = _policy_rows(db_path)
    assert json.loads(policy_json)["verdict_rules"] == {"critical": "threshold", "standard": "majority"}
    assert plan.policy_revision_id == await _current_policy_id(db_path)


@pytest.mark.asyncio
async def test_the_owners_next_test_moves_an_older_policy_onto_verdict_rules(tmp_path: Path) -> None:
    """A policy from before verdict rules keeps its plans and evidence until the Owner tests again."""
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    plan, control = await _comparison_context(db_path, draft=replace(_draft(), verdict_rules=None))
    [(_sequence, legacy_json, legacy_hash)] = _policy_rows(db_path)
    assert "verdict_rules" not in json.loads(legacy_json)
    store = AgentEvalComparisonStore(db_path, clock=_Clock())
    common = {
        "team_id": "team_p",
        "agent_id": "osa_policy",
        "candidate_id": "arc_policy",
        "actor_user_id": "user_owner",
        "candidate_row_version": 1,
        "expected_control_row_version": control.row_version,
        "candidate_hash": "sha256:" + "9" * 64,
        "plan_id": plan.id,
        "plan_hash": plan.plan_hash,
        "model_config_hashes": _model_hashes(),
    }

    started = await store.start_release(**common, idempotency_key="comparison-adopt", refresh_behind_plan=True)

    (first, adopted) = _policy_rows(db_path)
    # The older revision is never rewritten; the new one copies it and adds only the rules.
    assert first == (1, legacy_json, legacy_hash)
    assert adopted[0] == 2
    assert json.loads(adopted[1]) == {**json.loads(legacy_json), "verdict_rules": {"critical": "threshold", "standard": "majority"}}
    assert started.plan_id != plan.id
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT policy_revision_id FROM agent_eval_plan WHERE id=?", (started.plan_id,)).fetchone() == (
            await _current_policy_id(db_path),
        )
        assert db.execute("SELECT plan_hash FROM agent_eval_plan WHERE id=?", (plan.id,)).fetchone() == (plan.plan_hash,)


@pytest.mark.asyncio
async def test_runs_other_than_the_owners_test_keep_an_older_policy_and_its_plan(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    plan, control = await _comparison_context(db_path, draft=replace(_draft(), verdict_rules=None))
    before = _policy_rows(db_path)

    finalized = await _finish_passing_comparison(db_path, plan=plan, control=control, key="comparison-legacy")

    assert finalized.snapshot is not None and finalized.snapshot.readiness == "ready"
    assert _policy_rows(db_path) == before
    assert await _current_policy_id(db_path) == plan.policy_revision_id


@pytest.mark.asyncio
async def test_accepting_risks_refuses_a_plan_behind_the_saved_cases(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    plan, control = await _comparison_context(db_path, compatibility_verdict="unknown")
    finalized = await _finish_passing_comparison(db_path, plan=plan, control=control, key="comparison-risk-behind")
    assert finalized.snapshot is not None and finalized.snapshot.readiness == "ready_with_risks"
    await _append_owner_case(db_path)
    revision_store = AgentRevisionStore(str(db_path), validator=_UnusedValidator(), clock=_Clock())  # type: ignore[arg-type]
    service = AgentVersionService(db_path, revision_store=revision_store, clock=_Clock())
    workbench = await service.workbench(team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner")
    assert workbench["primary_action"] == {"disabled_reason_codes": [], "enabled": True, "kind": "run_release_comparison"}

    with pytest.raises(AgentVersionError) as refused:
        await service.create_override(
            team_id="team_p",
            agent_id="osa_policy",
            candidate_id="arc_policy",
            actor_user_id="user_owner",
            candidate_row_version=1,
            expected_control_row_version=workbench["control_row_version"],
            plan_id=plan.id,
            plan_hash=plan.plan_hash,
            snapshot_id=finalized.snapshot.id,
            snapshot_hash=finalized.snapshot.snapshot_hash,
            reason_overrides=[{"reason_code": "compatibility_unknown", "reason_note": "Checked the private callers by hand."}],
            team_evidence_selection={"regression_category_ids": [], "suite_revision_ids": []},
        )
    assert refused.value.code == "agent_eval_plan_behind"
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_eval_override WHERE candidate_id='arc_policy'").fetchone() == (0,)


@pytest.mark.asyncio
async def test_reviewer_approval_is_refused_once_the_owner_left_the_team(tmp_path: Path) -> None:
    """审核人批准与 approve_candidate 同口径：owner 已被移出团队，不能再批准为全团队可见。"""
    from bowhead_core.agent_revision_store import AgentPublicationBlockedError

    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    service, plan, snapshot = await _submitted_shared(db_path)
    before = _live_release(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE team_members SET status='removed' WHERE user_id='user_owner'")

    with pytest.raises(AgentPublicationBlockedError, match="agent_owner_inactive"):
        await _approve(service, plan, snapshot)
    assert _live_release(db_path) == before


@pytest.mark.asyncio
async def test_reviewer_approval_marks_stale_when_the_owner_changed(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    service, plan, snapshot = await _submitted_shared(db_path)
    before = _live_release(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE team_subagent SET owner_user_id='user_other' WHERE id='osa_policy'")

    with pytest.raises(AgentReleaseStaleError):
        await _approve(service, plan, snapshot)
    assert _live_release(db_path) == before
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT state FROM team_subagent_release_candidate WHERE id='arc_policy'").fetchone() == ("stale",)


@pytest.mark.asyncio
@pytest.mark.parametrize("reviewer", [False, True])
async def test_archived_agent_cannot_have_its_live_release_rewritten(tmp_path: Path, reviewer: bool) -> None:
    """已归档的 Agent：owner 私有激活与审核人批准都必须拒绝，live release 不变。"""
    from bowhead_core.agent_revision_store import AgentPublicationBlockedError

    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    if reviewer:
        service, plan, snapshot = await _submitted_shared(db_path)
    else:
        service, plan, snapshot = await _ready_candidate(db_path, clock=_Clock(), audience_mode="private")
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE team_subagent SET lifecycle_state='archived',status='disabled' WHERE id='osa_policy'")
    before = _live_release(db_path)

    with pytest.raises(AgentPublicationBlockedError, match="agent_not_active"):
        if reviewer:
            await _approve(service, plan, snapshot)
        else:
            await service.activate_private(
                team_id="team_p",
                agent_id="osa_policy",
                candidate_id="arc_policy",
                actor_user_id="user_owner",
                candidate_row_version=1,
                plan_id=plan.id,
                plan_hash=plan.plan_hash,
                snapshot_id=snapshot.id,
                snapshot_hash=snapshot.snapshot_hash,
                idempotency_key="activate-archived",
                **_EVIDENCE_KW,
            )
    assert _live_release(db_path) == before


@pytest.mark.asyncio
async def test_new_owner_cannot_activate_the_previous_owners_private_candidate(tmp_path: Path) -> None:
    """After an owner change the previous owner's prepared private candidate is stale, as on the reviewer path."""
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    service, plan, snapshot = await _ready_candidate(db_path, clock=_Clock(), audience_mode="private")
    before = _live_release(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE team_subagent SET owner_user_id='user_other' WHERE id='osa_policy'")

    with pytest.raises(AgentReleaseStaleError) as caught:
        await service.activate_private(
            team_id="team_p",
            agent_id="osa_policy",
            candidate_id="arc_policy",
            actor_user_id="user_other",
            candidate_row_version=1,
            plan_id=plan.id,
            plan_hash=plan.plan_hash,
            snapshot_id=snapshot.id,
            snapshot_hash=snapshot.snapshot_hash,
            idempotency_key="activate-after-owner-change",
            **_EVIDENCE_KW,
        )

    assert caught.value.reason_codes == ("agent_owner_changed",)
    assert _live_release(db_path) == before
    with sqlite3.connect(db_path) as db:
        # Going stale frees the candidate slot so the new owner can prepare from their own working copy.
        assert db.execute("SELECT state,decision_reason_code FROM team_subagent_release_candidate WHERE id='arc_policy'").fetchone() == (
            "stale",
            "agent_owner_changed",
        )
        assert db.execute(
            "SELECT COUNT(*) FROM team_subagent_release_candidate WHERE subagent_id='osa_policy' AND state IN ('prepared','pending','approved')"
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_archived_agent_cannot_submit_a_shared_candidate_for_review(tmp_path: Path) -> None:
    """Submitting for review is gated like activation: no pending candidate and no reviewer notice."""
    from bowhead_core.agent_revision_store import AgentPublicationBlockedError

    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    service, plan, snapshot = await _ready_candidate(db_path, clock=_Clock())
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE team_subagent SET lifecycle_state='archived',status='disabled' WHERE id='osa_policy'")
        owner_notices_before = db.execute("SELECT COUNT(*) FROM agent_version_notification_outbox").fetchone()

    with pytest.raises(AgentPublicationBlockedError, match="agent_not_active"):
        await service.submit_for_review(
            team_id="team_p",
            agent_id="osa_policy",
            candidate_id="arc_policy",
            actor_user_id="user_owner",
            candidate_row_version=1,
            plan_id=plan.id,
            plan_hash=plan.plan_hash,
            snapshot_id=snapshot.id,
            snapshot_hash=snapshot.snapshot_hash,
            idempotency_key="submit-archived",
            **_EVIDENCE_KW,
        )

    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT state FROM team_subagent_release_candidate WHERE id='arc_policy'").fetchone() == ("prepared",)
        assert db.execute(
            "SELECT COUNT(*) FROM team_subagent_release_event WHERE candidate_id='arc_policy' AND event_type='candidate.submitted'"
        ).fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM team_subagent_notification_outbox").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM agent_version_notification_outbox").fetchone() == owner_notices_before


def test_comparison_run_budget_scales_the_attempt_limit_by_the_attempts_each_worker_runs() -> None:
    limits = AgentEvalPolicyDraft.default().cost_and_latency_limits
    # 4 cases x 3 attempts on 4 workers: 3 attempt slots of 120 s, plus scheduling slack.
    assert comparison_run_budget(case_count=4, attempts=3, limits=limits) == (3 * 120_000 + RUN_SCHEDULING_SLACK_MS, 12 * 5_000_000)
    # Never past the comparison-wide limits, since all runs of a comparison start together.
    assert comparison_run_budget(case_count=100, attempts=3, limits=limits) == (1_800_000, 100_000_000)


@pytest.mark.asyncio
async def test_release_comparison_runs_get_a_run_budget_not_the_attempt_limit(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    plan, control = await _comparison_context(db_path)
    await AgentEvalComparisonStore(db_path, clock=_Clock()).start_release(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        actor_user_id="user_owner",
        candidate_row_version=1,
        expected_control_row_version=control.row_version,
        candidate_hash="sha256:" + "9" * 64,
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        model_config_hashes=_model_hashes(),
        idempotency_key="comparison-budget",
    )
    with sqlite3.connect(db_path) as db:
        budgets = set(db.execute("SELECT max_duration_ms,max_cost_microusd FROM agent_eval_run_execution").fetchall())
        run_ids = [row[0] for row in db.execute("SELECT run_id FROM agent_eval_comparison_run")]
    # One case x 3 attempts per run: one round of 120 s attempts plus slack, not 120 s for the whole run.
    assert budgets == {(120_000 + RUN_SCHEDULING_SLACK_MS, 3 * 5_000_000)}
    # Each attempt still runs under the policy's own per-attempt limit.
    assert {await _comparison_attempt_limit_ms(str(db_path), run_id) for run_id in run_ids} == {120_000}


@pytest.mark.asyncio
async def test_comparison_cut_by_the_attempt_time_limit_offers_a_rerun_and_says_why(tmp_path: Path) -> None:
    db_path = tmp_path / "core.db"
    _upgrade(db_path)
    _seed(db_path)
    plan, control = await _comparison_context(db_path)
    comparison = AgentEvalComparisonStore(db_path, clock=_Clock())
    started = await comparison.start_release(
        team_id="team_p",
        agent_id="osa_policy",
        candidate_id="arc_policy",
        actor_user_id="user_owner",
        candidate_row_version=1,
        expected_control_row_version=control.row_version,
        candidate_hash="sha256:" + "9" * 64,
        plan_id=plan.id,
        plan_hash=plan.plan_hash,
        model_config_hashes=_model_hashes(),
        idempotency_key="comparison-time-limit",
    )
    eval_store = AgentEvalStore(db_path)
    for link in started.runs:
        await eval_store.mark_run_running(link.run_id)
        revision = await eval_store.read_suite_revision(link.suite_revision_id)
        assert revision is not None
        for case in revision.cases:
            for attempt in range(1, 4):
                # Every attempt was cut at the per-attempt limit, so neither side has a result. (A cut
                # attempt beside finished ones drops only itself; see test_agent_version_eval.)
                timed_out = True
                await eval_store.record_case_result(
                    run_id=link.run_id,
                    case_revision_id=case.case_revision_id,
                    attempt=attempt,
                    status="infra_failed" if timed_out else "passed",
                    deterministic={"passed": not timed_out},
                    duration_ms=120_300 if timed_out else 40_000,
                    failure_code="agent_eval_attempt_timeout" if timed_out else None,
                )
                with sqlite3.connect(db_path) as db:
                    db.execute(
                        """
                        INSERT INTO agent_eval_attempt_identity(
                          run_id,case_revision_id,attempt,subject_provider_ref,
                          subject_provider_hash,reproducibility,created_at
                        ) VALUES(?,?,?,'model-config',?,'exact',?)
                        """,
                        (link.run_id, case.case_revision_id, attempt, f"sha256:{'6' * 64}", NOW),
                    )
        await eval_store.mark_run_terminal(link.run_id, status="incomplete", summary={"schema_version": 1})
    finalized = await comparison.finalize(started.id)
    assert finalized.snapshot is not None
    revision_store = AgentRevisionStore(str(db_path), validator=_ActivationValidator(), clock=_Clock())
    service = AgentVersionService(db_path, revision_store=revision_store, clock=_Clock())

    workbench = await service.workbench(team_id="team_p", agent_id="osa_policy", actor_user_id="user_owner")

    assert workbench["readiness"] == "unknown"
    assert workbench["reason_codes"] == ["attempt_time_limit_exceeded"]
    assert workbench["primary_action"] == {"disabled_reason_codes": [], "enabled": True, "kind": "retry_after_environment_fix"}
