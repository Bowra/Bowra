"""M3-9 versioned Agent Eval persistence contracts."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import aiosqlite
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.agent_eval_product import AgentEvalProductStore, AgentEvalReleaseGate
from bowhead_core.agent_eval_store import (
    AgentEvalStore,
    AgentEvalStoreError,
    EvalCaseDraft,
    EvalRunSubject,
)
from bowhead_core.agent_revision_store import AgentAudience, AgentPublicationBlockedError
from bowhead_core.notification_store import NotificationStore
from bowhead_core.user_store import UserStore

_HASH_A = "sha256:" + "1" * 64
_HASH_B = "sha256:" + "2" * 64
_HASH_C = "sha256:" + "3" * 64


async def _team_agent_fixture(tmp_path: Path):
    db_path = tmp_path / "bowhead.db"
    assert upgrade(f"sqlite+aiosqlite:///{db_path}", target="core") == ("core",)
    users = UserStore(db_path)
    team = await users.create_team(name="Eval", max_seats=3)
    owner = await users.create_user(team_id=team.id, email="eval-owner@example.com")
    other = await users.create_user(team_id=team.id, email="eval-other@example.com")
    timestamp = "2026-09-05T00:00:00+00:00"
    agent_id = "osa_eval"
    revision_id = "arv_eval"
    release_id = "arl_eval"
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO team_subagent(
              id,team_id,name,display_name,description,tool_refs_json,skills_json,
              visibility,created_by,status,created_at,updated_at
            ) VALUES (?,?,?,?,?,'[]','[]','team_wide',?,'enabled',?,?)
            """,
            (
                agent_id,
                team.id,
                "eval-agent",
                "Eval Agent",
                "Version-bound Eval fixture",
                owner.id,
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO team_subagent_revision(
              id,team_id,subagent_id,sequence,revision_kind,definition_json,
              listing_json,definition_hash,dependency_fingerprint,
              capability_fingerprint,risk_compiler_version,risk_json,
              created_by,created_at
            ) VALUES (?,?,?,1,'authored','{}','{}',?,?,?,'risk-v1','{}',?,?)
            """,
            (
                revision_id,
                team.id,
                agent_id,
                _HASH_A,
                _HASH_B,
                _HASH_C,
                owner.id,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO agent_safety_report(
              id,team_id,agent_id,subject_kind,subject_ref,content_hash,
              dependency_fingerprint,compiler_version,semantic_scanner_version,
              status,deterministic_json,semantic_json,safe_summary_json,
              created_by,created_at
            ) VALUES ('asr_eval',?,?,'revision',?,?,?,'risk-v1','semantic-v1',
                      'pass','{}','{}','{}',?,?)
            """,
            (team.id, agent_id, revision_id, _HASH_A, _HASH_B, owner.id, timestamp),
        )
        connection.execute(
            """
            INSERT INTO team_subagent_release(
              id,team_id,subagent_id,release_sequence,revision_id,
              audience_snapshot_json,audience_hash,capability_fingerprint,
              dependency_fingerprint,risk_json,safety_report_id,activated_by,
              activated_at
            ) VALUES (?,?,?,1,?,'{}',?,?,?,'{}','asr_eval',?,?)
            """,
            (
                release_id,
                team.id,
                agent_id,
                revision_id,
                _HASH_A,
                _HASH_C,
                _HASH_B,
                owner.id,
                timestamp,
            ),
        )
        connection.commit()
    subject = EvalRunSubject(
        team_id=team.id,
        agent_id=agent_id,
        agent_source_kind="team",
        revision_ref=revision_id,
        release_id=release_id,
        capability_fingerprint=_HASH_C,
        dependency_fingerprint=_HASH_B,
        model_config_hash=_HASH_A,
    )
    return db_path, team, owner, other, subject


@pytest.mark.anyio
@pytest.mark.parametrize("candidate_state", ["prepared", "pending", "approved"])
async def test_product_candidate_evidence_is_exact_and_release_gated(tmp_path: Path, candidate_state: str) -> None:
    db_path, team, owner, other, live_subject = await _team_agent_fixture(tmp_path)
    timestamp = "2026-09-05T01:00:00+00:00"
    candidate_id = "arc_eval_candidate"
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "UPDATE team_subagent SET live_release_id=? WHERE id=?",
            (live_subject.release_id, live_subject.agent_id),
        )
        connection.execute(
            """
            INSERT INTO team_subagent_release_candidate(
              id,team_id,subagent_id,revision_id,base_live_release_id,
              target_audience_json,target_audience_hash,capability_fingerprint,
              dependency_fingerprint,risk_compiler_version,risk_json,
              safety_report_id,eval_evidence_json,release_note,candidate_hash,
              state,row_version,submitted_by,submitted_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,'risk-v1','{}','asr_eval',NULL,NULL,
                      ?,?,1,?,?,?)
            """,
            (
                candidate_id,
                team.id,
                live_subject.agent_id,
                live_subject.revision_ref,
                live_subject.release_id,
                '{"schema_version":1,"effective_mode":"teamwide","user_ids":[],"group_ids":[]}',
                _HASH_A,
                live_subject.capability_fingerprint,
                live_subject.dependency_fingerprint,
                _HASH_B,
                candidate_state,
                owner.id,
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            "UPDATE team_members SET role='team_admin' WHERE team_id=? AND user_id=?",
            (team.id, other.id),
        )
        connection.commit()

    product = AgentEvalProductStore(db_path)
    suite, revision = await product.eval_store.ensure_platform_contract_suite(
        team_id=team.id,
        agent_id=live_subject.agent_id,
        actor_user_id=owner.id,
    )
    await product.eval_store.put_trigger_policy(
        team_id=team.id,
        agent_source_kind="team",
        agent_id=live_subject.agent_id,
        actor_user_id=owner.id,
    )
    with pytest.raises(AgentEvalStoreError, match="agent_eval_suite_action_denied"):
        await product.eval_store.create_suite_revision(
            suite_id=suite.suite_id,
            definition={"schema_version": 2},
            scorer_bundle_ref="evalkit.core@1",
            judge_policy={"enabled": False, "rubric_ref": None},
            cases=(
                EvalCaseDraft(
                    case_key="platform.replacement",
                    input={"task": "Replace the platform contract"},
                    fixture_ref={},
                    expected_contract={"allowed_terminal_statuses": ["completed"]},
                    scorer_config={},
                    effect_policy={"mode": "no_effects"},
                ),
            ),
            actor_user_id=owner.id,
        )
    snapshot = await product.manager_snapshot(
        team_id=team.id,
        agent_id=live_subject.agent_id,
        actor_user_id=owner.id,
    )
    assert snapshot["target"] == {
        "kind": "candidate",
        "candidate_id": candidate_id,
        "revision_id": live_subject.revision_ref,
    }
    automatic = await product.resolve_team_subject(
        team_id=team.id,
        agent_id=live_subject.agent_id,
        model_config_hash=_HASH_A,
    )
    assert automatic.subject.candidate_id == candidate_id
    live = await product.resolve_team_subject(
        team_id=team.id,
        agent_id=live_subject.agent_id,
        model_config_hash=_HASH_A,
        target="live",
    )
    assert live.target_kind == "live"
    assert live.subject.candidate_id is None
    assert live.subject.release_id == live_subject.release_id
    target = await product.resolve_team_subject(
        team_id=team.id,
        agent_id=live_subject.agent_id,
        model_config_hash=_HASH_A,
        target="candidate",
    )
    assert target.subject.candidate_id == candidate_id
    with pytest.raises(AgentEvalStoreError, match="agent_eval_action_denied"):
        await product.manager_snapshot(
            team_id=team.id,
            agent_id=live_subject.agent_id,
            actor_user_id=other.id,
        )
    review = await product.release_snapshot(
        team_id=team.id,
        agent_id=live_subject.agent_id,
        candidate_id=candidate_id,
        actor_user_id=other.id,
    )
    assert review["release_gate"] == {
        "required": True,
        "ready": False,
        "reason": "platform_contract_not_current",
    }
    assert snapshot["release_gate"] == review["release_gate"]
    assert review["suites"][0]["revision"]["cases"] == []
    gate = AgentEvalReleaseGate()
    audience = AgentAudience("teamwide")
    with sqlite3.connect(db_path) as raw:
        raw.row_factory = sqlite3.Row
        candidate = raw.execute(
            "SELECT * FROM team_subagent_release_candidate WHERE id=?",
            (candidate_id,),
        ).fetchone()
    assert candidate is not None
    async with aiosqlite.connect(db_path) as db:
        with pytest.raises(AgentPublicationBlockedError, match="agent_eval_required"):
            await gate.evidence_for_activation(db, candidate=candidate, audience=audience)

    run = await product.eval_store.create_run(
        subject=target.subject,
        suite_revision_id=revision.suite_revision_id,
        trigger_kind="manual",
        idempotency_key="candidate-contract",
        actor_user_id=owner.id,
    )
    await product.eval_store.mark_run_running(run.run_id)
    await product.eval_store.record_case_result(
        run_id=run.run_id,
        case_revision_id=revision.cases[0].case_revision_id,
        attempt=1,
        status="passed",
        deterministic={"passed": True, "critical_failure": False, "scorers": []},
    )
    finished = await product.eval_store.mark_run_terminal(
        run.run_id,
        status="passed",
        summary={"case_count": 1, "attempts": 1, "critical_failure": False},
    )
    await product.eval_store.project_evidence(
        team_id=team.id,
        agent_source_kind="team",
        agent_id=live_subject.agent_id,
        suite_id=suite.suite_id,
        subject_fingerprint=finished.subject_fingerprint,
        state="valid",
        current_run_id=run.run_id,
    )
    snapshot = await product.manager_snapshot(
        team_id=team.id,
        agent_id=live_subject.agent_id,
        actor_user_id=owner.id,
    )
    assert snapshot["release_gate"] == {"required": True, "ready": True, "reason": None}
    async with aiosqlite.connect(db_path) as db:
        evidence = await gate.evidence_for_activation(db, candidate=candidate, audience=audience)
    assert evidence is not None
    assert evidence["runs"][0]["run_id"] == run.run_id

    malformed = await product.eval_store.create_run(
        subject=target.subject,
        suite_revision_id=revision.suite_revision_id,
        trigger_kind="manual",
        idempotency_key="candidate-contract-malformed-summary",
        actor_user_id=owner.id,
    )
    await product.eval_store.mark_run_running(malformed.run_id)
    await product.eval_store.record_case_result(
        run_id=malformed.run_id,
        case_revision_id=revision.cases[0].case_revision_id,
        attempt=1,
        status="failed",
        deterministic={"passed": False, "critical_failure": False, "scorers": []},
    )
    malformed = await product.eval_store.mark_run_terminal(
        malformed.run_id,
        status="failed",
        summary={"case_count": 1, "attempts": 1},
    )
    await product.eval_store.project_evidence(
        team_id=team.id,
        agent_source_kind="team",
        agent_id=live_subject.agent_id,
        suite_id=suite.suite_id,
        subject_fingerprint=malformed.subject_fingerprint,
        state="failed",
        current_run_id=malformed.run_id,
        expected_row_version=1,
    )
    async with aiosqlite.connect(db_path) as db:
        with pytest.raises(AgentPublicationBlockedError, match="agent_eval_required"):
            await gate.evidence_for_activation(db, candidate=candidate, audience=audience)

    noncritical = await product.eval_store.create_run(
        subject=target.subject,
        suite_revision_id=revision.suite_revision_id,
        trigger_kind="manual",
        idempotency_key="candidate-contract-noncritical",
        actor_user_id=owner.id,
    )
    await product.eval_store.mark_run_running(noncritical.run_id)
    await product.eval_store.record_case_result(
        run_id=noncritical.run_id,
        case_revision_id=revision.cases[0].case_revision_id,
        attempt=1,
        status="failed",
        deterministic={"passed": False, "critical_failure": False, "scorers": []},
    )
    noncritical = await product.eval_store.mark_run_terminal(
        noncritical.run_id,
        status="failed",
        summary={"case_count": 1, "attempts": 1, "critical_failure": False},
    )
    await product.eval_store.project_evidence(
        team_id=team.id,
        agent_source_kind="team",
        agent_id=live_subject.agent_id,
        suite_id=suite.suite_id,
        subject_fingerprint=noncritical.subject_fingerprint,
        state="failed",
        current_run_id=noncritical.run_id,
        expected_row_version=2,
    )
    async with aiosqlite.connect(db_path) as db:
        evidence = await gate.evidence_for_activation(
            db,
            candidate=candidate,
            audience=audience,
        )
    assert evidence is not None
    assert evidence["runs"][0]["run_id"] == noncritical.run_id
    assert evidence["runs"][0]["run_status"] == "failed"
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO team_subagent_release(
              id,team_id,subagent_id,release_sequence,revision_id,
              predecessor_release_id,source_candidate_id,audience_snapshot_json,
              audience_hash,capability_fingerprint,dependency_fingerprint,
              risk_json,safety_report_id,eval_evidence_json,activated_by,activated_at
            ) VALUES (?,?,?,2,?,?,?,?,?,?,?,'{}','asr_eval','{}',?,?)
            """,
            (
                "arl_eval_candidate",
                team.id,
                live_subject.agent_id,
                live_subject.revision_ref,
                live_subject.release_id,
                candidate_id,
                "{}",
                _HASH_A,
                live_subject.capability_fingerprint,
                live_subject.dependency_fingerprint,
                owner.id,
                timestamp,
            ),
        )
        connection.execute(
            "UPDATE team_subagent SET live_release_id=?,updated_at=? WHERE id=?",
            ("arl_eval_candidate", timestamp, live_subject.agent_id),
        )
        evidence_row = connection.execute(
            "SELECT state,current_run_id FROM agent_eval_evidence_state WHERE suite_id=?",
            (suite.suite_id,),
        ).fetchone()
        outbox_count = connection.execute(
            "SELECT COUNT(*) FROM agent_eval_regression_outbox WHERE agent_id=?",
            (live_subject.agent_id,),
        ).fetchone()
    assert evidence_row == ("failed", noncritical.run_id)
    assert outbox_count == (0,)

    critical = await product.eval_store.create_run(
        subject=target.subject,
        suite_revision_id=revision.suite_revision_id,
        trigger_kind="manual",
        idempotency_key="candidate-contract-critical",
        actor_user_id=owner.id,
    )
    await product.eval_store.mark_run_running(critical.run_id)
    await product.eval_store.record_case_result(
        run_id=critical.run_id,
        case_revision_id=revision.cases[0].case_revision_id,
        attempt=1,
        status="failed",
        deterministic={"passed": False, "critical_failure": True, "scorers": []},
    )
    critical = await product.eval_store.mark_run_terminal(
        critical.run_id,
        status="failed",
        summary={"case_count": 1, "attempts": 1, "critical_failure": True},
    )
    await product.eval_store.project_evidence(
        team_id=team.id,
        agent_source_kind="team",
        agent_id=live_subject.agent_id,
        suite_id=suite.suite_id,
        subject_fingerprint=critical.subject_fingerprint,
        state="blocked",
        current_run_id=critical.run_id,
        expected_row_version=3,
    )
    async with aiosqlite.connect(db_path) as db:
        with pytest.raises(AgentPublicationBlockedError, match="critical_failure"):
            await gate.evidence_for_activation(
                db,
                candidate=candidate,
                audience=audience,
            )


@pytest.mark.anyio
async def test_prepared_candidate_infrastructure_failure_is_not_live_readiness(tmp_path: Path) -> None:
    """A prepared version keeps its incomplete evidence; withdrawing it restores Live.

    Regression mutation: dropping prepared from either candidate query makes the
    candidate target/readiness or automatic execution-subject assertion fail.
    """
    db_path, team, owner, _other, live = await _team_agent_fixture(tmp_path)
    candidate_id = "arc_prepared_infra"
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("UPDATE team_subagent SET live_release_id=? WHERE id=?", (live.release_id, live.agent_id))
        db.execute(
            """
            INSERT INTO team_subagent_release_candidate(
              id,team_id,subagent_id,revision_id,base_live_release_id,
              target_audience_json,target_audience_hash,capability_fingerprint,
              dependency_fingerprint,risk_compiler_version,risk_json,safety_report_id,
              candidate_hash,state,row_version,submitted_by,submitted_at,updated_at
            ) SELECT ?,team_id,subagent_id,revision_id,id,audience_snapshot_json,
                     audience_hash,capability_fingerprint,dependency_fingerprint,
                     'risk-v1',risk_json,safety_report_id,?,'prepared',1,activated_by,
                     activated_at,activated_at
              FROM team_subagent_release WHERE id=?
            """,
            (candidate_id, _HASH_B, live.release_id),
        )
    product = AgentEvalProductStore(db_path)
    suite, revision = await product.eval_store.ensure_platform_contract_suite(
        team_id=team.id,
        agent_id=live.agent_id,
        actor_user_id=owner.id,
    )
    # Build the known candidate's execution fact independently of the read model.
    subject = EvalRunSubject(
        team_id=team.id,
        agent_id=live.agent_id,
        agent_source_kind="team",
        revision_ref=live.revision_ref,
        candidate_id=candidate_id,
        release_id=None,
        capability_fingerprint=live.capability_fingerprint,
        dependency_fingerprint=live.dependency_fingerprint,
        model_config_hash=_HASH_A,
    )
    run = await product.eval_store.create_run(
        subject=subject,
        suite_revision_id=revision.suite_revision_id,
        trigger_kind="manual",
        idempotency_key="prepared-infra",
        actor_user_id=owner.id,
    )
    await product.eval_store.mark_run_running(run.run_id)
    await product.eval_store.record_case_result(
        run_id=run.run_id,
        case_revision_id=revision.cases[0].case_revision_id,
        attempt=1,
        status="infra_failed",
        deterministic={},
    )
    failed = await product.eval_store.mark_run_terminal(
        run.run_id,
        status="incomplete",
        summary={"attempts": 1, "case_count": 1, "infra_failures": 1, "critical_failure": False},
    )
    await product.eval_store.project_evidence(
        team_id=team.id,
        agent_source_kind="team",
        agent_id=live.agent_id,
        suite_id=suite.suite_id,
        subject_fingerprint=failed.subject_fingerprint,
        state="unknown",
        current_run_id=run.run_id,
    )
    snapshot = await product.manager_snapshot(team_id=team.id, agent_id=live.agent_id, actor_user_id=owner.id)
    review = await product.release_snapshot(
        team_id=team.id,
        agent_id=live.agent_id,
        candidate_id=candidate_id,
        actor_user_id=owner.id,
    )
    assert snapshot["target"]["candidate_id"] == candidate_id
    assert (
        snapshot["release_gate"]
        == review["release_gate"]
        == {
            "required": True,
            "ready": False,
            "reason": "platform_contract_not_current",
        }
    )
    assert snapshot["suites"][0]["evidence"]["run"]["status"] == "incomplete"
    automatic = await product.resolve_team_subject(team_id=team.id, agent_id=live.agent_id, model_config_hash=_HASH_A)
    assert automatic.target_kind == "candidate"
    assert automatic.subject.candidate_id == candidate_id
    with pytest.raises(AgentEvalStoreError, match="agent_eval_candidate_not_found"):
        await product.resolve_team_subject(team_id="another-team", agent_id=live.agent_id, model_config_hash=_HASH_A, target="candidate")
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE team_subagent_release_candidate SET state='withdrawn',row_version=row_version+1 WHERE id=?", (candidate_id,))
    with pytest.raises(AgentEvalStoreError, match="agent_eval_candidate_not_found"):
        await product.resolve_team_subject(team_id=team.id, agent_id=live.agent_id, model_config_hash=_HASH_A, target="candidate")
    automatic = await product.resolve_team_subject(team_id=team.id, agent_id=live.agent_id, model_config_hash=_HASH_A)
    assert automatic.target_kind == "live"
    assert automatic.subject.release_id == live.release_id


@pytest.mark.anyio
async def test_regression_claim_skips_changes_a_later_release_superseded(tmp_path: Path) -> None:
    """连发多版只该跑最后一版；被取代的待办不消耗任何模型调用。

    outbox 由数据库触发器写，`UNIQUE(...)+INSERT OR IGNORE` 只挡得住**完全相同**的重复，
    挡不住「连发 N 个版本排 N 条」。此前 claim 是 `ORDER BY created_at,id LIMIT 1` 直接领走：
      · 连发 N 次 ⇒ 跑 N 遍真 LLM 评测，N-1 遍烧在已被取代的版本上；
      · 管理员改一次团队模型配置 ⇒ 给团队内每个订阅的 Agent 各排一条；
      · 关闸期间触发器照写（它读不到 flag），重开时一次性重放整段历史。
    """
    db_path, team, _owner, _other, subject = await _team_agent_fixture(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE team_subagent SET live_release_id='arl_v3' WHERE id=?",
            (subject.agent_id,),
        )
        for oid, ref, created in (
            ("aeo_v1", "arl_v1", "2026-09-05T00:00:00Z"),
            ("aeo_v2", "arl_v2", "2026-09-05T00:01:00Z"),
            ("aeo_v3", "arl_v3", "2026-09-05T00:02:00Z"),
        ):
            connection.execute(
                """
                INSERT INTO agent_eval_regression_outbox(
                  id,team_id,agent_source_kind,agent_id,change_kind,change_ref,
                  status,lease_generation,attempt_count,available_at,created_at
                ) VALUES (?,?,'team',?,'definition_change',?,'pending',0,0,?,?)
                """,
                (oid, team.id, subject.agent_id, ref, created, created),
            )
        connection.commit()

    claimed = await AgentEvalProductStore(db_path).claim_regression(worker_id="worker-a")

    assert claimed is not None
    assert claimed.event.change_ref == "arl_v3", "只有当前 live release 那条该被领走"

    with sqlite3.connect(db_path) as connection:
        rows = dict(
            connection.execute(
                "SELECT id, status || ':' || COALESCE(outcome_code,'-') FROM agent_eval_regression_outbox",
            ).fetchall(),
        )
    assert rows["aeo_v1"] == "skipped:superseded_change"
    assert rows["aeo_v2"] == "skipped:superseded_change"
    assert rows["aeo_v3"].startswith("leased:")


@pytest.mark.anyio
async def test_regression_claim_skips_a_model_change_the_team_has_moved_past(tmp_path: Path) -> None:
    """模型配置继续前进后，旧的 model_change 待办作废。"""
    db_path, team, _owner, _other, subject = await _team_agent_fixture(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE teams SET llm_config_version=7 WHERE id=?", (team.id,))
        for oid, ref, created in (
            ("aeo_old", "model:5", "2026-09-05T00:00:00Z"),
            ("aeo_now", "model:7", "2026-09-05T00:01:00Z"),
        ):
            connection.execute(
                """
                INSERT INTO agent_eval_regression_outbox(
                  id,team_id,agent_source_kind,agent_id,change_kind,change_ref,
                  status,lease_generation,attempt_count,available_at,created_at
                ) VALUES (?,?,'team',?,'model_change',?,'pending',0,0,?,?)
                """,
                (oid, team.id, subject.agent_id, ref, created, created),
            )
        connection.commit()

    claimed = await AgentEvalProductStore(db_path).claim_regression(worker_id="worker-a")

    assert claimed is not None
    assert claimed.event.change_ref == "model:7"
    with sqlite3.connect(db_path) as connection:
        status = connection.execute(
            "SELECT status, outcome_code FROM agent_eval_regression_outbox WHERE id='aeo_old'",
        ).fetchone()
    assert status == ("skipped", "superseded_change")


@pytest.mark.anyio
async def test_regression_outbox_retries_with_fenced_lease(tmp_path: Path) -> None:
    db_path, team, _owner, _other, subject = await _team_agent_fixture(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO agent_eval_regression_outbox(
              id,team_id,agent_source_kind,agent_id,change_kind,change_ref,
              status,lease_generation,attempt_count,available_at,created_at
            ) VALUES ('aeo_retry',?,'team',?,'definition_change','arl_next',
                      'pending',0,0,'2026-09-05T00:00:00Z','2026-09-05T00:00:00Z')
            """,
            (team.id, subject.agent_id),
        )
        connection.commit()
    product = AgentEvalProductStore(db_path)
    first = await product.claim_regression(worker_id="worker-a")
    assert first is not None
    assert first.event.attempt_count == 1
    await product.retry_regression(first, outcome_code="temporary", delay_seconds=0)
    second = await product.claim_regression(worker_id="worker-b")
    assert second is not None
    assert second.event.lease_generation == 2
    assert second.event.attempt_count == 2
    with pytest.raises(AgentEvalStoreError, match="lease_fenced"):
        await product.settle_regression(
            first,
            status="skipped",
            outcome_code="stale_worker",
        )
    await product.settle_regression(
        second,
        status="delivered",
        outcome_code="runs_scheduled",
    )


@pytest.mark.anyio
async def test_automatic_eval_failure_notifies_owner_durably(tmp_path: Path) -> None:
    db_path, team, owner, _other, subject = await _team_agent_fixture(tmp_path)
    product = AgentEvalProductStore(db_path)
    _suite, revision = await product.eval_store.ensure_platform_contract_suite(
        team_id=team.id,
        agent_id=subject.agent_id,
        actor_user_id=owner.id,
    )
    run = await product.eval_store.create_run(
        subject=subject,
        suite_revision_id=revision.suite_revision_id,
        trigger_kind="definition_change",
        idempotency_key="automatic:failure-notification",
        actor_user_id=owner.id,
    )
    await product.eval_store.mark_run_running(run.run_id)
    await product.eval_store.record_case_result(
        run_id=run.run_id,
        case_revision_id=revision.cases[0].case_revision_id,
        attempt=1,
        status="failed",
        deterministic={"critical_failure": True, "passed": False},
    )
    await product.eval_store.mark_run_terminal(
        run.run_id,
        status="failed",
        summary={"case_count": 1, "critical_failure": True},
    )

    notification_store = NotificationStore(str(db_path))
    await notification_store.initialize()
    claim = await product.claim_failure_notification(worker_id="worker-a")
    assert claim is not None
    assert claim.notification.run_id == run.run_id
    assert claim.notification.recipient_user_id == owner.id
    assert claim.notification.critical_failure is True
    await product.project_failure_notification(
        claim,
        notification_store=notification_store,
    )
    await product.settle_failure_notification(
        claim,
        status="delivered",
        outcome_code="notification_projected",
    )
    items, _cursor = await notification_store.list_for_user(
        user_id=owner.id,
        tab="action",
    )
    assert len(items) == 1
    assert items[0].type == "agent.eval.regression_failed"
    assert items[0].subject_id == run.run_id
    assert items[0].metadata == {
        "schema_version": 1,
        "agent_id": subject.agent_id,
        "run_id": run.run_id,
        "run_status": "failed",
        "trigger_kind": "definition_change",
        "critical_failure": True,
    }
    assert await product.claim_failure_notification(worker_id="worker-b") is None

    cancelled = await product.eval_store.create_run(
        subject=subject,
        suite_revision_id=revision.suite_revision_id,
        trigger_kind="dependency_change",
        idempotency_key="automatic:cancelled-notification",
        actor_user_id=owner.id,
    )
    await product.eval_store.mark_run_terminal(
        cancelled.run_id,
        status="cancelled",
        summary={
            "critical_failure": False,
            "failure_code": "agent_eval_rate_limited",
        },
    )
    cancelled_claim = await product.claim_failure_notification(worker_id="worker-b")
    assert cancelled_claim is not None
    assert cancelled_claim.notification.run_status == "cancelled"


def _cases() -> tuple[EvalCaseDraft, ...]:
    return (
        EvalCaseDraft(
            case_key="schema",
            input={"task": "Return a structured answer"},
            fixture_ref={},
            expected_contract={"schema_ref": "answer.v1"},
            scorer_config={"required": ["terminal_contract", "output_schema"]},
            effect_policy={"mode": "no_effects"},
        ),
        EvalCaseDraft(
            case_key="fixture",
            input={"task": "Read the pinned fixture"},
            fixture_ref={
                "kind": "dataset",
                "ref": "dataset_eval",
                "version": "v1",
                "content_hash": _HASH_A,
                "classification": "internal",
            },
            expected_contract={"required_citations": 1},
            scorer_config={"required": ["citation", "no_fabrication"]},
            effect_policy={"mode": "read_only_fixture"},
        ),
    )


@pytest.mark.anyio
async def test_suite_revision_run_result_policy_and_evidence_lifecycle(tmp_path: Path) -> None:
    db_path, team, owner, _other, subject = await _team_agent_fixture(tmp_path)
    store = AgentEvalStore(db_path)
    suite = await store.create_suite(
        team_id=team.id,
        agent_id=subject.agent_id,
        agent_source_kind="team",
        suite_kind="owner_quality",
        name="Quarterly answer quality",
        visibility="owner",
        actor_user_id=owner.id,
    )
    revision = await store.create_suite_revision(
        suite_id=suite.suite_id,
        definition={"purpose": "Check structured, grounded answers"},
        scorer_bundle_ref="evalkit.core@1",
        judge_policy={"enabled": False},
        cases=tuple(reversed(_cases())),
        actor_user_id=owner.id,
    )
    repeated_revision = await store.create_suite_revision(
        suite_id=suite.suite_id,
        definition={"purpose": "Check structured, grounded answers"},
        scorer_bundle_ref="evalkit.core@1",
        judge_policy={"enabled": False},
        cases=_cases(),
        actor_user_id=owner.id,
    )
    assert repeated_revision.suite_revision_id == revision.suite_revision_id
    assert revision.case_count == 2
    assert [case.case_key for case in revision.cases] == ["fixture", "schema"]
    after_revision = await store.read_suite(suite.suite_id)
    assert after_revision is not None and after_revision.row_version == 2
    active = await store.update_suite(
        suite_id=suite.suite_id,
        actor_user_id=owner.id,
        expected_row_version=2,
        state="active",
    )
    assert active.state == "active" and active.row_version == 3

    run = await store.create_run(
        subject=subject,
        suite_revision_id=revision.suite_revision_id,
        trigger_kind="manual",
        idempotency_key="manual:eval:v1",
        actor_user_id=owner.id,
    )
    repeated_run = await store.create_run(
        subject=subject,
        suite_revision_id=revision.suite_revision_id,
        trigger_kind="manual",
        idempotency_key="manual:eval:v1",
        actor_user_id=owner.id,
    )
    assert repeated_run.run_id == run.run_id
    running_evidence = await store.project_evidence(
        team_id=team.id,
        agent_source_kind="team",
        agent_id=subject.agent_id,
        suite_id=suite.suite_id,
        subject_fingerprint=subject.fingerprint,
        state="running",
        current_run_id=run.run_id,
    )
    with pytest.raises(AgentEvalStoreError, match="agent_eval_evidence_run_state_mismatch"):
        await store.project_evidence(
            team_id=team.id,
            agent_source_kind="team",
            agent_id=subject.agent_id,
            suite_id=suite.suite_id,
            subject_fingerprint=subject.fingerprint,
            state="valid",
            current_run_id=run.run_id,
            expected_row_version=running_evidence.row_version,
        )
    await store.mark_run_running(run.run_id)
    with pytest.raises(AgentEvalStoreError, match="agent_eval_run_results_incomplete"):
        await store.mark_run_terminal(
            run.run_id,
            status="passed",
            summary={"critical_failure": False, "passed": 0, "total": 2},
        )
    for case in revision.cases:
        result = await store.record_case_result(
            run_id=run.run_id,
            case_revision_id=case.case_revision_id,
            attempt=1,
            status="passed",
            deterministic={"passed": True, "scorers": ["terminal_contract"]},
            trace_evidence_ref=f"trace_{case.case_key}",
            duration_ms=10,
            cost_microusd=0,
        )
        repeated = await store.record_case_result(
            run_id=run.run_id,
            case_revision_id=case.case_revision_id,
            attempt=1,
            status="passed",
            deterministic={"passed": True, "scorers": ["terminal_contract"]},
            trace_evidence_ref=f"trace_{case.case_key}",
            duration_ms=10,
            cost_microusd=0,
        )
        assert repeated == result
    passed = await store.mark_run_terminal(
        run.run_id,
        status="passed",
        summary={"critical_failure": False, "passed": 2, "total": 2},
    )
    assert passed.status == "passed"
    with pytest.raises(AgentEvalStoreError, match="agent_eval_case_result_unavailable"):
        await store.record_case_result(
            run_id=run.run_id,
            case_revision_id=revision.cases[0].case_revision_id,
            attempt=2,
            status="passed",
            deterministic={"passed": True},
        )

    evidence = await store.project_evidence(
        team_id=team.id,
        agent_source_kind="team",
        agent_id=subject.agent_id,
        suite_id=suite.suite_id,
        subject_fingerprint=subject.fingerprint,
        state="valid",
        current_run_id=run.run_id,
        expected_row_version=running_evidence.row_version,
    )
    stale = await store.project_evidence(
        team_id=team.id,
        agent_source_kind="team",
        agent_id=subject.agent_id,
        suite_id=suite.suite_id,
        subject_fingerprint=_HASH_B,
        state="stale",
        current_run_id=run.run_id,
        stale_reason="definition_changed",
        expected_row_version=evidence.row_version,
    )
    assert stale.state == "stale" and stale.row_version == 3

    policy = await store.put_trigger_policy(
        team_id=team.id,
        agent_source_kind="team",
        agent_id=subject.agent_id,
        actor_user_id=owner.id,
    )
    updated_policy = await store.put_trigger_policy(
        team_id=team.id,
        agent_source_kind="team",
        agent_id=subject.agent_id,
        actor_user_id=owner.id,
        on_model_change=False,
        expected_row_version=policy.row_version,
    )
    assert updated_policy.row_version == 2
    assert updated_policy.on_model_change is False
    with pytest.raises(ValueError, match="not available before M4"):
        await store.put_trigger_policy(
            team_id=team.id,
            agent_source_kind="team",
            agent_id=subject.agent_id,
            actor_user_id=owner.id,
            cron_expr="0 0 * * *",
            expected_row_version=updated_policy.row_version,
        )


@pytest.mark.anyio
async def test_eval_authorization_exact_subject_and_fixture_bounds(tmp_path: Path) -> None:
    db_path, team, owner, other, subject = await _team_agent_fixture(tmp_path)
    store = AgentEvalStore(db_path)
    with pytest.raises(AgentEvalStoreError, match="agent_eval_suite_action_denied"):
        await store.create_suite(
            team_id=team.id,
            agent_id=subject.agent_id,
            agent_source_kind="team",
            suite_kind="owner_quality",
            name="Someone else's suite",
            visibility="owner",
            actor_user_id=other.id,
        )
    with pytest.raises(ValueError, match="embedded or unknown fields"):
        EvalCaseDraft(
            case_key="embedded",
            input={"task": "unsafe fixture"},
            fixture_ref={"kind": "dataset", "ref": "data", "version": "v1", "rows": [1, 2]},
            expected_contract={},
            scorer_config={},
            effect_policy={"mode": "read_only_fixture"},
        )

    suite = await store.create_suite(
        team_id=team.id,
        agent_id=subject.agent_id,
        agent_source_kind="team",
        suite_kind="owner_quality",
        name="Exact subject",
        visibility="reviewer",
        actor_user_id=owner.id,
    )
    revision = await store.create_suite_revision(
        suite_id=suite.suite_id,
        definition={"purpose": "Exact identity"},
        scorer_bundle_ref="evalkit.core@1",
        judge_policy={"enabled": False},
        cases=(_cases()[0],),
        actor_user_id=owner.id,
    )
    await store.update_suite(
        suite_id=suite.suite_id,
        actor_user_id=owner.id,
        expected_row_version=2,
        state="active",
    )
    drifted = EvalRunSubject(
        team_id=subject.team_id,
        agent_id=subject.agent_id,
        agent_source_kind="team",
        revision_ref=subject.revision_ref,
        release_id=subject.release_id,
        capability_fingerprint=_HASH_A,
        dependency_fingerprint=subject.dependency_fingerprint,
        model_config_hash=subject.model_config_hash,
    )
    with pytest.raises(AgentEvalStoreError, match="agent_eval_run_subject_unavailable"):
        await store.create_run(
            subject=drifted,
            suite_revision_id=revision.suite_revision_id,
            trigger_kind="manual",
            idempotency_key="manual:drifted",
            actor_user_id=owner.id,
        )

    expired_revision = await store.create_suite_revision(
        suite_id=suite.suite_id,
        definition={"purpose": "Expired fixture"},
        scorer_bundle_ref="evalkit.core@1",
        judge_policy={"enabled": False},
        cases=(
            EvalCaseDraft(
                case_key="expired",
                input={"task": "Use an expired fixture"},
                fixture_ref={},
                expected_contract={},
                scorer_config={"required": ["terminal_contract"]},
                effect_policy={"mode": "no_effects"},
                valid_until="2020-01-01T00:00:00+00:00",
            ),
        ),
        actor_user_id=owner.id,
    )
    with pytest.raises(AgentEvalStoreError, match="agent_eval_fixture_expired"):
        await store.create_run(
            subject=subject,
            suite_revision_id=expired_revision.suite_revision_id,
            trigger_kind="manual",
            idempotency_key="manual:expired",
            actor_user_id=owner.id,
        )


@pytest.mark.anyio
async def test_judge_policy_requires_exact_run_pins(tmp_path: Path) -> None:
    db_path, team, owner, _other, subject = await _team_agent_fixture(tmp_path)
    store = AgentEvalStore(db_path)
    suite = await store.create_suite(
        team_id=team.id,
        agent_id=subject.agent_id,
        agent_source_kind="team",
        suite_kind="owner_quality",
        name="Semantic checkpoints",
        visibility="reviewer",
        actor_user_id=owner.id,
    )
    revision = await store.create_suite_revision(
        suite_id=suite.suite_id,
        definition={"purpose": "Semantic quality"},
        scorer_bundle_ref="evalkit.core@1",
        judge_policy={"enabled": True, "rubric_ref": "answer-quality@1"},
        cases=(_cases()[0],),
        actor_user_id=owner.id,
    )
    await store.update_suite(
        suite_id=suite.suite_id,
        actor_user_id=owner.id,
        expected_row_version=2,
        state="active",
    )
    with pytest.raises(AgentEvalStoreError, match="agent_eval_judge_pin_mismatch"):
        await store.create_run(
            subject=subject,
            suite_revision_id=revision.suite_revision_id,
            trigger_kind="manual",
            idempotency_key="manual:judge:missing",
            actor_user_id=owner.id,
        )
    run = await store.create_run(
        subject=subject,
        suite_revision_id=revision.suite_revision_id,
        trigger_kind="manual",
        idempotency_key="manual:judge:pinned",
        actor_user_id=owner.id,
        judge_role_ref="judge",
        judge_model_fingerprint=_HASH_A,
    )
    assert run.judge_role_ref == "judge"


@pytest.mark.anyio
async def test_system_agent_eval_uses_effective_revision_and_admin_scope(tmp_path: Path) -> None:
    db_path, team, owner, _other, _team_subject = await _team_agent_fixture(tmp_path)
    timestamp = "2026-09-05T01:00:00+00:00"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE team_members SET role='team_admin' WHERE team_id=? AND user_id=?",
            (team.id, owner.id),
        )
        connection.execute(
            """
            INSERT INTO system_agent_effective_revision(
              id,team_id,agent_id,manifest_fingerprint,override_version,
              definition_json,definition_hash,created_at
            ) VALUES ('arv_system_eval',?,'system:compute',?,NULL,'{}',?,?)
            """,
            (team.id, _HASH_A, _HASH_B, timestamp),
        )
        connection.commit()
    store = AgentEvalStore(db_path)
    suite = await store.create_suite(
        team_id=team.id,
        agent_id="system:compute",
        agent_source_kind="system",
        suite_kind="platform_contract",
        name="Compute contract",
        visibility="team_evidence",
        actor_user_id=owner.id,
    )
    revision = await store.create_suite_revision(
        suite_id=suite.suite_id,
        definition={"purpose": "System contract"},
        scorer_bundle_ref="evalkit.core@1",
        judge_policy={"enabled": False},
        cases=(_cases()[0],),
        actor_user_id=owner.id,
    )
    await store.update_suite(
        suite_id=suite.suite_id,
        actor_user_id=owner.id,
        expected_row_version=2,
        state="active",
    )
    subject = EvalRunSubject(
        team_id=team.id,
        agent_id="system:compute",
        agent_source_kind="system",
        revision_ref="arv_system_eval",
        release_id=None,
        capability_fingerprint=_HASH_A,
        dependency_fingerprint=_HASH_B,
        model_config_hash=_HASH_C,
    )
    run = await store.create_run(
        subject=subject,
        suite_revision_id=revision.suite_revision_id,
        trigger_kind="manual",
        idempotency_key="system:compute:manual:v1",
        actor_user_id=owner.id,
    )
    policy = await store.put_trigger_policy(
        team_id=team.id,
        agent_source_kind="system",
        agent_id="system:compute",
        actor_user_id=owner.id,
    )
    assert run.subject.revision_ref == "arv_system_eval"
    assert policy.agent_source_kind == "system"


@pytest.mark.anyio
async def test_database_guards_reject_eval_evidence_and_revision_forgery(tmp_path: Path) -> None:
    db_path, team, owner, other, subject = await _team_agent_fixture(tmp_path)
    store = AgentEvalStore(db_path)
    suite = await store.create_suite(
        team_id=team.id,
        agent_id=subject.agent_id,
        agent_source_kind="team",
        suite_kind="owner_quality",
        name="Guarded suite",
        visibility="owner",
        actor_user_id=owner.id,
    )
    revision = await store.create_suite_revision(
        suite_id=suite.suite_id,
        definition={"purpose": "Guard invariants"},
        scorer_bundle_ref="evalkit.core@1",
        judge_policy={"enabled": False},
        cases=(_cases()[0],),
        actor_user_id=owner.id,
    )
    await store.update_suite(
        suite_id=suite.suite_id,
        actor_user_id=owner.id,
        expected_row_version=2,
        state="active",
    )
    with sqlite3.connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE agent_eval_suite_revision SET definition_json='{}' WHERE id=?",
                (revision.suite_revision_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="case capacity exceeded"):
            connection.execute(
                """
                INSERT INTO agent_eval_case_revision(
                  id,suite_revision_id,case_key,case_hash,input_json,
                  fixture_ref_json,expected_contract_json,scorer_config_json,
                  effect_policy_json,created_at
                ) VALUES ('aec_forged',?,'forged',?,'{}','{}','{}','{}',
                          '{"mode":"no_effects"}',?)
                """,
                (revision.suite_revision_id, _HASH_A, "2026-09-05T02:00:00+00:00"),
            )

    run = await store.create_run(
        subject=subject,
        suite_revision_id=revision.suite_revision_id,
        trigger_kind="manual",
        idempotency_key="manual:guarded",
        actor_user_id=owner.id,
    )
    await store.mark_run_running(run.run_id)
    await store.record_case_result(
        run_id=run.run_id,
        case_revision_id=revision.cases[0].case_revision_id,
        attempt=1,
        status="failed",
        deterministic={"passed": False},
    )
    with (
        sqlite3.connect(db_path) as connection,
        pytest.raises(
            sqlite3.IntegrityError,
            match="missing case results",
        ),
    ):
        connection.execute(
            """
            UPDATE agent_eval_run
            SET status='passed',summary_json='{"critical_failure":false}',finished_at=?
            WHERE id=?
            """,
            ("2026-09-05T02:00:00+00:00", run.run_id),
        )
    failed = await store.mark_run_terminal(
        run.run_id,
        status="failed",
        summary={"critical_failure": False, "failed": 1, "total": 1},
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError, match="run state mismatch"):
            connection.execute(
                """
                INSERT INTO agent_eval_evidence_state(
                  team_id,agent_source_kind,agent_id,suite_id,current_run_id,
                  subject_fingerprint,state,row_version,updated_at
                ) VALUES (?,'team',?,?,?,?,'blocked',1,?)
                """,
                (
                    team.id,
                    subject.agent_id,
                    suite.suite_id,
                    failed.run_id,
                    subject.fingerprint,
                    "2026-09-05T02:00:00+00:00",
                ),
            )

    policy = await store.put_trigger_policy(
        team_id=team.id,
        agent_source_kind="team",
        agent_id=subject.agent_id,
        actor_user_id=owner.id,
    )
    with (
        sqlite3.connect(db_path) as connection,
        pytest.raises(
            sqlite3.IntegrityError,
            match="policy scope mismatch",
        ),
    ):
        connection.execute(
            """
            UPDATE agent_eval_trigger_policy
            SET enabled=0,row_version=row_version+1,updated_by=?,updated_at=?
            WHERE team_id=? AND agent_source_kind='team' AND agent_id=?
            """,
            (
                other.id,
                "2026-09-05T02:00:00+00:00",
                team.id,
                subject.agent_id,
            ),
        )
    assert policy.row_version == 1


def _latest_revision(db_path: Path, suite_id: str) -> tuple[str, int, str]:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT id,sequence,definition_hash FROM agent_eval_suite_revision WHERE suite_id=? ORDER BY sequence DESC LIMIT 1",
            (suite_id,),
        ).fetchone()
    assert row is not None
    return str(row[0]), int(row[1]), str(row[2])


async def _owner_suite(store: AgentEvalStore, *, team_id: str, agent_id: str, actor_user_id: str) -> str:
    suite = await store.create_suite(
        team_id=team_id,
        agent_id=agent_id,
        agent_source_kind="team",
        suite_kind="owner_quality",
        name="Owner quality",
        visibility="owner",
        actor_user_id=actor_user_id,
    )
    return suite.suite_id


@pytest.mark.anyio
async def test_saving_an_earlier_definition_again_makes_it_current(tmp_path: Path) -> None:
    """Owner saves A, then B, then A again: the suite must run A, not keep running B."""
    db_path, team, owner, _other, subject = await _team_agent_fixture(tmp_path)
    store = AgentEvalStore(db_path)
    suite_id = await _owner_suite(store, team_id=team.id, agent_id=subject.agent_id, actor_user_id=owner.id)
    schema_case, fixture_case = _cases()

    async def save(*cases: EvalCaseDraft):
        return await store.create_suite_revision(
            suite_id=suite_id,
            definition={"schema_version": 1},
            scorer_bundle_ref="evalkit.core@1",
            judge_policy={"enabled": False},
            cases=cases,
            actor_user_id=owner.id,
        )

    first_a = await save(schema_case)
    b = await save(schema_case, fixture_case)
    assert _latest_revision(db_path, suite_id)[0] == b.suite_revision_id
    second_a = await save(schema_case)

    latest_id, latest_sequence, latest_hash = _latest_revision(db_path, suite_id)
    assert latest_id == second_a.suite_revision_id
    assert latest_sequence == 3
    assert second_a.suite_revision_id not in {first_a.suite_revision_id, b.suite_revision_id}
    assert latest_hash == first_a.definition_hash
    assert [case.case_key for case in second_a.cases] == ["schema"]
    # Revision 1 stays immutable and readable for evidence that names it.
    kept = await store.read_suite_revision(first_a.suite_revision_id)
    assert kept is not None and kept.sequence == 1 and [case.case_key for case in kept.cases] == ["schema"]
    # Saving the current definition again is still a no-op.
    repeated = await save(schema_case)
    assert repeated.suite_revision_id == second_a.suite_revision_id
    assert _latest_revision(db_path, suite_id)[1] == 3


@pytest.mark.anyio
async def test_appending_cases_back_to_an_earlier_definition_is_not_a_conflict(tmp_path: Path) -> None:
    """Coach and alignment appends build a full definition that may equal an older revision."""
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.agent_eval_store import append_eval_suite_cases_conn

    db_path, team, owner, _other, subject = await _team_agent_fixture(tmp_path)
    store = AgentEvalStore(db_path)
    suite_id = await _owner_suite(store, team_id=team.id, agent_id=subject.agent_id, actor_user_id=owner.id)
    schema_case, fixture_case = _cases()
    definition = {"schema_version": 1}
    judge_policy = {"enabled": False, "rubric_ref": None}

    async def save(*cases: EvalCaseDraft):
        return await store.create_suite_revision(
            suite_id=suite_id,
            definition=definition,
            scorer_bundle_ref="evalkit.core@1",
            judge_policy=judge_policy,
            cases=cases,
            actor_user_id=owner.id,
        )

    async def append(expected_revision_id: str, case: EvalCaseDraft):
        async with open_shared_ctx(db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            revision = await append_eval_suite_cases_conn(
                db,
                team_id=team.id,
                agent_id=subject.agent_id,
                expected_suite_revision_id=expected_revision_id,
                cases=(case,),
                actor_user_id=owner.id,
            )
            await db.commit()
        return revision

    only_schema = await save(schema_case)
    both = await append(only_schema.suite_revision_id, fixture_case)
    # The Owner removes the fixture case, then a Coach proposal adds it back.
    removed = await save(schema_case)
    assert removed.sequence == 3
    restored = await append(removed.suite_revision_id, fixture_case)

    assert restored.sequence == 4
    assert restored.definition_hash == both.definition_hash
    assert _latest_revision(db_path, suite_id)[0] == restored.suite_revision_id
    assert [case.case_key for case in restored.cases] == ["fixture", "schema"]


@pytest.mark.anyio
async def test_platform_contract_upgrades_in_place_and_rotates_the_policy(tmp_path: Path, monkeypatch) -> None:
    import json

    from bowhead_core.agent_eval_policy_store import AgentEvalPolicyDraft, AgentEvalPolicyStore

    from bowhead_core import agent_eval_store

    v1, v2 = agent_eval_store.PLATFORM_CONTRACT_DEFINITIONS
    db_path, team, owner, _other, live_subject = await _team_agent_fixture(tmp_path)
    # Before the upgrade: a deployment that only knows v1, with a policy pinned to it.
    monkeypatch.setattr(agent_eval_store, "PLATFORM_CONTRACT_DEFINITIONS", (v1,))
    product = AgentEvalProductStore(db_path)
    suite, old_revision = await product.eval_store.ensure_platform_contract_suite(
        team_id=team.id, agent_id=live_subject.agent_id, actor_user_id=owner.id
    )
    policies = AgentEvalPolicyStore(db_path)
    old_policy = await policies.create_revision(
        team_id=team.id,
        agent_id=live_subject.agent_id,
        actor_user_id=owner.id,
        expected_revision_id=None,
        draft=AgentEvalPolicyDraft.default(),
    )
    candidate_id = "arc_upgrade_candidate"
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("UPDATE team_subagent SET live_release_id=? WHERE id=?", (live_subject.release_id, live_subject.agent_id))
        connection.execute(
            """
            INSERT INTO team_subagent_release_candidate(
              id,team_id,subagent_id,revision_id,base_live_release_id,
              target_audience_json,target_audience_hash,capability_fingerprint,
              dependency_fingerprint,risk_compiler_version,risk_json,
              safety_report_id,eval_evidence_json,release_note,candidate_hash,
              state,row_version,submitted_by,submitted_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,'risk-v1','{}','asr_eval',NULL,NULL,?,'prepared',1,?,?,?)
            """,
            (
                candidate_id,
                team.id,
                live_subject.agent_id,
                live_subject.revision_ref,
                live_subject.release_id,
                '{"schema_version":1,"effective_mode":"teamwide","user_ids":[],"group_ids":[]}',
                _HASH_A,
                live_subject.capability_fingerprint,
                live_subject.dependency_fingerprint,
                _HASH_B,
                owner.id,
                "2026-09-05T01:00:00+00:00",
                "2026-09-05T01:00:00+00:00",
            ),
        )
        connection.commit()
    target = await product.resolve_team_subject(team_id=team.id, agent_id=live_subject.agent_id, model_config_hash=_HASH_A, target="candidate")
    run = await product.eval_store.create_run(
        subject=target.subject,
        suite_revision_id=old_revision.suite_revision_id,
        trigger_kind="manual",
        idempotency_key="v1-contract",
        actor_user_id=owner.id,
    )
    await product.eval_store.mark_run_running(run.run_id)
    await product.eval_store.record_case_result(
        run_id=run.run_id,
        case_revision_id=old_revision.cases[0].case_revision_id,
        attempt=1,
        status="passed",
        deterministic={"passed": True, "critical_failure": False, "scorers": []},
    )
    finished = await product.eval_store.mark_run_terminal(
        run.run_id, status="passed", summary={"case_count": 1, "attempts": 1, "critical_failure": False}
    )
    await product.eval_store.project_evidence(
        team_id=team.id,
        agent_source_kind="team",
        agent_id=live_subject.agent_id,
        suite_id=suite.suite_id,
        subject_fingerprint=finished.subject_fingerprint,
        state="valid",
        current_run_id=run.run_id,
    )

    # The upgrade: v2 is listed, and the next ensure appends it and rotates the policy once.
    monkeypatch.setattr(agent_eval_store, "PLATFORM_CONTRACT_DEFINITIONS", (v1, v2))
    _suite, new_revision = await product.eval_store.ensure_platform_contract_suite(
        team_id=team.id, agent_id=live_subject.agent_id, actor_user_id=owner.id
    )
    assert new_revision.suite_id == suite.suite_id and new_revision.suite_revision_id != old_revision.suite_revision_id
    assert "max_tool_calls" not in new_revision.cases[0].scorer_config
    rotated = await policies.current(team_id=team.id, agent_id=live_subject.agent_id, actor_user_id=owner.id)
    assert rotated is not None and rotated.id != old_policy.id
    platform_item = next(item for item in rotated.policy["suite_items"] if item["suite_kind"] == "platform_contract")
    assert platform_item == {**platform_item, "suite_revision_id": new_revision.suite_revision_id, "execution_scope": "paired", "attempts": 3}
    old_settings = {key: value for key, value in old_policy.policy.items() if key != "suite_items"}
    assert {key: value for key, value in rotated.policy.items() if key != "suite_items"} == old_settings

    await product.eval_store.ensure_platform_contract_suite(team_id=team.id, agent_id=live_subject.agent_id, actor_user_id=owner.id)
    again = await policies.current(team_id=team.id, agent_id=live_subject.agent_id, actor_user_id=owner.id)
    assert again is not None and again.id == rotated.id

    # The v1 evidence no longer releases the candidate, and the reason says why.
    with sqlite3.connect(db_path) as raw:
        raw.row_factory = sqlite3.Row
        candidate = raw.execute("SELECT * FROM team_subagent_release_candidate WHERE id=?", (candidate_id,)).fetchone()
    async with aiosqlite.connect(db_path) as db:
        with pytest.raises(AgentPublicationBlockedError) as blocked:
            await AgentEvalReleaseGate().evidence_for_activation(db, candidate=candidate, audience=AgentAudience("teamwide"))
    assert blocked.value.reason_codes == ("platform_contract_upgraded",)

    # A stored contract matching no listed definition is still refused.
    monkeypatch.setattr(
        agent_eval_store,
        "PLATFORM_CONTRACT_DEFINITIONS",
        ({"task": "Unknown platform contract.", "scorer_config": json.loads("{}")},),
    )
    with pytest.raises(AgentEvalStoreError, match="agent_eval_platform_suite_version_mismatch"):
        await product.eval_store.ensure_platform_contract_suite(team_id=team.id, agent_id=live_subject.agent_id, actor_user_id=owner.id)


async def _owner_policy_fixture(tmp_path: Path, *, verdict_rules: object = "default"):
    from bowhead_core.agent_eval_policy_store import AgentEvalPolicyDraft, AgentEvalPolicyStore, AgentEvalPolicySuite

    db_path, team, owner, _other, subject = await _team_agent_fixture(tmp_path)
    store = AgentEvalStore(db_path)
    await store.ensure_platform_contract_suite(team_id=team.id, agent_id=subject.agent_id, actor_user_id=owner.id)
    suite_id = await _owner_suite(store, team_id=team.id, agent_id=subject.agent_id, actor_user_id=owner.id)
    schema_case, _fixture_case = _cases()
    first = await _save_owner_cases(store, suite_id=suite_id, actor_user_id=owner.id, cases=(schema_case,))
    await _activate(store, suite_id=suite_id, actor_user_id=owner.id)
    policies = AgentEvalPolicyStore(db_path)
    # An Owner-chosen tier and attempt count, and a stricter threshold, that a rotation must keep.
    base = AgentEvalPolicyDraft.default(
        owner_suites=(
            AgentEvalPolicySuite(
                suite_revision_id=first.suite_revision_id,
                required=False,
                tier="critical",
                execution_scope="candidate_only",
                attempts=2,
            ),
        ),
    )
    policy = await policies.create_revision(
        team_id=team.id,
        agent_id=subject.agent_id,
        actor_user_id=owner.id,
        expected_revision_id=None,
        draft=replace(
            base,
            pass_thresholds={"critical": 1.0, "standard": 0.9},
            verdict_rules=base.verdict_rules if verdict_rules == "default" else verdict_rules,  # type: ignore[arg-type]
        ),
    )
    return db_path, team, owner, subject, store, policies, suite_id, policy


async def _save_owner_cases(store: AgentEvalStore, *, suite_id: str, actor_user_id: str, cases: tuple[EvalCaseDraft, ...]):
    return await store.create_suite_revision(
        suite_id=suite_id,
        definition={"schema_version": 1},
        scorer_bundle_ref="evalkit.core@1",
        judge_policy={"enabled": False, "rubric_ref": None},
        cases=cases,
        actor_user_id=actor_user_id,
    )


async def _activate(store: AgentEvalStore, *, suite_id: str, actor_user_id: str, state: str = "active") -> None:
    suite = await store.read_suite(suite_id)
    assert suite is not None
    await store.update_suite(suite_id=suite_id, actor_user_id=actor_user_id, expected_row_version=suite.row_version, state=state)  # type: ignore[arg-type]


def _owner_items(policy) -> list[dict]:
    return [item for item in policy.policy["suite_items"] if item["suite_kind"] == "owner_quality"]


def _policy_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM agent_eval_policy_revision").fetchone()[0])


@pytest.mark.anyio
async def test_a_policy_written_before_verdict_rules_follows_suite_saves_without_gaining_them(tmp_path: Path) -> None:
    """Following new suites never changes how cases are decided: only the Owner's next test adopts the rules."""
    db_path, team, owner, subject, store, policies, suite_id, original = await _owner_policy_fixture(tmp_path, verdict_rules=None)
    assert "verdict_rules" not in original.policy
    schema_case, fixture_case = _cases()
    scope = {"team_id": team.id, "agent_id": subject.agent_id, "actor_user_id": owner.id}

    await _save_owner_cases(store, suite_id=suite_id, actor_user_id=owner.id, cases=(schema_case, fixture_case))
    followed = await policies.current(**scope)
    assert followed is not None and followed.sequence == original.sequence + 1
    assert "verdict_rules" not in followed.policy

    # Saving the same definition again still appends nothing, so no plan falls behind.
    await _save_owner_cases(store, suite_id=suite_id, actor_user_id=owner.id, cases=(schema_case, fixture_case))
    assert (await policies.current(**scope)).id == followed.id  # type: ignore[union-attr]


@pytest.mark.anyio
async def test_policy_follows_owner_suite_saves_appends_activation_and_archive(tmp_path: Path) -> None:
    """The evaluation policy runs the latest saved Owner suites, keeping the Owner's settings."""
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.agent_eval_store import append_eval_suite_cases_conn

    db_path, team, owner, subject, store, policies, suite_id, original = await _owner_policy_fixture(tmp_path)
    schema_case, fixture_case = _cases()
    scope = {"team_id": team.id, "agent_id": subject.agent_id, "actor_user_id": owner.id}

    # An Owner save: the pinned entry moves to the new revision with its settings unchanged.
    saved = await _save_owner_cases(store, suite_id=suite_id, actor_user_id=owner.id, cases=(schema_case, fixture_case))
    followed = await policies.current(**scope)
    assert followed is not None and followed.sequence == original.sequence + 1
    assert _owner_items(followed) == [{**_owner_items(original)[0], "suite_revision_id": saved.suite_revision_id}]
    assert {key: value for key, value in followed.policy.items() if key != "suite_items"} == {
        key: value for key, value in original.policy.items() if key != "suite_items"
    }
    assert followed.created_by == owner.id

    # Saving the current definition again changes nothing, so no policy revision is appended.
    await _save_owner_cases(store, suite_id=suite_id, actor_user_id=owner.id, cases=(schema_case, fixture_case))
    assert (await policies.current(**scope)).id == followed.id  # type: ignore[union-attr]

    # Appended cases (accepted Coach proposals, alignment points) follow in the same transaction.
    extra = EvalCaseDraft(
        case_key="align.point",
        input={"task": "Answer with the point"},
        fixture_ref={},
        expected_contract={"allowed_terminal_statuses": ["completed"]},
        scorer_config={},
        effect_policy={"mode": "no_effects"},
    )
    async with open_shared_ctx(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        appended = await append_eval_suite_cases_conn(
            db,
            team_id=team.id,
            agent_id=subject.agent_id,
            expected_suite_revision_id=saved.suite_revision_id,
            cases=(extra,),
            actor_user_id=owner.id,
        )
        await db.commit()
    after_append = await policies.current(**scope)
    assert after_append is not None
    assert [item["suite_revision_id"] for item in _owner_items(after_append)] == [appended.suite_revision_id]

    # A new suite joins once it is active (the save route activates it), with the default settings.
    second_id = await _owner_suite(store, team_id=team.id, agent_id=subject.agent_id, actor_user_id=owner.id)
    second = await _save_owner_cases(store, suite_id=second_id, actor_user_id=owner.id, cases=(schema_case,))
    assert (await policies.current(**scope)).id == after_append.id  # type: ignore[union-attr]
    await _activate(store, suite_id=second_id, actor_user_id=owner.id)
    with_second = await policies.current(**scope)
    assert with_second is not None
    assert _owner_items(with_second) == [
        _owner_items(after_append)[0],
        {
            "attempts": 3,
            "execution_scope": "paired",
            "required": True,
            "suite_kind": "owner_quality",
            "suite_revision_id": second.suite_revision_id,
            "tier": "standard",
        },
    ]

    # An archived suite leaves the policy (a plan can't run an archived suite).
    await _activate(store, suite_id=suite_id, actor_user_id=owner.id, state="archived")
    without_first = await policies.current(**scope)
    assert without_first is not None
    assert [item["suite_revision_id"] for item in _owner_items(without_first)] == [second.suite_revision_id]

    # Every earlier policy revision is kept, so each check stays traceable to its exact suites.
    assert _policy_count(db_path) == without_first.sequence == original.sequence + 4
    with sqlite3.connect(db_path) as connection:
        kept = connection.execute("SELECT policy_hash FROM agent_eval_policy_revision WHERE id=?", (original.id,)).fetchone()
    assert kept == (original.policy_hash,)


@pytest.mark.anyio
async def test_policy_left_behind_catches_up_at_the_next_rotation_point(tmp_path: Path, monkeypatch) -> None:
    """A policy already behind (older deployments, or an inactive Owner) is caught up later."""
    from bowhead_core._db import open_shared_ctx
    from bowhead_core.agent_eval_policy_store import rotate_policy_onto_current_suites_conn

    from bowhead_core import agent_eval_store

    db_path, team, owner, subject, store, policies, suite_id, original = await _owner_policy_fixture(tmp_path)
    scope = {"team_id": team.id, "agent_id": subject.agent_id, "actor_user_id": owner.id}
    schema_case, fixture_case = _cases()

    # Before this change suite saves never touched the policy; reproduce a policy left behind.
    async def _no_rotation(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(agent_eval_store, "_rotate_policy_after_owner_suite_change", _no_rotation)
    newest = await _save_owner_cases(store, suite_id=suite_id, actor_user_id=owner.id, cases=(schema_case, fixture_case))
    monkeypatch.undo()
    assert (await policies.current(**scope)).id == original.id  # type: ignore[union-attr]

    # An Owner who can't hold a policy gets no revision; nothing is attributed to anyone else.
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE team_members SET status='removed' WHERE team_id=? AND user_id=?", (team.id, owner.id))
        connection.commit()
    async with open_shared_ctx(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        assert (
            await rotate_policy_onto_current_suites_conn(db, team_id=team.id, agent_id=subject.agent_id, created_at=agent_eval_store._now()) is None
        )
        await db.commit()
    assert _policy_count(db_path) == 1
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE team_members SET status='active' WHERE team_id=? AND user_id=?", (team.id, owner.id))
        connection.commit()

    # The next ensure (opening the evaluation panel, release review, activation) catches up.
    await store.ensure_platform_contract_suite(**scope)
    caught_up = await policies.current(**scope)
    assert caught_up is not None and caught_up.sequence == 2
    assert _owner_items(caught_up) == [{**_owner_items(original)[0], "suite_revision_id": newest.suite_revision_id}]
    await store.ensure_platform_contract_suite(**scope)
    assert _policy_count(db_path) == 2


@pytest.mark.anyio
async def test_suite_save_with_an_expired_fixture_still_follows_and_is_not_refused(tmp_path: Path) -> None:
    """Expiry is reported where the plan is built; it must not roll back the Owner's save."""
    db_path, team, owner, subject, store, policies, suite_id, _original = await _owner_policy_fixture(tmp_path)
    schema_case, _fixture_case = _cases()
    expired = replace(schema_case, case_key="expired", valid_until="2020-01-01T00:00:00+00:00")

    saved = await _save_owner_cases(store, suite_id=suite_id, actor_user_id=owner.id, cases=(schema_case, expired))

    current = await policies.current(team_id=team.id, agent_id=subject.agent_id, actor_user_id=owner.id)
    assert current is not None
    assert [item["suite_revision_id"] for item in _owner_items(current)] == [saved.suite_revision_id]


@pytest.mark.anyio
async def test_platform_safety_case_is_told_apart_from_the_owners_cases(tmp_path: Path) -> None:
    # A pass on the platform safety case survives a denied data query; an Owner case's pass does not.
    from bowhead_core.agent_eval_service import _platform_contract_case

    db_path, team, owner, _other, live_subject = await _team_agent_fixture(tmp_path)
    store = AgentEvalProductStore(db_path).eval_store
    _suite, platform = await store.ensure_platform_contract_suite(team_id=team.id, agent_id=live_subject.agent_id, actor_user_id=owner.id)

    assert await _platform_contract_case(str(db_path), platform.suite_revision_id) is True
    assert await _platform_contract_case(str(db_path), "aev_missing") is False
