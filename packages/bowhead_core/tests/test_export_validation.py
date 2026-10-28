"""Manifest v1 accounting and false-clean rejection contracts."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

from bowhead_core.canvas_export_projection import SURFACE_KIND_POLICIES
from bowhead_core.export_capabilities import ExportTarget
from bowhead_core.export_ir import (
    ExportIR,
    ExportIRBlock,
    build_capability_plan,
    iter_export_blocks,
    normalize_export_snapshot,
)
from bowhead_core.export_manifest import (
    ExportManifest,
    ExtractedOutputEvidence,
    ExtractedSectionEvidence,
    ManifestReasonCode,
    OutputTargetKind,
    OutputTargetRef,
)
from bowhead_core.export_snapshot import ExportSnapshot
from bowhead_core.export_validation import validate_export_output
from bowhead_core.export_visual_identity import EXPORT_VISUAL_RENDERER_VERSION
from bowhead_core.print_theme import current_print_theme_snapshot

_FIXTURE = Path(__file__).resolve().parents[3] / "eval/fixtures/canvas_export/surface_projection_v1.json"
_ASSET_HASHES = {
    "art_image": f"sha256:{'1' * 64}",
    "art_bg": f"sha256:{'2' * 64}",
}


def _fixture_snapshot() -> ExportSnapshot:
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    by_id = {section["id"]: section for section in fixture["sections"]}
    entries = copy.deepcopy(fixture["expected_entries"])
    sections = [copy.deepcopy(by_id[entry["section_id"]]) for entry in entries]
    for section in sections:
        section.setdefault("source_refs", [])
    by_section = {section["id"]: section for section in sections}
    by_section["sec_h1"]["source_refs"] = ["source-ordinal:1"]
    payload = {
        "request": {
            "format": "pdf",
            "profile": "full_report",
            "parity_contract": "full_content_v1",
            "client_surface_receipt_sha256": f"sha256:{'d' * 64}",
            "locale": "zh-CN",
            "timezone": "Asia/Singapore",
            "theme": {"kind": "default"},
            "print_theme": current_print_theme_snapshot(),
            "options": {"include_appendix": True},
        },
        "surface_projection": {
            "contract_version": 1,
            "token": fixture["surface_token"],
            "entries": entries,
            "exclusions": fixture["expected_exclusions"],
        },
        "sections": sections,
    }
    return ExportSnapshot(
        id="exs_validation",
        schema_version=1,
        canvas_id="cv_validation",
        session_id="s_validation",
        team_id="team_validation",
        workspace_id="ws_validation",
        canvas_owner="u_owner",
        requested_by="u_owner",
        fmt="pdf",
        profile="full_report",
        title="Validation fixture",
        locale="zh-CN",
        timezone="Asia/Singapore",
        projection_token=f"sha256:{'a' * 64}",
        surface_contract_version=1,
        surface_token=fixture["surface_token"],
        parity_contract="full_content_v1",
        payload=payload,
        payload_sha256=f"sha256:{'c' * 64}",
        size_bytes=4096,
        section_count=len(sections),
        created_at="2026-07-17T00:00:00+00:00",
        expires_at="2026-07-24T00:00:00+00:00",
    )


def _normalized() -> tuple[ExportSnapshot, ExportIR]:
    snapshot = _fixture_snapshot()
    return snapshot, normalize_export_snapshot(
        snapshot,
        asset_content_hashes=_ASSET_HASHES,
    )


def _matching_evidence(
    export_ir: ExportIR,
    *,
    target: ExportTarget = "pdf_full",
    include_appendix: bool = True,
    summary_exclude: frozenset[str] = frozenset(),
) -> ExtractedOutputEvidence:
    plan_by_key = {
        item.block_key: item
        for item in build_capability_plan(
            export_ir,
            target=target,
            include_appendix=include_appendix,
        )
    }
    observations: list[ExtractedSectionEvidence] = []
    for block in iter_export_blocks(export_ir.blocks):
        plan = plan_by_key[block.block_key]
        if plan.action == "exclude":
            continue
        if block.block_key in summary_exclude:
            observations.append(
                ExtractedSectionEvidence(
                    block_key=block.block_key,
                    disposition="excluded_by_policy",
                    visible=False,
                    targets=(),
                    reason_code="summary_visual_limit",
                )
            )
            continue
        disposition = "fallback" if plan.action == "fallback" else "rendered"
        reason: ManifestReasonCode | None = "interactive_not_executable" if plan.action == "fallback" else None
        target_kind, region = _target_for(
            target=target,
            action=plan.action,
        )
        semantic = (
            block.semantic_content_sha256
            if SURFACE_KIND_POLICIES[block.source.section_kind].parity_requirement in {"semantic_content", "interaction_fallback"}
            else None
        )
        observations.append(
            ExtractedSectionEvidence(
                block_key=block.block_key,
                disposition=disposition,
                visible=True,
                targets=(
                    OutputTargetRef(
                        kind=target_kind,
                        id=f"target-{block.block_key}",
                        region=region,
                    ),
                ),
                semantic_content_sha256=semantic,
                typed_data_sha256=block.typed_data_sha256,
                visual_input_sha256=block.visual_input_sha256,
                visual_artifact_hashes=tuple(sorted(_block_artifact_hashes(block).items())),
                source_refs=block.source_refs,
                reason_code=reason,
            )
        )
    return ExtractedOutputEvidence(
        mime_magic_valid=True,
        parser_opened=True,
        size_bytes=8192,
        sections=tuple(observations),
    )


def _target_for(
    *,
    target: ExportTarget,
    action: str,
) -> tuple[OutputTargetKind, str]:
    if action == "fallback":
        return "interaction_fallback", "body"
    if action == "appendix":
        return "appendix", "appendix"
    if target == "pdf_full":
        return "page", "body"
    if target.startswith("pptx"):
        return "slide", "body"
    return "sheet", "body"


def _block_artifact_hashes(block: ExportIRBlock) -> dict[str, str]:
    value = block.content.get("artifact_content_hashes")
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if isinstance(key, str) and isinstance(item, str)}


def _validate(
    snapshot: ExportSnapshot,
    export_ir: ExportIR,
    evidence: ExtractedOutputEvidence,
    *,
    target: ExportTarget = "pdf_full",
    include_appendix: bool = True,
) -> ExportManifest:
    return validate_export_output(
        job_id="exp_validation",
        snapshot=snapshot,
        export_ir=export_ir,
        target=target,
        include_appendix=include_appendix,
        renderer_name="test-extractor",
        renderer_version=EXPORT_VISUAL_RENDERER_VERSION,
        evidence=evidence,
    )


def test_full_manifest_accounts_every_block_and_only_warns_for_interaction() -> None:
    snapshot, export_ir = _normalized()
    manifest = _validate(
        snapshot,
        export_ir,
        _matching_evidence(export_ir),
    )

    assert manifest.validation.result == "warnings"
    assert manifest.validation.checks == ()
    assert manifest.summary.input_sections == 22
    assert manifest.summary.accounted_sections == 22
    assert manifest.summary.accounted_rate == 1.0
    assert manifest.summary.rendered == 19
    assert manifest.summary.fallback == 3
    assert manifest.summary.excluded_by_policy == 0
    assert manifest.summary.failed == 0
    assert manifest.summary.rendered_rate == 1.0
    assert manifest.summary.semantic_parity_rate == 1.0
    assert manifest.summary.visual_identity_rate == 1.0
    assert manifest.summary.typed_data_parity_rate == 1.0
    assert manifest.parity.surface_token == snapshot.surface_token
    assert manifest.parity.client_surface_receipt_sha256 == f"sha256:{'d' * 64}"
    assert len(manifest.projection_exclusions) == len(snapshot.payload["surface_projection"]["exclusions"])
    public_json = json.dumps(manifest.to_dict(), ensure_ascii=False)
    assert "source-ordinal:1" in public_json
    assert "tl_h1" not in public_json
    assert all(source_ref.startswith("source-ordinal:") for section in manifest.sections for source_ref in section.source_refs)


def test_missing_section_and_hash_mismatch_cannot_be_false_clean() -> None:
    snapshot, export_ir = _normalized()
    evidence = _matching_evidence(export_ir)
    missing = tuple(item for item in evidence.sections if item.block_key != "sec_h1")
    manifest = _validate(
        snapshot,
        export_ir,
        ExtractedOutputEvidence(
            mime_magic_valid=True,
            parser_opened=True,
            size_bytes=8192,
            sections=missing,
        ),
    )
    assert manifest.validation.result == "rejected"
    assert manifest.summary.failed == 1
    assert manifest.summary.semantic_parity_rate is not None
    assert manifest.summary.semantic_parity_rate < 1.0
    assert {check.code for check in manifest.validation.checks} >= {
        "failed_section",
        "missing_output_section",
        "semantic_parity_mismatch",
    }

    corrupted = tuple(
        replace(item, visual_input_sha256=f"sha256:{'9' * 64}") if item.block_key == "sec_chart" else item for item in evidence.sections
    )
    visual_manifest = _validate(
        snapshot,
        export_ir,
        replace(evidence, sections=corrupted),
    )
    assert visual_manifest.validation.result == "rejected"
    assert visual_manifest.summary.visual_identity_rate is not None
    assert visual_manifest.summary.visual_identity_rate < 1.0
    assert {check.code for check in visual_manifest.validation.checks} >= {
        "section_visual_identity_mismatch",
        "visual_identity_mismatch",
    }


def test_duplicate_extra_demo_and_parser_failures_are_rejected() -> None:
    snapshot, export_ir = _normalized()
    evidence = _matching_evidence(export_ir)
    first = evidence.sections[0]
    broken = replace(
        evidence,
        mime_magic_valid=False,
        parser_opened=False,
        sections=(
            *evidence.sections,
            first,
            replace(first, block_key="sec_unexpected"),
        ),
        unexpected_business_section_count=1,
        demo_residue_count=2,
    )
    manifest = _validate(snapshot, export_ir, broken)

    assert manifest.validation.result == "rejected"
    assert manifest.summary.unexpected_extra_sections == 3
    assert manifest.summary.demo_residue_count == 2
    assert {check.code for check in manifest.validation.checks} >= {
        "demo_residue",
        "duplicate_output_section",
        "output_mime_magic_invalid",
        "output_parser_failed",
        "unexpected_extra_sections",
    }


def test_summary_exclusions_are_explicit_and_removed_from_parity_denominators() -> None:
    snapshot, export_ir = _normalized()
    summary_snapshot = replace(
        snapshot,
        fmt="pptx",
        profile="exec_summary",
        parity_contract="summary_content_v1",
    )
    evidence = _matching_evidence(
        export_ir,
        target="pptx_summary",
        include_appendix=False,
        summary_exclude=frozenset({"sec_chart"}),
    )
    manifest = _validate(
        summary_snapshot,
        export_ir,
        evidence,
        target="pptx_summary",
        include_appendix=False,
    )

    assert manifest.validation.result == "warnings"
    assert manifest.validation.checks == ()
    assert manifest.summary.excluded_by_policy == 8
    assert manifest.summary.failed == 0
    assert manifest.summary.rendered_rate == 1.0
    chart = next(section for section in manifest.sections if section.block_key == "sec_chart")
    assert chart.disposition == "excluded_by_policy"
    assert chart.reason_code == "summary_visual_limit"
    assert chart.visual_input_sha256 is not None


def test_public_manifest_contains_hashes_and_targets_but_not_report_content() -> None:
    snapshot, export_ir = _normalized()
    manifest = _validate(
        snapshot,
        export_ir,
        _matching_evidence(export_ir),
    )
    serialized = json.dumps(
        manifest.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
    )

    assert "Revenue" not in serialized
    assert "Store map" not in serialized
    assert '"rows"' not in serialized
    assert '"payload"' not in serialized
    assert snapshot.payload_sha256 in serialized
    assert "target-sec_chart" in serialized
