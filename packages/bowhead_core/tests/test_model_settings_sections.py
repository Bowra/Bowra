"""Connection-first management remains separate from strict production routing.

Mutation check: removing allow_unassigned_tiers or catalog expansion must fail.
"""

from __future__ import annotations

import base64
import copy

import pytest
from bowhead_core.team_llm_config import TeamLlmConfigError, build_router_from_v2_config, validate_v2_config
from bowhead_core.team_llm_readiness import build_catalog_assignments, build_required_assignments


def _config() -> dict:
    return {
        "schema_version": 2,
        "providers": {
            "corp": {
                "type": "openai_compatible",
                "base_url": "https://93.184.216.34/v1",
                "api_key": "secret",
                "models": [
                    {"id": "main", "capabilities": ["chat", "tool"]},
                    {"id": "unused", "capabilities": ["embed", "think"]},
                ],
            }
        },
        "tiers": {},
        "kb": {"models": {}, "active": {}},
    }


@pytest.mark.unit
def test_connection_can_be_saved_without_assignments_but_cannot_run() -> None:
    config = _config()
    validate_v2_config(config, allow_unassigned_tiers=True)
    with pytest.raises(TeamLlmConfigError, match="primary"):
        validate_v2_config(config)
    with pytest.raises(TeamLlmConfigError, match="primary"):
        build_router_from_v2_config(config)
    config["tiers"]["fast"] = {"provider": "corp", "model": "main"}
    with pytest.raises(TeamLlmConfigError, match="primary"):
        validate_v2_config(config, allow_unassigned_tiers=True)


@pytest.mark.unit
def test_catalog_includes_unused_models_with_the_production_transport_signature(monkeypatch) -> None:
    monkeypatch.setenv("BOWHEAD_FERNET_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode())
    config = _config()
    config["tiers"] = {"primary": {"provider": "corp", "model": "main"}}
    ids = {"corp": "llmp_corp"}
    catalog = build_catalog_assignments(config, provider_ids=ids)
    assert {(row.model_id, row.capability) for row in catalog} == {("main", "chat"), ("main", "tool"), ("unused", "embed")}
    production = build_required_assignments(config, provider_ids=ids)
    for row in production:
        matching = next(item for item in catalog if item.model_id == row.model_id and item.capability == row.capability)
        assert matching.config_signature == row.config_signature
    changed = copy.deepcopy(config)
    changed["providers"]["corp"]["base_url"] = "https://93.184.216.35/v1"
    assert [row.config_signature for row in build_catalog_assignments(changed, provider_ids=ids)] != [row.config_signature for row in catalog]
