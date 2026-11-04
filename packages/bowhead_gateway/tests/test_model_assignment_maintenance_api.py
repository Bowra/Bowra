"""Original Review/Apply gates cover automatic assignment maintenance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import test_team_llm_config_api as config_api_tests
from bowhead_core.team_llm_readiness import build_catalog_assignments
from bowhead_core.team_llm_verification_store import CapabilityEvidence, TeamLlmVerificationStore
from test_team_llm_config_api import _apply_config, _jwt_cookie, _seed_team, _seed_user, _tier_v2_config

app = config_api_tests.app
client = config_api_tests.client
store = config_api_tests.store
pytestmark = pytest.mark.integration


async def _verify_config(store, config, *, expired: bool = False):
    verification = TeamLlmVerificationStore(store.db_path)
    ids = {}
    for key in config["providers"]:
        identity = await verification.ensure_legacy_provider_identity(team_id="team_llm", runtime_key=key)
        ids[key] = identity.provider_id
    now = datetime.now(UTC)
    for row in build_catalog_assignments(config, provider_ids=ids):
        await verification.insert_evidence(
            CapabilityEvidence(
                evidence_id=f"lve_{uuid4().hex}",
                team_id="team_llm",
                provider_id=row.provider_id,
                provider_display_name_snapshot=row.provider_display_name,
                model_id=row.model_id,
                capability=row.capability,
                config_signature=row.config_signature,
                testability_contract_version="v1",
                probe_contract_version="v1",
                probe_execution_policy_version="v1",
                outcome="verified",
                safe_code="ok",
                safe_http_status=200,
                safe_summary_code=None,
                vector_dimension=None,
                batch_id="test-batch",
                item_id=f"test-item-{row.model_id}-{row.capability}",
                initiated_by="u_maintain",
                observed_at=now - timedelta(days=2 if expired else 0),
                fresh_until=now + timedelta(days=-1 if expired else 1),
                duration_ms=1,
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_verify_later_reconciles_only_fresh_evidence_through_original_writer(client, store, monkeypatch, expired):
    monkeypatch.setenv("FEATURE_MODEL_ASSIGNMENT_RECONCILIATION", "1")
    await _seed_team(store)
    await _seed_user(store, "u_maintain", "maintain@t.com", is_creator=True)
    config = _tier_v2_config()
    config["tiers"] = {}
    config["providers"]["main"]["base_url"] = "https://93.184.216.34/v1"
    await store.update_team_llm_config("team_llm", config)
    await _verify_config(store, config, expired=expired)
    cookies = {"bowhead_jwt": _jwt_cookie("u_maintain")}
    before = await store.get_team_llm_config_with_version("team_llm")
    result = client.post("/api/v1/admin/llm-config/reconcile-assignments", cookies=cookies)
    assert result.status_code == 200, result.text
    after, version = await store.get_team_llm_config_with_version("team_llm")
    if expired:
        assert (after, version) == before
    else:
        assert after["tiers"]["primary"] == {"provider": "main", "model": "gpt-main"}
        assert "source_description" not in after["kb"]["active"]
        assert version == before[1] + 1
        assert result.json()["assignment_changes"][0]["path"] == "tiers.primary"
        assert result.json()["provider_identities"]["main"]
        assert {row["capability"] for row in result.json()["verification"]["entries"] if row["fact_state"] == "verified"} == {"chat", "tool"}
        assert "sk-team-secret" not in result.text
        replay = client.post("/api/v1/admin/llm-config/reconcile-assignments", cookies=cookies)
        assert replay.status_code == 200
        assert (await store.get_team_llm_config_with_version("team_llm"))[1] == version


@pytest.mark.asyncio
async def test_connection_removal_reviews_repairs_and_commits_them_atomically(client, store, monkeypatch):
    monkeypatch.setenv("FEATURE_MODEL_ASSIGNMENT_RECONCILIATION", "1")
    await _seed_team(store)
    await _seed_user(store, "u_maintain", "maintain@t.com", is_creator=True)
    config = _tier_v2_config()
    config["providers"]["main"]["base_url"] = "https://93.184.216.34/v1"
    await store.update_team_llm_config("team_llm", config)
    await _verify_config(store, config)
    _, version = await store.get_team_llm_config_with_version("team_llm")
    cookies = {"bowhead_jwt": _jwt_cookie("u_maintain")}
    draft = {**config, "providers": {}}
    checked = client.post("/api/v1/admin/llm-config/validate", json={"config": draft, "expected_version": version}, cookies=cookies)
    assert checked.status_code == 200, checked.text
    assert checked.json()["valid"] and checked.json()["assignment_changes"][0]["selected"] is None
    assert (await store.get_team_llm_config_with_version("team_llm"))[0] == config
    saved = _apply_config(client, "/api/v1/admin/llm-config", json={"config": draft, "expected_version": version}, cookies=cookies)
    assert saved.status_code == 200, saved.text
    after, _ = await store.get_team_llm_config_with_version("team_llm")
    assert not after["providers"] and not after["tiers"].get("primary")
    stale = client.put("/api/v1/admin/llm-config", json={"config": draft, "expected_version": version}, cookies=cookies)
    assert stale.status_code == 409


@pytest.mark.asyncio
async def test_maintenance_requires_team_administrator_and_is_default_off(client, store, monkeypatch):
    monkeypatch.delenv("FEATURE_MODEL_ASSIGNMENT_RECONCILIATION", raising=False)
    await _seed_team(store)
    await _seed_user(store, "u_maintain", "maintain@t.com", is_creator=True)
    await _seed_user(store, "u_member", "member@t.com")
    config = _tier_v2_config()
    config["tiers"] = {}
    config["providers"]["main"]["base_url"] = "https://93.184.216.34/v1"
    await store.update_team_llm_config("team_llm", config)
    await _verify_config(store, config)
    before = await store.get_team_llm_config_with_version("team_llm")
    for user, expected in [("u_maintain", 200), ("u_member", 403)]:
        result = client.post("/api/v1/admin/llm-config/reconcile-assignments", cookies={"bowhead_jwt": _jwt_cookie(user)})
        assert result.status_code == expected, result.text
    assert await store.get_team_llm_config_with_version("team_llm") == before
