"""A connection's 验证 can cover knowledge-base models that its catalog does not declare.

Design Q3 (docs/sprints/model-settings-save-flow-2026-11-25/connection-purposes-design.md): the editor plans
verification from the saved connection draft, whose models are only the catalog. A knowledge-base model outside the
catalog must plan under the same configuration signature readiness reads for its role, or a pass would never show.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest
import test_team_llm_config_api as config_api_tests
from bowhead_core.team_llm_readiness import build_required_assignments
from bowhead_core.team_llm_verification_store import TeamLlmVerificationStore
from test_team_llm_config_api import _jwt_cookie, _seed_team, _seed_user

app = config_api_tests.app
client = config_api_tests.client
store = config_api_tests.store
pytestmark = pytest.mark.integration


def _config() -> dict:
    return {
        "schema_version": 2,
        "providers": {
            "qwen": {
                "type": "openai_compatible",
                "base_url": "https://93.184.216.34/v1",
                "api_key": "sk-qwen-secret-1234567890",
                "models": [{"id": "qwen-chat", "capabilities": ["chat", "tool"], "source": "manual"}],
            },
        },
        "tiers": {"primary": {"provider": "qwen", "model": "qwen-chat"}},
        "kb": {
            "models": {
                "embed-v4": {"capability": "embed", "dim": 1024, "provider": "qwen", "real_id": "text-embedding-v4"},
                "ocr": {"capability": "vision", "provider": "qwen", "real_id": "qwen-vl-ocr-latest"},
            },
            "active": {"embed": "embed-v4", "vision": "ocr"},
        },
        "media": {"models": {}, "active": {}},
        "search": {"models": {}, "active": {}},
    }


@pytest.mark.asyncio
async def test_knowledge_base_models_outside_the_catalog_plan_under_their_readiness_signature(client, store):
    await _seed_team(store)
    await _seed_user(store, "u_owner", "owner@t.com", is_creator=True)
    config = _config()
    await store.update_team_llm_config("team_llm", config)
    identity = await TeamLlmVerificationStore(store.db_path).ensure_legacy_provider_identity(team_id="team_llm", runtime_key="qwen")
    provider_id = identity.provider_id
    _saved, version = await store.get_team_llm_config_with_version("team_llm")

    readiness = {
        (row.role_key, row.model_id, row.capability): row.config_signature
        for row in build_required_assignments(config, provider_ids={"qwen": provider_id})
    }

    # The draft the connection list builds from the saved config: catalog models only, the key left on the server.
    draft = {
        "provider_id": provider_id,
        "runtime_key": "qwen",
        "display_name": "qwen",
        "type": "openai_compatible",
        "base_url": "https://93.184.216.34/v1",
        "models": [{"id": "qwen-chat", "capabilities": ["chat", "tool"], "declaration_origin": "manual"}],
    }
    planned = client.post(
        "/api/v1/admin/llm-config/verification-plans",
        json={
            "base_config_version": version,
            "provider_drafts": [draft],
            "items": [
                {"provider_id": provider_id, "model_id": "text-embedding-v4", "capability": "embed"},
                {"provider_id": provider_id, "model_id": "qwen-vl-ocr-latest", "capability": "vision"},
            ],
        },
        cookies={"bowhead_jwt": _jwt_cookie("u_owner")},
    )
    assert planned.status_code == 200, planned.text
    assert [item["testability"] for item in planned.json()["items"]] == ["testable", "testable"]

    async with (
        aiosqlite.connect(store.db_path) as db,
        db.execute(
            "SELECT items_json FROM team_llm_verification_plans WHERE id = ?",
            (planned.json()["plan_id"],),
        ) as cursor,
    ):
        items = json.loads((await cursor.fetchone())[0])
    signatures = {(item["model_id"], item["capability"]): item["config_signature"] for item in items}

    assert signatures[("text-embedding-v4", "embed")] == readiness[("kb.embed", "text-embedding-v4", "embed")]
    assert signatures[("qwen-vl-ocr-latest", "vision")] == readiness[("kb.vision", "qwen-vl-ocr-latest", "vision")]
