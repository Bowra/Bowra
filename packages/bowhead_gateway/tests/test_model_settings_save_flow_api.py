"""Connection saves ask only about the edited connection, and the editor can read results for a draft.

Design: docs/sprints/model-settings-save-flow-2026-11-25/design.md §2.2–§2.3.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import aiosqlite
import pytest
import test_team_llm_config_api as config_api_tests
from bowhead_core.team_llm_readiness import build_catalog_assignments
from bowhead_core.team_llm_verification_store import CapabilityEvidence, TeamLlmVerificationStore
from test_team_llm_config_api import _jwt_cookie, _seed_team, _seed_user

app = config_api_tests.app
client = config_api_tests.client
store = config_api_tests.store
pytestmark = pytest.mark.integration

URL = "/api/v1/admin/llm-config"


def _two_connections() -> dict:
    return {
        "schema_version": 2,
        "providers": {
            "main": {
                "type": "openai_compatible",
                "base_url": "https://93.184.216.34/v1",
                "api_key": "sk-main-secret-1234567890",
                "models": [{"id": "gpt-main", "capabilities": ["chat", "tool"]}],
            },
            "backup": {
                "type": "openai_compatible",
                "base_url": "https://93.184.216.35/v1",
                "api_key": "sk-backup-secret-1234567890",
                "models": [{"id": "gpt-backup", "capabilities": ["chat", "tool"]}],
            },
        },
        "tiers": {
            "primary": {"provider": "main", "model": "gpt-main"},
            "primary_fallback": [{"provider": "backup", "model": "gpt-backup"}],
            "fast": {"follow": "primary"},
            "judge": {"provider": "main", "model": "gpt-main"},
        },
        "kb": {"models": {}, "active": {}},
        "media": {"models": {}, "active": {}},
        "search": {"models": {}, "active": {}},
    }


async def _record(store, config, outcome_by_provider: dict[str, str], *, observed_days_ago: int = 0) -> dict[str, str]:
    verification = TeamLlmVerificationStore(store.db_path)
    ids = {}
    for key in config["providers"]:
        identity = await verification.ensure_legacy_provider_identity(team_id="team_llm", runtime_key=key)
        ids[key] = identity.provider_id
    by_id = {value: key for key, value in ids.items()}
    now = datetime.now(UTC)
    for row in build_catalog_assignments(config, provider_ids=ids):
        outcome = outcome_by_provider[by_id[row.provider_id]]
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
                outcome=outcome,
                safe_code="ok" if outcome == "verified" else "tool_call_missing",
                safe_http_status=200,
                safe_summary_code=None,
                vector_dimension=None,
                batch_id="test-batch",
                item_id=f"test-item-{row.provider_id}-{row.model_id}-{row.capability}",
                initiated_by="u_owner",
                observed_at=now - timedelta(days=observed_days_ago),
                fresh_until=now - timedelta(days=observed_days_ago) + timedelta(days=1),
                duration_ms=1,
            )
        )
    return ids


async def _count(store, table: str) -> int:
    async with aiosqlite.connect(store.db_path) as db, db.execute(f"SELECT COUNT(*) FROM {table}") as cursor:  # noqa: S608
        return int((await cursor.fetchone())[0])


async def _seed(store) -> tuple[dict, dict[str, str], dict[str, str]]:
    await _seed_team(store)
    await _seed_user(store, "u_owner", "owner@t.com", is_creator=True)
    config = _two_connections()
    await store.update_team_llm_config("team_llm", config)
    ids = await _record(store, config, {"main": "verified", "backup": "failed"})
    return config, ids, {"bowhead_jwt": _jwt_cookie("u_owner")}


def _apply(client, draft, checked, cookies):
    readiness = checked["readiness"]
    migration = readiness.get("migration_acceptance")
    return client.put(
        URL,
        json={
            "config": draft,
            "expected_version": checked["version"],
            "readiness": {
                "draft_semantic_hash": readiness["draft_semantic_hash"],
                "acceptance_tokens": [],
                **({"migration_acceptance_token": migration["acceptance_token"]} if migration else {}),
            },
        },
        cookies=cookies,
    )


@pytest.mark.asyncio
async def test_editing_one_connection_saves_without_signing_for_another(client, store):
    config, ids, cookies = await _seed(store)
    draft = copy.deepcopy(config)
    draft["providers"]["main"]["display_name"] = "Main service"

    checked = client.post(f"{URL}/validate", json={"config": draft}, cookies=cookies)
    assert checked.status_code == 200, checked.text
    readiness = checked.json()["readiness"]
    assert readiness["required_acceptances"] == []
    assert readiness["may_apply"] is True
    backup_rows = [row for row in readiness["rows"] if row["provider_id"] == ids["backup"]]
    assert backup_rows and {row["status"] for row in backup_rows} == {"warning"}

    # Apply recomputes the same decision, so the reviewed hash still matches.
    saved = _apply(client, draft, checked.json(), cookies)
    assert saved.status_code == 200, saved.text
    stored, _version = await store.get_team_llm_config_with_version("team_llm")
    assert stored["providers"]["main"]["display_name"] == "Main service"


@pytest.mark.asyncio
async def test_editing_the_connection_with_a_failed_result_still_asks(client, store):
    config, ids, cookies = await _seed(store)
    draft = copy.deepcopy(config)
    draft["providers"]["backup"]["display_name"] = "Backup service"

    checked = client.post(f"{URL}/validate", json={"config": draft}, cookies=cookies)
    assert checked.status_code == 200, checked.text
    readiness = checked.json()["readiness"]
    assert readiness["may_apply"] is False
    assert {row["provider_id"] for row in readiness["required_acceptances"]} == {ids["backup"]}

    refused = _apply(client, draft, checked.json(), cookies)
    assert refused.status_code == 409, refused.text


@pytest.mark.asyncio
async def test_draft_evidence_shows_which_results_an_edit_keeps(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_owner", "owner@t.com", is_creator=True)
    config = _two_connections()
    await store.update_team_llm_config("team_llm", config)
    ids = await _record(store, config, {"main": "verified", "backup": "verified"}, observed_days_ago=30)
    cookies = {"bowhead_jwt": _jwt_cookie("u_owner")}
    main = {key: value for key, value in config["providers"]["main"].items() if key != "api_key"}

    def project(provider):
        response = client.post(f"{URL}/providers/draft-evidence", json={"provider_draft": provider}, cookies=cookies)
        assert response.status_code == 200, response.text
        return response.json(), {(item["model_id"], item["capability"]): item["fact_state"] for item in response.json()["items"]}

    with_vision = copy.deepcopy(main) | {"runtime_key": "main"}
    with_vision["models"][0]["capabilities"].append("vision")
    body, states = project(with_vision)
    assert body["provider_id"] == ids["main"] and body["draft_invalid"] is False
    # A pass recorded 30 days ago still applies; only the new capability has no result.
    assert states == {("gpt-main", "chat"): "verified", ("gpt-main", "tool"): "verified", ("gpt-main", "vision"): "unknown"}
    assert all(item["observed_at"] for item in body["items"] if item["fact_state"] == "verified")

    moved = copy.deepcopy(main) | {"runtime_key": "main", "base_url": "https://93.184.216.36/v1"}
    _body, states = project(moved)
    assert set(states.values()) == {"stale"}

    unsaved = copy.deepcopy(main) | {"runtime_key": "brand_new"}
    body, states = project(unsaved)
    assert body["provider_id"] is None and set(states.values()) == {"unknown"}

    # Reading results writes nothing: no evidence, plan or identity for the unsaved draft.
    assert await _count(store, "team_llm_capability_evidence") == 4
    assert await _count(store, "team_llm_verification_plans") == 0
    assert await _count(store, "team_llm_provider_identities") == 2


@pytest.mark.asyncio
async def test_draft_evidence_is_for_team_administrators(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_owner", "owner@t.com", is_creator=True)
    await _seed_user(store, "u_member", "member@t.com")
    config = _two_connections()
    await store.update_team_llm_config("team_llm", config)
    draft = {key: value for key, value in config["providers"]["main"].items() if key != "api_key"} | {"runtime_key": "main"}

    response = client.post(
        f"{URL}/providers/draft-evidence",
        json={"provider_draft": draft},
        cookies={"bowhead_jwt": _jwt_cookie("u_member")},
    )
    assert response.status_code == 403
