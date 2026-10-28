from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from bowhead_core.agent_artifact_capability import (
    ArtifactCapabilityRegistry,
    ArtifactGenerationAttempt,
    ArtifactGenerationGrant,
    ArtifactWriteAttempt,
    ArtifactWriteGrant,
    CanvasArtifactFacade,
    ExactComponentRef,
    artifact_generation_denial,
    artifact_write_denial,
)
from bowhead_core.agent_capability_catalog import CapabilityCatalog, PromptModuleRef
from bowhead_core.agents.exceptions import ManifestValidationError


def test_registry_validates_exact_report_contracts() -> None:
    registry = ArtifactCapabilityRegistry.bootstrap()
    catalog = CapabilityCatalog.bootstrap()

    registry.validate_capability_catalog(catalog)

    assert [item.exact_ref for item in registry.adapters] == ["canvas-report@1"]
    # The list changes as Prompt Modules are added/removed; assert **membership**, not length:
    # builtin-capability-composition added three modules for data fetching / delivery / body.
    assert {item.exact_ref for item in registry.prompt_modules} == {
        "canvas-report-protocol@1",
        "canvas-delivery-protocol@1",
        "report-authoring-protocol@1",
        "data-query-datarc-protocol@1",
        "sandbox-io-protocol@1",
    }
    assert [item.exact_ref for item in registry.grant_policies] == ["report-writer-grant@1"]
    assert registry.fingerprint.startswith("sha256:")


def test_registry_rejects_tampered_prompt_module(tmp_path: Path) -> None:
    source_root = Path(__file__).parents[1] / "src" / "bowhead_core" / "agents"
    manifest_dir = tmp_path / "manifests"
    prompt_dir = tmp_path / "prompts"
    manifest_dir.mkdir()
    prompt_dir.mkdir()
    manifest = source_root / "manifests" / "artifacts" / "catalog-v1.yaml"
    (manifest_dir / manifest.name).write_bytes(manifest.read_bytes())
    # Copy every module: a missing one yields load failure rather than digest mismatch, testing a different issue.
    # Corrupt only one of them.
    for prompt in sorted((source_root / "prompts" / "capabilities").glob("*.md")):
        raw = prompt.read_bytes()
        if prompt.name == "canvas-report-protocol-v1.md":
            raw += b"\ntampered"
        (prompt_dir / prompt.name).write_bytes(raw)

    with pytest.raises(ManifestValidationError, match="digest mismatch"):
        ArtifactCapabilityRegistry.bootstrap(manifest_dir, prompt_dir)


def test_prompt_bundle_is_deduplicated_ordered_and_budgeted() -> None:
    registry = ArtifactCapabilityRegistry.bootstrap()
    # Select by explicit name, not index: new modules reorder the list (this once caused a false failure).
    module = next(item for item in registry.prompt_modules if item.id == "canvas-report-protocol")
    ref = PromptModuleRef(id=module.id, version=module.version, sha256=module.sha256)

    compiled = registry.compile_prompt_bundle(
        (ref, ref),
        max_tokens=1000,
        token_counter=lambda value: len(value.split()),
    )

    assert [item.exact_ref for item in compiled.modules] == ["canvas-report-protocol@1"]
    assert compiled.content.startswith("# Canvas report protocol")
    with pytest.raises(ManifestValidationError, match="token budget exceeded"):
        registry.compile_prompt_bundle((ref,), max_tokens=1, token_counter=lambda value: len(value.split()))
    with pytest.raises(ManifestValidationError, match="token counter returned an invalid value"):
        registry.compile_prompt_bundle((ref,), max_tokens=1000, token_counter=lambda value: -1)


def test_prompt_bundle_rejects_declared_conflicts(tmp_path: Path) -> None:
    manifest_dir = tmp_path / "manifests"
    prompt_dir = tmp_path / "prompts"
    manifest_dir.mkdir()
    prompt_dir.mkdir()
    modules = []
    for module_id, content, conflict in (("module-a", "first", "module-b"), ("module-b", "second", None)):
        path = f"{module_id}.md"
        raw = content.encode()
        (prompt_dir / path).write_bytes(raw)
        modules.append(
            {
                "id": module_id,
                "version": 1,
                "path": path,
                "sha256": f"sha256:{hashlib.sha256(raw).hexdigest()}",
                "priority": 100,
                "conflicts_with": [conflict] if conflict else [],
            },
        )
    (manifest_dir / "catalog.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "prompt_modules": modules,
                "adapters": [],
                "grant_policies": [],
            },
        ),
        encoding="utf-8",
    )
    registry = ArtifactCapabilityRegistry.bootstrap(manifest_dir, prompt_dir)
    refs = tuple(PromptModuleRef(id=item.id, version=item.version, sha256=item.sha256) for item in registry.prompt_modules)

    with pytest.raises(ManifestValidationError, match="Prompt Module conflict"):
        registry.compile_prompt_bundle(refs, max_tokens=100, token_counter=lambda value: len(value.split()))


def test_canvas_facade_maps_semantics_without_executing_tools() -> None:
    facade = CanvasArtifactFacade(ArtifactCapabilityRegistry.bootstrap())

    patch = facade.plan(ExactComponentRef(id="canvas-report", version=1), "artifact_apply_patch")
    finalize = facade.plan(ExactComponentRef(id="canvas-report", version=1), "artifact_finalize")

    assert patch.delegate_tools == ("write_report",)
    assert patch.grant_operations == ("append_owned", "patch_owned")
    assert patch.requires_expected_revision
    assert finalize.delegate_tools == ()
    assert finalize.fingerprint.startswith("sha256:")


def test_canvas_report_registers_cross_turn_revision() -> None:
    """canvas-update-turn：续轮修订（就地改上一轮正文 / 章标题、收起上一轮的图）登记进 canvas-report@1 与写手 grant。"""
    registry = ArtifactCapabilityRegistry.bootstrap()
    facade = CanvasArtifactFacade(registry)
    revise = facade.plan(ExactComponentRef(id="canvas-report", version=1), "artifact_revise_prior")
    retire = facade.plan(ExactComponentRef(id="canvas-report", version=1), "artifact_retire_asset")

    assert revise.delegate_tools == ("write_report",) and revise.grant_operations == ("patch_prior",) and revise.requires_expected_revision
    assert retire.delegate_tools == ("remove_chart",) and retire.grant_operations == ("retire_prior_asset",)
    assert not retire.requires_expected_revision  # remove_chart 按图 id 软折叠，不带 rev
    policy = registry.get_grant_policy("report-writer-grant", 1)
    assert {"patch_prior", "retire_prior_asset"} <= set(policy.allowed_operations) and "heading" in policy.allowed_section_kinds
    protocol = next(item for item in registry.prompt_modules if item.id == "canvas-report-protocol")
    assert "remove_chart" in protocol.anchor_tools


def test_grant_missing_a_revise_operation_fails_validation(tmp_path: Path) -> None:
    """负对照：adapter 声明了修订操作、grant 没跟上 ⇒ 加载期就拒（否则 compute 的派发在运行期才炸）。"""
    source_root = Path(__file__).parents[1] / "src" / "bowhead_core" / "agents"
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()
    raw = yaml.safe_load((source_root / "manifests" / "artifacts" / "catalog-v1.yaml").read_text(encoding="utf-8"))
    policy = next(item for item in raw["grant_policies"] if item["id"] == "report-writer-grant")
    policy["allowed_operations"] = [op for op in policy["allowed_operations"] if op != "patch_prior"]
    (manifest_dir / "catalog-v1.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    registry = ArtifactCapabilityRegistry.bootstrap(manifest_dir, source_root / "prompts" / "capabilities")

    with pytest.raises(ManifestValidationError, match="grant operation mismatch"):
        registry.validate_capability_catalog(CapabilityCatalog.bootstrap())


def test_revise_operations_skip_ownership_and_retire_skips_cas() -> None:
    now = datetime.now(UTC)
    grant = replace(
        _write_grant(now),
        allowed_operations=("patch_owned", "patch_prior", "retire_prior_asset"),
        allowed_section_kinds=("analysis", "chart", "heading"),
    )
    attempt = _write_attempt(now)
    # 上一轮别的写手的段：patch_owned 拒（不是自己的），patch_prior 放（跨轮修订本就不看 owner）
    assert artifact_write_denial(grant, replace(attempt, owns_target_section=False)) == "artifact_section_ownership_denied"
    assert artifact_write_denial(grant, replace(attempt, operation="patch_prior", owns_target_section=False)) is None
    assert artifact_write_denial(grant, replace(attempt, operation="patch_prior", section_kind="heading", owns_target_section=False)) is None
    # patch_prior 仍要 CAS；收图不带 rev 也放
    assert artifact_write_denial(grant, replace(attempt, operation="patch_prior", expected_revision="stale")) == "artifact_revision_conflict"
    retire = replace(
        attempt, operation="retire_prior_asset", section_kind="chart", owns_target_section=False, expected_revision=None, current_revision=None
    )
    assert artifact_write_denial(grant, retire) is None
    # 「收图」只收图：拿它去折叠正文段照拒（security review P2）
    assert artifact_write_denial(grant, replace(retire, section_kind="analysis")) == "artifact_section_kind_denied"
    # grant 没给的操作照拒
    assert artifact_write_denial(replace(grant, allowed_operations=("patch_owned",)), retire) == "artifact_operation_denied"


def _write_grant(now: datetime) -> ArtifactWriteGrant:
    return ArtifactWriteGrant(
        team_id="team-1",
        user_id="user-1",
        session_id="session-1",
        turn_id="turn-1",
        agent_id="agent-1",
        agent_revision="revision-1",
        execution_id="execution-1",
        capability_execution_id="capx_1",
        canvas_id="canvas-1",
        canvas_generation=2,
        allowed_operations=("append_owned", "patch_owned", "finalize"),
        allowed_section_kinds=("analysis", "summary"),
        max_sections=3,
        max_bytes=1000,
        expires_at=now + timedelta(minutes=5),
    )


def _write_attempt(now: datetime) -> ArtifactWriteAttempt:
    return ArtifactWriteAttempt(
        team_id="team-1",
        user_id="user-1",
        session_id="session-1",
        turn_id="turn-1",
        agent_id="agent-1",
        agent_revision="revision-1",
        execution_id="execution-1",
        capability_execution_id="capx_1",
        canvas_id="canvas-1",
        canvas_generation=2,
        operation="patch_owned",
        section_kind="analysis",
        section_count=1,
        payload_bytes=200,
        owns_target_section=True,
        expected_revision="canvas-rev-2",
        current_revision="canvas-rev-2",
        now=now,
    )


def test_artifact_write_grant_is_exact_scoped_and_cas_bound() -> None:
    now = datetime.now(UTC)
    grant = _write_grant(now)
    attempt = _write_attempt(now)

    assert artifact_write_denial(grant, attempt) is None
    assert (
        artifact_write_denial(
            grant,
            replace(attempt, capability_execution_id="capx_2"),
        )
        == "artifact_grant_scope_mismatch"
    )
    assert artifact_write_denial(grant, replace(attempt, agent_revision="revision-2")) == "artifact_grant_scope_mismatch"
    assert artifact_write_denial(grant, replace(attempt, owns_target_section=False)) == "artifact_section_ownership_denied"
    assert artifact_write_denial(grant, replace(attempt, expected_revision="stale")) == "artifact_revision_conflict"
    assert artifact_write_denial(grant, replace(attempt, payload_bytes=1001)) == "artifact_grant_limit_exceeded"
    assert artifact_write_denial(grant, replace(attempt, now=grant.expires_at)) == "artifact_grant_expired"
    assert artifact_write_denial(grant, replace(attempt, now=now.replace(tzinfo=None))) == "artifact_time_invalid"


def test_artifact_generation_grant_limits_inputs_paths_extensions_and_validators() -> None:
    now = datetime.now(UTC)
    grant = ArtifactGenerationGrant(
        team_id="team-1",
        user_id="user-1",
        turn_id="turn-1",
        agent_id="agent-1",
        agent_revision="revision-1",
        execution_id="execution-1",
        capability_execution_id="capx_1",
        input_artifact_ids=("artifact-1",),
        allowed_extensions=(".pptx",),
        workdir="/sandbox/execution-1",
        validators=("pptx_structure",),
        max_files=1,
        expires_at=now + timedelta(minutes=5),
    )
    attempt = ArtifactGenerationAttempt(
        team_id="team-1",
        user_id="user-1",
        turn_id="turn-1",
        agent_id="agent-1",
        agent_revision="revision-1",
        execution_id="execution-1",
        capability_execution_id="capx_1",
        input_artifact_ids=("artifact-1",),
        output_paths=("/sandbox/execution-1/result.pptx",),
        validators=("pptx_structure",),
        now=now,
    )

    assert artifact_generation_denial(grant, attempt) is None
    assert (
        artifact_generation_denial(
            grant,
            replace(attempt, capability_execution_id="capx_2"),
        )
        == "artifact_generation_scope_mismatch"
    )
    assert artifact_generation_denial(grant, replace(attempt, output_paths=())) == "artifact_generation_file_limit_exceeded"
    assert artifact_generation_denial(grant, replace(attempt, output_paths=("/sandbox/other/result.pptx",))) == "artifact_generation_path_denied"
    assert (
        artifact_generation_denial(
            grant,
            replace(attempt, output_paths=("/sandbox/execution-1/../escape.pptx",)),
        )
        == "artifact_generation_path_denied"
    )
    assert (
        artifact_generation_denial(grant, replace(attempt, output_paths=("/sandbox/execution-1/result.exe",)))
        == "artifact_generation_extension_denied"
    )
    assert artifact_generation_denial(grant, replace(attempt, validators=())) == "artifact_generation_validator_missing"
