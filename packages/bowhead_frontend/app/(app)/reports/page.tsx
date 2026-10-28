/**
 * P4.2 Reports library — list all canvases for the current user across sessions.
 *
 * Route /reports.
 *
 * Default updated_desc sort, with search / space filtering / deletion. Clicking a card opens its session.
 * Paging (Header, Language & Paging Spec §3, D8): batches of 50 through useReportsList with a manual
 * "Load more" footer; the toolbar shows the true total.
 */

"use client";

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useState, useMemo, useRef } from "react";
import Link from "next/link";
import { PageFrame, PageHeader } from "@/components/shell/page-frame";
import { REPORTS_PAGE_SIZE, ReportListFooter } from "@/components/reports/report-list-footer";
import {
  ErrorDiagnosticsInline,
  shownError,
  useCopyDiagnosticsToastAction,
} from "@/components/error-notice";
import {
  exportJobDownloadUrl,
  deleteCanvas,
  setCanvasStatus,
  type ReportListItem,
  type ReportListQuery,
  type ReportStatus,
} from "@/lib/api";
import { useReportsList } from "@/lib/use-reports-list";
import { ReportStatusControl } from "@/components/canvas/report-status";
import { reportStatusTransitions } from "@/lib/report-status-transitions";
import { MenuFloating, useMenu, useMenuItem } from "@/components/ui/menu";
import { useCanvasExport } from "@/lib/use-canvas-export";
import { ExportToast, ExportsDrawer } from "@/components/canvas/export-ui";
import { reportDisplayTitle } from "@/lib/report-title";
import { copyPermissionedReportLink, reportExportHrefForSession, reportHrefForSession } from "@/lib/report-share";
import { Doc, FileDown, Search, Spinner } from "@/components/icons";
import { track } from "@/lib/metrics";
import { ShareDialog } from "@/components/share/share-dialog";
import { ShAudienceBadge, ShIcon, ShPermTag, T } from "@/components/share/share-ui";
import { getActiveSpace, useSpace } from "@/lib/use-space";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { ActiveFilterChips } from "@/components/ui/active-filter-chips";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import { FilterMenuButton } from "@/components/ui/filter-menu-button";
import { ListTime as SharedListTime } from "@/components/ui/list-time";
import { useEntitlement } from "@/lib/entitlement-store";
import { toast } from "@/lib/use-toast";
import { appPath } from "@/lib/base-path";
import type { ExportFmt } from "@/lib/use-canvas-export";

type SortKey = "updated_desc" | "sections_desc" | "title_asc";

// B2: hide 0-section reports by default — many empty canvases (turns that produced no sections)
// clutter the list. Keep a toggle to reveal them; persist the choice in localStorage.
const SHOW_EMPTY_KEY = "bowhead.reports.show_empty";

/** For server errors with status codes, do not blame the network; request a network check only when no request was sent. */
function loadFailedKey(error: unknown): "load_failed_detail" | "load_failed_offline" {
  return error && typeof error === "object" && typeof (error as { status?: unknown }).status === "number"
    ? "load_failed_detail"
    : "load_failed_offline";
}

export default function ReportsPage() {
  const t = useTranslations("reports_page");
  const tStatus = useTranslations("report_status");
  const copyDiagnosticsAction = useCopyDiagnosticsToastAction();
  // Batch 4b: hide "分享" for read-only accounts (same rule as the workbench report toolbar).
  const entitlement = useEntitlement();
  const hideShareForReadOnly = entitlement.readOnly;
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<SortKey>("updated_desc");
  // An empty default omits workspace_id and lists reports from every Space.
  // Follow the active Space so newly created Spaces start with their own report library.
  // Sessions already defaulted to the current space (`fetchSessions` falls back to `getActiveSpaceId()`);
  // align here. Retain "全部空间" in the dropdown so users can manually return to the cross-space overview.
  // ⚠ Empty localStorage (new browser / incognito) falls back to `""` = all spaces, the same edge case as sessions.
  const [workspaceFilter, setWorkspaceFilter] = useState<string>(() => getActiveSpace(""));
  // B2: hide 0-section reports by default; persist the toggle in localStorage.
  const [showEmpty, setShowEmpty] = useState<boolean>(false);
  // B3: report being deleted (prevent double-click confirmation and racing duplicate DELETE calls).
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ReportListItem | null>(null);
  // R2: report whose status is changing (lock that card's status menu).
  const [statusBusyId, setStatusBusyId] = useState<string | null>(null);
  // Feishu 07.06 #25: per-row download menu state (stores report ID).
  // WP1: unified export hook — busy key derived from lastArgs (locks current-row button), drawer opens per row.
  const exportCtl = useCanvasExport();
  const [drawerCanvasId, setDrawerCanvasId] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const exportingKey = exportCtl.busy && exportCtl.state.lastArgs
    ? `${exportCtl.state.lastArgs.canvasId}:${exportCtl.state.lastArgs.fmt}`
    : null;
  // content-permissions S7: share-popover target.
  const [shareTarget, setShareTarget] = useState<{ rid: string; label: string } | null>(null);
  const workspace = useSpace();
  // The report library follows space switches. Reset the filter only when activeId **actually changes**;
  // synchronizing on every render would repeatedly overwrite the user's manually selected "全部空间".
  const lastActiveSpaceRef = useRef<string | null>(workspace.activeId);
  useEffect(() => {
    const next = workspace.activeId;
    if (next && next !== lastActiveSpaceRef.current) {
      lastActiveSpaceRef.current = next;
      setWorkspaceFilter(next);
    }
  }, [workspace.activeId]);
  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(SHOW_EMPTY_KEY);
      if (raw === "1") setShowEmpty(true);
    } catch { /* ignore */ }
  }, []);
  const toggleShowEmpty = useCallback(() => {
    setShowEmpty((v) => {
      const next = !v;
      try { window.localStorage.setItem(SHOW_EMPTY_KEY, next ? "1" : "0"); } catch { /* ignore */ }
      return next;
    });
  }, []);

  // One batch of 50 at a time; any change of search, Space, sort or "show empty" restarts from the first batch
  // (the hook drops responses of superseded queries by generation).
  const query = useMemo<ReportListQuery>(() => ({
    workspace_id: workspaceFilter || undefined,
    q: q || undefined,
    sort,
    limit: REPORTS_PAGE_SIZE,
    include_empty: showEmpty,
  }), [q, showEmpty, sort, workspaceFilter]);
  const list = useReportsList(query);
  const items = list.items;
  const loading = list.loading;
  const error = list.error ? shownError(t(loadFailedKey(list.error)), list.error) : null;
  const reload = list.reload;

  // Library telemetry follows each first-batch result (load / empty / error).
  useEffect(() => {
    if (list.loading) return;
    if (list.error) {
      track("bowhead_report_library_total", { action: "load_error" });
      const message = list.error instanceof Error ? list.error.message : String(list.error);
      if (message.includes("404")) track("bowhead_report_link_total", { action: "route_404", surface: "reports" });
      return;
    }
    track("bowhead_report_library_total", { action: list.items.length === 0 ? "empty" : "load_success" });
    // Only the first-batch outcome counts; later batches and silent refreshes do not re-report.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [list.loading, list.error]);

  // Conditions changed: the list restarts at the top; focus stays on the control the user changed.
  const queryKey = JSON.stringify(query);
  const firstQueryRef = useRef(true);
  useEffect(() => {
    if (firstQueryRef.current) {
      firstQueryRef.current = false;
      return;
    }
    const scroller = document.querySelector<HTMLElement>(".ui-page-frame-scroll");
    if (scroller) scroller.scrollTop = 0;
  }, [queryKey]);

  // "Show empty reports · N": reports hidden for having no sections, counted by the first page itself.
  const emptyCount = showEmpty ? null : list.emptyTotal;

  // "Load more": focus moves to the first row of the new batch, which flashes briefly; screen readers hear the
  // progress once ("Loaded 50. Showing 150 of 236").
  const [announcement, setAnnouncement] = useState("");
  const pendingFocusRef = useRef<number | null>(null);
  const [flashIndex, setFlashIndex] = useState<number | null>(null);
  const loadMore = useCallback(() => {
    pendingFocusRef.current = list.items.length;
    void list.loadMore();
  }, [list]);
  useEffect(() => {
    const from = pendingFocusRef.current;
    if (from === null || list.loadingMore) return;
    pendingFocusRef.current = null;
    if (list.loadMoreError || list.items.length <= from) return;
    const loaded = list.items.length - from;
    setAnnouncement(t("loaded_announce", { count: loaded, shown: list.items.length, total: Math.max(list.total, list.items.length) }));
    setFlashIndex(from);
    document.querySelector<HTMLElement>(`[data-report-index="${from}"] .reports-row-title`)?.focus();
    const timer = window.setTimeout(() => setFlashIndex(null), 1200);
    return () => window.clearTimeout(timer);
  }, [list.items.length, list.loadMoreError, list.loadingMore, list.total, t]);
  useEffect(() => setAnnouncement(""), [queryKey]);

  // content-permissions S7: "分享给我的" — scope=shared through the same paged hook (reverse grant lookup),
  // following the Space filter on the server; a failure hides the section without blocking the main list.
  const sharedQuery = useMemo<ReportListQuery>(() => ({
    scope: "shared",
    workspace_id: workspaceFilter || undefined,
    limit: REPORTS_PAGE_SIZE,
    include_empty: false,
  }), [workspaceFilter]);
  const sharedList = useReportsList(sharedQuery);
  const shared = useMemo(() => (sharedList.error ? [] : sharedList.items), [sharedList.error, sharedList.items]);
  const reloadShared = sharedList.reload;

  // image-slim sprint UI fix (#284): measured on 0.6.0 production — /reports did not refresh after turns; users had to reload.
  // Fix: listen for (a) window focus (user returns to this page) and (b) global 'bowhead.canvas.changed' events
  // (dispatched by turn-runner on SSE done). Silent refreshes refetch the first batch only and merge it by id,
  // keeping later batches and the scroll position (spec §3a).
  useEffect(() => {
    const refresh = () => {
      void reload({ silent: true });
      void reloadShared({ silent: true });
    };
    window.addEventListener("focus", refresh);
    window.addEventListener("bowhead.canvas.changed", refresh as EventListener);
    return () => {
      window.removeEventListener("focus", refresh);
      window.removeEventListener("bowhead.canvas.changed", refresh as EventListener);
    };
  }, [reload, reloadShared]);

  // feedback-0713 #10: dropdown options must not derive from filtered items — selecting space X then
  // refetching returns only X's reports, collapsing options to X (self-narrowing filter). Use the full useSpace()
  // space list as a base; merge workspace_id values actually seen in reports (sticky accumulation covering
  // old canvases storing Datarc IDs / historical reports from spaces already left, without losing them on filtering).
  const seenReportWorkspaceIdsRef = useRef<Set<string>>(new Set());
  const workspaces = useMemo(() => {
    const seen = seenReportWorkspaceIdsRef.current;
    [...items, ...shared].forEach((r) => seen.add(r.workspace_id));
    const all = new Set<string>(workspace.items.map((w) => w.id));
    seen.forEach((id) => all.add(id));
    return Array.from(all).sort();
  }, [items, shared, workspace.items]);
  const workspaceNameById = useMemo(
    () => new Map(workspace.items.map((w) => [w.id, w.name])),
    [workspace.items],
  );
  const reportWorkspaceNameById = useMemo(() => {
    const map = new Map<string, string>();
    [...items, ...shared].forEach((r) => {
      if (r.workspace_name) map.set(r.workspace_id, r.workspace_name);
    });
    return map;
  }, [items, shared]);
  const workspaceLabel = useCallback(
    (id: string) => reportWorkspaceNameById.get(id) || workspaceNameById.get(id) || id,
    [reportWorkspaceNameById, workspaceNameById],
  );

  // B2: the server already leaves out 0-section reports while the toggle is off (include_empty=false).
  const visibleItems = items;
  // feedback-0713 F3 (#10 follow-up): the shared section follows the Space filter (server-side, see sharedQuery).
  const visibleShared = useMemo(() => shared.filter((r) => r.sections_count > 0), [shared]);
  const total = Math.max(list.total, items.length);
  // Reports without sections hidden by the toggle (for the empty state when they are all there is).
  const hiddenEmpty = emptyCount ?? 0;

  // B3: use product confirmation before deletion, avoiding native confirm's system copy / focus behavior.
  // Ignore repeat triggers while deletingId is set to prevent duplicate DELETE / confirmation.
  // feedback-0713 R1: "同时删除相关对话" toggle in dialog (default off) — on makes backend jointly
  // soft-delete the source session (no longer in session list). Reset to off whenever the dialog opens.
  const [deleteSessionToo, setDeleteSessionToo] = useState(false);
  const confirmDelete = async () => {
    const item = deleteTarget;
    if (!item || deletingId === item.id) return;  // race guard
    setDeletingId(item.id);
    try {
      await deleteCanvas(item.id, { deleteSession: deleteSessionToo });
      list.removeItem(item.id);
      setDeleteTarget(null);
      track("bowhead_report_library_total", { action: deleteSessionToo ? "delete_with_session" : "delete" });
    } catch (e) {
      track("bowhead_report_library_total", { action: "delete_fail" });
      toast({ kind: "error", text: t("delete_failed"), action: copyDiagnosticsAction(e, "route") });
    } finally {
      setDeletingId(null);
    }
  };

  // R2: change report status (archive/restore/mark-verified) → silently reload after persistence for accurate effective_status.
  const onStatusChange = async (item: ReportListItem, to: ReportStatus) => {
    if (statusBusyId === item.id) return;
    setStatusBusyId(item.id);
    try {
      await setCanvasStatus(item.id, to);
      await reload({ silent: true });
      window.dispatchEvent(new Event("bowhead.canvas.changed"));
      track("bowhead_report_library_total", { action: `status_${to}` });
    } catch (e) {
      toast({ kind: "error", text: t("status_update_failed"), action: copyDiagnosticsAction(e, "route") });
    } finally {
      setStatusBusyId(null);
    }
  };

  const reportHref = useCallback((item: ReportListItem) => reportHrefForSession(item.session_id), []);

  const copyReportLink = async (item: ReportListItem) => {
    await copyPermissionedReportLink(item.session_id);
    track("bowhead_report_library_total", { action: "copy_link" });
  };

  // WP1 (design D5): consolidate start/poll/timeout into useCanvasExport;
  // bottom ExportToast presents terminal states (download / failure retry / timeout directing to drawer).
  // Library export **does not start a job here**; navigate to the workbench report surface for export.
  //
  // Reason: `exportCtl.start(id, fmt)` carries no v2 payload and bypasses `prepareCanvasExport`,
  // ⇒ necessarily takes the legacy endpoint (singular `POST /canvas/{id}/export`) without any fallback notice.
  // Same-content measurements on 116: xlsx 3 sheets/29 rows vs workbench V2 9 sheets/426 rows;
  // pdf **1 page / 3.8KB / 0 images** vs 6 pages / 143KB / actual images. V2 receipts can only come from
  // mounted, fully rendered reports (see `reportExportHrefForSession` comments); the library renders none,
  // so route to the already-rendering workbench rather than build another rendering stack here.
  const exportReport = (item: ReportListItem, fmt: ExportFmt) => {
    track("bowhead_canvas_export_total", { action: "start", fmt });
    window.location.href = appPath(reportExportHrefForSession(item.session_id, fmt));
  };

  return (
    // PageFrame (Header, Language & Paging Spec §2, D4, D10): the shared header without eyebrow or "Back to home";
    // the count moves to the toolbar.
    <PageFrame title={t("heading")}>
    <div className="reports-library">
      <PageHeader description={t("description")} />

      <section className="reports-library-controls" aria-label={t("filters_aria")}>
        <label className="reports-search">
          <Search aria-hidden="true" />
        <input
          type="text" value={q} onChange={(e) => setQ(e.target.value)}
          placeholder={t("search_placeholder")}
        />
        </label>
        {/* Polish spec 2f: "Space: All ⌄" / "Sort: Recently updated ⌄" menu buttons replace the native selects. */}
        <FilterMenuButton
          value={workspaceFilter}
          options={[
            { value: "", label: t("filter_all") },
            ...workspaces.map((w) => ({ value: w, label: workspaceLabel(w) })),
          ]}
          onChange={setWorkspaceFilter}
          format={(value) => t("filter_space", { value })}
          menuLabel={t("filter_workspace_aria")}
          active={Boolean(workspaceFilter)}
          testId="reports-space-filter"
        />
        <FilterMenuButton<SortKey>
          value={sort}
          options={[
            { value: "updated_desc", label: t("sort_updated") },
            { value: "sections_desc", label: t("sort_longest") },
            { value: "title_asc", label: t("sort_title") },
          ]}
          onChange={setSort}
          format={(value) => t("sort_label", { value })}
          menuLabel={t("sort_aria")}
          active={false}
          testId="reports-sort"
        />
        {/* B2: 0-section toggle. Default OFF (hide empty reports). admin/debug can reveal them. */}
        <button
          type="button"
          onClick={toggleShowEmpty}
          aria-pressed={showEmpty}
          title={showEmpty ? t("toggle_empty_on_title") : t("toggle_empty_off_title")}
          className="reports-filter-toggle"
          data-active={showEmpty ? "true" : "false"}
        >
          {showEmpty ? t("toggle_empty_on") : t("toggle_empty_off")}
          {emptyCount ? <span className="cnt">{emptyCount}</span> : null}
        </button>
        {/* Toolbar count (D8): the true number of reports matching the search and filters. */}
        {!loading && !error ? (
          <span className="reports-toolbar-count" data-testid="reports-total" aria-live="polite">
            {q ? t("count_found", { total }) : t("count_total", { total })}
          </span>
        ) : null}
      </section>

      {/* With at least 2 conditions met, show selected filters below the toolbar (tradeoff 20). */}
      <ActiveFilterChips
        filters={[
          ...(q ? [{ key: "q", label: `${t("filter_chip_search")}：${q}`, onRemove: () => setQ("") }] : []),
          ...(workspaceFilter
            ? [{ key: "workspace", label: `${t("filter_chip_space")}：${workspaceLabel(workspaceFilter)}`, onRemove: () => setWorkspaceFilter("") }]
            : []),
        ]}
        onClearAll={() => { setQ(""); setWorkspaceFilter(""); }}
      />

      {error ? (
        <>
          <section className="reports-state reports-state--error" role="alert">
            <strong>{t("load_failed")}</strong>
            <span>{error.message}</span>
            <button type="button" onClick={() => void reload()}>
              {t("retry")}
            </button>
          </section>
          <ErrorDiagnosticsInline error={error.error} surface="route" />
        </>
      ) : loading ? (
        <LoadingSkeleton rows={4} label={t("loading_aria")} />
      ) : visibleItems.length === 0 ? (
        <section className="reports-state">
          <Doc aria-hidden="true" />
          <strong>
            {q
              ? t("empty_no_match")
              : hiddenEmpty > 0
                ? t("empty_hidden")
                : workspaceFilter
                  ? t("empty_workspace_filtered")
                  : t("empty_none")}
          </strong>
          <span>
            {q
              ? t("empty_no_match_for", { q })
              : hiddenEmpty > 0
                ? t("empty_all_zero_sections", { count: hiddenEmpty })
                : workspaceFilter
                  ? t("empty_workspace_filtered_hint")
                  : t("empty_hint")}
          </span>
        </section>
      ) : (
        <section className="reports-list" aria-label={t("list_aria")}>
          {visibleItems.map((r, index) => {
            const href = reportHref(r);
            const title = reportDisplayTitle(r);
            const busy = exportingKey?.startsWith(`${r.id}:`) ?? false;
            const downloadDisabled = exportCtl.busy || r.sections_count === 0;
            const moreLabel = t("more_actions_for", { title });
            const deleteAction = {
              id: "delete",
              label: t("delete"),
              danger: true,
              disabled: busy,
              onSelect: () => { setDeleteSessionToo(false); setDeleteTarget(r); },
            };
            const copyAction = { id: "copy-link", label: t("copy_perm_link"), onSelect: () => void copyReportLink(r) };
            // The default state (active) carries no chip; its transitions (mark reviewed, archive) live in the row ⋯.
            const effectiveStatus = r.effective_status ?? r.status;
            const showStatus = effectiveStatus !== "active";
            const statusActions = showStatus
              ? []
              : reportStatusTransitions(r.status).map((transition) => ({
                id: `status-${transition.key}`,
                label: tStatus(`action_${transition.key}` as never),
                disabled: statusBusyId === r.id,
                onSelect: () => void onStatusChange(r, transition.to),
              }));
            return (
              // Polish spec 2f: one list row — title and "N sections · Space" on the left, the date in its own
              // right-aligned column, then the row actions. Phones merge the date into the second line and keep
              // only an always-visible ⋯ (spec §4).
              <article
                key={r.id}
                className="reports-row"
                data-report-index={index}
                data-flash={flashIndex === index ? "true" : undefined}
              >
                <div className="reports-row-main">
                  <div className="reports-row-title-line">
                    <Link href={appPath(href)} className="reports-row-title">
                      <Doc aria-hidden="true" />
                      <span>{title}</span>
                    </Link>
                    {r.audience && <span className="reports-row-audience"><ShAudienceBadge level={r.audience} n={r.share_count} /></span>}
                  </div>
                  <div className="reports-card-meta">
                    <span>{t("sections_count", { count: r.sections_count })}</span>
                    {/* Phones drop the Space name (spec §4: "N sections · date"); the dot belongs to the Space. */}
                    <span className="reports-row-space-group">
                      <span aria-hidden="true">·</span>
                      <span className="reports-row-space">{workspaceLabel(r.workspace_id)}</span>
                    </span>
                    <span className="reports-row-phone-time">
                      <span aria-hidden="true">·</span>
                      <ListTime iso={r.updated_at} />
                    </span>
                    {/* Phones move the visibility chip out of the title line so the title keeps its width. */}
                    {r.audience ? (
                      <span className="reports-row-phone-audience">
                        <ShAudienceBadge level={r.audience} n={r.share_count} />
                      </span>
                    ) : null}
                    {/* R2: status badge + transition menu, only for a non-default status (reviewed, stale, archived). */}
                    {showStatus ? (
                      <ReportStatusControl
                        storedStatus={r.status}
                        effectiveStatus={effectiveStatus}
                        quarantinedCount={r.quarantined_count ?? 0}
                        busy={statusBusyId === r.id}
                        onChange={(to) => onStatusChange(r, to)}
                      />
                    ) : null}
                  </div>
                </div>
                <div className="reports-row-time">
                  <ListTime iso={r.updated_at} />
                </div>
                <div className="reports-card-actions">
                  <Link href={appPath(href)} className="settings-button settings-button--secondary settings-button--sm reports-row-open">
                    {t("open")}
                  </Link>
                  {hideShareForReadOnly ? null : (
                    <button type="button" onClick={() => setShareTarget({ rid: r.session_id, label: title })} className="settings-button settings-button--secondary settings-button--sm">
                      {t("share")}
                    </button>
                  )}
                  {/* Feishu 07.06 #25: one "下载" button, then choose the format. */}
                  <ReportDownloadMenu
                    busy={busy}
                    disabled={downloadDisabled}
                    label={busy ? t("exporting") : t("download")}
                    onPick={(fmt) => void exportReport(r, fmt)}
                  />
                  {/* "复制权限链接" and "删除" (dangerous) live in the row-end ⋯ (batch four convention, 逐页验收 B). */}
                  <RowActionsMenu
                    className="reports-card-more"
                    label={moreLabel}
                    busy={deletingId === r.id}
                    busyLabel={t("deleting")}
                    actions={[...statusActions, copyAction, deleteAction]}
                  />
                </div>
                {/* Phones: every row action in one ⋯ (44px, always shown); opening is the title link. */}
                <RowActionsMenu
                  className="reports-row-phone-more"
                  label={moreLabel}
                  busy={deletingId === r.id}
                  busyLabel={t("deleting")}
                  actions={[
                    ...(hideShareForReadOnly ? [] : [{ id: "share", label: t("share"), onSelect: () => setShareTarget({ rid: r.session_id, label: title }) }]),
                    ...(["pdf", "pptx", "xlsx"] as const).map((fmt) => ({
                      id: `download-${fmt}`,
                      label: `${t("download")} ${fmt.toUpperCase()}`,
                      disabled: downloadDisabled,
                      onSelect: () => void exportReport(r, fmt),
                    })),
                    ...statusActions,
                    copyAction,
                    deleteAction,
                  ]}
                />
              </article>
            );
          })}
        </section>
      )}
      {/* Footer under the list, above "Shared with me" so that section stays reachable (D8). */}
      {!loading && !error && visibleItems.length > 0 ? (
        <ReportListFooter
          shown={visibleItems.length}
          total={total}
          hasMore={list.hasMore}
          loadingMore={list.loadingMore}
          loadMoreError={list.loadMoreError}
          searching={Boolean(q)}
          onLoadMore={loadMore}
          announcement={announcement}
        />
      ) : null}

      {visibleShared.length > 0 && (
        <section aria-label={t("shared_section_aria")} className="legacy-reports-shared">
          <div className="legacy-reports-shared-head">
            <span className="legacy-reports-shared-title">{t("shared_with_me")}</span>
            <span className="legacy-reports-shared-count">{Math.max(sharedList.total, visibleShared.length)}</span>
            <span className="legacy-reports-shared-spacer" />
            <span className="legacy-reports-shared-hint">{t("shared_managed_hint")}</span>
          </div>
          <div className="legacy-reports-shared-list">
            {visibleShared.map((r) => (
              <Link key={r.id} href={appPath(reportHref(r))} className="legacy-reports-shared-row">
                <span className="legacy-reports-shared-icon">
                  <Doc aria-hidden="true" />
                </span>
                <div className="legacy-reports-shared-text">
                  <div className="legacy-reports-shared-name">{reportDisplayTitle(r)}</div>
                  <div className="legacy-reports-shared-meta">{t("shared_by", { by: r.shared_by ?? t("colleague") })} · <ListTime iso={r.updated_at} /></div>
                </div>
                <ShPermTag label={t("can_view")} />
                <ShIcon name="chevR" size={12} style={{ color: T.ink4 }} />
              </Link>
            ))}
          </div>
          <ReportListFooter
            shown={visibleShared.length}
            total={Math.max(sharedList.total, visibleShared.length)}
            hasMore={sharedList.hasMore}
            loadingMore={sharedList.loadingMore}
            loadMoreError={sharedList.loadMoreError}
            searching={false}
            onLoadMore={() => void sharedList.loadMore()}
            announcement=""
          />
        </section>
      )}
      <ExportToast
        state={exportCtl.state}
        onDismiss={exportCtl.dismiss}
        onRetry={exportCtl.retry}
        onRetryDefaultTheme={exportCtl.retryWithDefaultTheme}
        onOpenHistory={() => setDrawerOpen(true)}
      />
      <ExportsDrawer
        canvasId={drawerCanvasId}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        // Regeneration also goes to the workbench; otherwise a second legacy path repeats the same issue here.
        onRegenerate={drawerCanvasId
          ? (fmt) => {
              const owner = items.find((entry) => entry.id === drawerCanvasId)
                ?? shared.find((entry) => entry.id === drawerCanvasId);
              if (!owner) return;
              window.location.href = appPath(reportExportHrefForSession(owner.session_id, fmt));
            }
          : undefined}
      />
    </div>
    {shareTarget && (
      <ShareDialog
        kind="report"
        resourceId={shareTarget.rid}
        label={shareTarget.label}
        onClose={() => setShareTarget(null)}
        onChanged={() => { void reload({ silent: true }); }}
      />
    )}
    {deleteTarget && (
      <ConfirmDialog
        title={t("delete_confirm_title", { title: reportDisplayTitle(deleteTarget) })}
        description={t("delete_confirm_description", { count: deleteTarget.sections_count })}
        confirmLabel={t("delete_confirm")}
        cancelLabel={t("cancel")}
        busy={deletingId === deleteTarget.id}
        tone="danger"
        onCancel={() => {
          if (!deletingId) setDeleteTarget(null);
        }}
        onConfirm={() => void confirmDelete()}
      >
        {/* feedback-0713 R1: jointly delete conversation toggle (default off). */}
        <label className="legacy-reports-delete-session">
          <input
            type="checkbox"
            checked={deleteSessionToo}
            onChange={(e) => setDeleteSessionToo(e.target.checked)}
            disabled={deletingId === deleteTarget.id}
            className="legacy-reports-delete-session-check"
          />
          <span>
            {t("delete_with_session_label")}
            <span className="legacy-reports-delete-session-hint">
              {t("delete_with_session_hint")}
            </span>
          </span>
        </label>
      </ConfirmDialog>
    )}
    </PageFrame>
  );
}

/** Feishu 07.06 #25: open Download to choose format. useMenu handles keyboard, Esc, outside clicks and focus restoration. */
function ReportDownloadMenu({
  busy,
  disabled,
  label,
  onPick,
}: {
  busy: boolean;
  disabled: boolean;
  label: string;
  onPick: (fmt: ExportFmt) => void;
}) {
  const [open, setOpen] = useState(false);
  const menu = useMenu({
    open: open && !busy && !disabled,
    onOpenChange: setOpen,
    placement: "bottom-start",
    offset: 4,
  });
  return (
    <div className="legacy-reports-download">
      <button
        type="button"
        ref={menu.refs.setReference}
        disabled={disabled}
        aria-busy={busy}
        className="settings-button settings-button--secondary settings-button--sm"
        {...menu.getReferenceProps()}
      >
        {busy ? <Spinner aria-hidden="true" /> : null}
        <span>{label}</span>
      </button>
      <MenuFloating menu={menu}>
        <div
          ref={menu.refs.setFloating}
          {...menu.getFloatingProps({ className: "reports-download-menu", "aria-label": label })}
        >
          {(["pdf", "pptx", "xlsx"] as const).map((fmt) => (
            <ReportDownloadItem key={fmt} fmt={fmt} onPick={onPick} />
          ))}
        </div>
      </MenuFloating>
    </div>
  );
}

function ReportDownloadItem({ fmt, onPick }: { fmt: ExportFmt; onPick: (fmt: ExportFmt) => void }) {
  const itemProps = useMenuItem({ onSelect: () => onPick(fmt) });
  return (
    <button type="button" {...itemProps}>
      <FileDown aria-hidden="true" /> {fmt.toUpperCase()}
    </button>
  );
}

/** List date (polish spec §3a): relative within 7 days, then month and day; the full time on hover. */
function ListTime({ iso }: { iso: string }) {
  return <SharedListTime className="reports-list-time" value={iso} empty="" />;
}
