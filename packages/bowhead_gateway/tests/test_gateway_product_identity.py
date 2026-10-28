"""Edition display identity must not rewrite user content or protocol values."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bowhead_gateway.im.error_messages import im_error_labels, im_interface_message
from bowhead_gateway.product_identity import product_name

from bowhead_gateway import edition_extensions


@pytest.mark.parametrize(("edition", "name"), [("community", "Bowra"), ("business", "Bowhead")])
def test_fixed_edition_name_and_im_templates_preserve_user_content(tmp_path: Path, monkeypatch, edition: str, name: str) -> None:
    manifest = tmp_path / "build_manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "edition": edition}))
    monkeypatch.setattr(edition_extensions, "BUILD_MANIFEST_PATH", manifest)
    monkeypatch.setenv("BOWHEAD_EDITION", "business" if edition == "community" else "community")
    assert product_name() == name
    assert im_error_labels("en-US")["open_web"] == f"Open {name} Web"
    assert im_interface_message("scheduled_email_subject", "en-US", name="Bowhead customer report") == f"[{name} routine] Bowhead customer report"
