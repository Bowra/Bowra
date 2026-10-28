"""Pure Results read-model projection shared by owner and report routes.

This module deliberately knows nothing about FastAPI or authorization.  Routes
must first construct an authorized scope; the projector only turns persisted
metadata into stable, versioned navigation items.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import cmp_to_key
from typing import Literal, Protocol

ResultPrimaryGroup = Literal["deliverables", "evidence"]
ResultSecondaryGroup = Literal["data", "code", "input"] | None
ResultViewerProfile = Literal[
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
ResultStatus = Literal["ready", "running", "processing", "failed", "unverified"]
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

RESULTS_SORT_VERSION_V1 = "results-core-v2-report-relations"

# Fixed public vocabulary only.  Values may be searched, but are never copied
# into response titles or telemetry.  Every public type/source/relation has a
# bilingual entry so adding a new enum cannot silently make it unsearchable.
RESULT_SEARCH_ALIASES_V1: Mapping[str, tuple[str, ...]] = {
    "type:report": ("report", "报告"),
    "type:artifact": ("artifact", "成果", "文件"),
    "type:code": ("code", "代码", "运行"),
    "source:report": ("report", "报告"),
    "source:canvas_export": ("export", "导出", "交付"),
    "source:generated": ("generated", "生成"),
    "source:user_upload": ("upload", "上传", "输入文件"),
    "source:data_query": ("data", "query", "数据", "查询"),
    "source:web": ("web", "网页", "网络"),
    "source:sandbox": ("sandbox", "compute", "沙盒", "计算"),
    "source:code": ("code", "代码", "运行"),
    "role:report_chart_data": ("chart data", "图表数据"),
    "role:report_detail_table": ("detail table", "详情表"),
    "role:report_computation": ("computation", "计算结果"),
    "role:report_embedded_media": ("embedded media", "报告媒体"),
    "role:code_output": ("code output", "代码输出"),
    "role:code_input": ("code input", "代码输入"),
    "role:run_output": ("run output", "运行输出"),
    "role:technical_output": ("technical output", "技术输出"),
    "role:main_deliverable": ("deliverable", "交付物"),
    "role:ordinary_source": ("source", "来源"),
}


class ArtifactLike(Protocol):
    id: str
    filename: str
    mime: str
    size: int
    source: str
    created_at: str
    kind: str
    trust: str
    producer_kind: str | None


@dataclass(frozen=True, slots=True)
class ResultRoute:
    type: Literal["report", "artifact", "code"]
    artifact_id: str | None = None
    default_mode: Literal["preview", "raw", "details"] | None = None


@dataclass(frozen=True, slots=True)
class ResultArtifactMetadata:
    mime: str
    size: int
    source: str
    kind: str
    trust: str
    created_at: str
    producer_kind: str | None


@dataclass(frozen=True, slots=True)
class ProjectedResult:
    key: str
    object_type: Literal["report", "artifact", "code"]
    title: str
    primary_group: ResultPrimaryGroup
    secondary_group: ResultSecondaryGroup
    viewer_profile: ResultViewerProfile
    status: ResultStatus
    route: ResultRoute
    artifact_metadata: ResultArtifactMetadata | None
    capabilities: tuple[ResultCapability, ...]
    relation_roles: tuple[ResultRelationRole, ...]
    source_role: ResultSourceRole
    output_role: ResultRelationRole | None
    occurred_at: str | None
    sort_tuple: tuple[int | str | None, ...]
    position: int = 1


def normalize_results_query(value: str | None) -> str:
    return unicodedata.normalize("NFKC", value or "").strip().casefold()


def results_query_filter_hash(
    *,
    query: str,
    filters: Mapping[str, object],
) -> str:
    payload = json.dumps(
        {"query": query, "filters": dict(filters)},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def results_scope_fingerprint(
    *,
    route_kind: str,
    team_id: str,
    user_id: str,
    scope_id: str,
) -> str:
    value = f"results-scope-v1\0{route_kind}\0{team_id}\0{user_id}\0{scope_id}"
    return hashlib.sha256(value.encode()).hexdigest()


def results_projection_digest(parts: Iterable[object]) -> str:
    payload = json.dumps(
        list(parts),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def artifact_kind(artifact: ArtifactLike) -> str:
    if artifact.kind:
        return artifact.kind
    if artifact.source == "canvas_export":
        return "canvas_export"
    if artifact.source == "upload":
        return "user_upload"
    if artifact.source in {"python_sandbox", "sandbox_output"}:
        return "sandbox_output"
    return "tool_data"


def artifact_source_role(artifact: ArtifactLike) -> ResultSourceRole:
    kind = artifact_kind(artifact)
    if kind == "canvas_export" or artifact.source == "canvas_export":
        return "canvas_export"
    if kind == "user_upload" or artifact.source == "upload":
        return "user_upload"
    if kind == "web_clip":
        return "web"
    if kind == "sandbox_output" or artifact.source in {"python_sandbox", "sandbox_output"}:
        return "sandbox"
    if kind == "tool_data" or artifact.source == "bi_fetch_rows":
        return "data_query"
    return "generated"


def artifact_groups(artifact: ArtifactLike) -> tuple[ResultPrimaryGroup, ResultSecondaryGroup]:
    kind = artifact_kind(artifact)
    if kind in {"deliverable", "canvas_export"}:
        return "deliverables", None
    if kind == "user_upload":
        return "evidence", "input"
    return "evidence", "data"


def artifact_group_ranks(artifact: ArtifactLike) -> tuple[int, int, int]:
    primary, secondary = artifact_groups(artifact)
    primary_rank = 0 if primary == "deliverables" else 1
    main_rank = 1  # Report is rank 0; artifacts remain stable below it.
    secondary_rank = {None: 0, "data": 0, "code": 1, "input": 2}[secondary]
    return primary_rank, main_rank, secondary_rank


def artifact_supports_raw(mime: str, filename: str) -> bool:
    normalized = mime.split(";", 1)[0].strip().lower()
    if normalized.startswith("text/") or normalized.endswith("+json"):
        return True
    if normalized in {
        "application/json",
        "application/x-ndjson",
        "application/jsonl",
        "application/ld+json",
        "application/xhtml+xml",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "application/javascript",
        "application/x-javascript",
        "application/typescript",
        "application/x-python",
    }:
        return True
    return normalized in {"", "application/octet-stream"} and ("." + filename.rsplit(".", 1)[-1].lower() if "." in filename else "") in {
        ".csv",
        ".tsv",
        ".json",
        ".jsonl",
        ".ndjson",
        ".md",
        ".markdown",
        ".txt",
        ".log",
        ".sql",
        ".py",
        ".js",
        ".ts",
        ".css",
        ".xml",
        ".yaml",
        ".yml",
    }


def derive_artifact_viewer_profile(mime: str | None, filename: str = "") -> ResultViewerProfile:
    normalized = (mime or "").split(";", 1)[0].strip().lower()
    if normalized in {"text/html", "application/xhtml+xml"}:
        return "html"
    if normalized == "image/svg+xml":
        return "unsupported"
    if normalized.startswith("image/"):
        return "image"
    if normalized in {"video/mp4", "video/webm", "video/ogg"}:
        return "video"
    if normalized in {"text/csv", "text/tab-separated-values"}:
        return "table"
    if normalized in {
        "application/json",
        "application/ld+json",
        "application/x-ndjson",
        "application/jsonl",
        "text/markdown",
        "text/x-markdown",
    } or normalized.endswith("+json"):
        return "text" if "markdown" in normalized else "table"
    if (
        normalized == "application/pdf"
        or any(token in normalized for token in ("wordprocessingml", "presentationml"))
        or normalized == "application/vnd.ms-powerpoint"
    ):
        return "document"
    if "spreadsheetml" in normalized or normalized == "application/vnd.ms-excel":
        return "table"
    if normalized.startswith("text/") or artifact_supports_raw(normalized, ""):
        return "text"
    if normalized in {"", "application/octet-stream"}:
        suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if suffix in {"csv", "tsv", "json", "jsonl", "ndjson"}:
            return "table"
        if suffix in {"md", "markdown", "txt", "log", "sql", "py", "js", "ts", "css", "xml", "yaml", "yml"}:
            return "text"
    return "unsupported"


def _aliases_for_artifact(
    artifact: ArtifactLike,
    relation_roles: Sequence[ResultRelationRole] = (),
) -> tuple[str, ...]:
    source_role = artifact_source_role(artifact)
    roles = ("main_deliverable",) if artifact_groups(artifact)[0] == "deliverables" else ()
    aliases = [*RESULT_SEARCH_ALIASES_V1["type:artifact"]]
    aliases.extend(RESULT_SEARCH_ALIASES_V1[f"source:{source_role}"])
    for role in (*roles, *relation_roles):
        aliases.extend(RESULT_SEARCH_ALIASES_V1[f"role:{role}"])
    return tuple(aliases)


def artifact_search_blob(artifact: ArtifactLike) -> str:
    return normalize_results_query(" ".join((artifact.filename, *_aliases_for_artifact(artifact))))


def title_search_rank(title: str, query: str, *, aliases: Sequence[str] = ()) -> int | None:
    if not query:
        return 0
    normalized_title = normalize_results_query(title)
    if normalized_title == query:
        return 0
    if normalized_title.startswith(query):
        return 1
    if query in normalized_title:
        return 2
    alias_blob = normalize_results_query(" ".join(aliases))
    if query in alias_blob:
        return 4
    return None


def artifact_search_rank(
    artifact: ArtifactLike,
    query: str,
    *,
    relation_roles: Sequence[ResultRelationRole] = (),
) -> int | None:
    return title_search_rank(
        artifact.filename,
        query,
        aliases=_aliases_for_artifact(artifact, relation_roles),
    )


def relation_alias_matches(
    relation_roles: Sequence[ResultRelationRole],
    query: str,
) -> bool:
    if not query:
        return False
    aliases = [alias for role in relation_roles for alias in RESULT_SEARCH_ALIASES_V1[f"role:{role}"]]
    return query in normalize_results_query(" ".join(aliases))


def artifact_sort_tuple(
    artifact: ArtifactLike,
    query: str,
    *,
    direct_report: bool = False,
    relation_roles: Sequence[ResultRelationRole] = (),
) -> tuple[int | str | None, ...]:
    search_rank = artifact_search_rank(
        artifact,
        query,
        relation_roles=relation_roles,
    )
    if search_rank is None:
        search_rank = 99
    primary_rank, main_rank, secondary_rank = artifact_group_ranks(artifact)
    # Search is relevance-first across groups; the group hierarchy is the
    # default-sort contract only when no query is active.
    if query:
        primary_rank = main_rank = secondary_rank = 0
    return (
        search_rank,
        primary_rank,
        main_rank,
        0 if direct_report else 1,
        secondary_rank,
        artifact.created_at,
        f"artifact:{artifact.id}",
    )


def _artifact_capabilities(
    artifact: ArtifactLike,
    profile: ResultViewerProfile,
    *,
    writable: bool,
    report_only: bool,
) -> tuple[ResultCapability, ...]:
    primary, secondary = artifact_groups(artifact)
    capabilities: list[ResultCapability] = ["download", "view_details"]
    if not report_only and (primary == "deliverables" or secondary == "input" or profile == "unsupported"):
        capabilities.append("manage_in_files")
    if writable and not report_only and profile != "unsupported":
        capabilities.append("reference_in_chat")
    if artifact_supports_raw(artifact.mime or "", artifact.filename):
        capabilities.append("view_raw")
    if profile == "unsupported" and not report_only:
        capabilities.append("feedback_unsupported")
    return tuple(capabilities)


def project_artifact_result(
    artifact: ArtifactLike,
    query: str,
    *,
    writable: bool = True,
    report_only: bool = False,
    relation_roles: tuple[ResultRelationRole, ...] = (),
) -> ProjectedResult:
    primary, secondary = artifact_groups(artifact)
    profile = derive_artifact_viewer_profile(artifact.mime, artifact.filename)
    source_role = artifact_source_role(artifact)
    main_deliverable = primary == "deliverables"
    display_title = (artifact.filename or "未命名成果")[:500]
    return ProjectedResult(
        key=f"artifact:{artifact.id}",
        object_type="artifact",
        title=display_title,
        primary_group=primary,
        secondary_group=secondary,
        viewer_profile=profile,
        status="unverified" if artifact.trust == "unverified" else "ready",
        route=ResultRoute(type="artifact", artifact_id=artifact.id, default_mode=("details" if profile == "unsupported" else "preview")),
        artifact_metadata=ResultArtifactMetadata(
            mime=artifact.mime,
            size=artifact.size,
            source=artifact.source,
            kind=artifact_kind(artifact),
            trust=artifact.trust,
            created_at=artifact.created_at,
            producer_kind=artifact.producer_kind,
        ),
        capabilities=_artifact_capabilities(
            artifact,
            profile,
            writable=writable,
            report_only=report_only,
        ),
        relation_roles=tuple(
            dict.fromkeys(
                (
                    *(("main_deliverable",) if main_deliverable else ()),
                    *relation_roles,
                ),
            ),
        ),
        source_role=source_role,
        output_role="main_deliverable" if main_deliverable else None,
        occurred_at=artifact.created_at,
        sort_tuple=artifact_sort_tuple(
            artifact,
            query,
            direct_report=any(role.startswith("report_") for role in relation_roles),
            relation_roles=relation_roles,
        ),
    )


def project_report_result(
    *,
    canvas_id: str,
    title: str,
    occurred_at: str,
    editable: bool,
    query: str,
    history_available: bool = True,
    export_available: bool = True,
) -> ProjectedResult | None:
    rank = title_search_rank(title, query, aliases=RESULT_SEARCH_ALIASES_V1["type:report"])
    if rank is None:
        return None
    capabilities: list[ResultCapability] = ["read_report"]
    if export_available:
        capabilities.append("export_report")
    if history_available:
        capabilities.append("view_history")
    if editable:
        capabilities.extend(("edit_report", "finish_editing", "share_report", "clear_report", "delete_report"))
    return ProjectedResult(
        key=f"report:{canvas_id}",
        object_type="report",
        title=title,
        primary_group="deliverables",
        secondary_group=None,
        viewer_profile="report",
        status="ready",
        route=ResultRoute(type="report"),
        artifact_metadata=None,
        capabilities=tuple(capabilities),
        relation_roles=("main_deliverable",),
        source_role="report",
        output_role="main_deliverable",
        occurred_at=occurred_at,
        sort_tuple=(rank, 0, 0, 0, 0, occurred_at, f"report:{canvas_id}"),
    )


def project_code_result(
    *,
    session_id: str,
    occurred_at: str,
    running: bool,
    query: str,
    writable: bool = True,
) -> ProjectedResult | None:
    title = "代码与运行"
    rank = title_search_rank(title, query, aliases=RESULT_SEARCH_ALIASES_V1["type:code"])
    if rank is None:
        return None
    capabilities: list[ResultCapability] = [
        "copy_code",
        "open_outputs",
        "view_run_details",
    ]
    if writable:
        capabilities.append("reference_in_chat")
    return ProjectedResult(
        key=f"code:{session_id}",
        object_type="code",
        title=title,
        primary_group="evidence",
        secondary_group="code",
        viewer_profile="code",
        status="running" if running else "ready",
        route=ResultRoute(type="code"),
        artifact_metadata=None,
        capabilities=tuple(capabilities),
        relation_roles=(),
        source_role="code",
        output_role=None,
        occurred_at=occurred_at,
        sort_tuple=(rank, 1 if not query else 0, 1 if not query else 0, 1, 1 if not query else 0, occurred_at, f"code:{session_id}"),
    )


def compare_sort_tuples(left: Sequence[object], right: Sequence[object]) -> int:
    """Compare the frozen mixed-direction Results sort tuple."""

    for index in range(5):
        if left[index] != right[index]:
            return -1 if left[index] < right[index] else 1  # type: ignore[operator]
    # created_at is the sole descending field.
    if left[5] != right[5]:
        return -1 if left[5] > right[5] else 1  # type: ignore[operator]
    if left[6] == right[6]:
        return 0
    return -1 if left[6] < right[6] else 1  # type: ignore[operator]


def sort_projected_results(items: Iterable[ProjectedResult]) -> list[ProjectedResult]:
    return sorted(items, key=cmp_to_key(lambda left, right: compare_sort_tuples(left.sort_tuple, right.sort_tuple)))


def with_positions(items: Sequence[ProjectedResult], *, start: int = 1) -> list[ProjectedResult]:
    return [replace(item, position=position) for position, item in enumerate(items, start=start)]


def latest_occurred_at(values: Iterable[str | None]) -> str:
    valid = [value for value in values if value]
    if valid:
        return max(valid)
    return datetime.now(tz=UTC).isoformat()


__all__ = [
    "ArtifactLike",
    "ProjectedResult",
    "ResultArtifactMetadata",
    "RESULTS_SORT_VERSION_V1",
    "RESULT_SEARCH_ALIASES_V1",
    "artifact_group_ranks",
    "artifact_groups",
    "artifact_search_blob",
    "artifact_search_rank",
    "artifact_sort_tuple",
    "compare_sort_tuples",
    "derive_artifact_viewer_profile",
    "latest_occurred_at",
    "normalize_results_query",
    "project_artifact_result",
    "project_code_result",
    "project_report_result",
    "results_projection_digest",
    "results_query_filter_hash",
    "results_scope_fingerprint",
    "sort_projected_results",
    "with_positions",
]
