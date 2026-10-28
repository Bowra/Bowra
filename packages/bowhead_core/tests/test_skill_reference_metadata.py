"""Exact builtin metadata is built from installed packages, not request-time names."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import yaml
from bowhead_core.skill_reference_metadata import build_builtin_skill_metadata
from bowhead_core.skill_upload import bundle_sha256
from bowhead_skill.loader import load_skill

from bowhead_core import skill_reference_metadata as metadata

pytestmark = pytest.mark.unit


def _skill(root: Path, name="dupont", *, directory=None, readonly=True):
    path = root / (directory or name)
    path.mkdir(parents=True)
    declaration = {
        "schema_version": 1,
        "entry_file": "SKILL.md",
        "required_files": [],
        "parameter_schema": {"type": "object", "properties": {"company": {"type": "string", "default": "Example"}}, "additionalProperties": False},
        "required_skill_refs": [],
        "requires_script": False,
        "requires_new_tools": False,
        "requires_new_connections": False,
    }
    data = {"name": name, "description": "Profitability method", "version": "1.2.3"}
    if readonly:
        data["read_only_entry"] = declaration
    (path / "SKILL.md").write_text("---\n" + yaml.safe_dump(data) + "---\nPRIVATE_BODY_NOT_IN_METADATA\n")
    (path / "scripts").mkdir()
    (path / "scripts" / "unused.py").write_text("raise RuntimeError('MUST_NOT_EXECUTE')")
    return path


def test_whole_package_hash_and_schema_are_detached_without_body_paths_or_declared_version(tmp_path):
    path = _skill(tmp_path)
    index = build_builtin_skill_metadata(builtin_root=tmp_path)
    (item,) = index.items
    assert not index.incomplete
    assert item.id == "builtin:dupont"
    assert item.revision == "sha256:" + bundle_sha256(path)
    assert item.revision != "1.2.3"
    assert item.parameter_schema() == load_skill(path).read_only_entry.parameter_schema
    detached = item.parameter_schema()
    detached["properties"]["company"]["default"] = "changed"
    assert item.parameter_schema()["properties"]["company"]["default"] == "Example"
    wire = repr(dataclasses.asdict(index))
    assert "PRIVATE_BODY_NOT_IN_METADATA" not in wire and str(tmp_path) not in wire and "MUST_NOT_EXECUTE" not in wire
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.name = "changed"
    (path / "scripts/unused.py").write_text("raise RuntimeError('DIFFERENT_UNUSED_SCRIPT')")
    rebuilt = build_builtin_skill_metadata(builtin_root=tmp_path)
    assert rebuilt.generation != index.generation
    assert rebuilt.items[0].revision != item.revision
    assert rebuilt.items[0].parameter_schema() == item.parameter_schema()


def test_plain_skill_exposes_parameterless_manifest_read_without_body_in_catalog(tmp_path):
    _skill(tmp_path, readonly=False)
    (item,) = build_builtin_skill_metadata(builtin_root=tmp_path).items
    assert item.parameter_schema() == {"type": "object", "properties": {}, "additionalProperties": False}
    assert item.read_only_entry_json is None
    assert "PRIVATE_BODY_NOT_IN_METADATA" not in repr(item)


def test_internal_origin_path_changes_generation_without_changing_package_version(tmp_path):
    path = _skill(tmp_path, directory="nested/package")
    original = build_builtin_skill_metadata(builtin_root=tmp_path)
    assert original.items[0].relative_path == "nested/package"
    assert "nested/package" not in repr(original)
    path.rename(tmp_path / "relocated")
    moved = build_builtin_skill_metadata(builtin_root=tmp_path)
    assert moved.items[0].revision == original.items[0].revision
    assert moved.items[0].relative_path == "relocated"
    assert moved.generation != original.generation


def test_default_builder_ignores_same_named_host_and_project_catalogs(tmp_path, monkeypatch):
    installed = tmp_path / "installed"
    host = tmp_path / "host"
    project = tmp_path / "project"
    real = _skill(installed)
    _skill(host / ".bowhead/skills")
    shadow = _skill(project / ".bowhead/skills")
    (shadow / "SKILL.md").write_text("---\nname: dupont\ndescription: SHADOW DESCRIPTION\n---\nshadow")
    monkeypatch.setenv("HOME", str(host))
    monkeypatch.chdir(project)
    monkeypatch.setattr(metadata, "builtin_catalog_dir", lambda: installed)
    index = build_builtin_skill_metadata()
    assert index.items[0].revision == "sha256:" + bundle_sha256(real)
    assert "SHADOW DESCRIPTION" not in repr(index)


def test_duplicate_names_fail_individually_without_a_last_wins_choice(tmp_path):
    _skill(tmp_path, directory="first")
    _skill(tmp_path, directory="second")
    _skill(tmp_path, "other")
    index = build_builtin_skill_metadata(builtin_root=tmp_path)
    assert index.incomplete
    assert [item.name for item in index.items] == ["other"]


@pytest.mark.parametrize("damage", ["symlink_file", "symlink_dir", "malformed", "oversized", "too_many", "invalid_name"])
def test_unavailable_package_does_not_hide_other_builtin_metadata(tmp_path, damage):
    bad = _skill(tmp_path, "broken")
    _skill(tmp_path, "healthy")
    if damage == "symlink_file":
        (bad / "escape.txt").symlink_to(tmp_path / "healthy/SKILL.md")
    elif damage == "symlink_dir":
        (bad / "escape").symlink_to(tmp_path / "healthy", target_is_directory=True)
    elif damage == "malformed":
        (bad / "SKILL.md").write_text("---\ninvalid: [\n---\nbad")
    elif damage == "oversized":
        with (bad / "large.bin").open("wb") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
    elif damage == "too_many":
        for i in range(1025):
            (bad / f"file{i}").touch()
    else:
        (bad / "SKILL.md").write_text("---\nname: invalid/name\ndescription: Bad\n---\nbad")
    index = build_builtin_skill_metadata(builtin_root=tmp_path)
    assert index.incomplete and [item.name for item in index.items] == ["healthy"]


def test_missing_root_does_not_abort_startup(tmp_path):
    index = build_builtin_skill_metadata(builtin_root=tmp_path / "missing")
    assert index.items == () and index.incomplete


def test_package_change_during_parsing_does_not_bind_schema_to_another_revision(tmp_path, monkeypatch):
    path = _skill(tmp_path)
    original = metadata.load_skill

    def changed(root):
        value = original(root)
        (path / "scripts/unused.py").write_text("changed after metadata was read")
        return value

    monkeypatch.setattr(metadata, "load_skill", changed)
    index = build_builtin_skill_metadata(builtin_root=tmp_path)
    assert index.items == () and index.incomplete


def test_display_title_and_long_identity_respect_wire_bounds(tmp_path):
    path = _skill(tmp_path, "n" * 504, directory="long")
    content = path.joinpath("SKILL.md").read_text().replace("description:", "title: 杜邦分析\ndescription:")
    path.joinpath("SKILL.md").write_text(content)
    item = build_builtin_skill_metadata(builtin_root=tmp_path).items[0]
    assert item.label == "杜邦分析" and len(item.id) == 512
    path.joinpath("SKILL.md").write_text(content.replace("title: 杜邦分析\n", ""))
    assert len(build_builtin_skill_metadata(builtin_root=tmp_path).items[0].label) == 256


def test_installed_document_display_uses_english_without_rebinding_package_revision(tmp_path, monkeypatch):
    path = _skill(tmp_path, "pdf", readonly=False)
    manifest = path / "SKILL.md"
    manifest.write_text(manifest.read_text().replace("description:", "title: 测试文档\ndescription:"))
    before = bundle_sha256(path)
    monkeypatch.setattr("bowhead_skill.catalog.builtin_catalog_dir", lambda: tmp_path)

    (item,) = build_builtin_skill_metadata(builtin_root=tmp_path).items

    assert item.label == "PDF processing guide"
    assert item.revision == f"sha256:{before}"
    assert bundle_sha256(path) == before
    assert load_skill(path).frontmatter["title"] == "测试文档"
