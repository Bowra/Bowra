"""Validate explicit edition image inventory boundaries."""

import runpy
from pathlib import Path

import pytest

GENERATOR = runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/check_third_party_notices.py"))


def test_community_inventory_omits_unsupported_legacy_context() -> None:
    selected = GENERATOR["_select_dockerfiles"](["bowhead-app", "bowhead-sandbox-executor"])
    assert set(selected) == {"bowhead-app", "bowhead-sandbox-executor"}
    assert selected["bowhead-app"].name == "Dockerfile.app"


@pytest.mark.parametrize("names", [[], ["bowhead-app", "bowhead-app"], ["bowhead-sandbox-executor"], ["bowhead-app", "../../private"], "bowhead-app"])
def test_image_inventory_rejects_ambiguous_or_unknown_contexts(names: object) -> None:
    with pytest.raises(ValueError, match="image_contexts"):
        GENERATOR["_select_dockerfiles"](names)
