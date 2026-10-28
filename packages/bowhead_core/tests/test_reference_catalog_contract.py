"""Cross-language discovery wire examples, including unavailable groups/cells."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bowhead_core.chat_reference_catalog import ReferenceCatalogRequest, ReferenceCatalogResponse, _catalog_response

pytestmark = pytest.mark.unit
CASES = json.loads((Path(__file__).resolve().parents[3] / "contracts/chat-references/catalog-v1.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_current_metadata_projection_matches_browser_contract(case):
    request = ReferenceCatalogRequest.model_validate(case["request"])
    response = ReferenceCatalogResponse.model_validate(case["response"])
    actual = _catalog_response(team_id="team_catalog", user_id="user_catalog", query=request, locale="zh-CN", epoch=0, groups=response.groups)
    assert actual.model_dump(mode="json") == case["response"]
