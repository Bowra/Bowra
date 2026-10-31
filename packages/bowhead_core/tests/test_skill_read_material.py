"""Real filesystem capture, exact versions and text-only projection."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import traceback
from pathlib import Path

import pytest
import yaml
from bowhead_core.chat_reference_storage import ContentDescriptor, ReferenceCipher, ReferenceScope
from bowhead_core.chat_references import SkillResourceRef
from bowhead_core.skill_read_material import SkillReadMaterialError, prepare_skill_read, restore_skill_read
from bowhead_core.skill_upload import bundle_sha256
from bowhead_skill.loader import load_skill

from bowhead_core import skill_read_material as material

pytestmark = pytest.mark.unit


def _skill(root: Path, *, entry_file="references/method.md", required_files=None):
    root.mkdir()
    (root / "references").mkdir()
    (root / "scripts").mkdir()
    declaration = {
        "schema_version": 1,
        "entry_file": entry_file,
        "required_files": required_files or [],
        "parameter_schema": {
            "type": "object",
            "properties": {"company": {"type": "string", "default": "DEFAULT_NOT_INSERTED"}},
            "additionalProperties": False,
        },
        "required_skill_refs": [],
        "requires_script": False,
        "requires_new_tools": False,
        "requires_new_connections": False,
    }
    frontmatter = {
        "name": "dupont",
        "description": "Profitability",
        "allowed-tools": "PRIVATE_TOOL_NAME",
        "script_command": "PRIVATE_EXEC_COMMAND",
        "read_only_entry": declaration,
    }
    (root / "SKILL.md").write_text("---\n" + yaml.safe_dump(frontmatter, allow_unicode=True) + "---\nMANIFEST_BODY\n")
    (root / "references/method.md").write_text("ROE = margin × turnover × leverage\n{{ company }} stays literal\n")
    (root / "references/data.csv").write_text("company,value\nExample,42\n")
    (root / "private.txt").write_text("UNSELECTED_PRIVATE_FILE")
    (root / "scripts/run.py").write_text("raise RuntimeError('MUST_NOT_EXECUTE')")
    return root


def _ref(root: Path, *, source="builtin"):
    return SkillResourceRef(
        kind="skill", source_kind=source, id="builtin:dupont" if source == "builtin" else "skill_stable_id", version="sha256:" + bundle_sha256(root)
    )


def _prepare(root, ref=None):
    return prepare_skill_read(root, resource=ref or _ref(root), expected_name="dupont")


@pytest.mark.parametrize("source", ["builtin", "team", "user"])
def test_exact_projection_contains_only_declared_text_and_detached_contract(tmp_path, source):
    root = _skill(tmp_path / "bundle", required_files=["references/data.csv"])
    ref = _ref(root, source=source)
    result = _prepare(root, ref)
    document = result.document()
    assert document["resource"] == ref.model_dump(mode="json")
    assert [item["path"] for item in document["files"]] == ["references/method.md", "references/data.csv"]
    assert document["read_only_entry"] == load_skill(root).read_only_entry.to_dict()
    assert document["files"][0]["text"] == (root / "references/method.md").read_text()
    for item in document["files"]:
        assert item["sha256"] == "sha256:" + hashlib.sha256(item["text"].encode()).hexdigest()
    assert result.content_hash == "sha256:" + hashlib.sha256(result.content).hexdigest()
    assert all(
        value not in result.content.decode()
        for value in ("PRIVATE_TOOL_NAME", "PRIVATE_EXEC_COMMAND", "UNSELECTED_PRIVATE_FILE", "MUST_NOT_EXECUTE", "MANIFEST_BODY", str(root))
    )
    assert "ROE" not in repr(result)
    document["files"][0]["text"] = "changed caller copy"
    assert "changed caller copy" not in result.content.decode()
    changed_ref = ref.model_copy(update={"version": "sha256:" + "b" * 64})
    assert result.document()["resource"]["version"] != changed_ref.version
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.content = b"changed"
    (root / "references/method.md").write_text("changed disk")
    assert result.document()["files"][0]["text"].startswith("ROE")


def test_manifest_projection_uses_shared_parser_and_never_injects_execution_frontmatter(tmp_path):
    root = _skill(tmp_path / "bundle", entry_file="SKILL.md", required_files=["references/data.csv"])
    result = _prepare(root)
    assert result.document()["files"][0]["text"] == load_skill(root).body_md
    assert all(
        value not in result.content.decode() for value in ("PRIVATE_TOOL_NAME", "PRIVATE_EXEC_COMMAND", "allowed-tools", "source_dir", "body_raw")
    )
    assert result.document()["read_only_entry"]["parameter_schema"]["properties"]["company"]["default"] == "DEFAULT_NOT_INSERTED"
    assert "DEFAULT_NOT_INSERTED" not in result.document()["files"][0]["text"]


def test_legacy_hash_order_and_binary_unused_files_are_part_of_selected_revision(tmp_path):
    root = _skill(tmp_path / "bundle")
    (root / "a").mkdir()
    (root / "a/b").write_bytes(b"binary\xff\0")
    (root / "a.txt").write_bytes(b"another\0file")
    ref = _ref(root)
    first = _prepare(root, ref)
    (root / "scripts/run.py").write_text("unused script changed")
    with pytest.raises(SkillReadMaterialError, match="^reference_skill_version_changed$"):
        _prepare(root, ref)
    second = _prepare(root)
    assert first.content_hash != second.content_hash
    assert first.materialization_hash == second.materialization_hash
    (root / "references/method.md").write_text("Changed selected text")
    third = _prepare(root)
    assert third.materialization_hash != second.materialization_hash


@pytest.mark.parametrize(
    "failure",
    [
        "name",
        "builtin_id",
        "hash",
        "invalid_declaration",
        "malformed_yaml",
        "missing_file",
        "non_utf8",
        "nul",
        "blank",
        "blank_manifest",
        "file_limit",
        "json_budget",
    ],
)
def test_preparation_rejects_invalid_material_with_finite_private_errors(tmp_path, failure):
    root = _skill(tmp_path / "PRIVATE_PACKAGE_PATH")
    if failure == "invalid_declaration":
        (root / "SKILL.md").write_text("---\nname: dupont\ndescription: PRIVATE_RAW_VALUE\nread_only_entry: null\n---\nbody")
    elif failure == "malformed_yaml":
        (root / "SKILL.md").write_text("---\nname: [PRIVATE_RAW_VALUE\n---\nbody")
    elif failure == "missing_file":
        (root / "references/method.md").unlink()
    elif failure in {"non_utf8", "nul", "blank", "file_limit", "json_budget"}:
        value = {
            "non_utf8": b"PRIVATE_RAW_VALUE\xff",
            "nul": b"PRIVATE_RAW_VALUE\0",
            "blank": b" \n",
            "file_limit": b"x" * (256 * 1024 + 1),
            "json_budget": b"\x01" * (256 * 1024),
        }[failure]
        (root / "references/method.md").write_bytes(value)
    elif failure == "blank_manifest":
        text = (root / "SKILL.md").read_text().replace("entry_file: references/method.md", "entry_file: SKILL.md").replace("MANIFEST_BODY", "  ")
        (root / "SKILL.md").write_text(text)
    ref = _ref(root)
    if failure == "name":
        ref = _ref(root, source="user")
    if failure == "builtin_id":
        ref = ref.model_copy(update={"id": "builtin:other"})
    if failure == "hash":
        ref = ref.model_copy(update={"version": "sha256:" + "0" * 64})
    with pytest.raises(SkillReadMaterialError) as raised:
        prepare_skill_read(root, resource=ref, expected_name="other" if failure == "name" else "dupont")
    assert str(raised.value) in {"reference_skill_material_unavailable", "reference_skill_version_changed", "reference_skill_material_too_large"}
    rendered = "".join(traceback.format_exception(raised.value))
    assert "PRIVATE_RAW_VALUE" not in rendered and str(root) not in rendered


@pytest.mark.parametrize("failure", ["root_symlink", "file_symlink", "directory_symlink", "fifo", "hardlink", "entries", "depth", "bundle_limit"])
def test_capture_rejects_links_special_files_and_global_budget_before_reading(tmp_path, failure):
    root = _skill(tmp_path / "bundle")
    ref = _ref(root)
    if failure == "root_symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(root, target_is_directory=True)
        root = alias
    elif failure == "file_symlink":
        (root / "symlink.txt").symlink_to(root / "private.txt")
    elif failure == "directory_symlink":
        (root / "alias").symlink_to(root / "references", target_is_directory=True)
    elif failure == "fifo":
        os.mkfifo(root / "pipe")
    elif failure == "hardlink":
        (root / "linked.txt").hardlink_to(root / "private.txt")
    elif failure == "entries":
        for i in range(1025):
            (root / f"entry{i}").touch()
    elif failure == "depth":
        (root / Path(*["deep"] * 18)).mkdir(parents=True)
    else:
        with (root / "huge.bin").open("wb") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
    if failure in {"hardlink", "entries"}:
        ref = _ref(root)
    with pytest.raises(SkillReadMaterialError) as raised:
        _prepare(root, ref)
    assert str(raised.value) != "reference_skill_version_changed", "reject unsupported tree before hash comparison"


def test_same_file_mutation_during_actual_read_fails(tmp_path, monkeypatch):
    root = _skill(tmp_path / "bundle")
    path = root / "references/method.md"
    path.write_text("a" * 100000)
    inode = path.stat().st_ino
    ref = _ref(root)
    read = os.read
    changed = False

    def changing(fd, size):
        nonlocal changed
        data = read(fd, size)
        if os.fstat(fd).st_ino == inode and not changed:
            changed = True
            path.write_text("b" * 100000)
        return data

    monkeypatch.setattr(os, "read", changing)
    with pytest.raises(SkillReadMaterialError):
        _prepare(root, ref)
    assert changed


def test_file_replaced_by_symlink_between_stat_and_open_is_never_followed(tmp_path, monkeypatch):
    root = _skill(tmp_path / "bundle")
    ref = _ref(root)
    outside = tmp_path / "OUTSIDE_PRIVATE"
    outside.write_text("PRIVATE_FILE_NEVER_READ")
    opened = os.open
    changed = False

    def replacing(path, flags, *args, **kwargs):
        nonlocal changed
        if path == "method.md" and not changed:
            changed = True
            (root / "references/method.md").unlink()
            (root / "references/method.md").symlink_to(outside)
        return opened(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replacing)
    with pytest.raises(SkillReadMaterialError):
        _prepare(root, ref)
    assert changed


def test_replacing_package_after_capture_does_not_change_schema_or_text(tmp_path, monkeypatch):
    root = _skill(tmp_path / "bundle")
    ref = _ref(root)
    capture = material._capture

    def changed(path, **kwargs):
        result = capture(path, **kwargs)
        (root / "SKILL.md").write_text("---\nname: dupont\nread_only_entry: BAD_REPLACEMENT\n---\nNEW_BODY")
        (root / "references/method.md").write_text("NEW_TEXT")
        return result

    monkeypatch.setattr(material, "_capture", changed)
    result = _prepare(root, ref)
    assert result.document()["files"][0]["text"].startswith("ROE")
    assert "BAD_REPLACEMENT" not in result.content.decode() and "NEW_TEXT" not in result.content.decode()


def test_prepared_bytes_fit_existing_scope_bound_encryption_and_descriptor(tmp_path):
    root = _skill(tmp_path / "bundle")
    result = _prepare(root)
    resource = _ref(root)
    descriptor = ContentDescriptor(
        schema_version=1,
        content_key="material_dupont",
        node_id="00000000-0000-4000-8000-000000000001",
        resource=resource.model_dump(mode="json"),
        content_kind="skill_read",
        materialization_hash=result.materialization_hash,
        content_hash=result.content_hash,
        byte_count=len(result.content),
    )
    scope = ReferenceScope(team_id="team_one", user_id="user_one", workspace_id="ws_one", session_id="s_one", policy_epoch=0)
    cipher = ReferenceCipher(base64.urlsafe_b64encode(b"m" * 32).decode())
    token = cipher.seal(scope=scope, purpose="test_material", identity=descriptor.content_key, content=result.content)
    assert "ROE = margin × turnover × leverage" not in token
    assert cipher.open(scope=scope, purpose="test_material", identity=descriptor.content_key, ciphertext=token) == result.content


def test_growing_file_is_rejected_at_actual_byte_budget(tmp_path, monkeypatch):
    root = _skill(tmp_path / "bundle")
    path = root / "references/method.md"
    path.write_bytes(b"a" * 100000)
    inode = path.stat().st_ino
    ref = _ref(root)
    read = os.read
    changed = False
    consumed = 0

    def growing(fd, size):
        nonlocal changed, consumed
        data = read(fd, size)
        consumed += len(data)
        if os.fstat(fd).st_ino == inode and not changed:
            changed = True
            with path.open("ab") as stream:
                stream.truncate(18 * 1024 * 1024)
        return data

    monkeypatch.setattr(os, "read", growing)
    with pytest.raises(SkillReadMaterialError, match="^reference_skill_material_too_large$"):
        _prepare(root, ref)
    assert changed and consumed <= 16 * 1024 * 1024 + 1


def test_directory_replacement_never_reads_a_later_same_named_tree(tmp_path, monkeypatch):
    root = _skill(tmp_path / "bundle")
    ref = _ref(root)
    opened = os.open
    changed = False

    def replacing(path, flags, *args, **kwargs):
        nonlocal changed
        if path == "references" and not changed:
            changed = True
            (root / "references").rename(root / "original_references")
            (root / "references").mkdir()
            (root / "references/method.md").write_text("DIFFERENT_TREE")
        return opened(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replacing)
    with pytest.raises(SkillReadMaterialError):
        _prepare(root, ref)
    assert changed


def test_failed_capture_closes_all_opened_file_descriptors(tmp_path, monkeypatch):
    root = _skill(tmp_path / "bundle")
    ref = _ref(root)
    opened, closed = os.open, os.close
    live = set()

    def track_open(*args, **kwargs):
        fd = opened(*args, **kwargs)
        live.add(fd)
        return fd

    def track_close(fd):
        live.discard(fd)
        return closed(fd)

    def fail_read(*args):
        raise OSError("PRIVATE_FILE_NAME")

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "close", track_close)
    monkeypatch.setattr(os, "read", fail_read)
    with pytest.raises(SkillReadMaterialError):
        _prepare(root, ref)
    assert not live


def _descriptor(result):
    return ContentDescriptor(
        schema_version=1,
        content_key="material_dupont",
        node_id="00000000-0000-4000-8000-000000000001",
        resource=result.document()["resource"],
        content_kind="skill_read",
        materialization_hash=result.materialization_hash,
        content_hash=result.content_hash,
        byte_count=len(result.content),
    )


@pytest.mark.parametrize("damage", ["content_hash", "material_hash", "byte_count", "source", "version"])
def test_restore_rejects_mismatched_persisted_descriptors(tmp_path, damage):
    result = _prepare(_skill(tmp_path / "bundle"))
    descriptor = _descriptor(result)
    assert restore_skill_read(result.content, descriptor=descriptor) == result
    fields = descriptor.model_dump(mode="json")
    if damage == "content_hash":
        fields["content_hash"] = "sha256:" + "0" * 64
    elif damage == "material_hash":
        fields["materialization_hash"] = "sha256:" + "0" * 64
    elif damage == "byte_count":
        fields["byte_count"] += 1
    elif damage == "source":
        fields["resource"]["source_kind"] = "user"
    else:
        fields["resource"]["version"] = "sha256:" + "0" * 64
    with pytest.raises(SkillReadMaterialError, match="^reference_skill_material_unavailable$"):
        restore_skill_read(result.content, descriptor=ContentDescriptor.model_validate(fields))


@pytest.mark.parametrize(
    "damage",
    [
        "extra_key",
        "bool_version",
        "duplicate_key",
        "extra_file",
        "missing_file",
        "wrong_path",
        "file_hash",
        "nul",
        "blank",
        "file_type",
        "file_limit",
        "extra_file_field",
        "schema",
    ],
)
def test_restore_validates_material_contract_even_when_outer_checksums_match(tmp_path, damage):
    result = _prepare(_skill(tmp_path / "bundle"))
    document = result.document()
    if damage == "extra_key":
        document["source_dir"] = "PRIVATE_SOURCE_PATH"
    elif damage == "bool_version":
        document["schema_version"] = True
    elif damage == "extra_file":
        document["files"].append(document["files"][0])
    elif damage == "missing_file":
        document["files"] = []
    elif damage == "wrong_path":
        document["files"][0]["path"] = "scripts/run.py"
    elif damage == "file_hash":
        document["files"][0]["sha256"] = "sha256:" + "0" * 64
    elif damage in {"nul", "blank", "file_type", "file_limit"}:
        text = {"nul": "PRIVATE_RAW_VALUE\0", "blank": " \n", "file_type": 42, "file_limit": "x" * (256 * 1024 + 1)}[damage]
        document["files"][0]["text"] = text
        if isinstance(text, str):
            document["files"][0]["sha256"] = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    elif damage == "extra_file_field":
        document["files"][0]["execute"] = "PRIVATE_COMMAND"
    elif damage == "schema":
        document["read_only_entry"]["requires_script"] = True
    content = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if damage == "duplicate_key":
        content = content.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1', 1)
    descriptor = _descriptor(result).model_dump(mode="json")
    descriptor["content_hash"] = "sha256:" + hashlib.sha256(content).hexdigest()
    descriptor["byte_count"] = len(content)
    projection = {key: document[key] for key in ("schema_version", "read_only_entry", "files")}
    descriptor["materialization_hash"] = (
        "sha256:" + hashlib.sha256(json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )
    with pytest.raises(SkillReadMaterialError):
        restore_skill_read(content, descriptor=ContentDescriptor.model_validate(descriptor))


def test_maximum_depth_legal_contract_roundtrips_inside_material_envelope(tmp_path):
    root = _skill(tmp_path / "bundle")
    frontmatter, body = material.split_skill_markdown((root / "SKILL.md").read_text())
    data = yaml.safe_load(frontmatter)
    schema = {"type": "string"}
    for _ in range(6):
        schema = {"type": "object", "properties": {"child": schema}, "additionalProperties": False}
    data["read_only_entry"]["parameter_schema"] = schema
    (root / "SKILL.md").write_text("---\n" + yaml.safe_dump(data) + "---\n" + body)
    prepared = _prepare(root)
    assert restore_skill_read(prepared.content, descriptor=_descriptor(prepared)) == prepared


@pytest.mark.parametrize("identity", ["personal_identity", "builtin:"])
def test_restore_requires_the_frozen_builtin_identity_shape(tmp_path, identity):
    prepared = _prepare(_skill(tmp_path / "bundle"))
    document = prepared.document()
    document["resource"]["id"] = identity
    content = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    fields = _descriptor(prepared).model_dump(mode="json")
    fields.update(resource=document["resource"], content_hash="sha256:" + hashlib.sha256(content).hexdigest(), byte_count=len(content))
    with pytest.raises(SkillReadMaterialError):
        restore_skill_read(content, descriptor=ContentDescriptor.model_validate(fields))


@pytest.mark.parametrize("description,valid", [("  Profitability  ", True), ("   ", False)])
def test_manifest_required_fields_share_loader_normalization(tmp_path, description, valid):
    root = _skill(tmp_path / "bundle")
    frontmatter, body = material.split_skill_markdown((root / "SKILL.md").read_text())
    data = yaml.safe_load(frontmatter)
    data.update(name="  dupont  ", description=description)
    (root / "SKILL.md").write_text("---\n" + yaml.safe_dump(data) + "---\n" + body)
    if valid:
        assert load_skill(root).name == "dupont"
        assert _prepare(root).document()["resource"]["id"] == "builtin:dupont"
    else:
        with pytest.raises(ValueError):
            load_skill(root)
        with pytest.raises(SkillReadMaterialError):
            _prepare(root)


@pytest.mark.parametrize("source", ["builtin", "team", "user"])
def test_plain_skill_reference_reads_only_manifest_body_and_keeps_exact_revision(tmp_path, source):
    root = _skill(tmp_path / "bundle")
    path = root / "SKILL.md"
    text = path.read_text()
    frontmatter, body = text.split("---\n")[1:3]
    header = yaml.safe_load(frontmatter)
    del header["read_only_entry"]
    path.write_text("---\n" + yaml.safe_dump(header) + "---\n" + body)
    ref = _ref(root, source=source)
    with pytest.raises(SkillReadMaterialError):
        prepare_skill_read(root, resource=ref, expected_name="dupont", require_declaration=True)
    result = _prepare(root, ref)
    assert [item["path"] for item in result.document()["files"]] == ["SKILL.md"]
    assert result.document()["files"][0]["text"] == load_skill(root).body_md
    assert result.document()["read_only_entry"]["required_skill_refs"] == []
    assert all(value not in result.content.decode() for value in ("MUST_NOT_EXECUTE", "PRIVATE_EXEC_COMMAND", "UNSELECTED_PRIVATE_FILE"))
    with pytest.raises(ValueError):
        material.ReadOnlyEntry(result.document()["read_only_entry"]).validate_parameters({"company": "Example"})
    (root / "scripts/run.py").write_text("Changed unused script")
    with pytest.raises(SkillReadMaterialError, match="reference_skill_version_changed"):
        _prepare(root, ref)
