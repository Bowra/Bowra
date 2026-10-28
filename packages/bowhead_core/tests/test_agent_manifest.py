"""Sprint multi-agent-v1 PR-#1 — manifest schema + system loader 单测.

覆盖:
- Pydantic schema validation (name regex / tool allow / output_schema)
- AgentRegistry.bootstrap() load system manifest
- Path traversal + symlink reject (T-SEC-P0.1)
- JSONSchema additionalProperties=false enforcement
- 文件名必须 match name 字段
- Duplicate name reject
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import yaml
from bowhead_core.agents import (
    AgentManifest,
    AgentRegistry,
    ManifestSecurityError,
    ManifestValidationError,
)
from bowhead_core.agents import registry as registry_module
from bowhead_core.agents.registry import _validate_path
from pydantic import ValidationError

if TYPE_CHECKING:
    from pathlib import Path

# -- Schema validation --


def _minimal_manifest_dict() -> dict:
    """构造一个合法的 minimal manifest dict (用于单测改字段)."""
    return {
        "name": "test-agent",
        "model": "analyst",
        "trust_class": "read-mostly",
        "max_iterations": 10,
        "max_output_tokens": 1000,
        "description": "Test agent for unit tests",
        "system": {"file": "test-agent.md"},
        "tools": {"default_enabled": False, "allow": ["bi_query"]},
    }


def test_minimal_manifest_validates() -> None:
    m = AgentManifest.model_validate(_minimal_manifest_dict())
    assert m.name == "test-agent"
    assert m.version == 1  # default
    assert m.trust_class == "read-mostly"


def test_name_regex_rejects_uppercase() -> None:
    bad = _minimal_manifest_dict() | {"name": "TestAgent"}
    with pytest.raises(ValidationError, match="pattern"):
        AgentManifest.model_validate(bad)


def test_name_regex_rejects_underscore() -> None:
    bad = _minimal_manifest_dict() | {"name": "test_agent"}
    with pytest.raises(ValidationError, match="pattern"):
        AgentManifest.model_validate(bad)


def test_name_regex_rejects_path_traversal_chars() -> None:
    bad = _minimal_manifest_dict() | {"name": "../etc"}
    with pytest.raises(ValidationError, match="pattern"):
        AgentManifest.model_validate(bad)


def test_max_iterations_lower_bound() -> None:
    bad = _minimal_manifest_dict() | {"max_iterations": 0}
    with pytest.raises(ValidationError):
        AgentManifest.model_validate(bad)


def test_max_iterations_upper_bound() -> None:
    bad = _minimal_manifest_dict() | {"max_iterations": 201}
    with pytest.raises(ValidationError):
        AgentManifest.model_validate(bad)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "32000"])
def test_max_output_tokens_rejects_invalid_explicit_values(value) -> None:
    with pytest.raises(ValidationError):
        AgentManifest.model_validate(_minimal_manifest_dict() | {"max_output_tokens": value})


@pytest.mark.parametrize("value", [None, 16, 32000, 393216])
def test_max_output_tokens_preserves_automatic_or_explicit_intent(value) -> None:
    assert AgentManifest.model_validate(_minimal_manifest_dict() | {"max_output_tokens": value}).max_output_tokens == value


def test_tools_allow_min_length() -> None:
    bad = _minimal_manifest_dict()
    bad["tools"] = {"default_enabled": False, "allow": []}
    with pytest.raises(ValidationError):
        AgentManifest.model_validate(bad)


def test_tools_no_dup() -> None:
    bad = _minimal_manifest_dict()
    bad["tools"] = {"allow": ["bi_query", "bi_query"]}
    with pytest.raises(ValidationError, match="duplicate"):
        AgentManifest.model_validate(bad)


def test_extra_fields_rejected() -> None:
    """ConfigDict(extra='forbid') 防 typo 静默通过."""
    bad = _minimal_manifest_dict() | {"unknown_field": "x"}
    with pytest.raises(ValidationError):
        AgentManifest.model_validate(bad)


def test_callable_peers_no_dup() -> None:
    bad = _minimal_manifest_dict() | {"callable_peers": ["market-agent", "market-agent"]}
    with pytest.raises(ValidationError, match="duplicate"):
        AgentManifest.model_validate(bad)


def test_system_append_max_length() -> None:
    bad = _minimal_manifest_dict()
    bad["system"] = {"file": "x.md", "append": "x" * 2001}
    with pytest.raises(ValidationError):
        AgentManifest.model_validate(bad)


def test_examples_max_count() -> None:
    bad = _minimal_manifest_dict() | {"examples": [{"event": f"q{i}", "description": "d"} for i in range(11)]}
    with pytest.raises(ValidationError):
        AgentManifest.model_validate(bad)


# -- Registry: load real compute.yaml --


def test_registry_bootstrap_loads_compute() -> None:
    """实际 load 我们 ship 的 compute.yaml — 端到端 smoke."""
    registry = AgentRegistry.bootstrap()
    assert "compute" in registry
    m = registry.get("compute")
    assert m.name == "compute"
    assert m.model == "analyst"
    # delivery-surface M-C: compute uses report-writer (data discipline stays read-mostly; guarded writing).
    assert m.trust_class == "report-writer"
    assert m.max_iterations == 100
    assert "bi_query" in m.tools.allow
    assert "subagent_return" in m.tools.allow
    # Manifest `skills:` block is deprecated and empty (G3/G6 terminal state; skill awareness uses prompt text).
    assert m.skills.framework == []
    # ★ Editing an existing skill through conversation requires read + save in the same role. Previously compute had only
    # read and lead only save, structurally disconnecting this path (a user actually hit it on 2026-08-27). Assert both together;
    # losing either disconnects the path again.
    assert "skill_read" in m.tools.allow
    assert "skill_save_generated" in m.tools.allow


def test_registry_get_unknown_raises() -> None:
    registry = AgentRegistry.bootstrap()
    with pytest.raises(KeyError):
        registry.get("nonexistent-agent")


def test_registry_list_all() -> None:
    registry = AgentRegistry.bootstrap()
    assert "compute" in registry.list_all()


# -- Registry: error paths via temp dirs --


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_registry_filename_must_match_name(tmp_path: Path) -> None:
    """compute.yaml 文件 name 字段是 'compute' — 改成别的 reject."""
    sysdir = tmp_path / "manifests" / "system"
    sysdir.mkdir(parents=True)
    promptdir = tmp_path / "prompts" / "system"
    promptdir.mkdir(parents=True)
    (promptdir / "x.md").write_text("test", encoding="utf-8")

    data = _minimal_manifest_dict() | {"name": "wrong-name"}
    data["system"] = {"file": "x.md"}
    _write_yaml(sysdir / "compute.yaml", data)

    with pytest.raises(ManifestValidationError, match="filename stem"):
        _bootstrap_with_dirs(sysdir, promptdir)


def test_registry_dup_name_rejected(tmp_path: Path) -> None:
    sysdir = tmp_path / "manifests" / "system"
    sysdir.mkdir(parents=True)
    promptdir = tmp_path / "prompts" / "system"
    promptdir.mkdir(parents=True)
    (promptdir / "x.md").write_text("test", encoding="utf-8")

    # Two files with the same name → filename must equal name, so this naturally requires identical filenames too.
    # YAML filenames differing only in case? No: name regex requires lowercase. Different filenames
    # with identical name fields trigger filename mismatch (already covered by the previous test).
    # A true duplicate is the same name + same YAML filename, which filesystems prohibit.
    # Thus duplicate names arise across system + user sources (added in PR-#2).
    # Skip this test in PR-#1 (one directory cannot contain the same name twice).
    pytest.skip("PR-#1 single dir 不可能 dup name; PR-#2 system+user 跨源测")


def test_registry_tools_allow_deny_overlap(tmp_path: Path) -> None:
    sysdir = tmp_path / "manifests" / "system"
    sysdir.mkdir(parents=True)
    promptdir = tmp_path / "prompts" / "system"
    promptdir.mkdir(parents=True)
    (promptdir / "x.md").write_text("test", encoding="utf-8")

    data = _minimal_manifest_dict() | {"name": "demo-x"}
    data["system"] = {"file": "x.md"}
    data["tools"] = {
        "default_enabled": False,
        "allow": ["bi_query", "bi.delete"],
        "deny": ["bi.delete"],  # Intersects allow
    }
    _write_yaml(sysdir / "demo-x.yaml", data)

    with pytest.raises(ManifestValidationError, match=r"allow.*deny"):
        _bootstrap_with_dirs(sysdir, promptdir)


def test_registry_system_file_missing(tmp_path: Path) -> None:
    sysdir = tmp_path / "manifests" / "system"
    sysdir.mkdir(parents=True)
    promptdir = tmp_path / "prompts" / "system"
    promptdir.mkdir(parents=True)
    # Do not create x.md.
    data = _minimal_manifest_dict() | {"name": "demo-x"}
    data["system"] = {"file": "x.md"}
    _write_yaml(sysdir / "demo-x.yaml", data)

    with pytest.raises(ManifestValidationError, match=r"system\.file"):
        _bootstrap_with_dirs(sysdir, promptdir)


def test_registry_output_schema_must_have_additional_properties_false(
    tmp_path: Path,
) -> None:
    sysdir = tmp_path / "manifests" / "system"
    sysdir.mkdir(parents=True)
    promptdir = tmp_path / "prompts" / "system"
    promptdir.mkdir(parents=True)
    (promptdir / "x.md").write_text("test", encoding="utf-8")

    data = _minimal_manifest_dict() | {"name": "demo-x"}
    data["system"] = {"file": "x.md"}
    # Provide an object schema without additionalProperties=false.
    data["output_schema"] = {
        "type": "object",
        "required": ["title"],
        "properties": {"title": {"type": "string"}},
    }
    _write_yaml(sysdir / "demo-x.yaml", data)

    with pytest.raises(ManifestValidationError, match="additionalProperties"):
        _bootstrap_with_dirs(sysdir, promptdir)


# -- Path traversal / symlink (T-SEC-P0.1) --


def test_validate_path_rejects_traversal(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside.yaml"
    outside.write_text("x", encoding="utf-8")

    with pytest.raises(ManifestSecurityError, match="path traversal"):
        _validate_path(outside, base)


def test_validate_path_rejects_symlink(tmp_path: Path) -> None:
    """T-SEC-P0.1: symlink 必 reject (即使 target 在 base 内也 reject)."""
    base = tmp_path / "base"
    base.mkdir()
    real = base / "real.yaml"
    real.write_text("x", encoding="utf-8")
    link = base / "link.yaml"
    link.symlink_to(real)

    with pytest.raises(ManifestSecurityError, match="symlink"):
        _validate_path(link, base)


def test_validate_path_rejects_missing(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    with pytest.raises(ManifestValidationError):
        _validate_path(base / "missing.yaml", base)


def test_validate_path_accepts_normal(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    real = base / "real.yaml"
    real.write_text("x", encoding="utf-8")
    out = _validate_path(real, base)
    assert out == real.resolve()


def test_system_manifest_rejects_unknown_primary_capability(tmp_path: Path) -> None:
    manifest_dir = tmp_path / "manifests"
    prompt_dir = tmp_path / "prompts"
    manifest_dir.mkdir()
    prompt_dir.mkdir()
    raw = _minimal_manifest_dict()
    raw["primary_capability"] = "cap_not_registered"
    (manifest_dir / "test-agent.yaml").write_text(
        yaml.safe_dump(raw),
        encoding="utf-8",
    )
    (prompt_dir / "test-agent.md").write_text("safe prompt", encoding="utf-8")

    with pytest.raises(ManifestValidationError, match="active Capability Definition"):
        _bootstrap_with_dirs(manifest_dir, prompt_dir)


# -- helpers --


def _bootstrap_with_dirs(system_dir: Path, prompt_dir: Path) -> AgentRegistry:
    """启动 registry 时 patch system + prompt dir (用 tmp_path 测错误路径)."""
    orig_sys = registry_module._SYSTEM_MANIFEST_DIR
    orig_pr = registry_module._SYSTEM_PROMPT_DIR
    registry_module._SYSTEM_MANIFEST_DIR = system_dir
    registry_module._SYSTEM_PROMPT_DIR = prompt_dir
    try:
        return AgentRegistry.bootstrap()
    finally:
        registry_module._SYSTEM_MANIFEST_DIR = orig_sys
        registry_module._SYSTEM_PROMPT_DIR = orig_pr
