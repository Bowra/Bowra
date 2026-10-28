"""Renderer-independent Canvas export manifest and parity validation."""

from __future__ import annotations

from collections import Counter
from typing import Any

from bowhead_core.canvas_export_projection import SURFACE_KIND_POLICIES
from bowhead_core.error_catalog import CodedError
from bowhead_core.export_capabilities import ExportTarget
from bowhead_core.export_copy_contract import TRUST_COPY_CONTRACT
from bowhead_core.export_ir import (
    CapabilityPlanEntry,
    ExportIR,
    ExportIRBlock,
    build_capability_plan,
    iter_export_blocks,
)
from bowhead_core.export_manifest import (
    EXPORT_MANIFEST_VERSION,
    ExportManifest,
    ExtractedOutputEvidence,
    ExtractedSectionEvidence,
    ManifestDisposition,
    ManifestParity,
    ManifestParityResult,
    ManifestQuality,
    ManifestReasonCode,
    ManifestRenderer,
    ManifestSection,
    ManifestSummary,
    ManifestValidation,
    ManifestValidationCheck,
    OutputTargetRef,
)
from bowhead_core.export_snapshot import ExportSnapshot

_SUMMARY_EXCLUSION_REASONS: frozenset[ManifestReasonCode] = frozenset(
    {
        "summary_detail_omitted",
        "summary_visual_limit",
    }
)
_FULL_TARGETS: frozenset[ExportTarget] = frozenset(
    {
        "pdf_full",
        "pptx_full",
        "xlsx_data",
    }
)


class ExportValidationError(CodedError):
    """The immutable snapshot, IR, or validator call is inconsistent."""


def validate_export_output(
    *,
    job_id: str,
    snapshot: ExportSnapshot,
    export_ir: ExportIR,
    target: ExportTarget,
    include_appendix: bool,
    renderer_name: str,
    renderer_version: str,
    evidence: ExtractedOutputEvidence,
) -> ExportManifest:
    """Build a public manifest from independently extracted output evidence."""
    blocks = iter_export_blocks(export_ir.blocks)
    _validate_inputs(snapshot=snapshot, export_ir=export_ir, blocks=blocks)
    plan = build_capability_plan(
        export_ir,
        target=target,
        include_appendix=include_appendix,
    )
    plan_by_key = {entry.block_key: entry for entry in plan}
    if len(plan_by_key) != len(plan):
        raise ExportValidationError("duplicate_ir_block_key")

    observations, duplicate_count = _index_observations(evidence.sections)
    expected_keys = {block.block_key for block in blocks}
    unexpected_count = evidence.unexpected_business_section_count + duplicate_count + sum(1 for key in observations if key not in expected_keys)
    failed_checks: set[str] = set(evidence.hard_check_failures)
    if not evidence.mime_magic_valid:
        failed_checks.add("output_mime_magic_invalid")
    if not evidence.parser_opened:
        failed_checks.add("output_parser_failed")
    if evidence.size_bytes <= 0:
        failed_checks.add("output_empty")
    if evidence.demo_residue_count:
        failed_checks.add("demo_residue")
    if evidence.quarantined_section_count:
        failed_checks.add("quarantined_content")
    if unexpected_count:
        failed_checks.add("unexpected_extra_sections")
    if duplicate_count:
        failed_checks.add("duplicate_output_section")

    surface_entries = _surface_entries(snapshot)
    manifest_sections: list[ManifestSection] = []
    semantic_required = 0
    semantic_matched = 0
    visual_required = 0
    visual_matched = 0
    typed_required = 0
    typed_matched = 0

    for block in blocks:
        entry = plan_by_key[block.block_key]
        observation = observations.get(block.block_key)
        surface_entry = surface_entries.get(block.source.section_id)
        if surface_entry is None:
            raise ExportValidationError("export_ir_surface_mismatch")
        section, section_failures = _validate_section(
            block=block,
            plan=entry,
            observation=observation,
            surface_entry=surface_entry,
            full_target=target in _FULL_TARGETS,
        )
        failed_checks.update(section_failures)
        manifest_sections.append(section)
        section_matched = section.parity_result == "matched"
        if section.disposition != "excluded_by_policy" and section.semantic_content_sha256 is not None:
            semantic_required += 1
            semantic_matched += int(section_matched and section.observed_semantic_content_sha256 == section.semantic_content_sha256)
        if section.disposition != "excluded_by_policy" and section.visual_input_sha256 is not None:
            visual_required += 1
            visual_matched += int(section_matched and section.observed_visual_input_sha256 == section.visual_input_sha256)
        if section.disposition != "excluded_by_policy" and section.typed_data_sha256 is not None:
            typed_required += 1
            typed_matched += int(section_matched and section.observed_typed_data_sha256 == section.typed_data_sha256)

    counts = Counter(section.disposition for section in manifest_sections)
    if counts["failed"]:
        failed_checks.add("failed_section")
    if semantic_matched != semantic_required:
        failed_checks.add("semantic_parity_mismatch")
    if visual_matched != visual_required:
        failed_checks.add("visual_identity_mismatch")
    if typed_matched != typed_required:
        failed_checks.add("typed_data_parity_mismatch")

    quality = _quality(
        failed_checks=failed_checks,
        sections=manifest_sections,
        output_warnings=evidence.warnings,
    )
    input_sections = len(manifest_sections)
    eligible = input_sections - counts["excluded_by_policy"]
    client_receipt = _client_surface_receipt(snapshot)
    projection_exclusions = _projection_exclusions(snapshot)
    manifest = ExportManifest(
        manifest_version=EXPORT_MANIFEST_VERSION,
        trust_copy_contract=TRUST_COPY_CONTRACT,
        job_id=job_id,
        snapshot_id=snapshot.id,
        input_sha256=snapshot.payload_sha256,
        format=snapshot.fmt,
        profile=snapshot.profile,
        renderer=ManifestRenderer(
            name=renderer_name,
            version=renderer_version,
        ),
        summary=ManifestSummary(
            input_sections=input_sections,
            accounted_sections=len(manifest_sections),
            rendered=counts["rendered"],
            fallback=counts["fallback"],
            excluded_by_policy=counts["excluded_by_policy"],
            failed=counts["failed"],
            accounted_rate=1.0,
            rendered_rate=((counts["rendered"] + counts["fallback"]) / eligible if eligible else None),
            semantic_parity_rate=_rate(semantic_matched, semantic_required),
            visual_identity_rate=_rate(visual_matched, visual_required),
            typed_data_parity_rate=_rate(typed_matched, typed_required),
            unexpected_extra_sections=unexpected_count,
            demo_residue_count=evidence.demo_residue_count,
        ),
        parity=ManifestParity(
            contract=snapshot.parity_contract,
            surface_contract_version=snapshot.surface_contract_version,
            surface_token=snapshot.surface_token,
            client_surface_receipt_sha256=client_receipt,
            semantic_required=semantic_required,
            semantic_matched=semantic_matched,
            visual_required=visual_required,
            visual_matched=visual_matched,
            typed_data_required=typed_required,
            typed_data_matched=typed_matched,
        ),
        projection_exclusions=projection_exclusions,
        sections=tuple(manifest_sections),
        validation=ManifestValidation(
            result=quality,
            checks=tuple(ManifestValidationCheck(code=code) for code in sorted(failed_checks)),
        ),
    )
    return manifest


def _validate_inputs(
    *,
    snapshot: ExportSnapshot,
    export_ir: ExportIR,
    blocks: tuple[ExportIRBlock, ...],
) -> None:
    if export_ir.snapshot_id != snapshot.id or export_ir.input_sha256 != snapshot.payload_sha256 or export_ir.surface_token != snapshot.surface_token:
        raise ExportValidationError("export_ir_snapshot_mismatch")
    # Snapshot section_count includes nested native views; IR source IDs identify
    # their owning Canvas sections. Compare those exact owners with the surface,
    # while normalization validates nested counts and each block keeps its parity checks.
    if {block.source.section_id for block in blocks} != set(_surface_entries(snapshot)):
        raise ExportValidationError("export_ir_section_accounting_mismatch")


def _index_observations(
    observations: tuple[ExtractedSectionEvidence, ...],
) -> tuple[dict[str, ExtractedSectionEvidence], int]:
    indexed: dict[str, ExtractedSectionEvidence] = {}
    duplicates = 0
    for observation in observations:
        if observation.block_key in indexed:
            duplicates += 1
            continue
        indexed[observation.block_key] = observation
    return indexed, duplicates


def _validate_section(
    *,
    block: ExportIRBlock,
    plan: CapabilityPlanEntry,
    observation: ExtractedSectionEvidence | None,
    surface_entry: dict[str, Any],
    full_target: bool,
) -> tuple[ManifestSection, set[str]]:
    failures: set[str] = set()
    if block.source.surface_placement != surface_entry.get("placement"):
        failures.add("export_ir_surface_placement_mismatch")
    expected_disposition, expected_reason = _expected_disposition(plan)
    if full_target and expected_disposition == "excluded_by_policy":
        failures.add("full_profile_exclusion")

    disposition: ManifestDisposition
    reason_code: ManifestReasonCode | None
    targets: tuple[OutputTargetRef, ...]
    observed_semantic: str | None
    observed_typed: str | None
    observed_visual: str | None
    observed_sources: tuple[str, ...]
    observed_artifacts: dict[str, str]
    observation_warnings: tuple[str, ...]
    if observation is None and expected_disposition == "excluded_by_policy":
        disposition = expected_disposition
        reason_code = expected_reason
        targets = ()
        observed_semantic = None
        observed_typed = None
        observed_visual = None
        observed_sources = ()
        observed_artifacts = {}
        observation_warnings = ()
    elif observation is None:
        disposition = "failed"
        reason_code = "renderer_failed"
        targets = ()
        observed_semantic = None
        observed_typed = None
        observed_visual = None
        observed_sources = ()
        observed_artifacts = {}
        observation_warnings = ()
        failures.add("missing_output_section")
    else:
        disposition = observation.disposition
        reason_code = observation.reason_code
        targets = observation.targets
        observed_semantic = observation.semantic_content_sha256
        observed_typed = observation.typed_data_sha256
        observed_visual = observation.visual_input_sha256
        observed_sources = observation.source_refs
        observed_artifacts = dict(observation.visual_artifact_hashes)
        observation_warnings = observation.warnings
        failures.update(
            _validate_observation_policy(
                plan=plan,
                observation=observation,
                expected_disposition=expected_disposition,
                expected_reason=expected_reason,
            )
        )

    expected_semantic = block.semantic_content_sha256 if _semantic_required(block) else None
    expected_typed = block.typed_data_sha256
    expected_visual = block.visual_input_sha256
    parity_active = disposition != "excluded_by_policy"
    all_expected_artifacts = _visual_artifact_hashes(block)
    expected_artifacts = all_expected_artifacts if disposition != "excluded_by_policy" else {}
    if parity_active and expected_semantic is not None and observed_semantic != expected_semantic:
        failures.add("section_semantic_mismatch")
    if parity_active and expected_typed is not None and observed_typed != expected_typed:
        failures.add("section_typed_data_mismatch")
    if parity_active and expected_visual is not None and observed_visual != expected_visual:
        failures.add("section_visual_identity_mismatch")
    if expected_artifacts and observed_artifacts != expected_artifacts:
        failures.add("section_visual_asset_mismatch")
    if parity_active and tuple(sorted(observed_sources)) != block.source_refs:
        failures.add("section_source_refs_mismatch")

    required_count = sum(parity_active and digest is not None for digest in (expected_semantic, expected_typed, expected_visual))
    matched_count = sum(
        parity_active and expected is not None and expected == observed
        for expected, observed in (
            (expected_semantic, observed_semantic),
            (expected_typed, observed_typed),
            (expected_visual, observed_visual),
        )
    )
    if failures:
        parity_result: ManifestParityResult = "mismatched"
    elif required_count:
        parity_result = "matched" if matched_count == required_count else "mismatched"
    else:
        parity_result = "not_required"

    visual_artifact_ids = tuple(sorted(all_expected_artifacts))
    return (
        ManifestSection(
            block_key=block.block_key,
            section_id=block.source.section_id,
            section_rev=block.source.section_rev,
            section_kind=block.source.section_kind,
            section_path=block.source.section_path,
            surface_placement=str(surface_entry["placement"]),
            parity_requirement=_parity_requirement(block),
            parity_result=parity_result,
            disposition=disposition,
            reason_code=reason_code,
            targets=targets,
            visual_artifact_ids=visual_artifact_ids,
            visual_input_sha256=expected_visual,
            observed_visual_input_sha256=observed_visual,
            semantic_content_sha256=expected_semantic,
            observed_semantic_content_sha256=observed_semantic,
            typed_data_sha256=expected_typed,
            observed_typed_data_sha256=observed_typed,
            source_refs=block.source_refs,
            warnings=tuple(sorted(set(block.warnings) | set(observation_warnings))),
        ),
        failures,
    )


def _expected_disposition(
    plan: CapabilityPlanEntry,
) -> tuple[ManifestDisposition | None, ManifestReasonCode | None]:
    if plan.action == "render" or plan.action == "appendix":
        return "rendered", None
    if plan.action == "fallback":
        reason: ManifestReasonCode = "interactive_not_executable" if plan.reason_code == "interaction_fallback" else "unsupported_kind_fallback"
        return "fallback", reason
    if plan.action == "exclude":
        return "excluded_by_policy", "summary_detail_omitted"
    return None, None


def _validate_observation_policy(
    *,
    plan: CapabilityPlanEntry,
    observation: ExtractedSectionEvidence,
    expected_disposition: ManifestDisposition | None,
    expected_reason: ManifestReasonCode | None,
) -> set[str]:
    failures: set[str] = set()
    if plan.action == "summary_candidate":
        if observation.disposition == "excluded_by_policy":
            if observation.reason_code not in _SUMMARY_EXCLUSION_REASONS:
                failures.add("invalid_summary_exclusion")
        elif observation.disposition != "rendered":
            failures.add("invalid_summary_disposition")
    elif observation.disposition != expected_disposition or observation.reason_code != expected_reason:
        failures.add("capability_disposition_mismatch")

    if observation.disposition in {"rendered", "fallback"}:
        if not observation.visible or not observation.targets:
            failures.add("output_target_not_visible")
    elif observation.visible or observation.targets:
        failures.add("excluded_output_has_target")
    if plan.action == "appendix" and not any(target.kind in {"appendix", "notes"} or target.region == "appendix" for target in observation.targets):
        failures.add("appendix_target_missing")
    if plan.action == "fallback" and not any(target.kind == "interaction_fallback" for target in observation.targets):
        failures.add("interaction_fallback_target_missing")
    return failures


def _semantic_required(block: ExportIRBlock) -> bool:
    parity = _parity_requirement(block)
    return parity in {"semantic_content", "interaction_fallback"}


def _parity_requirement(block: ExportIRBlock) -> str:
    policy = SURFACE_KIND_POLICIES.get(block.source.section_kind)
    return policy.parity_requirement if policy is not None else "semantic_content"


def _visual_artifact_hashes(block: ExportIRBlock) -> dict[str, str]:
    raw = block.content.get("artifact_content_hashes")
    if not isinstance(raw, dict):
        return {}
    return {
        str(artifact_id): str(content_hash)
        for artifact_id, content_hash in raw.items()
        if isinstance(artifact_id, str) and isinstance(content_hash, str)
    }


def _surface_entries(snapshot: ExportSnapshot) -> dict[str, dict[str, Any]]:
    raw_surface = snapshot.payload.get("surface_projection")
    if not isinstance(raw_surface, dict):
        raise ExportValidationError("invalid_snapshot_surface")
    raw_entries = raw_surface.get("entries")
    if not isinstance(raw_entries, list):
        raise ExportValidationError("invalid_snapshot_surface")
    entries: dict[str, dict[str, Any]] = {}
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise ExportValidationError("invalid_snapshot_surface")
        section_id = raw_entry.get("section_id")
        if not isinstance(section_id, str) or section_id in entries:
            raise ExportValidationError("invalid_snapshot_surface")
        entries[section_id] = raw_entry
    return entries


def _projection_exclusions(
    snapshot: ExportSnapshot,
) -> tuple[dict[str, str], ...]:
    raw_surface = snapshot.payload.get("surface_projection")
    if not isinstance(raw_surface, dict):
        raise ExportValidationError("invalid_snapshot_surface")
    raw_exclusions = raw_surface.get("exclusions", [])
    if not isinstance(raw_exclusions, list):
        raise ExportValidationError("invalid_snapshot_surface")
    exclusions: list[dict[str, str]] = []
    for item in raw_exclusions:
        if not isinstance(item, dict):
            raise ExportValidationError("invalid_snapshot_surface")
        section_id = item.get("section_id")
        reason = item.get("reason")
        if not isinstance(section_id, str) or not isinstance(reason, str):
            raise ExportValidationError("invalid_snapshot_surface")
        exclusions.append({"section_id": section_id, "reason": reason})
    return tuple(exclusions)


def _client_surface_receipt(snapshot: ExportSnapshot) -> str:
    request = snapshot.payload.get("request")
    if not isinstance(request, dict):
        raise ExportValidationError("invalid_snapshot_request")
    receipt = request.get("client_surface_receipt_sha256")
    if not isinstance(receipt, str) or not receipt:
        raise ExportValidationError("invalid_snapshot_request")
    return receipt


def _quality(
    *,
    failed_checks: set[str],
    sections: list[ManifestSection],
    output_warnings: tuple[str, ...],
) -> ManifestQuality:
    if failed_checks:
        return "rejected"
    if (
        output_warnings
        or any(section.warnings for section in sections)
        or any(section.disposition in {"fallback", "excluded_by_policy"} for section in sections)
    ):
        return "warnings"
    return "clean"


def _rate(matched: int, required: int) -> float | None:
    return matched / required if required else None


__all__ = [
    "ExportValidationError",
    "validate_export_output",
]
