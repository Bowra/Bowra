import type { ResultsFiltersDTO, ResultsScope } from "@/lib/results-api";
import { isStableResultToken } from "@/lib/results-workspace-route";
import { randomUuid } from "@/lib/random-id";

export const RESULTS_EXPLORER_HISTORY_KEY = "bowheadResultsExplorer";
export const RESULTS_NAVIGATION_HISTORY_KEY = "bowheadResultsNavigation";

export type ResultsFilters = ResultsFiltersDTO;

export const DEFAULT_RESULTS_FILTERS: ResultsFilters = {
  lens: "all",
  primary: "all",
  secondary: "all",
  technical_only: false,
};

export type ResultsHistoryStateV1 = {
  v: 1;
  entry_id: string;
  session_id: string;
  scope: ResultsScope;
  query: string;
  filters: ResultsFilters;
  expanded_groups: string[];
  selected_key: string | null;
  anchor: { key: string; offset_px: number } | null;
  focus_key: string | null;
};

export type ResultsOpeningContext =
  | {
      type: "report-inspection";
      reportEntryId: string;
      anchor: string | null;
      focusToken: string | null;
    }
  | { type: "explorer"; explorerEntryId: string; focusKey: string | null }
  | { type: "code"; runKey: string; focusToken: string | null }
  | {
      type: "chat";
      messageToken: string | null;
      panelMode: "chat" | "split";
    }
  | { type: "external" };

export type ResultsNavigationHistoryStateV1 = {
  v: 1;
  entry_id: string;
  session_id: string;
  scope: ResultsScope;
  opening_context: ResultsOpeningContext;
};

type ExpectedScope = {
  sessionId: string;
  scope: ResultsScope;
};

export function createResultsEntryId(): string {
  return `results_${randomUuid()}`;
}

export function createResultsNavigationState({
  entryId = createResultsEntryId(),
  openingContext = { type: "external" },
  scope,
  sessionId,
}: ExpectedScope & {
  entryId?: string;
  openingContext?: ResultsOpeningContext;
}): ResultsNavigationHistoryStateV1 {
  return {
    v: 1,
    entry_id: entryId,
    session_id: sessionId,
    scope,
    opening_context: openingContext,
  };
}

export function createResultsExplorerState({
  entryId = createResultsEntryId(),
  scope,
  sessionId,
}: ExpectedScope & {
  entryId?: string;
}): ResultsHistoryStateV1 {
  return {
    v: 1,
    entry_id: entryId,
    session_id: sessionId,
    scope,
    query: "",
    filters: { ...DEFAULT_RESULTS_FILTERS },
    expanded_groups: ["deliverables", "evidence"],
    selected_key: null,
    anchor: null,
    focus_key: null,
  };
}

export function readResultsNavigationState(
  historyState: unknown,
  expected: ExpectedScope,
): ResultsNavigationHistoryStateV1 | null {
  const root = asRecord(historyState);
  if (!root) return null;
  const value = asRecord(root[RESULTS_NAVIGATION_HISTORY_KEY]);
  if (
    !value
    || value.v !== 1
    || value.session_id !== expected.sessionId
    || value.scope !== expected.scope
    || !isEntryId(value.entry_id)
  ) return null;
  const openingContext = parseOpeningContext(value.opening_context);
  if (!openingContext) return null;
  return {
    v: 1,
    entry_id: value.entry_id as string,
    session_id: expected.sessionId,
    scope: expected.scope,
    opening_context: openingContext,
  };
}

export function readResultsExplorerState(
  historyState: unknown,
  expected: ExpectedScope,
): ResultsHistoryStateV1 | null {
  const root = asRecord(historyState);
  if (!root) return null;
  const value = asRecord(root[RESULTS_EXPLORER_HISTORY_KEY]);
  if (
    !value
    || value.v !== 1
    || value.session_id !== expected.sessionId
    || value.scope !== expected.scope
    || !isEntryId(value.entry_id)
    || typeof value.query !== "string"
    || Array.from(value.query).length > 120
  ) return null;
  const filters = parseFilters(value.filters);
  const expandedGroups = parseTokenList(value.expanded_groups, 32);
  const selectedKey = parseNullableToken(value.selected_key);
  const focusKey = parseNullableToken(value.focus_key);
  const anchor = parseAnchor(value.anchor);
  if (
    !filters
    || !expandedGroups
    || selectedKey === undefined
    || focusKey === undefined
    || anchor === undefined
  ) return null;
  return {
    v: 1,
    entry_id: value.entry_id as string,
    session_id: expected.sessionId,
    scope: expected.scope,
    query: value.query,
    filters,
    expanded_groups: expandedGroups,
    selected_key: selectedKey,
    anchor,
    focus_key: focusKey,
  };
}

export function withResultsHistoryState(
  historyState: unknown,
  navigation: ResultsNavigationHistoryStateV1 | null,
  explorer?: ResultsHistoryStateV1 | null,
): Record<string, unknown> {
  const next = { ...(asRecord(historyState) ?? {}) };
  if (navigation) next[RESULTS_NAVIGATION_HISTORY_KEY] = navigation;
  else delete next[RESULTS_NAVIGATION_HISTORY_KEY];
  if (explorer !== undefined) {
    if (explorer) next[RESULTS_EXPLORER_HISTORY_KEY] = explorer;
    else delete next[RESULTS_EXPLORER_HISTORY_KEY];
  }
  return next;
}

export function withoutResultsHistoryState(
  historyState: unknown,
): Record<string, unknown> {
  return withResultsHistoryState(historyState, null, null);
}

function parseFilters(value: unknown): ResultsFilters | null {
  const record = asRecord(value);
  if (!record) return null;
  if (
    (record.lens !== "all" && record.lens !== "current_report")
    || !["all", "deliverables", "evidence"].includes(String(record.primary))
    || !["all", "data", "code", "input"].includes(String(record.secondary))
    || typeof record.technical_only !== "boolean"
  ) return null;
  return {
    lens: record.lens,
    primary: record.primary as ResultsFilters["primary"],
    secondary: record.secondary as ResultsFilters["secondary"],
    technical_only: record.technical_only,
  };
}

function parseOpeningContext(value: unknown): ResultsOpeningContext | null {
  const record = asRecord(value);
  if (!record || typeof record.type !== "string") return null;
  if (record.type === "external") return { type: "external" };
  if (record.type === "report-inspection") {
    if (!isEntryId(record.reportEntryId)) return null;
    const anchor = parseNullableToken(record.anchor);
    const focusToken = parseNullableToken(record.focusToken);
    if (anchor === undefined || focusToken === undefined) return null;
    return {
      type: record.type,
      reportEntryId: record.reportEntryId,
      anchor,
      focusToken,
    };
  }
  if (record.type === "explorer") {
    if (!isEntryId(record.explorerEntryId)) return null;
    const focusKey = parseNullableToken(record.focusKey);
    if (focusKey === undefined) return null;
    return { type: record.type, explorerEntryId: record.explorerEntryId, focusKey };
  }
  if (record.type === "code") {
    if (!isStableResultToken(String(record.runKey ?? ""))) return null;
    const focusToken = parseNullableToken(record.focusToken);
    if (focusToken === undefined) return null;
    return { type: record.type, runKey: String(record.runKey), focusToken };
  }
  if (record.type === "chat") {
    const messageToken = parseNullableToken(record.messageToken);
    if (messageToken === undefined) return null;
    const panelMode = record.panelMode === undefined
      ? "chat"
      : record.panelMode === "chat" || record.panelMode === "split"
        ? record.panelMode
        : null;
    if (!panelMode) return null;
    return { type: record.type, messageToken, panelMode };
  }
  return null;
}

function parseAnchor(
  value: unknown,
): ResultsHistoryStateV1["anchor"] | undefined {
  if (value === null) return null;
  const record = asRecord(value);
  if (
    !record
    || !isStableResultToken(String(record.key ?? ""))
    || typeof record.offset_px !== "number"
    || !Number.isFinite(record.offset_px)
    || Math.abs(record.offset_px) > 100_000
  ) return undefined;
  return { key: String(record.key), offset_px: record.offset_px };
}

function parseTokenList(value: unknown, maxLength: number): string[] | null {
  if (!Array.isArray(value) || value.length > maxLength) return null;
  if (!value.every((item) => typeof item === "string" && isStableResultToken(item))) {
    return null;
  }
  return [...value];
}

function parseNullableToken(value: unknown): string | null | undefined {
  if (value === null) return null;
  return typeof value === "string" && isStableResultToken(value)
    ? value
    : undefined;
}

function isEntryId(value: unknown): value is string {
  return typeof value === "string" && isStableResultToken(value);
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}
