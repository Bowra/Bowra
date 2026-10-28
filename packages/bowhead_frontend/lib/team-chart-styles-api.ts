import { apiFetch, apiJson, apiVoid, readApiError } from "./api-client";
import type {
  EffectiveSurfacePaletteProjectionV2,
  ResolvedChartStyleSnapshotV2,
} from "./chart/chart-style-export";

export const CHART_STYLE_V2_HEADERS = {
  "X-Bowhead-Chart-Style-Contract": "2",
} as const;

export type BuiltInChartStyleId =
  | "bowhead-editorial"
  | "clean-blue"
  | "analytical-neutral";
export type TeamChartStyleState =
  | "draft_only"
  | "active_clean"
  | "active_editing"
  | "inactive_clean"
  | "inactive_editing";
export type ChartLineCurve = "straight" | "monotone";
export type ChartLineWidth = "thin" | "standard" | "strong";
export type ChartLinePoints = "auto" | "show" | "hide";
export type ChartBarRadius = "square" | "soft" | "round";
export type ChartTooltip = "enabled" | "disabled";
export type ChartGrid = "hidden" | "quiet" | "standard";

export interface TeamChartStyleOverrides {
  schemaVersion: 2;
  categoricalPalette?: { light?: string[]; dark?: string[] };
  line?: {
    curve?: ChartLineCurve;
    width?: ChartLineWidth;
    points?: ChartLinePoints;
  };
  bar?: { radius?: ChartBarRadius };
  components?: { tooltip?: ChartTooltip; grid?: ChartGrid };
}

export interface TeamChartStyleSource {
  schemaVersion: 2;
  name: string;
  description: string;
  overrides: TeamChartStyleOverrides;
}

export interface TeamChartStyleSummary {
  id: string;
  lifecycle: "active" | "inactive";
  state: TeamChartStyleState;
  name: string;
  activeRevisionId: string | null;
  fallbackBuiltInPresetId: BuiltInChartStyleId;
  headEtag: string;
  draftEtag: string | null;
  resourceEtag: string;
  updatedAt: string;
}

export interface ChartStyleRevision {
  schemaVersion: 2;
  id: string;
  styleId: string;
  sequence: number;
  tokenSchemaVersion: 2;
  resolverPolicyVersion: "chart-style/2.0";
  name: string;
  description: string;
  basePreset: {
    id: BuiltInChartStyleId;
    revision: string;
    checksum: string;
  };
  authoringBaseSnapshot: ResolvedChartStyleSnapshotV2;
  authoringBaseSnapshotChecksum: string;
  overrides: TeamChartStyleOverrides;
  resolvedSnapshot: ResolvedChartStyleSnapshotV2;
  snapshotChecksum: string;
  revisionChecksum: string;
  validationPolicyVersion: string;
  validation: ChartStyleValidation;
  operation: "publish" | "restore";
  sourceRevisionId: string | null;
  createdBy: string;
  createdAt: string;
}

export interface ChartStyleDraft {
  source: TeamChartStyleSource;
  basedOnRevisionId: string | null;
  basePreset: {
    id: BuiltInChartStyleId;
    revision: string;
    checksum: string;
  };
  rowVersion: number;
  updatedBy: string;
  updatedAt: string;
}

export interface ChartStyleCapabilityDecision {
  allowed: boolean;
  reason: string | null;
}

export interface TeamChartStyleDetail {
  style: {
    schemaVersion: 2;
    id: string;
    lifecycle: "active" | "inactive";
    activeRevisionId: string | null;
    fallbackBuiltInPresetId: BuiltInChartStyleId;
    rowVersion: number;
    createdBy: string;
    createdAt: string;
    updatedBy: string;
    updatedAt: string;
  };
  state: TeamChartStyleState;
  activeRevision: ChartStyleRevision | null;
  draft: ChartStyleDraft | null;
  headEtag: string;
  draftEtag: string | null;
  resourceEtag: string;
  capabilities: Record<
    | "createDraft"
    | "saveDraft"
    | "discardDraft"
    | "publish"
    | "restore"
    | "activate"
    | "deactivate"
    | "deleteStyle",
    ChartStyleCapabilityDecision
  >;
}

export interface ChartStyleMutationResult {
  detail: TeamChartStyleDetail;
  registryVersion: number;
  builtInRegistryChecksum: string;
}

export interface ChartStyleIssue {
  path: string;
  severity: "blocker" | "warning" | "info";
  code: string;
  details: Record<string, unknown>;
}

export interface ChartStyleValidation {
  blockers: ChartStyleIssue[];
  warnings: ChartStyleIssue[];
  info: ChartStyleIssue[];
}

export interface ChartStylePreviewResult {
  normalizedAuthoringSource: TeamChartStyleSource;
  candidateSnapshot: ResolvedChartStyleSnapshotV2 | null;
  candidateSnapshotChecksum: string | null;
  validationFingerprint: string;
  validation: ChartStyleValidation;
  surfaceEvidence: {
    contractVersion: "chart-surface/1";
    surfaceChecksums: { light: string; dark: string };
    modes: {
      light: EffectiveSurfacePaletteProjectionV2;
      dark: EffectiveSurfacePaletteProjectionV2;
    };
  } | null;
}

export interface TeamChartStylePolicyV2 {
  version: 2;
  defaultStyleId: string;
  enabledStyleIds: string[];
  fallbackBuiltInPresetId: BuiltInChartStyleId;
}

export interface TeamChartStylePolicyViewV2 {
  policy: TeamChartStylePolicyV2;
  etag: string;
  registryVersion: number;
  builtInRegistryChecksum: string;
}

export interface ChartStyleOptionV2 {
  id: string;
  kind: "builtIn" | "teamCustom";
  name: string;
  description: string;
  revision: string;
  snapshotChecksum: string;
  resolvedPresetSnapshot: ResolvedChartStyleSnapshotV2;
}

export interface ChartStyleOptionsV2 {
  schemaVersion: 2;
  items: ChartStyleOptionV2[];
  teamPolicy: TeamChartStylePolicyV2 & { etag: string };
  registryVersion: number;
  builtInRegistryChecksum: string;
}

export async function listTeamChartStyles(): Promise<{
  items: TeamChartStyleSummary[];
  nextCursor: string | null;
}> {
  return apiJson("/api/v1/team-settings/chart-styles/?limit=50", {
    headers: CHART_STYLE_V2_HEADERS,
  });
}

export async function createTeamChartStyle(input: {
  requestId: string;
  basePresetId: BuiltInChartStyleId;
  source: TeamChartStyleSource;
}): Promise<ChartStyleMutationResult> {
  return apiJson("/api/v1/team-settings/chart-styles/", {
    method: "POST",
    headers: {
      ...CHART_STYLE_V2_HEADERS,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(input),
  });
}

export async function getTeamChartStyle(
  styleId: string,
): Promise<TeamChartStyleDetail> {
  return apiJson(`/api/v1/team-settings/chart-styles/${encodeURIComponent(styleId)}`, {
    headers: CHART_STYLE_V2_HEADERS,
  });
}

async function mutateStyle(
  path: string,
  method: "POST" | "PUT" | "DELETE",
  etag: string,
  body?: unknown,
): Promise<ChartStyleMutationResult> {
  return apiJson(path, {
    method,
    headers: {
      ...CHART_STYLE_V2_HEADERS,
      "If-Match": etag,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
}

export function createTeamChartStyleDraft(styleId: string, headEtag: string) {
  return mutateStyle(
    `/api/v1/team-settings/chart-styles/${encodeURIComponent(styleId)}/draft`,
    "POST",
    headEtag,
  );
}

export function saveTeamChartStyleDraft(
  styleId: string,
  draftEtag: string,
  source: TeamChartStyleSource,
) {
  return mutateStyle(
    `/api/v1/team-settings/chart-styles/${encodeURIComponent(styleId)}/draft`,
    "PUT",
    draftEtag,
    { source },
  );
}

export function discardTeamChartStyleDraft(styleId: string, draftEtag: string) {
  return mutateStyle(
    `/api/v1/team-settings/chart-styles/${encodeURIComponent(styleId)}/draft`,
    "DELETE",
    draftEtag,
  );
}

export function previewTeamChartStyle(
  styleId: string,
  candidate: TeamChartStyleSource,
  signal?: AbortSignal,
): Promise<ChartStylePreviewResult> {
  return apiJson("/api/v1/team-settings/chart-styles/preview", {
    method: "POST",
    headers: {
      ...CHART_STYLE_V2_HEADERS,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ styleId, candidate, surface: "team-base" }),
    signal,
  });
}

export function publishTeamChartStyle(
  styleId: string,
  draftEtag: string,
  validationFingerprint?: string,
) {
  return mutateStyle(
    `/api/v1/team-settings/chart-styles/${encodeURIComponent(styleId)}/publish`,
    "POST",
    draftEtag,
    { acknowledgedValidationFingerprint: validationFingerprint },
  );
}

export async function listTeamChartStyleRevisions(styleId: string): Promise<{
  items: ChartStyleRevision[];
  nextCursor: string | null;
}> {
  return apiJson(
    `/api/v1/team-settings/chart-styles/${encodeURIComponent(styleId)}/revisions?limit=50`,
    { headers: CHART_STYLE_V2_HEADERS },
  );
}

export function restoreTeamChartStyleRevision(
  styleId: string,
  revisionId: string,
  headEtag: string,
  validationFingerprint?: string,
) {
  return mutateStyle(
    `/api/v1/team-settings/chart-styles/${encodeURIComponent(styleId)}/revisions/${encodeURIComponent(revisionId)}/restore`,
    "POST",
    headEtag,
    { acknowledgedValidationFingerprint: validationFingerprint },
  );
}

export function activateTeamChartStyle(styleId: string, headEtag: string) {
  return mutateStyle(
    `/api/v1/team-settings/chart-styles/${encodeURIComponent(styleId)}/activate`,
    "POST",
    headEtag,
  );
}

export function deactivateTeamChartStyle(styleId: string, headEtag: string) {
  return mutateStyle(
    `/api/v1/team-settings/chart-styles/${encodeURIComponent(styleId)}/deactivate`,
    "POST",
    headEtag,
  );
}

export async function deleteDraftOnlyTeamChartStyle(
  styleId: string,
  resourceEtag: string,
): Promise<void> {
  const response = await apiFetch(
    `/api/v1/team-settings/chart-styles/${encodeURIComponent(styleId)}`,
    {
      method: "DELETE",
      headers: {
        ...CHART_STYLE_V2_HEADERS,
        "If-Match": resourceEtag,
      },
    },
  );
  if (!response.ok) throw await readApiError(response);
}

export function getTeamChartStylePolicyV2(): Promise<TeamChartStylePolicyViewV2> {
  return apiJson("/api/v1/team-settings/chart-style-policy", {
    headers: CHART_STYLE_V2_HEADERS,
  });
}

export function setTeamChartStylePolicyV2(
  policy: TeamChartStylePolicyV2,
  etag: string,
): Promise<TeamChartStylePolicyViewV2> {
  return apiJson("/api/v1/team-settings/chart-style-policy", {
    method: "PUT",
    headers: {
      ...CHART_STYLE_V2_HEADERS,
      "Content-Type": "application/json",
      "If-Match": etag,
    },
    body: JSON.stringify(policy),
  });
}

export function getChartStyleOptionsV2(
  signal?: AbortSignal,
): Promise<ChartStyleOptionsV2> {
  return apiJson("/api/v1/theme/chart-style/options", {
    headers: CHART_STYLE_V2_HEADERS,
    signal,
  });
}

export async function setChartStyleSelectionV2(styleId: string | null) {
  await apiVoid("/api/v1/theme/chart-style/selection", {
    method: "PUT",
    headers: {
      ...CHART_STYLE_V2_HEADERS,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ version: 2, styleId }),
  });
}
