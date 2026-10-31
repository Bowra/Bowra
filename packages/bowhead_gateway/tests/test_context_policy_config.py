"""Original model DTO and safe projections retain explicit compaction choices."""

from __future__ import annotations

import copy

import pytest
from bowhead_core.team_llm_config import TeamLlmConfigError, validate_provider_draft
from bowhead_gateway.configuration_team_models import TeamModelSettings, merge_config, project_config
from bowhead_gateway.routes.admin_llm_config_v2 import V2ModelDeclaration, V2ProviderDraft
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def provider(policy: dict | None = None) -> dict:
    model = {"id": "deployment", "capabilities": ["chat"], "context_window": 128_000}
    if policy is not None:
        model["context_policy"] = policy
    return {"type": "openai_compatible", "base_url": "https://example.com/v1", "api_key": "private", "models": [model]}


def test_dto_and_configuration_projection_keep_off_and_unrelated_settings():
    policy = {"enabled": False, "trigger_input_tokens": 90_000, "keep_recent_tokens": 15_000}
    original = {"providers": {"main": provider(policy)}}
    draft = V2ProviderDraft(runtime_key="main", **original["providers"]["main"]).provider_config()
    assert draft["models"][0]["context_policy"] == policy
    safe = TeamModelSettings.model_validate(project_config(original)).model_dump(mode="json", exclude_none=True)
    restored = merge_config(original, safe)
    assert restored["providers"]["main"]["models"][0]["context_policy"] == policy
    assert restored["providers"]["main"]["models"][0]["context_window"] == 128_000
    assert restored["providers"]["main"]["api_key"] == "private"


def test_explicit_policy_reset_preserves_model_capacity_and_secret():
    original = {"providers": {"main": provider({"enabled": False, "keep_recent_tokens": 12_000})}}
    safe = project_config(original)
    safe["providers"]["main"]["models"][0].pop("context_policy")
    restored = merge_config(original, safe)
    assert "context_policy" not in restored["providers"]["main"]["models"][0]
    assert restored["providers"]["main"]["models"][0]["context_window"] == 128_000
    assert restored["providers"]["main"]["api_key"] == "private"
    assert original["providers"]["main"]["models"][0]["context_policy"]["enabled"] is False


@pytest.mark.parametrize(
    "policy",
    [
        {"enabled": 0},
        {"enabled": None},
        {"trigger_input_tokens": True},
        {"trigger_input_tokens": "1000"},
        {"keep_recent_tokens": 1.5},
        {"trigger_input_tokens": 1000, "keep_recent_tokens": 1000},
        {"trigger_input_tokens": 9_007_199_254_740_992},
        {"enabled": False, "unknown": 1},
    ],
)
def test_original_writers_and_dto_reject_invalid_policy(policy):
    with pytest.raises(ValidationError):
        V2ModelDeclaration(id="deployment", capabilities=["chat"], context_policy=policy)
    with pytest.raises(TeamLlmConfigError, match="context_policy"):
        validate_provider_draft("main", copy.deepcopy(provider(policy)))


def test_unset_policy_is_not_materialized_by_unrelated_dto_save():
    draft = V2ProviderDraft(runtime_key="main", **provider()).provider_config()
    assert "context_policy" not in draft["models"][0]
