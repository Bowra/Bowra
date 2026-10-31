"""Model proposals preserve secrets and finish only through original model Review/Apply."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from bowhead_core.config_copilot import reset_registry_for_tests
from bowhead_core.config_copilot_chat_tools import set_proposal_writer_provider
from bowhead_core.config_draft_store import ConfigDraftStore
from bowhead_gateway.config_copilot_forms import register_gateway_forms
from bowhead_gateway.routes import config_proposals
from test_configuration_team_settings import call, child_for, propose
from test_team_llm_config_api import _apply_config, _jwt_cookie, _seed_team, _tier_v2_config, app, client, store

__all__ = ["app", "client", "store"]
pytestmark = pytest.mark.asyncio
URL = "/api/v1/admin/llm-config"


@pytest.fixture
async def models(app, client, store):
    await _seed_team(store)
    user = await store.create_user(team_id="team_llm", email="config@example.com", password="Passw0rd!")
    await store.set_team_creator("team_llm", user.id)
    cookies = {"bowhead_jwt": _jwt_cookie(user.id)}
    config = _tier_v2_config()
    config["providers"]["main"]["base_url"] = "https://93.184.216.34/v1"
    response = _apply_config(client, URL, json={"config": config, "expected_version": 0}, cookies=cookies)
    assert response.status_code == 200, response.text
    register_gateway_forms(app)
    drafts = ConfigDraftStore(store.db_path)
    await drafts.initialize()
    app.state.config_draft_store = drafts
    app.include_router(config_proposals.router)
    set_proposal_writer_provider(lambda: drafts)
    yield SimpleNamespace(app=app, user=user, store=store, cookies=cookies, config=config)
    reset_registry_for_tests()
    set_proposal_writer_provider(None)


async def test_model_read_schema_and_assignment_review_use_original_apply(models, client):
    child = await child_for(models.user, "team_model_config")
    read = await call(models.app, child, "configuration_read", form_id="team_model_config")
    assert read["ok"], read
    assert (
        models.config["providers"]["main"]["api_key"] not in str(read)
        and "api_key" not in read["schema"]["properties"]["providers"]["additionalProperties"]["properties"]
    )
    tiers = copy.deepcopy(read["fields"]["tiers"]["value"])
    tiers["fast"] = {"provider": "main", "model": "gpt-main"}
    proposal = await call(models.app, child, "configuration_propose", form_id="team_model_config", patch={"tiers": tiers})
    assert proposal["ok"] and proposal["rows"][0]["to"] == tiers, proposal
    proposal_url = f"/api/v1/config-proposals/{proposal['proposal_id']}"
    assert client.post(proposal_url + "/apply", cookies=models.cookies).status_code == 409
    review = client.get(proposal_url + "/model-review", cookies=models.cookies)
    assert review.status_code == 200, review.text
    candidate = review.json()
    body = {"config": candidate["config"], "expected_version": candidate["version"], "configuration_proposal_id": proposal["proposal_id"]}
    # The proposal is not a substitute for the original readiness review.
    assert client.put(URL, json=body, cookies=models.cookies).status_code == 409
    response = _apply_config(client, URL, json=body, cookies=models.cookies)
    assert response.status_code == 200, response.text
    saved, _ = await models.store.get_team_llm_config_with_version(models.user.team_id)
    assert saved["tiers"]["fast"] == tiers["fast"]
    assert saved["providers"]["main"]["api_key"] == models.config["providers"]["main"]["api_key"]
    assert client.get(proposal_url, cookies=models.cookies).json()["status"] == "applied"


async def test_model_apply_retains_authorized_guidance_in_replacement_response(models, client):
    before = client.get(URL, cookies=models.cookies).json()
    candidate = copy.deepcopy(before["config"])
    candidate["tiers"]["fast"] = {"provider": "main", "model": "gpt-main"}
    response = _apply_config(client, URL, json={"config": candidate, "expected_version": before["version"]}, cookies=models.cookies)
    assert response.status_code == 200, response.text
    saved = response.json()
    after = client.get(URL, cookies=models.cookies).json()
    assert set(before["field_contracts"]) == {"team_model_config", "team_model_retry"}
    assert saved["field_contracts"] == before["field_contracts"] == after["field_contracts"]
    assert saved["version"] == before["version"] + 1
    assert saved["config"]["tiers"]["fast"] == candidate["tiers"]["fast"]


@pytest.mark.parametrize(
    "field,policy",
    [
        (
            "runtime_parameters",
            {
                "profile": "dashscope-mixed-chat-v1",
                "defaults": {"thinking_mode": "off", "outbound_params": {"parallel_tool_calls": {"mode": "fixed", "value": False}}},
            },
        ),
        ("context_policy", {"enabled": False, "trigger_input_tokens": 90000, "keep_recent_tokens": 15000}),
    ],
)
async def test_typed_model_policies_preserve_false_and_nested_values(models, client, field, policy):
    child = await child_for(models.user, "team_model_config")
    read = await call(models.app, child, "configuration_read", form_id="team_model_config")
    providers = copy.deepcopy(read["fields"]["providers"]["value"])
    providers["main"]["models"][0][field] = policy
    proposal = await call(models.app, child, "configuration_propose", form_id="team_model_config", patch={"providers": providers})
    assert proposal["ok"], proposal
    assert proposal["rows"][0]["to"]["main"]["models"][0][field] == policy
    review = client.get(f"/api/v1/config-proposals/{proposal['proposal_id']}/model-review", cookies=models.cookies).json()
    response = _apply_config(
        client,
        URL,
        json={"config": review["config"], "expected_version": review["version"], "configuration_proposal_id": proposal["proposal_id"]},
        cookies=models.cookies,
    )
    assert response.status_code == 200, response.text
    saved, _ = await models.store.get_team_llm_config_with_version(models.user.team_id)
    assert saved["providers"]["main"]["models"][0][field] == policy


async def test_secret_and_changed_proposal_cannot_be_approved(models, client):
    child = await child_for(models.user, "team_model_config")
    read = await call(models.app, child, "configuration_read", form_id="team_model_config")
    providers = copy.deepcopy(read["fields"]["providers"]["value"])
    providers["main"]["api_key"] = "must-not-be-stored"
    result = await call(models.app, child, "configuration_propose", form_id="team_model_config", patch={"providers": providers})
    assert not result["ok"] and result["error"] == "invalid_patch"
    _, _, proposal = await propose(
        models.app,
        models.user,
        "team_model_config",
        patch={"tiers": {**read["fields"]["tiers"]["value"], "fast": {"provider": "main", "model": "gpt-main"}}},
    )
    review = client.get(f"/api/v1/config-proposals/{proposal['proposal_id']}/model-review", cookies=models.cookies).json()
    review["config"]["tiers"]["fast"] = {"follow": "primary"}
    response = client.put(
        URL,
        json={"config": review["config"], "expected_version": review["version"], "configuration_proposal_id": proposal["proposal_id"]},
        cookies=models.cookies,
    )
    assert response.status_code == 409 and "configuration_proposal_changed" in response.text


async def test_retry_zero_and_stale_model_proposal(models, client):
    _, _, retry = await propose(models.app, models.user, "team_model_retry", patch={"max_retries": 0})
    assert retry["ok"] and retry["rows"][0]["to"] == 0
    _, _, model = await propose(
        models.app, models.user, "team_model_config", patch={"tiers": {**models.config["tiers"], "fast": {"provider": "main", "model": "gpt-main"}}}
    )
    assert client.post(f"/api/v1/config-proposals/{retry['proposal_id']}/apply", cookies=models.cookies).status_code == 200
    assert client.get(f"/api/v1/config-proposals/{model['proposal_id']}/model-review", cookies=models.cookies).status_code == 409
    saved, _ = await models.store.get_team_llm_config_with_version(models.user.team_id)
    assert saved["retry"]["max_retries"] == 0


async def test_original_editor_defaults_do_not_invalidate_knowledge_proposal(models, client):
    _, _, proposal = await propose(models.app, models.user, "team_model_config", patch={"kb": {"retrieval_top_k": 30}})
    assert proposal["ok"], proposal
    path = f"/api/v1/config-proposals/{proposal['proposal_id']}"
    review = client.get(path + "/model-review", cookies=models.cookies).json()
    # The original browser editor fills empty collections without a manual edit.
    review["config"]["kb"] = {"models": {}, "active": {}, **review["config"]["kb"]}
    response = _apply_config(
        client,
        URL,
        json={"config": review["config"], "expected_version": review["version"], "configuration_proposal_id": proposal["proposal_id"]},
        cookies=models.cookies,
    )
    assert response.status_code == 200, response.text
    saved, _ = await models.store.get_team_llm_config_with_version(models.user.team_id)
    assert saved["kb"]["retrieval_top_k"] == 30
    assert client.get(path, cookies=models.cookies).json()["status"] == "applied"


async def test_ignored_model_proposal_cannot_be_loaded_or_bound_to_save(models, client):
    _, _, proposal = await propose(
        models.app, models.user, "team_model_config", patch={"tiers": {**models.config["tiers"], "fast": {"provider": "main", "model": "gpt-main"}}}
    )
    path = f"/api/v1/config-proposals/{proposal['proposal_id']}"
    review = client.get(path + "/model-review", cookies=models.cookies).json()
    assert client.post(path + "/ignore", cookies=models.cookies).status_code == 200
    assert client.get(path + "/model-review", cookies=models.cookies).status_code == 409
    response = client.put(
        URL,
        json={"config": review["config"], "expected_version": review["version"], "configuration_proposal_id": proposal["proposal_id"]},
        cookies=models.cookies,
    )
    assert response.status_code == 409 and "proposal_state_conflict" in response.text


async def test_model_page_contracts_do_not_expose_credentials_or_change_config(models, client):
    before, version = await models.store.get_team_llm_config_with_version(models.user.team_id)
    response = client.get(URL, cookies=models.cookies)
    assert response.status_code == 200
    contracts = response.json()["field_contracts"]
    assert set(contracts) == {"team_model_config", "team_model_retry"}
    assert before["providers"]["main"]["api_key"] not in str(contracts)
    assert "api_key" not in str(contracts)
    assert await models.store.get_team_llm_config_with_version(models.user.team_id) == (before, version)
