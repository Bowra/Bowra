from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
from bowhead_cli.migrate import upgrade
from bowhead_core.migrations.baseline_schema import initial_schema
from bowhead_core.team_llm_verification_store import (
    CapabilityEvidence,
    ProviderIdentityConflict,
    TeamLlmVerificationStore,
)

ROOT_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()
NOW = datetime(2026, 7, 30, 8, tzinfo=UTC)


@pytest.fixture
def migrated_db(tmp_path):
    db_path = tmp_path / "bowhead.db"
    import sqlite3

    with sqlite3.connect(db_path) as connection:
        connection.executescript(initial_schema("core"))
    upgrade(f"sqlite+aiosqlite:///{db_path}", target="core")
    return db_path


@pytest.mark.asyncio
async def test_identity_is_stable_scoped_and_not_reused(migrated_db) -> None:
    store = TeamLlmVerificationStore(migrated_db)
    await store.assert_schema()
    first = await store.ensure_legacy_provider_identity(
        team_id="team_a",
        runtime_key="main",
        root_key=ROOT_KEY,
        now=NOW,
    )
    repeated = await store.ensure_legacy_provider_identity(
        team_id="team_a",
        runtime_key="main",
        root_key=ROOT_KEY,
        now=NOW,
    )
    other_team = await store.ensure_legacy_provider_identity(
        team_id="team_b",
        runtime_key="main",
        root_key=ROOT_KEY,
        now=NOW,
    )
    assert repeated.provider_id == first.provider_id
    assert other_team.provider_id != first.provider_id

    await store.delete_provider_identity(
        team_id="team_a",
        provider_id=first.provider_id,
        now=NOW,
    )
    replacement = await store.reserve_provider_identity(
        team_id="team_a",
        runtime_key="main",
        actor_user_id="usr_admin",
        now=NOW,
    )
    assert replacement.provider_id != first.provider_id

    repeated_reservation = await store.reserve_provider_identity(
        team_id="team_a",
        runtime_key="main",
        actor_user_id="usr_admin",
        now=NOW,
    )
    assert repeated_reservation.provider_id == replacement.provider_id

    with pytest.raises(ProviderIdentityConflict):
        await store.reserve_provider_identity(
            team_id="team_a",
            runtime_key="main",
            actor_user_id="usr_other",
            now=NOW,
        )

    expired_replacement = await store.reserve_provider_identity(
        team_id="team_a",
        runtime_key="main",
        actor_user_id="usr_other",
        now=NOW + timedelta(days=2),
    )
    assert expired_replacement.provider_id != replacement.provider_id


@pytest.mark.asyncio
async def test_evidence_projection_is_tenant_scoped_and_immutable(
    migrated_db,
) -> None:
    store = TeamLlmVerificationStore(migrated_db)
    common = {
        "provider_id": "llmp_00000000000000000000000000000000",
        "provider_display_name_snapshot": "Main",
        "model_id": "gpt-4.1",
        "capability": "tool",
        "config_signature": "sig-a",
        "testability_contract_version": "openai-tool-testability-v1",
        "probe_contract_version": "tool-call-v1",
        "probe_execution_policy_version": "probe-policy-v1",
        "outcome": "verified",
        "safe_code": "ok",
        "safe_http_status": 200,
        "safe_summary_code": None,
        "vector_dimension": None,
        "batch_id": "lvb_batch",
        "initiated_by": "usr_admin",
        "observed_at": NOW,
        "fresh_until": NOW + timedelta(days=1),
        "duration_ms": 120,
    }
    await store.insert_evidence(
        CapabilityEvidence(
            evidence_id="lve_team_a",
            team_id="team_a",
            item_id="lvi_team_a",
            **common,  # type: ignore[arg-type]
        )
    )
    await store.insert_evidence(
        CapabilityEvidence(
            evidence_id="lve_team_b",
            team_id="team_b",
            item_id="lvi_team_b",
            **common,  # type: ignore[arg-type]
        )
    )

    projection = await store.project_evidence(
        team_id="team_a",
        provider_id=str(common["provider_id"]),
        model_id="gpt-4.1",
        capability="tool",
        current_signature="sig-a",
        now=NOW,
    )
    assert projection.fact_state == "verified"
    assert projection.evidence_id == "lve_team_a"

    async with aiosqlite.connect(migrated_db) as db:
        with pytest.raises(
            aiosqlite.IntegrityError,
            match="team_llm_capability_evidence is immutable",
        ):
            await db.execute(
                """
                UPDATE team_llm_capability_evidence
                SET safe_code = 'changed'
                WHERE id = 'lve_team_a'
                """
            )
