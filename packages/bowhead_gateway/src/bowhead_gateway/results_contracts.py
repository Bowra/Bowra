"""Versioned HTTP contracts for the Results read model."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ResultCapability = Literal[
    "read_report",
    "edit_report",
    "finish_editing",
    "export_report",
    "share_report",
    "view_history",
    "clear_report",
    "delete_report",
    "download",
    "reference_in_chat",
    "manage_in_files",
    "view_raw",
    "view_details",
    "copy_code",
    "open_outputs",
    "view_run_details",
    "feedback_unsupported",
]
ResultRelationRole = Literal[
    "report_chart_data",
    "report_detail_table",
    "report_computation",
    "report_embedded_media",
    "code_output",
    "code_input",
    "run_output",
    "technical_output",
    "main_deliverable",
    "ordinary_source",
]
ResultSourceRole = Literal[
    "report",
    "canvas_export",
    "generated",
    "user_upload",
    "data_query",
    "web",
    "sandbox",
    "code",
]
ResultOutputRole = ResultRelationRole


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ReportResultRouteV1(_ContractModel):
    type: Literal["report"]


class CodeResultRouteV1(_ContractModel):
    type: Literal["code"]


class ArtifactResultRouteV1(_ContractModel):
    type: Literal["artifact"]
    artifact_id: str = Field(min_length=1, max_length=128)
    default_mode: Literal["preview", "raw", "details"]


ResultRouteV1 = Annotated[
    ReportResultRouteV1 | CodeResultRouteV1 | ArtifactResultRouteV1,
    Field(discriminator="type"),
]


class ResultExecutionV1(_ContractModel):
    key: str = Field(min_length=1, max_length=256)
    kind: Literal["code_run", "tool_call", "agent_task", "export_job"]
    label: str | None = Field(default=None, max_length=200)
    sequence: int | None = Field(default=None, ge=0)


class ResultDisplayContextV1(_ContractModel):
    source_role: ResultSourceRole
    output_role: ResultOutputRole | None = None
    execution_label: str | None = Field(default=None, max_length=200)
    occurred_at: str | None = None
    duplicate_ordinal: int | None = Field(default=None, ge=1)
    duplicate_count: int | None = Field(default=None, ge=1)


class ResultVersionV1(_ContractModel):
    format: Literal["pdf", "pptx", "xlsx", "docx"]
    available_version_count: int = Field(ge=1)


class ResultArtifactMetadataV1(_ContractModel):
    """Minimum authorized metadata needed to select the existing safe viewer."""

    mime: str = Field(max_length=255)
    size: int = Field(ge=0)
    source: str = Field(max_length=128)
    kind: str = Field(max_length=128)
    origin: Literal["query", "compute", "upload", "export", "web", "deck", "other"]
    trust: str = Field(max_length=128)
    created_at: str
    producer_kind: str | None = Field(default=None, max_length=128)


class ResultItemDTOV1(_ContractModel):
    key: str = Field(min_length=1, max_length=256)
    object_type: Literal["report", "artifact", "code"]
    title: str = Field(min_length=1, max_length=500)
    primary_group: Literal["deliverables", "evidence"]
    secondary_group: Literal["data", "code", "input"] | None
    viewer_profile: Literal[
        "report",
        "html",
        "image",
        "video",
        "table",
        "text",
        "document",
        "unsupported",
        "code",
    ]
    status: Literal["ready", "running", "processing", "failed", "unverified"]
    route: ResultRouteV1
    capabilities: list[ResultCapability]
    relation_roles: list[ResultRelationRole]
    execution: ResultExecutionV1 | None
    display_context: ResultDisplayContextV1
    position: int = Field(ge=1)
    version: ResultVersionV1 | None
    artifact_metadata: ResultArtifactMetadataV1 | None

    @model_validator(mode="after")
    def validate_artifact_metadata(self) -> ResultItemDTOV1:
        is_artifact = self.object_type == "artifact"
        if is_artifact != (self.artifact_metadata is not None):
            raise ValueError("artifact_metadata must exist only for artifact items")
        return self


class ResultsCapabilitiesResponseV1(_ContractModel):
    dto_version: Literal[1] = 1
    workspace: bool
    complete_list: bool
    scale_nav: bool
    interactive_html: bool = False
    max_page_size: Literal[50] = 50


class ResultsFiltersV1(_ContractModel):
    lens: Literal["all", "current_report"] = "all"
    primary: Literal["all", "deliverables", "evidence"] = "all"
    secondary: Literal["all", "data", "code", "input"] = "all"
    technical_only: bool = False


class ResultsQueryRequestV1(_ContractModel):
    dto_version: Literal[1] = 1
    surface: Literal["quick", "explorer", "locate", "changes"]
    query: str = Field(default="", max_length=120)
    filters: ResultsFiltersV1 = Field(default_factory=ResultsFiltersV1)
    page_size: int | None = Field(default=None, ge=1, le=50)
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)
    snapshot_token: str | None = Field(default=None, min_length=1, max_length=4096)
    current_key: str | None = Field(default=None, min_length=1, max_length=256)
    recent_keys: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(
        default_factory=list,
        max_length=3,
    )
    anchor_key: str | None = Field(default=None, min_length=1, max_length=256)


class ResultsEffectiveCapabilityV1(_ContractModel):
    complete_list: Literal[True] = True
    scale_nav: bool


class ResultsProjectionStatusV1(_ContractModel):
    report_relations: Literal["ready", "unavailable"]
    execution_relations: Literal["ready", "unavailable"]
    export_relations: Literal["ready", "degraded"]


class ResultsNewResultsV1(_ContractModel):
    count: int | None = Field(default=None, ge=0)
    projection_changed: bool


class ResultsGroupCountsV1(_ContractModel):
    deliverables: int = Field(ge=0)
    evidence: int = Field(ge=0)
    data: int = Field(ge=0)
    code: int = Field(ge=0)
    input: int = Field(ge=0)


class ResultsQueryResponseV1(_ContractModel):
    dto_version: Literal[1] = 1
    capability: ResultsEffectiveCapabilityV1
    snapshot_token: str
    items: list[ResultItemDTOV1]
    authorized_total: int = Field(ge=0)
    matched_total: int = Field(ge=0)
    group_counts: ResultsGroupCountsV1
    loaded_count: int = Field(ge=0)
    page_start: int = Field(ge=0)
    next_cursor: str | None
    previous_cursor: str | None
    has_more: bool
    projection_status: ResultsProjectionStatusV1
    projection_revision: str = Field(min_length=1, max_length=128)
    new_results: ResultsNewResultsV1
    resolved_current_key: str | None = Field(default=None, max_length=256)


__all__ = [
    "ArtifactResultRouteV1",
    "CodeResultRouteV1",
    "ReportResultRouteV1",
    "ResultCapability",
    "ResultArtifactMetadataV1",
    "ResultDisplayContextV1",
    "ResultExecutionV1",
    "ResultItemDTOV1",
    "ResultOutputRole",
    "ResultRelationRole",
    "ResultRouteV1",
    "ResultSourceRole",
    "ResultsCapabilitiesResponseV1",
    "ResultsEffectiveCapabilityV1",
    "ResultsFiltersV1",
    "ResultsGroupCountsV1",
    "ResultsNewResultsV1",
    "ResultsProjectionStatusV1",
    "ResultsQueryRequestV1",
    "ResultsQueryResponseV1",
    "ResultVersionV1",
]
