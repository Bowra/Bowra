"""Assignment maintenance preserves explicit intent and production boundaries."""

from __future__ import annotations

import copy
import json

import pytest
from bowhead_core.team_llm_assignments import reconcile_model_assignments
from bowhead_core.team_llm_config import TeamLlmConfigError, validate_v2_config

pytestmark = pytest.mark.unit


def config(models: list[dict] | None = None) -> dict:
    return {
        "schema_version": 2,
        "providers": {
            "corp": {
                "type": "openai_compatible",
                "base_url": "https://api.example.com/v1",
                "models": models or [{"id": "main", "capabilities": ["chat", "tool", "vision"]}],
            }
        },
        "tiers": {},
        "kb": {"models": {}, "active": {}},
    }


def facts(*caps: str, provider: str = "corp", model: str = "main") -> set[tuple[str, str, str]]:
    return {(provider, model, cap) for cap in caps}


def test_new_connection_requires_every_verified_capability_and_never_enables_source_descriptions():
    draft = config()
    unchanged = copy.deepcopy(draft)
    result, _ = reconcile_model_assignments({}, draft, facts("chat"))
    assert "primary" not in result["tiers"]
    result, changes = reconcile_model_assignments({}, draft, facts("chat", "tool", "vision"))
    assert result["tiers"]["primary"] == {"provider": "corp", "model": "main"}
    assert set(result["kb"]["active"]) == {"vision", "row_extractor"}
    assert "source_description" not in result["kb"]["active"]
    assert changes[0]["path"] == "tiers.primary"
    assert draft == unchanged


def test_existing_choices_and_follow_auto_are_preserved():
    current = config()
    current["tiers"] = {"primary": {"provider": "corp", "model": "main"}, "fast": {"follow": "primary"}, "judge": {"auto": True}}
    draft = copy.deepcopy(current)
    draft["providers"]["another"] = {**draft["providers"]["corp"], "models": [{"id": "other", "capabilities": ["chat", "tool"]}]}
    result, _ = reconcile_model_assignments(current, draft, facts("chat", "tool", provider="another", model="other"))
    assert result["tiers"] == current["tiers"]


@pytest.mark.parametrize("remove_provider", [True, False])
def test_removal_repairs_primary_and_cleans_knowledge_models(remove_provider):
    current = config()
    current["tiers"] = {"primary": {"provider": "corp", "model": "main"}, "primary_fallback": []}
    current["kb"] = {"models": {"v": {"provider": "corp", "real_id": "main", "capability": "vision"}}, "active": {"vision": "v"}}
    draft = copy.deepcopy(current)
    draft["providers"]["replacement"] = {**draft["providers"]["corp"], "models": [{"id": "new", "capabilities": ["chat", "tool", "vision"]}]}
    if remove_provider:
        draft["providers"].pop("corp")
    else:
        draft["providers"]["corp"]["models"][0]["capabilities"] = ["chat"]
    result, _ = reconcile_model_assignments(current, draft, facts("chat", "tool", "vision", provider="replacement", model="new"))
    assert result["tiers"]["primary"] == {"provider": "replacement", "model": "new"}
    assert "v" not in result["kb"]["models"]
    assert result["kb"]["models"][result["kb"]["active"]["vision"]]["real_id"] == "new"


def test_last_connection_can_be_removed_without_dangling_external_or_embedding_references():
    current = config()
    current["tiers"] = {
        "primary": {"provider": "corp", "model": "main"},
        "fast": {"provider": "corp", "model": "main"},
        "judge": {"provider": "corp", "model": "main"},
    }
    current["kb"] = {
        "models": {
            "emb": {"provider": "corp", "real_id": "vector", "capability": "embed", "dim": 8, "api_key": "must-not-enter-assignment-receipts"}
        },
        "active": {"embed": "emb"},
    }
    current["media"] = {"models": {"image": {"provider": "corp", "real_id": "image", "capability": "image_gen"}}, "active": {"image_gen": "image"}}
    draft = copy.deepcopy(current)
    draft["providers"] = {}
    result, changes = reconcile_model_assignments(current, draft, set())
    assert "must-not-enter-assignment-receipts" not in json.dumps(changes)
    assert "primary" not in result["tiers"]
    assert result["tiers"]["fast"] == {"follow": "primary"}
    assert not result["kb"]["models"] and not result["kb"]["active"]
    assert not result["media"]["models"] and not result["media"]["active"]
    validate_v2_config(result, allow_unassigned_tiers=True)
    with pytest.raises(TeamLlmConfigError):
        validate_v2_config(result)


def test_optional_off_survives_later_verification_and_embedding_never_auto_selects():
    current = config()
    current["kb"]["active"] = {"vision": "v"}
    current["kb"]["models"] = {"v": {"provider": "corp", "real_id": "main", "capability": "vision"}}
    draft = copy.deepcopy(current)
    draft["kb"]["active"] = {}
    disabled, _ = reconcile_model_assignments(current, draft, facts("chat", "tool", "vision"))
    assert disabled["kb"]["auto_assign_disabled"] == ["vision"]
    after, _ = reconcile_model_assignments(disabled, disabled, facts("chat", "tool", "vision", "embed"), verification_completed=True)
    assert "vision" not in after["kb"]["active"]
    assert "embed" not in after["kb"]["active"]
    assert "primary" in after["tiers"]


def test_verification_later_fills_saved_slots_without_a_provider_edit():
    current = config()
    result, changes = reconcile_model_assignments(current, current, facts("chat", "tool"))
    assert not changes and not result["tiers"]
    result, changes = reconcile_model_assignments(current, current, facts("chat", "tool"), verification_completed=True)
    assert result["tiers"]["primary"]["model"] == "main" and changes


@pytest.mark.parametrize("verified_embed", [True, False])
def test_deleted_connection_replaces_only_affected_settings(verified_embed):
    current = config([{"id": "main", "capabilities": ["chat", "tool", "embed"]}])
    current["providers"]["other"] = copy.deepcopy(current["providers"]["corp"])
    current["tiers"] = {"primary": {"provider": "corp", "model": "main"}, "judge": {"auto": True}}
    current["kb"] = {
        "models": {
            "old": {"provider": "corp", "real_id": "main", "capability": "embed", "dim": 8},
            "rerank": {"provider": "corp", "real_id": "ranker", "capability": "rerank"},
            "other": {"provider": "other", "real_id": "main", "capability": "embed", "dim": 16},
        },
        "active": {"embed": "old", "rerank": "rerank"},
        "auto_assign_disabled": ["vision"],
    }
    for section, cap in (("media", "image_gen"), ("search", "web_search")):
        current[section] = {
            "models": {name: {"provider": provider, "real_id": cap, "capability": cap} for name, provider in (("old", "corp"), ("other", "other"))},
            "active": {cap: "old"},
        }
    draft = copy.deepcopy(current)
    draft["providers"].pop("corp")
    verified = facts("chat", "tool", provider="other")
    if verified_embed:
        verified |= facts("embed", provider="other")
    result, changes = reconcile_model_assignments(current, draft, verified, deletion_only=True)
    reviewed, review_changes = reconcile_model_assignments(current, result, verified, deletion_only=True)
    assert reviewed == result
    assert {change["path"]: change["selected"] for change in review_changes} == {change["path"]: change["selected"] for change in changes}
    assert result["tiers"] == {"primary": {"provider": "other", "model": "main"}, "judge": {"auto": True}}
    assert result["kb"]["active"] == ({"embed": "other"} if verified_embed else {})
    assert result["kb"]["auto_assign_disabled"] == ["vision"]
    assert result["media"]["active"] == {"image_gen": "other"}
    assert result["search"]["active"] == {"web_search": "other"}
    assert draft["kb"]["active"] == {"embed": "old", "rerank": "rerank"}
