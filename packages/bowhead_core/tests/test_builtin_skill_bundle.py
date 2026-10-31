"""Builtin source pins stay identical before and after Python compilation."""

from __future__ import annotations

import hashlib
import py_compile
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from bowhead_core.agent_dependency_compiler import builtin_skill_bundle_revision, skill_bundle_revision
from bowhead_core.chat_references import SkillResourceRef
from bowhead_core.skill_package_material import SkillPackageMaterialError, _encode_package, prepare_skill_package, restore_skill_package
from bowhead_core.skill_read_material import SkillReadMaterialError, prepare_skill_read
from bowhead_core.skill_reference_metadata import build_builtin_skill_metadata
from bowhead_core.skill_upload import builtin_bundle_sha256, bundle_sha256
from bowhead_skill.runtime import SkillRuntime
from bowhead_skill.tools import build_skill_tools
from test_skill_read_material import _skill

from bowhead_skill.catalog import SkillCatalog

pytestmark = pytest.mark.unit


def ref(root: Path, source="builtin", *, legacy=False):
    revision = bundle_sha256(root) if legacy or source != "builtin" else builtin_bundle_sha256(root)
    return SkillResourceRef(
        kind="skill", source_kind=source, id="builtin:dupont" if source == "builtin" else "skill_stable_id", version="sha256:" + revision
    )


def compile_cache(root: Path):
    path = Path(py_compile.compile(str(root / "scripts/run.py"), doraise=True))
    assert path.is_file()
    return path


def test_real_bytecode_does_not_change_builtin_pin_or_packaged_material(tmp_path):
    root = _skill(tmp_path / "source")
    clean = builtin_skill_bundle_revision(str(root))
    assert clean == skill_bundle_revision(str(root))
    original = prepare_skill_package(root, resource=ref(root), expected_name="dupont")
    cache = compile_cache(root)
    py_compile.compile(str(root / "scripts/run.py"), cfile=str(root / "legacy.pyo"), doraise=True, optimize=2)
    assert builtin_skill_bundle_revision(str(root)) == clean
    assert skill_bundle_revision(str(root)) != clean
    assert prepare_skill_package(root, resource=ref(root), expected_name="dupont").content == original.content
    packaged = tmp_path / "packaged"
    shutil.copytree(root, packaged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    assert builtin_skill_bundle_revision(str(packaged)) == clean
    assert prepare_skill_package(packaged, resource=ref(root), expected_name="dupont").content == original.content
    cache.write_bytes(b"A different generated cache")
    assert builtin_skill_bundle_revision(str(root)) == clean
    with (root / "scripts/run.py").open("a") as stream:
        stream.write("\n# An actual source change.\n")
    assert builtin_skill_bundle_revision(str(root)) != clean


@pytest.mark.parametrize("source", ["team", "user"])
def test_team_and_user_cas_and_frozen_bytes_keep_cache_coverage(tmp_path, source):
    root = _skill(tmp_path / "source")
    previous = bundle_sha256(root)
    cache = compile_cache(root)
    resource = ref(root, source)
    assert resource.version != "sha256:" + previous
    package = prepare_skill_package(root, resource=resource, expected_name="dupont")
    relative = cache.relative_to(root).as_posix()
    assert package.files()[relative] == cache.read_bytes()
    restored = restore_skill_package(package.content, resource=resource, expected_name="dupont", content_hash=package.content_hash)
    assert restored.content == package.content
    assert restored.files() == package.files()
    assert prepare_skill_read(root, resource=resource, expected_name="dupont") == restored.selected_read()


def test_original_cached_builtin_envelope_keeps_its_original_full_hash(tmp_path):
    root = _skill(tmp_path / "source")
    compile_cache(root)
    resource = ref(root, legacy=True)
    files = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    content = _encode_package(files, resource=resource, name="dupont")
    restored = restore_skill_package(content, resource=resource, expected_name="dupont", content_hash="sha256:" + hashlib.sha256(content).hexdigest())
    assert restored.content == content and restored.files() == files
    assert restored.selected_read().document()["resource"] == resource.model_dump(mode="json")
    with pytest.raises(SkillPackageMaterialError, match="reference_skill_package_unavailable"):
        prepare_skill_package(root, resource=resource, expected_name="dupont")


def test_new_builtin_read_cannot_project_a_cache_reference(tmp_path):
    root = _skill(tmp_path / "source", entry_file="references/__pycache__/cached.md")
    cache = root / "references/__pycache__/cached.md"
    cache.parent.mkdir()
    cache.write_text("Generated cache data must not become a method.")
    with pytest.raises(SkillReadMaterialError):
        prepare_skill_read(root, resource=ref(root), expected_name="dupont")


def test_builtin_discovery_and_material_use_the_same_clean_revision(tmp_path):
    root = _skill(tmp_path / "dupont")
    compile_cache(root)
    index = build_builtin_skill_metadata(builtin_root=tmp_path)
    assert not index.incomplete and len(index.items) == 1
    assert index.items[0].revision == builtin_skill_bundle_revision(str(root))
    selected = prepare_skill_read(root, resource=ref(root), expected_name="dupont")
    assert selected.document()["resource"]["version"] == index.items[0].revision


@pytest.mark.asyncio
async def test_live_builtin_tool_omits_cache_references_and_rejects_explicit_cache_reads(tmp_path):
    root = _skill(tmp_path / "dupont")
    cache = root / "references/__pycache__/cached.md"
    cache.parent.mkdir()
    cache.write_text("Generated cache data")
    script = root / "scripts/__pycache__/cached.py"
    script.parent.mkdir()
    script.write_text("raise RuntimeError('Do not execute cache files')")
    catalog = SkillCatalog(roots=[tmp_path])
    tool = next(item for item in build_skill_tools(catalog, SkillRuntime()) if item.name == "skill_read")
    ctx = SimpleNamespace(user_id="usr_fixture", team_id="team_fixture", skill_instance_store=None, team_skill_store=None)
    read = await tool.handler({"name": "dupont"}, ctx)
    assert "__pycache__/cached.md" not in read["references"]
    assert "__pycache__/cached.py" not in read["scripts"]
    for args in ({"file": "references/__pycache__/cached.md"}, {"ref": "__pycache__/cached.md"}, {"file": "scripts/module.pyc"}):
        rejected = await tool.handler({"name": "dupont", **args}, ctx)
        assert rejected["error"] == "builtin_cache_file_unavailable"


@pytest.mark.parametrize("source", ["builtin", "user_upload", "team"])
def test_original_skill_impact_producers_use_the_explicit_source_digest(tmp_path, source):
    from bowhead_gateway.routes.skills import _impact_selector, _personal_skill_state

    root = _skill(tmp_path / "source")
    compile_cache(root)
    spec = SimpleNamespace(source_dir=root, name="dupont", version="1.0.0", title="Profitability")
    expected = builtin_bundle_sha256(root) if source == "builtin" else bundle_sha256(root)
    state = _personal_skill_state(spec, None, source)
    assert state["content_digest"] == expected
    target = SimpleNamespace(
        kind="personal_preference", source_type=source, spec=spec, resource_id="dupont", resource_ref="skill:dupont", enabled=True
    )
    assert _impact_selector(target, name="dupont").revision == expected
