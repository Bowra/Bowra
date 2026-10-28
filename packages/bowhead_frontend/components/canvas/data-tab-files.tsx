"use client";

import { appFetch as fetch, appPath } from "@/lib/base-path";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { formatDateTime, getCurrentLocale } from "@/lib/locale";
import { useFocusedItemScroll } from "@/lib/use-focused-item-scroll";
import {
  deriveArtifactViewerProfile,
  normalizeViewerMode,
  viewerModesForProfile,
} from "@/lib/artifact-viewer-profile";
import type { ResultsViewerMode } from "@/lib/results-workspace-route";
import {
  artifactResultRole,
  deriveArtifactCapabilities,
  type ArtifactResultRole,
  type ResultCapability,
} from "@/lib/results-workspace-model";
import { trackResultsWorkspace } from "@/lib/results-workspace-metrics";
import { toast } from "@/lib/use-toast";
import {
  classifyResultsError,
  classifyResultsResponse,
  ResultsWorkspaceError,
  type ResultsWorkspaceErrorClass,
} from "@/lib/results-workspace-errors";
import { X } from "@/components/icons";
import { Tab, TabList, Tabs } from "@/components/ui/tabs";
import { ArtifactPreview } from "./artifact-preview";
import { ResultObjectActions } from "./result-object-actions";
import type { CanvasExportFmt } from "@/lib/api";
import { useArtifactRepresentation } from "@/lib/use-artifact-representation";

/**
 * Data tab files v1 (#100): list current-session artifacts + open previews.
 *
 * Dependencies:
 * - GET /api/v1/sessions/:sid/artifacts → SessionArtifactListDTO
 * - GET /api/v1/files/:id/representations/:mode → independent Preview/Raw/Details
 * - GET /api/v1/files/:id (download link)
 *
 * Without a valid backend session_id, empty state guides users to send a message first to produce files.
 */

export type ArtifactItem = {
  id: string;
  filename: string;
  mime: string;
  size: number;
  source: string;
  created_at: string;
  preview_available: boolean;
  // Sprint pptx-from-template M1: asset-model fields. `kind` drives the badge
  // (DECK/WEB/UP/DATA/SBX/EXP); `trust` drives the unverified marker.
  kind?: string;
  origin?: string;
  trust?: string;
  created_by_turn?: string | null;
  producer_kind?: string | null;
  derived_from?: string | null;
  manifest_artifact_id?: string | null;
};
export type ArtifactScope = "session" | "report";
export type ArtifactRefreshReason =
  | "retry"
  | "turn-terminal"
  | "export-completed"
  | "document-published"
  | "window-focus"
  | "permissions-changed";
type ExportFmt = CanvasExportFmt;

export function fmtArtifactSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

// Sprint pptx-from-template M1: badge keyed on the asset `kind` (NOT the
// overloaded `source` — fixes the pre-existing stale "sandbox" case that never
// matched the real "python_sandbox"/"sandbox_output" source values).
function kindLabel(
  kind: string | undefined, source: string,
): { text: string; color: string } {
  switch (kind) {
    case "deliverable":    return { text: "OUT", color: "var(--c-accent)" };
    case "web_clip":       return { text: "WEB", color: "#6366f1" };
    case "user_upload":    return { text: "UP", color: "#10b981" };
    case "tool_data":      return { text: "DATA", color: "var(--c-accent-2, #3F7A65)" };
    case "sandbox_output": return { text: "SBX", color: "#8b5cf6" };
    case "canvas_export":  return { text: "EXP", color: "#f59e0b" };
    default:
      // Legacy rows with no kind → derive from source for back-compat.
      if (source === "bi_fetch_rows") return { text: "DATA", color: "var(--c-accent)" };
      if (source === "python_sandbox" || source === "sandbox_output")
        return { text: "SBX", color: "#8b5cf6" };
      if (source === "upload") return { text: "UP", color: "#10b981" };
      if (source === "canvas_export") return { text: "EXP", color: "#f59e0b" };
      return { text: (kind || source || "?").slice(0, 4).toUpperCase(), color: "var(--c-ink-3)" };
  }
}

function artifactListPath(sid: string, scope: ArtifactScope): string {
  return scope === "report"
    ? `/api/v1/canvas/by-session/${encodeURIComponent(sid)}/artifacts`
    : `/api/v1/sessions/${encodeURIComponent(sid)}/artifacts`;
}

function artifactDownloadPath(sid: string, id: string, scope: ArtifactScope): string {
  return appPath(scope === "report"
    ? `/api/v1/canvas/by-session/${encodeURIComponent(sid)}/artifacts/${encodeURIComponent(id)}`
    : `/api/v1/files/${encodeURIComponent(id)}`);
}

function artifactContentPath(sid: string, id: string, scope: ArtifactScope): string {
  return appPath(scope === "report"
    ? `/api/v1/canvas/by-session/${encodeURIComponent(sid)}/artifacts/${encodeURIComponent(id)}`
    : `/api/v1/sessions/${encodeURIComponent(sid)}/artifacts/${encodeURIComponent(id)}/content`);
}

function exportFormat(filename: string): ExportFmt | null {
  const suffix = filename.toLowerCase().split(".").pop();
  return suffix === "pdf" || suffix === "pptx" || suffix === "xlsx" || suffix === "docx"
    ? suffix
    : null;
}

async function fetchArtifacts(sid: string, scope: ArtifactScope, q?: string): Promise<ArtifactItem[]> {
  const path = artifactListPath(sid, scope);
  const r = await fetch(q ? `${path}?${new URLSearchParams({ q })}` : path);
  // ξ C3: 404 means session not yet in session_store (Data tab opened before new turn done,
  // or session truly missing). Treat 404 as empty array + empty state, not an error; actual errors (500/4xx) still throw.
  if (r.status === 404 && scope === "session") {
    return [];
  }
  if (!r.ok) throw new ResultsWorkspaceError(classifyResultsResponse(r.status));
  const data = await r.json();
  return data.items ?? [];
}

export function useArtifactItems(
  sessionId: string | null,
  scope: ArtifactScope,
  search = "",
  enabled = true,
): {
  items: ArtifactItem[] | null;
  error: ResultsWorkspaceErrorClass | null;
  retry: () => void;
  refresh: (reason: ArtifactRefreshReason) => Promise<ArtifactItem[]>;
} {
  const requestKey = `${scope}:${sessionId ?? ""}:${search}`;
  const [items, setItems] = useState<ArtifactItem[] | null>(null);
  const [error, setError] = useState<ResultsWorkspaceErrorClass | null>(null);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const activeRequestKeyRef = useRef(requestKey);
  const requestGenerationRef = useRef(0);
  activeRequestKeyRef.current = requestKey;

  const refresh = useCallback(async (
    _reason: ArtifactRefreshReason,
  ): Promise<ArtifactItem[]> => {
    if (!enabled) return [];
    const generation = requestGenerationRef.current + 1;
    requestGenerationRef.current = generation;
    const loadKey = requestKey;
    if (!sessionId) {
      if (
        activeRequestKeyRef.current === loadKey
        && requestGenerationRef.current === generation
      ) {
        setItems([]);
        setError(null);
        setLoadedFor(loadKey);
      }
      return [];
    }
    try {
      const rows = await fetchArtifacts(
        sessionId,
        scope,
        search || undefined,
      );
      if (
        activeRequestKeyRef.current === loadKey
        && requestGenerationRef.current === generation
      ) {
        setItems(rows);
        setError(null);
        setLoadedFor(loadKey);
        return rows;
      }
      return [];
    } catch (cause) {
      if (
        activeRequestKeyRef.current === loadKey
        && requestGenerationRef.current === generation
      ) {
        setItems([]);
        setError(classifyResultsError(cause));
        setLoadedFor(loadKey);
      }
      return [];
    }
  }, [enabled, requestKey, scope, search, sessionId]);
  const retry = useCallback(() => {
    void refresh("retry");
  }, [refresh]);

  useEffect(() => {
    if (enabled) return;
    requestGenerationRef.current += 1;
    setItems(null);
    setError(null);
    setLoadedFor(null);
  }, [enabled, requestKey]);

  useEffect(() => {
    if (!enabled) return;
    const timer = setTimeout(() => {
      void refresh("permissions-changed");
    }, search ? 200 : 0);
    return () => {
      clearTimeout(timer);
    };
  }, [enabled, refresh, search]);

  if (!enabled) return { items: null, error: null, retry, refresh };
  return loadedFor === requestKey
    ? { items, error, retry, refresh }
    : { items: null, error: null, retry, refresh };
}

export function DataTabFiles({
  sessionId,
  scope = "session",
  focusArtifactId = null,
  onFocusedArtifactChange,
  onRegenerateExport,
  viewerMode,
  onViewerModeChange,
  viewerOnly = false,
  artifactItems,
  artifactItemsError = null,
  onArtifactItemsRetry,
  onReferenceArtifact,
  onDownloadArtifact,
  resultCapabilities,
  resultRole,
  downloadLabel,
  onOpenArtifactHistory,
  onOpenFailed,
}: {
  sessionId: string | null;
  scope?: ArtifactScope;
  focusArtifactId?: string | null;
  onFocusedArtifactChange?: (artifactId: string | null) => void;
  onRegenerateExport?: (fmt: ExportFmt) => void;
  viewerMode?: ResultsViewerMode;
  onViewerModeChange?: (
    mode: ResultsViewerMode,
    history?: "push" | "replace",
  ) => void;
  viewerOnly?: boolean;
  artifactItems?: ArtifactItem[] | null;
  artifactItemsError?: ResultsWorkspaceErrorClass | null;
  onArtifactItemsRetry?: () => void;
  onReferenceArtifact?: (item: ArtifactItem) => void;
  onDownloadArtifact?: (item: ArtifactItem) => void;
  resultCapabilities?: ReadonlySet<ResultCapability>;
  resultRole?: ArtifactResultRole;
  downloadLabel?: string;
  onOpenArtifactHistory?: () => void;
  onOpenFailed?: (errorClass: ResultsWorkspaceErrorClass) => void;
}) {
  const t = useTranslations("data_files");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<ArtifactItem | null>(null);
  const getArtifactId = useCallback((item: ArtifactItem) => item.id, []);
  const useProvidedItems = viewerOnly && artifactItems !== undefined;
  const fetched = useArtifactItems(
    sessionId,
    scope,
    search,
    !useProvidedItems,
  );
  const items = useProvidedItems ? artifactItems : fetched.items;
  const err = useProvidedItems ? artifactItemsError : fetched.error;
  const retryItems = useProvidedItems ? onArtifactItemsRetry : fetched.retry;
  const visibleSelected =
    selected && items?.some((item) => item.id === selected.id)
      ? selected
      : null;
  const reportedOpenFailureRef = useRef<string | null>(null);

  useEffect(() => {
    let failure: ResultsWorkspaceErrorClass | null = null;
    if (viewerOnly && err) failure = err;
    else if (viewerOnly && items && focusArtifactId && !visibleSelected) {
      failure = "unavailable";
    }
    const key = failure ? `${focusArtifactId ?? "list"}:${failure}` : null;
    if (!key) {
      reportedOpenFailureRef.current = null;
      return;
    }
    if (reportedOpenFailureRef.current === key) return;
    reportedOpenFailureRef.current = key;
    onOpenFailed?.(failure as ResultsWorkspaceErrorClass);
  }, [err, focusArtifactId, items, onOpenFailed, viewerOnly, visibleSelected]);

  useFocusedItemScroll({
    focusId: focusArtifactId,
    items,
    dataAttribute: "data-artifact-id",
    searchValue: search,
    getItemId: getArtifactId,
    setSearchValue: setSearch,
    setSelectedItem: setSelected,
  });

  if (!sessionId) {
    return (
      <div className="v2-empty">
        <p>{t("empty_no_session")}</p>
      </div>
    );
  }

  if (viewerOnly) {
    if (err) {
      return (
        <div className="v2-empty" role="alert">
          <p>{t(`error_${err}`)}</p>
          {retryItems ? (
            <button type="button" onClick={retryItems}>{t("retry")}</button>
          ) : null}
        </div>
      );
    }
    if (!items || (focusArtifactId && !visibleSelected)) {
      return (
        <div className="v2-empty">
          <p>{items ? t("focused_missing") : t("loading")}</p>
          {items && retryItems ? (
            <button type="button" onClick={retryItems}>{t("retry")}</button>
          ) : null}
        </div>
      );
    }
    if (!visibleSelected) {
      return (
        <div className="v2-empty">
          <p>{t("focused_missing")}</p>
        </div>
      );
    }
    return (
      <FilePreviewer
        key={visibleSelected.id}
        item={visibleSelected}
        sessionId={sessionId}
        scope={scope}
        standalone
        viewerMode={viewerMode}
        onViewerModeChange={onViewerModeChange}
        onRegenerateExport={onRegenerateExport}
        onReference={onReferenceArtifact
          ? () => onReferenceArtifact(visibleSelected)
          : undefined}
        onDownload={onDownloadArtifact
          ? () => onDownloadArtifact(visibleSelected)
          : undefined}
        capabilities={resultCapabilities}
        resultRole={resultRole}
        downloadLabel={downloadLabel}
        onOpenHistory={onOpenArtifactHistory}
        onOpenFailed={onOpenFailed}
        onClose={onFocusedArtifactChange
          ? () => onFocusedArtifactChange(null)
          : undefined}
      />
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", maxWidth: 860, width: "100%", margin: "0 auto" }}>
      <div style={{
        padding: "10px 14px", borderBottom: "1px solid var(--c-line)",
        display: "flex", gap: 10, alignItems: "center",
      }}>
        <input
          type="text"
          placeholder={t("search_placeholder")}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{
            flex: 1, padding: "6px 10px", fontSize: "0.75rem",
            background: "var(--c-surface-2)",
            border: "1px solid var(--c-line)", borderRadius: 4,
            color: "var(--c-ink)",
          }}
        />
        <span style={{ fontSize: "0.6875rem", color: "var(--c-ink-3)" }}>
          {items ? t("count", { count: items.length }) : t("loading")}
        </span>
      </div>

      {err ? (
        <div style={{ padding: 20, color: "var(--c-err, crimson)" }}>
          {t(`error_${err}`)}
          {retryItems ? (
            <button type="button" onClick={retryItems}>{t("retry")}</button>
          ) : null}
        </div>
      ) : null}

      <div style={{ display: "flex", flex: 1, overflow: "hidden" }}>
        {/* File-card list */}
        <div style={{
          width: visibleSelected ? "40%" : "100%",
          overflowY: "auto", padding: 14,
          display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))",
          gap: 10, alignContent: "start",
          borderRight: visibleSelected ? "1px solid var(--c-line)" : "none",
        }}>
          {items && items.length === 0 ? (
            <div style={{ gridColumn: "1/-1", color: "var(--c-ink-3)", fontSize: "0.75rem" }}>
              {t("session_empty")}
            </div>
          ) : null}
          {items?.map((it) => {
            const src = kindLabel(it.kind, it.source);
            const unverified = it.trust === "unverified";
            const legacyExport = it.trust?.startsWith("export_copy_legacy_") ?? false;
            const isDeck = it.kind === "deliverable";
            const active = visibleSelected?.id === it.id;
            return (
              <button
                key={it.id}
                data-artifact-id={it.id}
                type="button"
                onClick={() => {
                  setSelected(it);
                  onFocusedArtifactChange?.(it.id);
                }}
                style={{
                  textAlign: "left",
                  padding: "10px 12px",
                  background: active ? "var(--c-surface-2)" : "var(--c-surface)",
                  border: `1px solid ${
                    active || focusArtifactId === it.id ? "var(--c-accent)" : "var(--c-line)"
                  }`,
                  borderRadius: 6,
                  cursor: it.preview_available ? "pointer" : "default",
                  fontFamily: "inherit",
                  color: "var(--c-ink)",
                  display: "flex", flexDirection: "column", gap: 6,
                }}
              >
                <div style={{
                  display: "flex", gap: 6, alignItems: "center",
                }}>
                  <span style={{
                    fontSize: "0.5625rem", padding: "1px 5px", borderRadius: 3,
                    color: src.color, border: `1px solid ${src.color}40`,
                    fontFamily: "var(--font-v2-mono)", fontWeight: 600,
                  }}>{src.text}</span>
                  {unverified ? (
                    <span title={t("unverified_hint")} style={{
                      fontSize: "0.5625rem", padding: "1px 5px", borderRadius: 3,
                      color: "var(--c-err-ink)", border: "1px solid var(--c-err-line)",
                      fontFamily: "var(--font-v2-mono)", fontWeight: 600,
                    }}>{t("unverified")}</span>
                  ) : null}
                  {legacyExport ? (
                    <span title={t("export_legacy_hint")} style={{
                      fontSize: "0.5625rem", padding: "1px 5px", borderRadius: 3,
                      color: "var(--c-warn-ink)", border: "1px solid var(--c-warn-line)",
                      fontFamily: "var(--font-v2-mono)", fontWeight: 600,
                    }}>{t("export_legacy")}</span>
                  ) : null}
                  <span style={{ fontSize: "0.625rem", color: "var(--c-ink-3)" }}>
                    {fmtArtifactSize(it.size)}
                  </span>
                  {isDeck ? (
                    // Deliverable deck → prominent download (does not open preview).
                    <a
                      href={artifactDownloadPath(sessionId, it.id, scope)}
                      download={it.filename}
                      onClick={(e) => e.stopPropagation()}
                      title={t("download")}
                      style={{
                        marginLeft: "auto", fontSize: "0.6875rem", fontWeight: 600,
                        color: "var(--c-accent)", textDecoration: "none",
                      }}
                    >⬇ {t("download")}</a>
                  ) : null}
                </div>
                <div style={{
                  fontSize: "0.75rem", fontWeight: 500,
                  overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                }}>
                  {it.filename}
                </div>
                <div style={{ fontSize: "0.625rem", color: "var(--c-ink-3)" }}>
                  {/* Feishu 07.06 #27: malformed created_at gives Invalid Date; formatDateTime
                      (Intl.DateTimeFormat) throws RangeError, crashing the card list. Guard invalid dates. */}
                  {(() => {
                    const d = new Date(it.created_at);
                    return Number.isNaN(d.getTime())
                      ? (it.created_at ?? "")
                      : formatDateTime(d, getCurrentLocale());
                  })()}
                </div>
              </button>
            );
          })}
        </div>

        {/* Preview panel */}
        {visibleSelected ? (
          <FilePreviewer
            key={visibleSelected.id}
            item={visibleSelected}
            sessionId={sessionId}
            scope={scope}
            onClose={() => setSelected(null)}
            onRegenerateExport={onRegenerateExport}
            onReference={onReferenceArtifact
              ? () => onReferenceArtifact(visibleSelected)
              : undefined}
            onDownload={onDownloadArtifact
              ? () => onDownloadArtifact(visibleSelected)
              : undefined}
            onOpenFailed={onOpenFailed}
          />
        ) : null}
      </div>
    </div>
  );
}

export function FilePreviewer({
  item,
  sessionId,
  scope,
  onClose,
  standalone = false,
  viewerMode,
  onViewerModeChange,
  onRegenerateExport,
  onReference,
  onDownload,
  capabilities: capabilitiesProp,
  resultRole: resultRoleProp,
  downloadLabel,
  onOpenHistory,
  onOpenFailed,
}: {
  item: ArtifactItem;
  sessionId: string;
  scope: ArtifactScope;
  onClose?: () => void;
  standalone?: boolean;
  viewerMode?: ResultsViewerMode;
  onViewerModeChange?: (
    mode: ResultsViewerMode,
    history?: "push" | "replace",
  ) => void;
  onRegenerateExport?: (fmt: ExportFmt) => void;
  onReference?: () => void;
  onDownload?: () => void;
  capabilities?: ReadonlySet<ResultCapability>;
  resultRole?: ArtifactResultRole;
  downloadLabel?: string;
  onOpenHistory?: () => void;
  onOpenFailed?: (errorClass: ResultsWorkspaceErrorClass) => void;
}) {
  const dataT = useTranslations("data_files");
  // Feishu 07.06 #21: unified ArtifactPreview header means FilePreviewer no longer draws its own copy.
  const resultsT = useTranslations("results_workspace");
  const [localMode, setLocalMode] = useState<ResultsViewerMode>("preview");
  const [feedbackRecorded, setFeedbackRecorded] = useState(false);
  const profile = deriveArtifactViewerProfile(item.mime, item.filename);
  const viewerModes = viewerModesForProfile(profile);
  const requestedMode = viewerMode ?? localMode;
  const mode = normalizeViewerMode(requestedMode, viewerModes);
  const capabilities = capabilitiesProp ?? deriveArtifactCapabilities({
    item,
    readOnly: !onReference,
    reportOnly: scope === "report",
  });
  const resultRole = resultRoleProp ?? artifactResultRole(item);
  const representation = useArtifactRepresentation({ sessionId, artifactId: item.id, scope, mode, profile });
  const preview = representation.data;
  const err = representation.error;
  const failureRef = useRef(onOpenFailed);
  failureRef.current = onOpenFailed;
  useEffect(() => { if (err) failureRef.current?.(err); }, [err]);

  useEffect(() => {
    if (viewerMode === undefined) setLocalMode(viewerModes[0] ?? "details");
    setFeedbackRecorded(false);
    // The supported mode set is a pure function of the newly selected object.
    // Capability changes are normalized by the next effect without resetting a
    // still-valid local choice.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [item.id, viewerMode]);

  const changeMode = useCallback((value: ResultsViewerMode) => {
    if (viewerMode === undefined) setLocalMode(value);
    onViewerModeChange?.(value);
  }, [onViewerModeChange, viewerMode]);

  const recordUnsupportedFeedback = useCallback(() => {
    if (feedbackRecorded || !capabilities.has("feedback_unsupported")) return;
    trackResultsWorkspace({
      action: "unsupported_feedback",
      resultType: "artifact",
      entryPoint: "navigator",
      mode,
      errorClass: "unsupported",
    });
    setFeedbackRecorded(true);
    toast({ kind: "info", text: resultsT("unsupported_feedback_recorded") });
  }, [capabilities, feedbackRecorded, mode, resultsT]);

  useEffect(() => {
    if (mode !== requestedMode) {
      onViewerModeChange?.(mode, "replace");
      if (viewerMode === undefined) setLocalMode(mode);
    }
  }, [
    mode,
    onViewerModeChange,
    requestedMode,
    viewerMode,
  ]);

  return (
    <div className={standalone ? "result-file-viewer standalone" : "result-file-viewer"}>
      {item.trust?.startsWith("export_copy_legacy_") ? (
        <div role="note" style={{
          padding: "10px 12px", border: "1px solid var(--c-warn-line)", borderRadius: 6,
          color: "var(--c-warn-ink)", background: "var(--c-warn-soft)", fontSize: "0.6875rem",
          display: "flex", alignItems: "center", gap: 10,
        }}>
          <span style={{ flex: 1 }}>{dataT(item.trust)}</span>
          {item.trust === "export_copy_legacy_regenerable"
          && onRegenerateExport
          && exportFormat(item.filename) ? (
            <button
              type="button"
              className="rc-pill-btn accept"
              onClick={() => onRegenerateExport(exportFormat(item.filename) as ExportFmt)}
            >
              {dataT("export_regenerate")}
            </button>
            ) : null}
        </div>
      ) : null}
      <Tabs value={mode} onValueChange={changeMode}>
        <TabList appearance="segmented" className="result-viewer-modes" aria-label={resultsT("viewer_modes")}>
          {viewerModes.map((value) => (
            <Tab key={value} value={value} className={mode === value ? "active" : undefined}>
              {resultsT(`mode_${value}`)}
            </Tab>
          ))}
        </TabList>
      </Tabs>
      {/* Feishu 07.06 #21: remove outer custom header; one ArtifactPreview header owns close/fullscreen/download
          in one row, showing filename + type/size once (previously duplicate headers split the actions). */}
      {mode === "details" && preview?.details && !err ? (
        <ArtifactDetails
          item={{
            ...item,
            ...preview.details,
            // Older backends return storage trust here, not the evaluated catalog status.
            trust: item.trust,
          }}
          downloadHref={artifactDownloadPath(sessionId, item.id, scope)}
          onReference={onReference}
          onDownload={onDownload}
          capabilities={capabilities}
          resultRole={resultRole}
          mode={mode}
          downloadLabel={downloadLabel}
          feedbackRecorded={feedbackRecorded}
          onViewerModeChange={changeMode}
          onOpenHistory={onOpenHistory}
          onFeedbackUnsupported={recordUnsupportedFeedback}
          onClose={onClose}
        />
      ) : (
        <ArtifactPreview
          key={`${representation.contextKey}:${representation.scopeUnavailable ? "unavailable" : "authorized"}`}
          fileId={item.id}
          filename={item.filename}
          mime={item.mime}
          sizeLabel={fmtArtifactSize(item.size)}
          preview={preview}
          profile={profile}
          mode={mode}
          loading={representation.loading}
          error={err ? dataT(`error_${err}`) : null}
          onRetry={representation.retry}
          onOpenFailed={onOpenFailed}
          downloadHref={artifactDownloadPath(sessionId, item.id, scope)}
          contentHref={artifactContentPath(sessionId, item.id, scope)}
          sizeBytes={item.size}
          trust={item.trust}
          onReference={onReference}
          onDownload={onDownload}
          capabilities={representation.scopeUnavailable ? new Set<ResultCapability>() : capabilities}
          resultRole={resultRole}
          downloadLabel={downloadLabel}
          feedbackRecorded={feedbackRecorded}
          onViewerModeChange={changeMode}
          onOpenHistory={onOpenHistory}
          onFeedbackUnsupported={recordUnsupportedFeedback}
          onClose={onClose}
        />
      )}
    </div>
  );
}

function ArtifactDetails({
  item,
  downloadHref,
  onReference,
  onDownload,
  capabilities,
  resultRole,
  mode,
  downloadLabel,
  feedbackRecorded,
  onViewerModeChange,
  onOpenHistory,
  onFeedbackUnsupported,
  onClose,
}: {
  item: ArtifactItem;
  downloadHref: string;
  onReference?: () => void;
  onDownload?: () => void;
  capabilities: ReadonlySet<ResultCapability>;
  resultRole: ArtifactResultRole;
  mode: ResultsViewerMode;
  downloadLabel?: string;
  feedbackRecorded: boolean;
  onViewerModeChange: (mode: ResultsViewerMode) => void;
  onOpenHistory?: () => void;
  onFeedbackUnsupported: () => void;
  onClose?: () => void;
}) {
  const t = useTranslations("results_workspace");
  const viewerT = useTranslations("artifact_viewer");
  const createdAt = new Date(item.created_at);
  const rows = [
    [t("detail_filename"), item.filename],
    [t("detail_type"), item.mime || "—"],
    [t("detail_size"), fmtArtifactSize(item.size)],
    [t("detail_source"), humanArtifactSource(item, t)],
    [t("detail_kind"), item.kind || "—"],
    [t("detail_origin"), item.origin || "—"],
    [t("detail_trust"), item.trust || "—"],
    [
      t("detail_created"),
      Number.isNaN(createdAt.getTime())
        ? item.created_at
        : formatDateTime(createdAt, getCurrentLocale()),
    ],
    [t("detail_id"), item.id],
  ];
  return (
    <div className="artifact-details">
      <div className="artifact-details-head">
        <div>
          <strong title={item.filename}>{item.filename}</strong>
          <span>{t("details_exact_object")}</span>
        </div>
        <div className="artifact-viewer-actions">
          <ResultObjectActions
            artifactId={item.id}
            capabilities={capabilities}
            role={resultRole}
            mode={mode}
            downloadHref={downloadHref}
            downloadLabel={downloadLabel}
            feedbackRecorded={feedbackRecorded}
            onReference={onReference}
            onDownload={onDownload}
            onModeChange={onViewerModeChange}
            onOpenHistory={onOpenHistory}
            onFeedbackUnsupported={onFeedbackUnsupported}
          />
          {onClose ? (
            <button type="button" onClick={onClose}>
              <X size={13} aria-hidden="true" />
              {viewerT("close")}
            </button>
          ) : null}
        </div>
      </div>
      <dl>
        {rows.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function humanArtifactSource(
  item: ArtifactItem,
  t: ReturnType<typeof useTranslations>,
): string {
  if (item.kind === "user_upload" || item.source === "upload") {
    return t("source_upload");
  }
  if (item.kind === "canvas_export" || item.source === "canvas_export") {
    return t("source_export");
  }
  if (item.kind === "web_clip") return t("source_web");
  if (
    item.kind === "sandbox_output"
    || item.source === "python_sandbox"
    || item.source === "sandbox_output"
  ) {
    return t("source_sandbox");
  }
  if (item.kind === "tool_data" || item.source === "bi_fetch_rows") {
    return t("source_data");
  }
  return item.source || t("source_generated");
}
