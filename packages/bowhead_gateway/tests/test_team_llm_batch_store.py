from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.team_llm_batch_store import (
    IdempotencyConflict,
    ItemCompletion,
    TeamLlmBatchStore,
    VerificationPlanItem,
)
from bowhead_core.team_llm_control_token import (
    ControlTokenError,
    issue_control_token,
    verify_control_token,
)
from bowhead_gateway.llm_verification_runner import (
    LlmVerificationRunner,
)

from bowhead_router import ProbeResult

ROOT_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()
NOW = datetime(2026, 7, 30, 8, tzinfo=UTC)


@pytest.fixture
def migrated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "bowhead.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", ROOT_KEY)
    return db_path


def _item(
    ordinal: int,
    *,
    outcome: str = "testable",
    request_count: int = 1,
) -> VerificationPlanItem:
    return VerificationPlanItem(
        provider_id="llmp_00000000000000000000000000000000",
        provider_display_name="Main",
        model_id=f"model-{ordinal}",
        capability="chat",
        config_signature=f"sig-{ordinal}",
        testability=outcome,  # type: ignore[arg-type]
        testability_contract_version="openai-chat-testability-v1",
        probe_contract_version=("chat-response-v1" if outcome == "testable" else None),
        probe_execution_policy_version="probe-policy-v1",
        provider_request_count=request_count,
    )


async def _create_batch(
    store: TeamLlmBatchStore,
    *,
    items: list[VerificationPlanItem],
    request_hash: str = "request-a",
    idempotency_key: str = "idem-a",
):
    plan = await store.create_plan(
        team_id="team_a",
        actor_user_id="usr_admin",
        request_hash=request_hash,
        items=items,
        now=NOW,
    )
    batch = await store.create_batch(
        team_id="team_a",
        actor_user_id="usr_admin",
        plan_id=plan.plan_id,
        base_config_version=7,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        probe_context={"providers": {"main": {"api_key": "secret"}}},
        runner_id="runner-a",
        now=NOW,
    )
    return plan, batch


def test_control_token_is_scoped_expiring_and_tamper_evident() -> None:
    token = issue_control_token(
        {"team_id": "team_a", "request_hash": "request-a"},
        purpose="verification-plan",
        expires_at_epoch=100,
        root_key=ROOT_KEY,
    )
    assert (
        verify_control_token(
            token,
            purpose="verification-plan",
            now_epoch=100,
            root_key=ROOT_KEY,
        )["team_id"]
        == "team_a"
    )

    with pytest.raises(ControlTokenError, match="purpose mismatch"):
        verify_control_token(
            token,
            purpose="readiness",
            now_epoch=100,
            root_key=ROOT_KEY,
        )
    with pytest.raises(ControlTokenError, match="expired"):
        verify_control_token(
            token,
            purpose="verification-plan",
            now_epoch=101,
            root_key=ROOT_KEY,
        )
    with pytest.raises(ControlTokenError, match="signature mismatch"):
        verify_control_token(
            f"{token[:-1]}A",
            purpose="verification-plan",
            now_epoch=100,
            root_key=ROOT_KEY,
        )


@pytest.mark.asyncio
async def test_plan_consumption_and_batch_idempotency(migrated_db) -> None:
    store = TeamLlmBatchStore(migrated_db)
    plan, batch = await _create_batch(store, items=[_item(0)])
    replay = await store.create_batch(
        team_id="team_a",
        actor_user_id="usr_admin",
        plan_id=plan.plan_id,
        base_config_version=7,
        idempotency_key="idem-a",
        request_hash="request-a",
        probe_context={"providers": {}},
        runner_id="runner-b",
        now=NOW,
    )
    assert replay.batch_id == batch.batch_id
    assert replay.replayed is True

    with pytest.raises(IdempotencyConflict):
        await store.create_batch(
            team_id="team_a",
            actor_user_id="usr_admin",
            plan_id=plan.plan_id,
            base_config_version=7,
            idempotency_key="idem-a",
            request_hash="different",
            probe_context={"providers": {}},
            runner_id="runner-b",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_team_claim_limit_is_three_and_summary_is_not_delta(
    migrated_db,
) -> None:
    store = TeamLlmBatchStore(migrated_db)
    _, batch = await _create_batch(
        store,
        items=[_item(index) for index in range(4)],
    )
    claims = [
        await store.claim_next_item(
            team_id="team_a",
            batch_id=batch.batch_id,
            runner_id="runner-a",
            now=NOW,
        )
        for _ in range(4)
    ]
    assert all(claims[index] is not None for index in range(3))
    assert claims[3] is None

    snapshot = await store.get_batch(
        team_id="team_a",
        batch_id=batch.batch_id,
        after_revision=2,
    )
    assert snapshot is not None
    assert snapshot["counts"] == {"queued": 1, "verifying": 3}
    assert len(snapshot["items"]) == 1


@pytest.mark.asyncio
async def test_expired_process_leases_become_interrupted_without_resume(
    migrated_db,
) -> None:
    store = TeamLlmBatchStore(migrated_db)
    _, batch = await _create_batch(
        store,
        items=[_item(0), _item(1)],
    )
    claimed = await store.claim_next_item(
        team_id="team_a",
        batch_id=batch.batch_id,
        runner_id="runner-a",
        now=NOW,
    )
    assert claimed is not None

    team_count = await store.interrupt_expired_runners(
        now=NOW + timedelta(seconds=46),
    )
    snapshot = await store.get_batch(
        team_id="team_a",
        batch_id=batch.batch_id,
    )

    assert team_count == 1
    assert snapshot is not None
    assert snapshot["state"] == "interrupted"
    assert snapshot["counts"] == {"interrupted": 2}


@pytest.mark.asyncio
async def test_only_factual_completion_appends_evidence_and_clears_context(
    migrated_db,
) -> None:
    store = TeamLlmBatchStore(migrated_db)
    _, batch = await _create_batch(
        store,
        items=[_item(0), _item(1)],
    )
    first = await store.claim_next_item(
        team_id="team_a",
        batch_id=batch.batch_id,
        runner_id="runner-a",
        now=NOW,
    )
    assert first is not None
    await store.complete_item(
        item=first,
        completion=ItemCompletion(
            state="errored",
            safe_code="provider_timeout",
            safe_http_status=None,
            safe_summary_code=None,
            duration_ms=8000,
        ),
        initiated_by="usr_admin",
        provider_display_name="Main",
        now=NOW + timedelta(seconds=1),
    )
    second = await store.claim_next_item(
        team_id="team_a",
        batch_id=batch.batch_id,
        runner_id="runner-a",
        now=NOW + timedelta(seconds=1),
    )
    assert second is not None
    await store.complete_item(
        item=second,
        completion=ItemCompletion(
            state="verified",
            safe_code="ok",
            safe_http_status=200,
            safe_summary_code=None,
            duration_ms=120,
        ),
        initiated_by="usr_admin",
        provider_display_name="Main",
        now=NOW + timedelta(seconds=2),
    )

    async with aiosqlite.connect(migrated_db) as db:
        evidence_count = await (await db.execute("SELECT COUNT(*) FROM team_llm_capability_evidence")).fetchone()
        ciphertext = await (
            await db.execute(
                """
                SELECT probe_context_ciphertext
                FROM team_llm_verification_batches
                WHERE id = ?
                """,
                (batch.batch_id,),
            )
        ).fetchone()
    assert evidence_count == (1,)
    assert ciphertext == (None,)


@pytest.mark.asyncio
async def test_retention_keeps_current_apply_evidence_and_bounds_history(
    migrated_db,
) -> None:
    store = TeamLlmBatchStore(migrated_db)
    _, batch = await _create_batch(
        store,
        items=[_item(0), _item(1)],
    )
    for offset in range(2):
        claimed = await store.claim_next_item(
            team_id="team_a",
            batch_id=batch.batch_id,
            runner_id="runner-a",
            now=NOW + timedelta(seconds=offset),
        )
        assert claimed is not None
        await store.complete_item(
            item=claimed,
            completion=ItemCompletion(
                state="verified",
                safe_code="ok",
                safe_http_status=200,
                safe_summary_code=None,
                duration_ms=25,
            ),
            initiated_by="usr_admin",
            provider_display_name="Main",
            now=NOW + timedelta(seconds=offset + 1),
        )

    async with aiosqlite.connect(migrated_db) as db:
        evidence = await (
            await db.execute(
                """
                SELECT id FROM team_llm_capability_evidence
                ORDER BY observed_at, id
                """
            )
        ).fetchall()
        retained_id = str(evidence[0][0])
        await db.execute(
            """
            INSERT INTO team_llm_apply_snapshots(
                id, team_id, config_version, previous_config_version,
                actor_user_id, draft_semantic_hash,
                readiness_contract_version, decision_json, applied_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                "llmas_current",
                "team_a",
                8,
                7,
                "usr_admin",
                "a" * 64,
                "team-llm-readiness-v1",
                json.dumps({"rows": [{"evidence_id": retained_id}]}),
                NOW.isoformat(),
            ),
        )
        await db.commit()

    result = await store.sweep_retention(
        now=NOW + timedelta(days=91),
    )
    async with aiosqlite.connect(migrated_db) as db:
        remaining = await (await db.execute("SELECT id FROM team_llm_capability_evidence")).fetchall()

    assert result.evidence == 1
    assert result.items == 2
    assert result.batches == 1
    assert result.plans == 1
    assert remaining == [(retained_id,)]


@pytest.mark.asyncio
async def test_cancelled_attempt_never_becomes_evidence(migrated_db) -> None:
    store = TeamLlmBatchStore(migrated_db)
    _, batch = await _create_batch(store, items=[_item(0), _item(1)])
    claimed = await store.claim_next_item(
        team_id="team_a",
        batch_id=batch.batch_id,
        runner_id="runner-a",
        now=NOW,
    )
    assert claimed is not None
    cancelling = await store.cancel_batch(
        team_id="team_a",
        batch_id=batch.batch_id,
        now=NOW + timedelta(seconds=1),
    )
    assert cancelling.state == "cancelling"
    final_state = await store.complete_item(
        item=claimed,
        completion=ItemCompletion(
            state="verified",
            safe_code="ok",
            safe_http_status=200,
            safe_summary_code=None,
            duration_ms=100,
        ),
        initiated_by="usr_admin",
        provider_display_name="Main",
        now=NOW + timedelta(seconds=2),
    )
    assert final_state == "cancelled"

    snapshot = await store.get_batch(
        team_id="team_a",
        batch_id=batch.batch_id,
    )
    assert snapshot is not None
    assert snapshot["state"] == "cancelled"
    assert snapshot["counts"] == {"cancelled": 2}
    async with aiosqlite.connect(migrated_db) as db:
        evidence_count = await (await db.execute("SELECT COUNT(*) FROM team_llm_capability_evidence")).fetchone()
    assert evidence_count == (0,)


@pytest.mark.asyncio
async def test_runner_executes_testable_and_persists_not_testable(
    migrated_db,
    monkeypatch,
) -> None:
    store = TeamLlmBatchStore(migrated_db)
    _, batch = await _create_batch(
        store,
        items=[
            _item(0),
            VerificationPlanItem(
                provider_id="llmp_00000000000000000000000000000000",
                provider_display_name="Main",
                model_id="rerank-1",
                capability="rerank",
                config_signature="sig-rerank",
                testability="not_testable",
                testability_contract_version=("openai-rerank-testability-v1"),
                probe_contract_version=None,
                probe_execution_policy_version="no-provider-request-v1",
                provider_request_count=0,
            ),
        ],
    )
    async with aiosqlite.connect(migrated_db) as db:
        from bowhead_core.llm_config_crypto import encrypt_llm_config

        context = encrypt_llm_config(
            {
                "providers": {
                    "llmp_00000000000000000000000000000000": {
                        "display_name": "Main",
                        "config": {
                            "type": "openai_compatible",
                            "base_url": "https://api.openai.com/v1",
                            "api_key": "secret",
                            "timeout_sec": 5,
                        },
                    }
                }
            }
        )
        await db.execute(
            """
            UPDATE team_llm_verification_batches
            SET probe_context_ciphertext = ?
            WHERE id = ?
            """,
            (context, batch.batch_id),
        )
        await db.execute(
            """
            UPDATE team_llm_verification_items
            SET lease_until = ?
            WHERE batch_id = ?
            """,
            (
                (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
                batch.batch_id,
            ),
        )
        await db.commit()

    calls = 0

    async def fake_execute(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return ProbeResult(
            classification="factual_evidence",
            outcome="verified",
            code="ok",
            safe_http_status=200,
            safe_summary_code=None,
            duration_ms=25,
        )

    monkeypatch.setattr(
        "bowhead_gateway.llm_verification_runner.CompatibleProbeRegistry.execute",
        fake_execute,
    )
    runner = LlmVerificationRunner(str(migrated_db))
    runner.start(
        batch_id=batch.batch_id,
        team_id="team_a",
        runner_id="runner-a",
        initiated_by="usr_admin",
    )
    async with asyncio.timeout(3):
        while batch.batch_id in runner.tasks:
            await asyncio.sleep(0.01)

    snapshot = await store.get_batch(
        team_id="team_a",
        batch_id=batch.batch_id,
    )
    assert calls == 1
    assert snapshot is not None
    assert snapshot["state"] == "completed"
    assert snapshot["counts"] == {
        "not_testable": 1,
        "verified": 1,
    }
