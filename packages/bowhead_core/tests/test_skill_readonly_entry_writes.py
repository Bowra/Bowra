"""Actual authoring, upload and whole-package copy share the opt-in entry gate."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
import yaml
from bowhead_core.chat_references import SkillResourceRef
from bowhead_core.skill_upload import SkillUploadError, bundle_sha256, copy_validated_bundle, validate_and_extract, validate_and_save_generated
from bowhead_skill.loader import load_skill

pytestmark = pytest.mark.unit
FIXTURE = Path(__file__).resolve().parents[3] / "contracts/skill-readonly-entry/v1.json"


def _declaration() -> dict:
    return json.loads(FIXTURE.read_text())


def _manifest(declaration: dict) -> str:
    return (
        "---\n"
        + yaml.safe_dump(
            {"name": "readonly-example", "description": "Profitability method", "read_only_entry": declaration}, allow_unicode=True, sort_keys=False
        )
        + "---\n# Read-only method\nExplain profitability.\n"
    )


def _zip(declaration: dict, *, empty_file: bool = False) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("SKILL.md", _manifest(declaration))
        archive.writestr("references/method.md", " " if empty_file else "# Method\nUse authorized data.")
        archive.writestr("references/metrics.md", "Net margin = net profit / revenue")
        archive.writestr("scripts/legacy.py", "raise RuntimeError('not executed by this validation')\n")
    return output.getvalue()


@pytest.mark.parametrize("runtime_flag", ["0", "1"])
def test_upload_copy_and_reference_mutation_use_original_package_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime_flag: str) -> None:
    monkeypatch.setenv("BOWHEAD_SKILL_MULTIRUNTIME", runtime_flag)
    declaration = _declaration()
    saved = validate_and_extract(_zip(declaration), "method.zip", user_id="u_author", dest_root=tmp_path / "personal", existing_user_skills={})
    entry = load_skill(saved.source_dir).read_only_entry
    assert entry.to_dict() == declaration
    for ref in entry.to_dict()["required_skill_refs"]:
        assert SkillResourceRef.model_validate(ref).model_dump() == ref
    before = bundle_sha256(saved.source_dir)
    copied = copy_validated_bundle(saved.source_dir, tmp_path / "team-candidate", expected_name="readonly-example")
    assert bundle_sha256(copied.source_dir) == before
    assert load_skill(copied.source_dir).read_only_entry.to_dict() == declaration
    (copied.source_dir / "references/metrics.md").write_text("Changed definition")
    assert bundle_sha256(copied.source_dir) != before
    assert bundle_sha256(saved.source_dir) == before
    (saved.source_dir / "SKILL.md").write_text(_manifest({**declaration, "required_skill_refs": []}))
    assert bundle_sha256(saved.source_dir) != before


def test_generated_manifest_retains_contract_and_failed_overwrite_keeps_original(tmp_path: Path) -> None:
    declaration = _declaration()
    declaration["entry_file"] = "SKILL.md"
    declaration["required_files"] = []
    saved = validate_and_save_generated(body_md=_manifest(declaration), scripts=None, user_id="u_author", dest_root=tmp_path, existing_user_skills={})
    assert load_skill(saved.source_dir).read_only_entry.to_dict() == declaration
    before = bundle_sha256(saved.source_dir)
    declaration["requires_new_tools"] = True
    with pytest.raises(SkillUploadError) as error:
        validate_and_save_generated(
            body_md=_manifest(declaration), scripts=[], user_id="u_author", dest_root=tmp_path, existing_user_skills={}, overwrite=True
        )
    assert error.value.code == "E_SKILL_READ_ONLY_ENTRY_INVALID"
    assert error.value.params == {}
    assert bundle_sha256(saved.source_dir) == before


@pytest.mark.parametrize("invalid", ["empty-file", "schema", "dependency"])
def test_failed_upload_and_copy_never_replace_existing_content(tmp_path: Path, invalid: str) -> None:
    good = _declaration()
    saved = validate_and_extract(_zip(good), "method.zip", user_id="u_author", dest_root=tmp_path / "personal", existing_user_skills={})
    target = copy_validated_bundle(saved.source_dir, tmp_path / "live", expected_name="readonly-example")
    before = bundle_sha256(target.source_dir)
    bad = _declaration()
    if invalid == "schema":
        bad["parameter_schema"]["properties"]["period"]["format"] = "private-format"
    if invalid == "dependency":
        bad["required_skill_refs"][0]["version"] = "latest"
    with pytest.raises(SkillUploadError) as error:
        validate_and_extract(
            _zip(bad, empty_file=invalid == "empty-file"),
            "method.zip",
            user_id="u_author",
            dest_root=tmp_path / "personal",
            existing_user_skills={},
            overwrite=True,
        )
    assert error.value.code == "E_SKILL_READ_ONLY_ENTRY_INVALID"
    assert bundle_sha256(saved.source_dir) == before
    (saved.source_dir / "SKILL.md").write_text(_manifest(bad))
    if invalid == "empty-file":
        (saved.source_dir / "references/method.md").write_text(" ")
    with pytest.raises(SkillUploadError) as error:
        copy_validated_bundle(saved.source_dir, target.source_dir, expected_name="readonly-example", overwrite=True)
    assert error.value.code == "E_SKILL_READ_ONLY_ENTRY_INVALID"
    assert bundle_sha256(target.source_dir) == before


@pytest.mark.parametrize("tree", ["directory", "archive"])
def test_missing_declared_files_use_existing_inventory_without_outer_bundle_fallback(tmp_path: Path, tree: str) -> None:
    from bowhead_core.skill_import.inventory import ResolutionSources, inventory_from_bytes, inventory_from_dir

    declaration = _declaration()
    declaration["entry_file"] = "method.md"
    declaration["required_files"] = ["data/metrics.csv"]
    declaration["parameter_schema"]["properties"]["private"] = {"type": "string", "default": "private-default-marker"}
    files = {
        "outer/skills/readonly-example/SKILL.md": _manifest(declaration),
        "outer/method.md": "Outer method cannot satisfy the nested Skill's input",
        "outer/data/metrics.csv": "net,100",
    }
    sources = ResolutionSources()

    def inventory():
        if tree == "directory":
            for relative, text in files.items():
                path = tmp_path / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            return inventory_from_dir(tmp_path / "outer", sources)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for relative, text in files.items():
                archive.writestr(relative, text)
        return inventory_from_bytes(stream.getvalue(), sources)

    report = inventory()
    missing = [f for f in report.findings if f.code == "file_ref_dangling"]
    assert {(f.subject, f.severity) for f in missing} == {("method.md", "red"), ("data/metrics.csv", "red")}
    assert all(f.resolved_against == "read_only_entry:skill_bundle_files" for f in missing)
    assert "private-default-marker" not in json.dumps(report.to_dict())
    files["outer/skills/readonly-example/method.md"] = "Actual entry"
    files["outer/skills/readonly-example/data/metrics.csv"] = "net,100"
    assert inventory().red_count == 0
