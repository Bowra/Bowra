import { appFetch } from "@/lib/base-path";

export type ResultsScope = "session" | "report";
export type ResultsSurface = "quick" | "explorer" | "locate" | "changes";
export type ResultsFiltersDTO = {
  lens: "all" | "current_report";
  primary: "all" | "deliverables" | "evidence";
  secondary: "all" | "data" | "code" | "input";
  technical_only: boolean;
};
export type ResultCapabilityDTO =
  | "read_report"
  | "edit_report"
  | "finish_editing"
  | "export_report"
  | "share_report"
  | "view_history"
  | "clear_report"
  | "delete_report"
  | "download"
  | "reference_in_chat"
  | "manage_in_files"
  | "view_raw"
  | "view_details"
  | "copy_code"
  | "open_outputs"
  | "view_run_details"
  | "feedback_unsupported";

export type ResultArtifactMetadataDTO = {
  mime: string;
  size: number;
  source: string;
  kind: string;
  origin: "query" | "compute" | "upload" | "export" | "web" | "deck" | "other";
  trust: string;
  created_at: string;
  producer_kind: string | null;
};

export type ResultItemDTO = {
  key: string;
  object_type: "report" | "artifact" | "code";
  title: string;
  primary_group: "deliverables" | "evidence";
  secondary_group: "data" | "code" | "input" | null;
  viewer_profile: "report" | "html" | "image" | "video" | "table" | "text" | "document" | "unsupported" | "code";
  status: "ready" | "running" | "processing" | "failed" | "unverified";
  route:
    | { type: "report" }
    | { type: "code" }
    | { type: "artifact"; artifact_id: string; default_mode: "preview" | "raw" | "details" };
  capabilities: ResultCapabilityDTO[];
  relation_roles: string[];
  execution: {
    key: string;
    kind: "code_run" | "tool_call" | "agent_task" | "export_job";
    label: string | null;
    sequence: number | null;
  } | null;
  display_context: {
    source_role: "report" | "canvas_export" | "generated" | "user_upload" | "data_query" | "web" | "sandbox" | "code";
    output_role: string | null;
    execution_label: string | null;
    occurred_at: string | null;
    duplicate_ordinal: number | null;
    duplicate_count: number | null;
  };
  position: number;
  version: {
    format: "pdf" | "pptx" | "xlsx" | "docx";
    available_version_count: number;
  } | null;
  artifact_metadata: ResultArtifactMetadataDTO | null;
};

export type ResultsQueryRequest = {
  surface: ResultsSurface;
  query?: string;
  filters?: ResultsFiltersDTO;
  page_size?: number;
  cursor?: string;
  snapshot_token?: string;
  current_key?: string;
  recent_keys?: string[];
  anchor_key?: string;
};

export type ResultsQueryResponse = {
  dto_version: 1;
  capability: { complete_list: true; scale_nav: boolean };
  snapshot_token: string;
  items: ResultItemDTO[];
  authorized_total: number;
  matched_total: number;
  group_counts: {
    deliverables: number;
    evidence: number;
    data: number;
    code: number;
    input: number;
  };
  loaded_count: number;
  page_start: number;
  next_cursor: string | null;
  previous_cursor: string | null;
  has_more: boolean;
  projection_status: {
    report_relations: "ready" | "unavailable";
    execution_relations: "ready" | "unavailable";
    export_relations: "ready" | "degraded";
  };
  projection_revision: string;
  new_results: { count: number | null; projection_changed: boolean };
  resolved_current_key: string | null;
};

export type ResultsApiErrorCode =
  | "results_query_invalid"
  | "results_snapshot_stale"
  | "results_anchor_not_in_view"
  | "results_filter_unavailable"
  | "results_read_model_unavailable"
  | "results_scope_unavailable"
  | "results_response_invalid"
  | "results_request_failed";

const RESULT_ITEM_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;

const SERVER_ERROR_CODES = new Set<ResultsApiErrorCode>([
  "results_query_invalid",
  "results_snapshot_stale",
  "results_anchor_not_in_view",
  "results_filter_unavailable",
  "results_read_model_unavailable",
  "results_scope_unavailable",
]);

const CAPABILITIES = new Set<ResultCapabilityDTO>([
  "read_report", "edit_report", "finish_editing", "export_report",
  "share_report", "view_history", "clear_report", "delete_report",
  "download", "reference_in_chat", "manage_in_files", "view_raw",
  "view_details", "copy_code", "open_outputs", "view_run_details",
  "feedback_unsupported",
]);
const OBJECT_TYPES = new Set(["report", "artifact", "code"]);
const VIEWER_PROFILES = new Set(["report", "html", "image", "video", "table", "text", "document", "unsupported", "code"]);
const STATUSES = new Set(["ready", "running", "processing", "failed", "unverified"]);
const ORIGINS = new Set(["query", "compute", "upload", "export", "web", "deck", "other"]);
const SOURCE_ROLES = new Set(["report", "canvas_export", "generated", "user_upload", "data_query", "web", "sandbox", "code"]);
const RELATION_ROLES = new Set([
  "report_chart_data", "report_detail_table", "report_computation",
  "report_embedded_media", "code_output", "code_input", "run_output",
  "technical_output", "main_deliverable", "ordinary_source",
]);
const EXECUTION_KINDS = new Set(["code_run", "tool_call", "agent_task", "export_job"]);
const VERSION_FORMATS = new Set(["pdf", "pptx", "xlsx", "docx"]);

export class ResultsApiError extends Error {
  constructor(
    readonly code: ResultsApiErrorCode,
    readonly status: number | null = null,
  ) {
    super(code);
    this.name = "ResultsApiError";
  }

  get purgesScope(): boolean {
    return this.code === "results_scope_unavailable";
  }

  get allowsLegacyFallback(): boolean {
    return this.code === "results_read_model_unavailable"
      || this.code === "results_response_invalid"
      || this.code === "results_request_failed";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isString(value: unknown): value is string {
  return typeof value === "string";
}

function isNullableString(value: unknown): value is string | null {
  return value === null || isString(value);
}

function isNonNegativeInteger(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) >= 0;
}

function isNullablePositiveInteger(value: unknown): value is number | null {
  return value === null || (Number.isInteger(value) && Number(value) >= 1);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(isString);
}

function parseArtifactMetadata(value: unknown): ResultArtifactMetadataDTO | null | undefined {
  if (value === null) return null;
  if (!isRecord(value)) return undefined;
  if (
    !isString(value.mime)
    || !isNonNegativeInteger(value.size)
    || !isString(value.source)
    || !isString(value.kind)
    || !isString(value.origin)
    || !ORIGINS.has(value.origin)
    || !isString(value.trust)
    || !isString(value.created_at)
    || !isNullableString(value.producer_kind)
  ) return undefined;
  return value as ResultArtifactMetadataDTO;
}

function parseResultItem(value: unknown): ResultItemDTO | null {
  if (!isRecord(value)) return null;
  if (
    !isString(value.key)
    || !RESULT_ITEM_KEY.test(value.key)
    || !isString(value.object_type)
    || !OBJECT_TYPES.has(value.object_type)
    || !isString(value.title)
    || value.title.length === 0
    || (value.primary_group !== "deliverables" && value.primary_group !== "evidence")
    || !["data", "code", "input", null].includes(value.secondary_group as string | null)
    || !isString(value.viewer_profile)
    || !VIEWER_PROFILES.has(value.viewer_profile)
    || !isString(value.status)
    || !STATUSES.has(value.status)
    || !Array.isArray(value.capabilities)
    || !value.capabilities.every((item) => isString(item) && CAPABILITIES.has(item as ResultCapabilityDTO))
    || !isStringArray(value.relation_roles)
    || !value.relation_roles.every((role) => RELATION_ROLES.has(role))
    || !isNonNegativeInteger(value.position)
    || value.position < 1
    || !isRecord(value.display_context)
    || !isString(value.display_context.source_role)
    || !SOURCE_ROLES.has(value.display_context.source_role)
    || !isNullableString(value.display_context.output_role)
    || (value.display_context.output_role !== null
      && !RELATION_ROLES.has(value.display_context.output_role))
    || !isNullableString(value.display_context.execution_label)
    || !isNullableString(value.display_context.occurred_at)
    || !isNullablePositiveInteger(value.display_context.duplicate_ordinal)
    || !isNullablePositiveInteger(value.display_context.duplicate_count)
  ) return null;
  if (value.execution !== null) {
    if (
      !isRecord(value.execution)
      || !isString(value.execution.key)
      || value.execution.key.length === 0
      || !isString(value.execution.kind)
      || !EXECUTION_KINDS.has(value.execution.kind)
      || !isNullableString(value.execution.label)
      || !(value.execution.sequence === null || isNonNegativeInteger(value.execution.sequence))
    ) return null;
  }
  if (value.version !== null) {
    if (
      !isRecord(value.version)
      || !isString(value.version.format)
      || !VERSION_FORMATS.has(value.version.format)
      || !Number.isInteger(value.version.available_version_count)
      || Number(value.version.available_version_count) < 1
    ) return null;
  }
  const route = value.route;
  if (!isRecord(route) || !isString(route.type)) return null;
  if (route.type === "artifact") {
    if (!isString(route.artifact_id) || route.artifact_id.length === 0 || !["preview", "raw", "details"].includes(String(route.default_mode))) return null;
  } else if (route.type !== "report" && route.type !== "code") return null;
  if (route.type !== value.object_type) return null;
  const metadata = parseArtifactMetadata(value.artifact_metadata);
  if (metadata === undefined) return null;
  if ((value.object_type === "artifact") !== (metadata !== null)) return null;
  if (
    (value.object_type === "report" && value.viewer_profile !== "report")
    || (value.object_type === "code" && value.viewer_profile !== "code")
    || (value.object_type === "artifact" && ["report", "code"].includes(value.viewer_profile))
  ) return null;
  if (value.object_type === "artifact" && route.type === "artifact" && value.key !== `artifact:${route.artifact_id}`) return null;
  return value as ResultItemDTO;
}

export function parseResultsQueryResponse(value: unknown): ResultsQueryResponse {
  if (!isRecord(value) || value.dto_version !== 1 || !isRecord(value.capability)) {
    throw new ResultsApiError("results_response_invalid");
  }
  const items = Array.isArray(value.items)
    ? value.items.map(parseResultItem)
    : [];
  if (
    value.capability.complete_list !== true
    || typeof value.capability.scale_nav !== "boolean"
    || !Array.isArray(value.items)
    || items.some((item) => item === null)
    || !isString(value.snapshot_token)
    || !isNonNegativeInteger(value.authorized_total)
    || !isNonNegativeInteger(value.matched_total)
    || !isRecord(value.group_counts)
    || !isNonNegativeInteger(value.group_counts.deliverables)
    || !isNonNegativeInteger(value.group_counts.evidence)
    || !isNonNegativeInteger(value.group_counts.data)
    || !isNonNegativeInteger(value.group_counts.code)
    || !isNonNegativeInteger(value.group_counts.input)
    || !isNonNegativeInteger(value.loaded_count)
    || !isNonNegativeInteger(value.page_start)
    || !isNullableString(value.next_cursor)
    || !isNullableString(value.previous_cursor)
    || typeof value.has_more !== "boolean"
    || !isRecord(value.projection_status)
    || !["ready", "unavailable"].includes(String(value.projection_status.report_relations))
    || !["ready", "unavailable"].includes(String(value.projection_status.execution_relations))
    || !["ready", "degraded"].includes(String(value.projection_status.export_relations))
    || !isString(value.projection_revision)
    || !isRecord(value.new_results)
    || !(value.new_results.count === null || isNonNegativeInteger(value.new_results.count))
    || typeof value.new_results.projection_changed !== "boolean"
    || !isNullableString(value.resolved_current_key)
  ) {
    throw new ResultsApiError("results_response_invalid");
  }
  if (value.loaded_count !== items.length) {
    throw new ResultsApiError("results_response_invalid");
  }
  if (
    Number(value.group_counts.data)
      + Number(value.group_counts.code)
      + Number(value.group_counts.input)
      !== Number(value.group_counts.evidence)
    || Number(value.group_counts.deliverables)
      + Number(value.group_counts.evidence)
      > Number(value.authorized_total)
  ) {
    throw new ResultsApiError("results_response_invalid");
  }
  return { ...value, items: items as ResultItemDTO[] } as ResultsQueryResponse;
}

async function resultsErrorFor(response: Response): Promise<ResultsApiError> {
  try {
    const value = await response.json() as unknown;
    const code = isRecord(value)
      && isRecord(value.detail)
      && isRecord(value.detail.error)
      && isString(value.detail.error.code)
      ? value.detail.error.code
      : null;
    if (code && SERVER_ERROR_CODES.has(code as ResultsApiErrorCode)) {
      return new ResultsApiError(code as ResultsApiErrorCode, response.status);
    }
  } catch {
    // Only finite public codes cross this boundary.
  }
  if ([401, 403, 410].includes(response.status)) {
    return new ResultsApiError("results_scope_unavailable", response.status);
  }
  return new ResultsApiError(
    response.status >= 500
      ? "results_read_model_unavailable"
      : "results_request_failed",
    response.status,
  );
}

export async function queryResults({
  sessionId,
  scope,
  request,
  signal,
}: {
  sessionId: string;
  scope: ResultsScope;
  request: ResultsQueryRequest;
  signal?: AbortSignal;
}): Promise<ResultsQueryResponse> {
  const path = scope === "report"
    ? `/api/v1/canvas/by-session/${encodeURIComponent(sessionId)}/results/query`
    : `/api/v1/sessions/${encodeURIComponent(sessionId)}/results/query`;
  let response: Response;
  try {
    response = await appFetch(path, {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        dto_version: 1,
        surface: request.surface,
        query: request.query ?? "",
        filters: request.filters ?? {
          lens: "all",
          primary: "all",
          secondary: "all",
          technical_only: false,
        },
        page_size: request.page_size ?? null,
        cursor: request.cursor ?? null,
        snapshot_token: request.snapshot_token ?? null,
        current_key: request.current_key ?? null,
        recent_keys: request.recent_keys ?? [],
        anchor_key: request.anchor_key ?? null,
      }),
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ResultsApiError("results_request_failed");
  }
  if (!response.ok) throw await resultsErrorFor(response);
  try {
    return parseResultsQueryResponse(await response.json());
  } catch (error) {
    if (error instanceof ResultsApiError) throw error;
    throw new ResultsApiError("results_response_invalid");
  }
}
