import type { ArtifactItem } from "@/components/canvas/data-tab-files";
import {
  deriveArtifactViewerProfile,
  viewerModesForProfile,
} from "@/lib/artifact-viewer-profile";
import type { ResultExportVersionGroup } from "@/lib/results-export-versions";
import type { ResultItemDTO } from "@/lib/results-api";
import type { ResultsRouteTarget } from "@/lib/results-workspace-route";

export type ResultPrimaryGroup = "deliverables" | "evidence";
export type ResultSecondaryGroup = "data" | "code" | "input" | null;
export type ResultObjectType = "report" | "artifact" | "code";
export type ResultViewerProfile =
  | "report"
  | "html"
  | "image"
  | "video"
  | "table"
  | "text"
  | "document"
  | "unsupported"
  | "code";
export type ArtifactResultRole = "deliverable" | "data" | "input" | "unsupported";

export type ResultCapability =
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

export interface ResultItem {
  key: string;
  objectType: ResultObjectType;
  primaryGroup: ResultPrimaryGroup;
  secondaryGroup: ResultSecondaryGroup;
  title: string;
  metadata: string;
  status: "ready" | "running" | "processing" | "failed" | "unverified";
  viewerProfile: ResultViewerProfile;
  capabilities: ReadonlySet<ResultCapability>;
  route: ResultsRouteTarget;
  stableId: string | null;
  memberStableIds: readonly string[];
  completedAt: string | null;
  /** Stable logical execution grouping. Null for legacy/unscoped objects. */
  runId: string | null;
  source: { kind: string | null; producer: string | null };
  artifactItem: ArtifactItem | null;
  version?: {
    format: "pdf" | "pptx" | "xlsx" | "docx";
    latestJobStatus: "queued" | "running" | "done" | "failed";
    availableVersionCount: number;
    exportJobCount: number;
  };
}

export type ResultModelLabels = {
  reportEditable: string;
  reportReadonly: string;
  codeRunning: string;
  codeTitle: string;
  codeLastRun: string;
  sourceUpload: string;
  sourceGenerated: string;
  sourceExport: string;
  sourceWeb: string;
  sourceSandbox: string;
  sourceData: string;
  exportVersions: (count: number) => string;
  formatDate: (value: string) => string;
};

export type ProjectResultsInput = {
  artifacts: readonly ArtifactItem[];
  exportGroups?: readonly ResultExportVersionGroup[];
  report?: {
    canvasId: string;
    title: string;
    editable: boolean;
  } | null;
  code?: {
    sessionId: string;
    available: boolean;
    running: boolean;
  } | null;
  labels: ResultModelLabels;
  readOnly: boolean;
  reportOnly: boolean;
};

function artifactKind(item: ArtifactItem): string {
  if (item.kind) return item.kind;
  switch (item.source) {
    case "canvas_export": return "canvas_export";
    case "upload": return "user_upload";
    case "bi_fetch_rows": return "tool_data";
    case "python_sandbox":
    case "sandbox_output": return "sandbox_output";
    default: return "legacy_unknown";
  }
}

function artifactSourceLabel(
  item: ArtifactItem,
  labels: ResultModelLabels,
): string {
  switch (item.origin) {
    case "upload": return labels.sourceUpload;
    case "export": return labels.sourceExport;
    case "web": return labels.sourceWeb;
    case "query": return labels.sourceData;
    case "compute":
      return item.source === "python_sandbox" || item.source === "sandbox_output"
        ? labels.sourceSandbox
        : labels.sourceGenerated;
    case "deck": return labels.sourceGenerated;
  }
  switch (artifactKind(item)) {
    case "user_upload": return labels.sourceUpload;
    case "canvas_export": return labels.sourceExport;
    case "web_clip": return labels.sourceWeb;
    case "sandbox_output": return labels.sourceSandbox;
    case "tool_data": return labels.sourceData;
    case "deliverable":
      return item.source === "canvas_export"
        ? labels.sourceExport
        : labels.sourceGenerated;
    default: return labels.sourceGenerated;
  }
}

export function artifactItemFromResultDTO(
  item: ResultItemDTO,
): ArtifactItem | null {
  if (
    item.object_type !== "artifact"
    || item.route.type !== "artifact"
    || !item.artifact_metadata
  ) return null;
  const metadata = item.artifact_metadata;
  return {
    id: item.route.artifact_id,
    filename: item.title,
    mime: metadata.mime,
    size: metadata.size,
    source: metadata.source,
    created_at: metadata.created_at,
    preview_available: item.route.default_mode !== "details",
    kind: metadata.kind,
    origin: metadata.origin,
    trust: metadata.trust,
    created_by_turn: item.execution?.key ?? null,
    producer_kind: metadata.producer_kind,
  };
}

/**
 * Title shown for a Results item. The Gateway names the session's code result with fixed Chinese text
 * ("代码与运行"); its object type is the structured field, so the interface-language title replaces it.
 */
export function resultDisplayTitle(
  item: Pick<ResultItemDTO, "object_type" | "title">,
  codeTitle: string,
): string {
  return item.object_type === "code" ? codeTitle : item.title;
}

export function projectResultsDTO(
  items: readonly ResultItemDTO[],
  labels: ResultModelLabels,
): ResultItem[] {
  return items.map((item) => {
    const artifact = artifactItemFromResultDTO(item);
    const capabilities = new Set<ResultCapability>(item.capabilities);
    const route: ResultsRouteTarget = item.route.type === "artifact"
      ? {
          type: "artifact",
          artifactId: item.route.artifact_id,
          mode: item.route.default_mode,
        }
      : { type: item.route.type };
    let metadata = labels.sourceGenerated;
    if (item.object_type === "report") {
      metadata = capabilities.has("edit_report")
        ? labels.reportEditable
        : labels.reportReadonly;
    } else if (item.object_type === "code") {
      metadata = item.status === "running"
        ? labels.codeRunning
        : labels.codeLastRun;
    } else if (artifact) {
      metadata = artifactSourceLabel(artifact, labels);
    }
    return {
      key: item.key,
      objectType: item.object_type,
      primaryGroup: item.primary_group,
      secondaryGroup: item.secondary_group,
      title: resultDisplayTitle(item, labels.codeTitle),
      metadata,
      status: item.status,
      viewerProfile: artifact ? resultViewerProfile(artifact) : item.viewer_profile,
      capabilities,
      route,
      stableId: artifact?.id
        ?? (item.object_type === "report" ? item.key.split(":", 2)[1] ?? null : null),
      memberStableIds: artifact ? [artifact.id] : [],
      completedAt: item.display_context.occurred_at,
      runId: item.execution?.key ?? null,
      source: {
        kind: artifact?.kind ?? item.display_context.source_role,
        producer: artifact?.producer_kind ?? null,
      },
      artifactItem: artifact,
    };
  });
}

function resultViewerProfile(item: ArtifactItem): ResultViewerProfile {
  switch (deriveArtifactViewerProfile(item.mime, item.filename)) {
    case "html": return "html";
    case "image": return "image";
    case "video": return "video";
    case "table":
    case "json":
    case "xlsx": return "table";
    case "markdown":
    case "text": return "text";
    case "pdf":
    case "docx":
    case "pptx": return "document";
    default: return "unsupported";
  }
}

export function artifactResultRole(item: ArtifactItem): ArtifactResultRole {
  if (deriveArtifactViewerProfile(item.mime, item.filename) === "unsupported") {
    return "unsupported";
  }
  const kind = artifactKind(item);
  if (kind === "deliverable" || kind === "canvas_export") return "deliverable";
  if (kind === "user_upload") return "input";
  return "data";
}

export function deriveArtifactCapabilities({
  item,
  readOnly,
  reportOnly,
  readable = true,
}: {
  item: ArtifactItem;
  readOnly: boolean;
  reportOnly: boolean;
  readable?: boolean;
}): ReadonlySet<ResultCapability> {
  const capabilities = new Set<ResultCapability>();
  if (!readable || !item.id) return capabilities;
  const profile = deriveArtifactViewerProfile(item.mime, item.filename);
  const role = artifactResultRole(item);
  capabilities.add("download");
  capabilities.add("view_details");
  if (
    !reportOnly
    && (role === "deliverable" || role === "input" || role === "unsupported")
  ) {
    capabilities.add("manage_in_files");
  }
  if (
    !readOnly
    && !reportOnly
    && role !== "unsupported"
  ) {
    capabilities.add("reference_in_chat");
  }
  if (viewerModesForProfile(profile).includes("raw")) {
    capabilities.add("view_raw");
  }
  if (role === "unsupported" && !reportOnly) {
    capabilities.add("feedback_unsupported");
  }
  return capabilities;
}

function artifactGrouping(item: ArtifactItem): {
  primaryGroup: ResultPrimaryGroup;
  secondaryGroup: ResultSecondaryGroup;
} {
  switch (artifactKind(item)) {
    case "deliverable":
    case "canvas_export":
      return { primaryGroup: "deliverables", secondaryGroup: null };
    case "user_upload":
      return { primaryGroup: "evidence", secondaryGroup: "input" };
    default:
      return { primaryGroup: "evidence", secondaryGroup: "data" };
  }
}

function compareItems(a: ResultItem, b: ResultItem): number {
  if (a.primaryGroup !== b.primaryGroup) {
    return a.primaryGroup === "deliverables" ? -1 : 1;
  }
  if (a.primaryGroup === "deliverables") {
    if (a.objectType === "report" && b.objectType !== "report") return -1;
    if (b.objectType === "report" && a.objectType !== "report") return 1;
  } else {
    const order: Record<NonNullable<ResultSecondaryGroup>, number> = {
      data: 0,
      code: 1,
      input: 2,
    };
    const secondary = order[a.secondaryGroup ?? "data"]
      - order[b.secondaryGroup ?? "data"];
    if (secondary) return secondary;
  }
  const completed = (b.completedAt ?? "").localeCompare(a.completedAt ?? "");
  return completed || a.key.localeCompare(b.key);
}

function normalizedExportStatus(
  status: ResultExportVersionGroup["latestStatus"],
): "queued" | "running" | "done" | "failed" {
  return status === "canceled" ? "failed" : status;
}

export function projectResultsWorkspace(input: ProjectResultsInput): ResultItem[] {
  const artifactsById = new Map(input.artifacts.map((item) => [item.id, item]));
  const groupedArtifactIds = new Set<string>();
  const resultItems: ResultItem[] = [];

  if (input.report) {
    const capabilities = new Set<ResultCapability>([
      "read_report",
      "export_report",
      "view_history",
    ]);
    if (!input.readOnly && !input.reportOnly && input.report.editable) {
      capabilities.add("edit_report");
      capabilities.add("finish_editing");
      capabilities.add("share_report");
      capabilities.add("clear_report");
      capabilities.add("delete_report");
    }
    resultItems.push({
      key: `report:${input.report.canvasId}`,
      objectType: "report",
      primaryGroup: "deliverables",
      secondaryGroup: null,
      title: input.report.title,
      metadata: input.report.editable
        ? input.labels.reportEditable
        : input.labels.reportReadonly,
      status: "ready",
      viewerProfile: "report",
      capabilities,
      route: { type: "report" },
      stableId: input.report.canvasId,
      memberStableIds: [input.report.canvasId],
      completedAt: null,
      runId: null,
      source: { kind: "report", producer: null },
      artifactItem: null,
    });
  }

  for (const group of input.exportGroups ?? []) {
    const authorizedVersions = group.versions.filter((version) =>
      artifactsById.has(version.artifactId),
    );
    const current = authorizedVersions[0];
    if (!current) continue;
    const artifact = artifactsById.get(current.artifactId);
    if (!artifact) continue;
    for (const version of authorizedVersions) {
      groupedArtifactIds.add(version.artifactId);
    }
    const format = group.fmt.toLowerCase();
    if (!["pdf", "pptx", "xlsx", "docx"].includes(format)) continue;
    resultItems.push({
      key: `export:${group.canvasId}:${format}`,
      objectType: "artifact",
      primaryGroup: "deliverables",
      secondaryGroup: null,
      title: artifact.filename,
      metadata: `${input.labels.sourceExport} · ${input.labels.exportVersions(authorizedVersions.length)}`,
      status: group.latestStatus === "running" || group.latestStatus === "queued"
        ? "processing"
        : group.latestStatus === "failed" || group.latestStatus === "canceled"
          ? "failed"
          : "ready",
      viewerProfile: resultViewerProfile(artifact),
      capabilities: new Set<ResultCapability>([
        ...deriveArtifactCapabilities({
          item: artifact,
          readOnly: input.readOnly,
          reportOnly: input.reportOnly,
        }),
        "view_history",
      ]),
      route: { type: "artifact", artifactId: artifact.id, mode: "preview" },
      stableId: artifact.id,
      memberStableIds: authorizedVersions.map((version) => version.artifactId),
      completedAt: current.createdAt,
      runId: artifact.created_by_turn ?? null,
      source: {
        kind: artifactKind(artifact),
        producer: artifact.producer_kind ?? null,
      },
      artifactItem: artifact,
      version: {
        format: format as "pdf" | "pptx" | "xlsx" | "docx",
        latestJobStatus: normalizedExportStatus(group.latestStatus),
        availableVersionCount: authorizedVersions.length,
        exportJobCount: group.exportJobCount,
      },
    });
  }

  const duplicateTitles = input.artifacts.reduce((counts, artifact) => {
    if (groupedArtifactIds.has(artifact.id)) return counts;
    const title = artifact.filename.toLowerCase();
    counts.set(title, (counts.get(title) ?? 0) + 1);
    return counts;
  }, new Map<string, number>());
  for (const artifact of input.artifacts) {
    if (groupedArtifactIds.has(artifact.id)) continue;
    const grouping = artifactGrouping(artifact);
    const sourceLabel = artifactSourceLabel(artifact, input.labels);
    const duplicate = (duplicateTitles.get(artifact.filename.toLowerCase()) ?? 0) > 1;
    resultItems.push({
      key: `artifact:${artifact.id}`,
      objectType: "artifact",
      ...grouping,
      title: artifact.filename,
      metadata: duplicate
        ? `${sourceLabel} · ${input.labels.formatDate(artifact.created_at)}`
        : sourceLabel,
      status: artifact.trust === "unverified" ? "unverified" : "ready",
      viewerProfile: resultViewerProfile(artifact),
      capabilities: deriveArtifactCapabilities({
        item: artifact,
        readOnly: input.readOnly,
        reportOnly: input.reportOnly,
      }),
      route: { type: "artifact", artifactId: artifact.id, mode: "preview" },
      stableId: artifact.id,
      memberStableIds: [artifact.id],
      completedAt: artifact.created_at,
      runId: artifact.created_by_turn ?? null,
      source: {
        kind: artifactKind(artifact),
        producer: artifact.producer_kind ?? null,
      },
      artifactItem: artifact,
    });
  }

  if (input.code?.available && !input.reportOnly) {
    resultItems.push({
      key: `code:${input.code.sessionId}`,
      objectType: "code",
      primaryGroup: "evidence",
      secondaryGroup: "code",
      title: input.labels.codeTitle,
      metadata: input.code.running
        ? input.labels.codeRunning
        : input.labels.codeLastRun,
      status: input.code.running ? "running" : "ready",
      viewerProfile: "code",
      capabilities: new Set<ResultCapability>([
        "copy_code",
        "open_outputs",
        "view_run_details",
      ]),
      route: { type: "code" },
      stableId: null,
      memberStableIds: [],
      completedAt: null,
      runId: null,
      source: { kind: "code", producer: null },
      artifactItem: null,
    });
  }

  return resultItems.sort(compareItems);
}

export function resolveSelectedResult(
  items: readonly ResultItem[],
  selection:
    | { type: "report" }
    | { type: "artifact"; artifactId: string }
    | { type: "code" }
    | { type: "none" },
): ResultItem | null {
  if (selection.type === "none") return null;
  if (selection.type === "report") {
    return items.find((item) => item.objectType === "report") ?? null;
  }
  if (selection.type === "code") {
    return items.find((item) => item.objectType === "code") ?? null;
  }
  return items.find((item) =>
    item.objectType === "artifact"
    && item.memberStableIds.includes(selection.artifactId),
  ) ?? null;
}
