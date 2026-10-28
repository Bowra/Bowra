"""Built-in Skill display copy must never change the bundle revision released Agents pin."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from bowhead_core.agent_dependency_compiler import skill_bundle_revision
from bowhead_core.skill_upload import bundle_sha256

from bowhead_skill import SkillCatalog, builtin_catalog_dir, builtin_listings, load_skill


def test_listing_metadata_lives_outside_every_builtin_bundle() -> None:
    catalog_root = builtin_catalog_dir().resolve()
    assert not Path(builtin_listings.__file__).resolve().is_relative_to(catalog_root)
    specs = SkillCatalog(roots=[catalog_root]).list_specs()
    assert len(specs) >= 50
    for spec in specs:
        assert spec.listing_descriptions, spec.name
        assert "listing" not in spec.frontmatter, spec.name


def test_changing_listing_text_keeps_the_bundle_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    builtin_root = tmp_path / "builtin"
    source = builtin_catalog_dir() / "analyst-sop-period-compare"
    installed = builtin_root / source.name
    shutil.copytree(source, installed)
    monkeypatch.setattr("bowhead_skill.catalog.builtin_catalog_dir", lambda: builtin_root)
    before = (bundle_sha256(installed), skill_bundle_revision(str(installed)))
    original = load_skill(installed).listing_descriptions

    monkeypatch.setitem(
        builtin_listings.BUILTIN_LISTING_DESCRIPTIONS,
        source.name,
        {locale: f"Rewritten {locale}" for locale in ("en-US", "zh-CN", "de-DE", "fr-FR")},
    )
    rewritten = load_skill(installed)

    assert rewritten.listing_descriptions != original
    assert rewritten.listing_descriptions["de-DE"] == "Rewritten de-DE"
    assert (bundle_sha256(installed), skill_bundle_revision(str(installed))) == before
    assert before[1] == f"sha256:{before[0]}"
