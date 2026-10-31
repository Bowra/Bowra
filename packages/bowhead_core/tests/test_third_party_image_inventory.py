"""Validate explicit edition image inventory boundaries."""

import json
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


def test_pinned_system_package_keeps_version_and_registered_license(tmp_path, monkeypatch) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("RUN apt-get install -y texlive-xetex=2024.20250309-1\n")
    namespace = GENERATOR["_collect_images"].__globals__
    # The registry is real; only the image input is an isolated package fixture.
    registry = json.loads(namespace["COMPONENTS_FILE"].read_text())
    registry["vendored"] = []
    registry_path = tmp_path / "components.json"
    registry_path.write_text(json.dumps(registry))
    monkeypatch.setitem(namespace, "COMPONENTS_FILE", registry_path)
    monkeypatch.setitem(namespace, "ROOT", tmp_path)
    monkeypatch.setitem(namespace, "_inventory_dockerfiles", lambda: {"bowhead-sandbox": dockerfile})
    components = GENERATOR["_collect_images"]()
    assert [(item.name, item.version, item.license) for item in components if item.name == "texlive-xetex"] == [
        ("texlive-xetex", "2024.20250309-1", "LicenseRef-TeX-Live-Collection")
    ]
