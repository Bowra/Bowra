"""Exact full-package capture and selected-method projection from retained bytes."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
from dataclasses import replace

import pytest
from bowhead_core.skill_package_material import SkillPackageMaterialError, prepare_skill_package, restore_skill_package
from bowhead_core.skill_read_material import prepare_skill_read
from test_skill_read_material import _ref, _skill

pytestmark = pytest.mark.unit


def capture(root, resource=None):
    return prepare_skill_package(root, resource=resource or _ref(root), expected_name="dupont")


@pytest.mark.parametrize("origin", ["builtin", "user", "team"])
def test_full_package_preserves_original_binary_scripts_and_selected_read(tmp_path, origin):
    root = _skill(tmp_path / "original")
    (root / "assets").mkdir()
    (root / "assets" / "template.bin").write_bytes(b"\x00\xff\xfe\x80")
    resource = _ref(root, source=origin)
    expected = prepare_skill_read(root, resource=resource, expected_name="dupont")
    files = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    package = capture(root, resource)
    shutil.rmtree(root)
    restored = restore_skill_package(package.content, resource=resource, expected_name="dupont", content_hash=package.content_hash)
    assert restored.files() == files
    assert restored.selected_read() == expected
    assert b"PRIVATE_TOOL_NAME" not in restored.selected_read().content
    assert b"MUST_NOT_EXECUTE" not in restored.selected_read().content
    assert "MANIFEST_BODY" not in repr(restored)
    copy = restored.files()
    copy["SKILL.md"] = b"changed caller copy"
    assert restored.files() == files


def test_ordinary_baseline_package_does_not_require_a_selected_read_contract(tmp_path):
    root = _skill(tmp_path / "ordinary")
    (root / "SKILL.md").write_text("---\nname: dupont\ndescription: Profitability\n---\nOriginal baseline SOP")
    package = capture(root)
    assert package.files()["SKILL.md"].endswith(b"Original baseline SOP")
    # 65a2914c5: an authorized baseline without an entry declaration contributes only its SKILL.md body.
    selected = package.selected_read()
    assert [(item["path"], item["text"]) for item in selected.document()["files"]] == [("SKILL.md", "Original baseline SOP")]
    assert selected.document()["read_only_entry"]["entry_file"] == "SKILL.md"


@pytest.mark.parametrize("change", ["script", "binary", "manifest", "removed"])
def test_package_revision_covers_every_retained_file(tmp_path, change):
    root = _skill(tmp_path / "revision")
    resource = _ref(root)
    if change == "removed":
        (root / "private.txt").unlink()
    else:
        name = {"script": "scripts/run.py", "binary": "private.txt", "manifest": "SKILL.md"}[change]
        with (root / name).open("ab") as stream:
            stream.write(b"CHANGED")
    with pytest.raises(SkillPackageMaterialError, match="^reference_skill_package_unavailable$"):
        capture(root, resource)


@pytest.mark.parametrize("change", ["hash", "resource", "name", "schema", "duplicate", "order", "content", "base64", "path", "extra"])
def test_restore_requires_independent_identity_and_canonical_complete_bytes(tmp_path, change):
    package = capture(_skill(tmp_path / "restore"))
    value = json.loads(package.content)
    resource, name = package.resource, package.name
    if change == "hash":
        with pytest.raises(SkillPackageMaterialError):
            replace(package, content_hash="sha256:" + "0" * 64).files()
        return
    if change == "resource":
        resource = resource.model_copy(update={"id": "builtin:other"})
    elif change == "name":
        name = "other"
    elif change == "schema":
        value["schema_version"] = True
    elif change == "duplicate":
        value["files"].append(value["files"][0])
    elif change == "order":
        value["files"].reverse()
    elif change == "content":
        value["files"][-1]["content"] = base64.b64encode(b"REPLACED SCRIPT").decode()
    elif change == "base64":
        value["files"][0]["content"] += "\n"
    elif change == "path":
        value["files"][0]["path"] = "../SKILL.md"
    else:
        value["source_path"] = "/private/source"
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(SkillPackageMaterialError, match="^reference_skill_package_unavailable$"):
        restore_skill_package(content, resource=resource, expected_name=name, content_hash="sha256:" + hashlib.sha256(content).hexdigest())


@pytest.mark.parametrize("entry", ["root_link", "file_link", "directory_link", "hardlink", "fifo"])
def test_package_capture_rejects_unsupported_filesystem_entries(tmp_path, entry):
    root = _skill(tmp_path / "real")
    resource = _ref(root)
    if entry == "root_link":
        link = tmp_path / "link"
        link.symlink_to(root, target_is_directory=True)
        root = link
    elif entry == "file_link":
        (root / "link").symlink_to(root / "private.txt")
    elif entry == "directory_link":
        (root / "link").symlink_to(root / "references", target_is_directory=True)
    elif entry == "hardlink":
        os.link(root / "private.txt", root / "alias.txt")
    else:
        os.mkfifo(root / "pipe")
    with pytest.raises(SkillPackageMaterialError):
        capture(root, resource)


def test_encoded_snapshot_budget_includes_base64_and_envelope(tmp_path):
    root = _skill(tmp_path / "large")
    (root / "private.txt").write_bytes(b"x" * (790 * 1024))
    with pytest.raises(SkillPackageMaterialError):
        capture(root)
