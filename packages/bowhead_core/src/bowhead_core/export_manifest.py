"""Public Canvas export manifest and extractor evidence contracts.

The public manifest contains content-free renderer and placement identities.
Source linkage is reduced to irreversible ``source-ordinal:N`` tokens; source
labels, canonical resource IDs, queries, previews, signed URLs, report body
text, SQL, row data and filesystem paths are excluded.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

EXPORT_MANIFEST_VERSION = 2

ManifestDisposition = Literal[
    "rendered",
    "fallback",
    "excluded_by_policy",
    "failed",
]
ManifestParityResult = Literal[
    "matched",
    "not_required",
    "mismatched",
]
ManifestQuality = Literal["clean", "warnings", "rejected"]
ManifestReasonCode = Literal[
    "interactive_not_executable",
    "summary_detail_omitted",
    "summary_visual_limit",
    "profile_no_visual_parity",
    "unsupported_kind_fallback",
    "asset_missing_or_forbidden",
    "visual_materialization_failed",
    "table_row_limit",
    "source_unresolved",
    "template_incompatible",
    "renderer_failed",
]
OutputTargetKind = Literal[
    "page",
    "slide",
    "sheet",
    "notes",
    "appendix",
    "interaction_fallback",
]


@dataclass(frozen=True, slots=True)
class OutputTargetRef:
    kind: OutputTargetKind
    id: str
    region: str


@dataclass(frozen=True, slots=True)
class ExtractedSectionEvidence:
    """Evidence produced by reopening the completed output.

    Renderers must not construct this from their in-memory write plan. A
    format-specific extractor creates it from the closed staged file.
    """

    block_key: str
    disposition: ManifestDisposition
    visible: bool
    targets: tuple[OutputTargetRef, ...]
    semantic_content_sha256: str | None = None
    typed_data_sha256: str | None = None
    visual_input_sha256: str | None = None
    visual_artifact_hashes: tuple[tuple[str, str], ...] = ()
    source_refs: tuple[str, ...] = ()
    reason_code: ManifestReasonCode | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtractedOutputEvidence:
    """Bounded, content-free evidence returned by a format validator."""

    mime_magic_valid: bool
    parser_opened: bool
    size_bytes: int
    sections: tuple[ExtractedSectionEvidence, ...]
    unexpected_business_section_count: int = 0
    demo_residue_count: int = 0
    quarantined_section_count: int = 0
    hard_check_failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ManifestRenderer:
    name: str
    version: str


@dataclass(frozen=True, slots=True)
class ManifestSummary:
    input_sections: int
    accounted_sections: int
    rendered: int
    fallback: int
    excluded_by_policy: int
    failed: int
    accounted_rate: float
    rendered_rate: float | None
    semantic_parity_rate: float | None
    visual_identity_rate: float | None
    typed_data_parity_rate: float | None
    unexpected_extra_sections: int
    demo_residue_count: int


@dataclass(frozen=True, slots=True)
class ManifestParity:
    contract: str
    surface_contract_version: int
    surface_token: str
    client_surface_receipt_sha256: str
    semantic_required: int
    semantic_matched: int
    visual_required: int
    visual_matched: int
    typed_data_required: int
    typed_data_matched: int


@dataclass(frozen=True, slots=True)
class ManifestSection:
    block_key: str
    section_id: str
    section_rev: int
    section_kind: str
    section_path: tuple[int, ...]
    surface_placement: str
    parity_requirement: str
    parity_result: ManifestParityResult
    disposition: ManifestDisposition
    reason_code: ManifestReasonCode | None
    targets: tuple[OutputTargetRef, ...]
    visual_artifact_ids: tuple[str, ...]
    visual_input_sha256: str | None
    observed_visual_input_sha256: str | None
    semantic_content_sha256: str | None
    observed_semantic_content_sha256: str | None
    typed_data_sha256: str | None
    observed_typed_data_sha256: str | None
    source_refs: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManifestValidationCheck:
    code: str


@dataclass(frozen=True, slots=True)
class ManifestValidation:
    result: ManifestQuality
    checks: tuple[ManifestValidationCheck, ...]


@dataclass(frozen=True, slots=True)
class ExportManifest:
    manifest_version: int
    trust_copy_contract: str
    job_id: str
    snapshot_id: str
    input_sha256: str
    format: str
    profile: str
    renderer: ManifestRenderer
    summary: ManifestSummary
    parity: ManifestParity
    projection_exclusions: tuple[dict[str, str], ...]
    sections: tuple[ManifestSection, ...]
    validation: ManifestValidation

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "EXPORT_MANIFEST_VERSION",
    "ExportManifest",
    "ExtractedOutputEvidence",
    "ExtractedSectionEvidence",
    "ManifestDisposition",
    "ManifestParity",
    "ManifestParityResult",
    "ManifestQuality",
    "ManifestReasonCode",
    "ManifestRenderer",
    "ManifestSection",
    "ManifestSummary",
    "ManifestValidation",
    "ManifestValidationCheck",
    "OutputTargetKind",
    "OutputTargetRef",
]
