"use client";

/**
 * kb-matrix M2 — real matrix (mx_*) main-view orchestration: snapshot loading + polling updates +
 * run flows (toolbar [▶ 运行 ▾] / column menu / single cell) + cost confirmation + run banner + add/edit column
 * + three cell operations (编辑值/标记已核/保留为文本) + single-cell rerun.
 * Document matrix step 1 (S2): header with breadcrumb, inline rename and ⋯ (Rename, Delete Matrix…); add files
 * (row under the grid) and remove files (row menu, multi-select). Editing is desktop only; phones stay read-only.
 * Step 1 (S3): run menu scopes (unfinished, stale, failed, trial, rerun all keeping verified and edited cells), the
 * cost breakdown with a trial option, and per-cell rerun buttons that show their cost.
 * Step 1 (S4): the review toolbar (search, status and value filters, sort, verified progress) filters and sorts the
 * snapshot client-side and keeps the view in the URL query; "整列标记已核" in the column menu; the side panel's history.
 * "查看失败" sets the status filter to failed. Phones show the progress only.
 * Step 1 (S8): an only-me matrix shows its badge; its creator changes who can see it from ⋯ after a confirmation that
 * names who loses access (or the refusal: personal files, other members' runs, navigation columns). Only-me matrices
 * take the creator's personal files and hide the source discovery purpose.
 * Component contract stays frozen from M1: grid/side panel consume only MatrixSnapshot (tech design decision 9).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";

import { ErrorDiagnosticsInline, type ShownError, shownError } from "@/components/error-notice";
import { notify } from "@/lib/use-toast";
import { useConfirmDialog } from "@/components/ui/confirm-dialog";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import {
  MATRIX_FILES_PER_CHANGE,
  type NewColumnIn,
  type RunEstimate,
  type RunScope,
  addColumn,
  addMatrixFiles,
  cancelRun,
  deleteColumn,
  deleteMatrix,
  estimateRun,
  fetchCellEvents,
  fetchMatrixVisibility,
  patchCell,
  removeMatrixFiles,
  renameMatrix,
  rerunCell,
  setMatrixVisibility,
  startRun,
  updateColumn,
  verifyCells,
  type CellsVerified,
  type MatrixVisibilityPreview,
} from "@/lib/kb-matrix/run-api";
import { MatrixApiError } from "@/lib/kb-matrix/api-error";
import { downloadMatrixExport } from "@/lib/kb-matrix/export-api";
import { kbMatrixMutationError } from "@/lib/kb-matrix/mutation-error";
import { kbMatrixRunErrorKey } from "@/lib/kb-matrix/run-error";
import { useCellRerunCredits } from "@/lib/kb-matrix/cell-estimate";
import { buildCellFromWire } from "@/lib/kb-matrix/snapshot-api";
import { patchMatrixCell, setMatrixSnapshot, useMatrixSnapshot } from "@/lib/kb-matrix/store";
import type { MatrixColumnVM, MatrixRowVM, MatrixSnapshot, MatrixVisibility } from "@/lib/kb-matrix/types";
import { runMenuCounts, snapshotRuns } from "@/lib/kb-matrix/types";
import {
  EMPTY_VIEW,
  type MatrixView,
  columnVerifyPreview,
  readMatrixView,
  verifiedProgress,
  viewIsFiltered,
  viewRows,
  writeMatrixView,
} from "@/lib/kb-matrix/review";
import { useIsMobile } from "@/lib/kb-matrix/use-is-mobile";
import { useMatrixPolling } from "@/lib/kb-matrix/use-matrix-poll";
import { withoutNextRouterHistoryMarkers } from "@/lib/url-session-nav";

import type { GraduateResult } from "@/lib/kb-matrix/graduate-api";

import { type CiteHoverInfo, type MatrixGridHandle, type MatrixRowControls, MatrixGrid } from "./matrix-grid";
import { AddColumnPanel } from "./add-column-panel";
import { AddFilesDialog } from "./add-files-dialog";
import { CitePopover } from "./cite-popover";
import { type ColMenuAction, type RunMenuAction, ColMenu, RunMenu } from "./column-menu";
import { GraduateDialog } from "./graduate-dialog";
import { IcCaret, IcCheck, IcDownload, IcGrid, IcPlay, IcPlus, IcX } from "./matrix-icons";
import { MatrixHeader } from "./matrix-header";
import { MatrixToolbar } from "./matrix-toolbar";
import { OptionsEditor } from "./options-editor";
import { TypePicker8 } from "./type-picker";
import { MatrixMobile } from "./matrix-mobile";
import { MatrixSkeleton } from "./matrix-skeleton";
import { RunBanners } from "./run-banner";
import { RemoveFilesDialog } from "./remove-files-dialog";
import { RunConfirmModal } from "./run-confirm-modal";
import { SidePanel } from "./side-panel";

type Focus = { fileId: string; columnId: string } | null;

/** §4.8: Esc respects editable controls (lost-input bug); do not close panels while focus is inside them. */
function escTargetIsEditable(target: EventTarget | null): boolean {
  return !!(target as HTMLElement | null)?.closest?.("input,textarea,select,[contenteditable]");
}

interface PendingRun {
  scope: RunScope;
  columnName?: string;
  estimate: RunEstimate | null;
  /** Step 1 (S3): run only the first rows with pending cells (`estimate.trial_file_ids`) first. */
  trial?: boolean;
  /** Estimate of the trial rows (null while loading). */
  trialEstimate?: RunEstimate | null;
  /** §4.2: confirmation variant after column editing (stale_confirm_* copy; secondary action = 仅对新行生效). */
  postEdit?: boolean;
  /** Focus-return button after close (run button or run/column menu trigger). */
  trigger?: HTMLElement | null;
}

/** The scope a confirmed run starts: the trial rows when the trial option is on. */
function confirmedScope(pending: PendingRun): RunScope {
  const rows = pending.estimate?.trial_file_ids ?? [];
  return pending.trial && rows.length ? { ...pending.scope, file_ids: rows } : pending.scope;
}

/** Rows the trial option offers: only when it runs fewer files than the full scope (or the trial was asked for). */
function trialOffer(pending: PendingRun): number {
  const rows = pending.estimate?.trial_file_ids?.length ?? 0;
  if (pending.postEdit || pending.scope.kind === "single_cell" || rows === 0) return 0;
  return pending.trial || (pending.estimate?.files ?? 0) > rows ? rows : 0;
}

const RUN_MENU_SCOPE: Record<RunMenuAction, RunScope["kind"]> = {
  all: "all_pending",
  stale: "stale",
  failed: "failed",
  trial: "all_pending",
  force: "force_all",
};

export function MatrixViewM2({ matrixId, onBack, onHome }: { matrixId: string; onBack: () => void; onHome?: () => void }) {
  const t = useTranslations("kbMatrix");
  const locale = useLocale();
  const { confirm, dialog: confirmDialog } = useConfirmDialog();
  // S8: only-me matrices are enabled in this deployment (`/me` `capabilities.kb_matrix_private`).
  const privateMatrices = useOptionalAuthSession()?.me?.capabilities?.kb_matrix_private === true;
  const storedSnapshot = useMatrixSnapshot();
  const [initialized, setInitialized] = useState(false);
  // A newly mounted permission scope must not paint the previous store contents.
  const snapshot = initialized && storedSnapshot?.matrixId === matrixId ? storedSnapshot : null;
  const [loadError, setLoadError] = useState<{ error: unknown } | null>(null);
  // §4.8: poll/reload 404/403 → neutral unavailable state (retry can recover; hot flag changes must not pretend deletion).
  const [unavailable, setUnavailable] = useState(false);
  const onUnavailable = useCallback(() => setUnavailable(true), []);
  const { reload } = useMatrixPolling(matrixId, snapshot, onUnavailable);
  const [focus, setFocus] = useState<Focus>(null);
  const [panel, setPanel] = useState<Focus>(null);
  // S4 review view (search, filters, sort): read from the URL query on load, written back as it changes, so back
  // and reload restore it. wave-2 [查看失败] is the status filter "failed".
  const [view, setViewState] = useState<MatrixView>(() => (typeof window === "undefined" ? EMPTY_VIEW : readMatrixView(window.location.search)));
  const setView = useCallback((next: MatrixView) => setViewState(next), []);
  const viewMatrix = useRef(matrixId);
  const [addcol, setAddcol] = useState(false);
  const [editCol, setEditCol] = useState<MatrixColumnVM | null>(null);
  const [runMenu, setRunMenu] = useState(false);
  const [colMenu, setColMenu] = useState<{ col: MatrixColumnVM; anchor: HTMLElement; viaKeyboard: boolean } | null>(null);
  // Whether the run menu was opened from the keyboard (shared menu: focus its first item).
  const [runMenuViaKeyboard, setRunMenuViaKeyboard] = useState(false);
  const [pendingRun, setPendingRun] = useState<PendingRun | null>(null);
  // M4 [转正为模板] dialog
  const [graduating, setGraduating] = useState(false);
  const [busy, setBusy] = useState(false);
  // S5: an export download in flight.
  const [exporting, setExporting] = useState(false);
  // S2: add files dialog, row selection and the rows waiting for removal confirmation.
  const [addingFiles, setAddingFiles] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  const [selected, setSelected] = useState<ReadonlySet<string>>(() => new Set());
  const [removing, setRemoving] = useState<{ rows: MatrixRowVM[]; error: string | null; refused: boolean } | null>(null);
  const [citePop, setCitePop] = useState<CiteHoverInfo | null>(null);
  const appRef = useRef<HTMLDivElement | null>(null);
  const runCaretRef = useRef<HTMLButtonElement | null>(null);
  const gridRef = useRef<MatrixGridHandle | null>(null);
  const isMobile = useIsMobile();

  // Another matrix brings its own query (links from the list carry none).
  useEffect(() => {
    if (viewMatrix.current === matrixId) return;
    viewMatrix.current = matrixId;
    setViewState(readMatrixView(window.location.search));
  }, [matrixId]);
  useEffect(() => {
    const params = writeMatrixView(window.location.search, view);
    if (params.toString() === new URLSearchParams(window.location.search).toString()) return;
    const url = new URL(window.location.href);
    url.search = params.toString();
    window.history.replaceState(withoutNextRouterHistoryMarkers(window.history.state), "", url);
  }, [view]);

  // ── Initial full fetch ──
  useEffect(() => {
    setMatrixSnapshot(null);
    setInitialized(true);
    setPanel(null);
    setFocus(null);
    setLoadError(null);
    setUnavailable(false);
    let cancelled = false;
    reload().catch((e: unknown) => !cancelled && setLoadError({ error: e }));
    return () => {
      cancelled = true;
      setMatrixSnapshot(null);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [matrixId]);

  const canEdit = snapshot?.canEdit ?? false;
  const canManage = snapshot?.canManage ?? false;
  // M3 §4.5: runBusy retired; active runs no longer disable Run (parallel runs; server
  // 409 matrix_active_runs_cap enforces the cap); disable only for in-flight requests (busy).
  const runs = snapshot ? snapshotRuns(snapshot) : [];
  // The rows the grid shows: the snapshot filtered and sorted by the review view.
  const searchLabels = useMemo(() => ({ boolTrue: t("bool_true"), boolFalse: t("bool_false") }), [t]);
  const visibleRows = useMemo(
    () => (snapshot ? viewRows(snapshot.rows, snapshot.columns, view, { labels: searchLabels, locale }) : []),
    [snapshot, view, searchLabels, locale],
  );
  const filtered = viewIsFiltered(view);

  const say = notify;

  const apiErr = useCallback(
    (e: unknown): string => {
      const message = kbMatrixMutationError(e);
      return message.values ? t(message.key, message.values) : t(message.key);
    },
    [t],
  );

  const runApiErr = useCallback((e: unknown): string => t(kbMatrixRunErrorKey(e)), [t]);

  // ── Run flow: estimate → dialog → confirm ──
  // The trial estimate prices `estimate.trial_file_ids`; it is fetched when the trial option turns on.
  const loadTrialEstimate = useCallback(
    async (scope: RunScope, est: RunEstimate) => {
      const rows = est.trial_file_ids ?? [];
      if (!rows.length) return;
      try {
        const trialEst = await estimateRun(matrixId, { ...scope, file_ids: rows });
        setPendingRun((cur) => (cur && cur.scope === scope ? { ...cur, trialEstimate: trialEst } : cur));
      } catch (e) {
        setPendingRun((cur) => (cur && cur.scope === scope ? { ...cur, trial: false } : cur));
        say("error", runApiErr(e));
      }
    },
    [matrixId, say, runApiErr],
  );

  const openRunModal = useCallback(
    async (scope: RunScope, columnName?: string, trigger?: HTMLElement | null, trial = false) => {
      setPendingRun({ scope, columnName, estimate: null, trigger, trial, trialEstimate: null });
      try {
        const est = await estimateRun(matrixId, scope);
        setPendingRun((cur) => (cur && cur.scope === scope ? { ...cur, estimate: est, trial: trial && !!est.trial_file_ids?.length } : cur));
        if (trial) await loadTrialEstimate(scope, est);
      } catch (e) {
        setPendingRun(null);
        say("error", runApiErr(e));
      }
    },
    [matrixId, say, runApiErr, loadTrialEstimate],
  );

  const onTrialChange = useCallback(
    (on: boolean) => {
      if (!pendingRun?.estimate) return;
      const { scope, estimate, trialEstimate } = pendingRun;
      setPendingRun({ ...pendingRun, trial: on });
      if (on && !trialEstimate) void loadTrialEstimate(scope, estimate);
    },
    [pendingRun, loadTrialEstimate],
  );

  const confirmRun = useCallback(async () => {
    if (!pendingRun) return;
    setBusy(true);
    try {
      await startRun(matrixId, confirmedScope(pendingRun));
      setPendingRun(null);
      await reload(); // Reflect run fields immediately → polling chain starts.
    } catch (e) {
      say("error", runApiErr(e));
    } finally {
      setBusy(false);
    }
  }, [pendingRun, matrixId, reload, say, runApiErr]);

  const onCancelRun = useCallback(
    async (runId: string) => {
      setBusy(true);
      try {
        await cancelRun(matrixId, runId);
        await reload();
      } catch (e) {
        say("error", runApiErr(e));
      } finally {
        setBusy(false);
      }
    },
    [matrixId, reload, say, runApiErr],
  );

  const onViewFailed = useCallback(
    (_runId?: string) => {
      // wave-2 §4.4: show the rows with failed cells (S4: the status filter "failed", keeping the sort) and
      // focus the first failed cell. Do not open the side panel automatically (the toolbar hint guides clicking a
      // failed cell, then retrying in the side panel); the first filtered row is index 0.
      if (!snapshot) return;
      const next: MatrixView = { ...EMPTY_VIEW, sort: view.sort, status: "failed" };
      const failedRows = viewRows(snapshot.rows, snapshot.columns, next, { labels: searchLabels, locale });
      const row = failedRows[0];
      const columnId = row ? snapshot.columns.find((c) => row.cells[c.columnId]?.status === "failed")?.columnId : undefined;
      if (!row || !columnId) return;
      setView(next);
      gridRef.current?.scrollToRow(0);
      setFocus({ fileId: row.fileId, columnId });
    },
    [snapshot, view.sort, searchLabels, locale, setView],
  );

  const clearFilters = useCallback(() => setView({ ...view, q: "", status: null, values: [] }), [view, setView]);

  // ── Single cell ──
  const onRunCell = useCallback(
    async (fileId: string, columnId: string) => {
      try {
        await rerunCell(matrixId, fileId, columnId);
        say("success", t("toast_cell_running"));
        await reload();
      } catch (e) {
        say("error", runApiErr(e));
      }
    },
    [matrixId, reload, say, runApiErr, t],
  );

  const onCellOpen = useCallback((fileId: string, columnId: string) => {
    setFocus({ fileId, columnId });
    setPanel({ fileId, columnId });
  }, []);

  // Stable reference (FE-P2-2): an inline lambda changes identity each render, defeating DataCell memoization.
  const onRunCellStable = useCallback(
    (fileId: string, columnId: string) => {
      void onRunCell(fileId, columnId);
    },
    [onRunCell],
  );
  const onColMenuStable = useCallback((col: MatrixColumnVM, e: React.MouseEvent) => {
    setColMenu({ col, anchor: e.currentTarget as HTMLElement, viaKeyboard: e.detail === 0 });
    setRunMenu(false);
  }, []);

  // ── Three cell operations ──
  const applyCellResult = useCallback((fileId: string, columnId: string, wire: unknown) => {
    const cell = buildCellFromWire(wire as Parameters<typeof buildCellFromWire>[0]);
    patchMatrixCell(fileId, columnId, cell);
  }, []);

  const onSaveValue = useCallback(
    async (newValue: unknown) => {
      if (!panel) return;
      setBusy(true);
      try {
        const res = await patchCell(matrixId, panel.fileId, panel.columnId, { op: "edit_value", value: newValue });
        applyCellResult(panel.fileId, panel.columnId, res.cell);
        say("success", t("toast_saved"));
      } catch (e) {
        say("error", apiErr(e));
      } finally {
        setBusy(false);
      }
    },
    [panel, matrixId, applyCellResult, say, apiErr, t],
  );

  const onMarkVerified = useCallback(async () => {
    if (!panel) return;
    setBusy(true);
    try {
      const res = await patchCell(matrixId, panel.fileId, panel.columnId, { op: "set_verified", verified: true });
      applyCellResult(panel.fileId, panel.columnId, res.cell);
      say("success", t("toast_verified"));
    } catch (e) {
      say("error", apiErr(e));
    } finally {
      setBusy(false);
    }
  }, [panel, matrixId, applyCellResult, say, apiErr, t]);

  const onKeepAsText = useCallback(async () => {
    if (!panel) return;
    setBusy(true);
    try {
      const res = await patchCell(matrixId, panel.fileId, panel.columnId, { op: "keep_as_text" });
      applyCellResult(panel.fileId, panel.columnId, res.cell);
      say("success", t("toast_kept_as_text"));
    } catch (e) {
      say("error", apiErr(e));
    } finally {
      setBusy(false);
    }
  }, [panel, matrixId, applyCellResult, say, apiErr, t]);

  // ── M3: two undo operations (§4.3) ──
  const onUnverify = useCallback(async () => {
    if (!panel) return;
    setBusy(true);
    try {
      const res = await patchCell(matrixId, panel.fileId, panel.columnId, { op: "set_verified", verified: false });
      applyCellResult(panel.fileId, panel.columnId, res.cell);
      say("success", t("toast_unverified"));
    } catch (e) {
      say("error", apiErr(e));
    } finally {
      setBusy(false);
    }
  }, [panel, matrixId, applyCellResult, say, apiErr, t]);

  const onRevertEdited = useCallback(async () => {
    if (!panel) return;
    setBusy(true);
    try {
      const res = await patchCell(matrixId, panel.fileId, panel.columnId, { op: "revert_edited" });
      applyCellResult(panel.fileId, panel.columnId, res.cell);
      say("success", t("toast_revert_edited"));
    } catch (e) {
      say("error", apiErr(e));
    } finally {
      setBusy(false);
    }
  }, [panel, matrixId, applyCellResult, say, apiErr, t]);

  const onPanelRerun = useCallback(async () => {
    if (!panel) return;
    setPanel(null);
    await onRunCell(panel.fileId, panel.columnId);
  }, [panel, onRunCell]);

  // ── S4: history and bulk verify ──
  const panelFileId = panel?.fileId;
  const panelColumnId = panel?.columnId;
  const loadHistory = useMemo(
    () => (panelFileId && panelColumnId ? () => fetchCellEvents(matrixId, panelFileId, panelColumnId) : undefined),
    [matrixId, panelFileId, panelColumnId],
  );

  const bulkVerifiedText = useCallback(
    (res: CellsVerified): string => {
      const parts = [
        res.skipped.not_done ? t("bulk_skip_not_done", { n: res.skipped.not_done }) : null,
        res.skipped.stale ? t("bulk_skip_stale", { n: res.skipped.stale }) : null,
        res.skipped.unavailable ? t("bulk_skip_unavailable", { n: res.skipped.unavailable }) : null,
      ].filter((x): x is string => !!x);
      const done = t("toast_bulk_verified", { n: res.changed });
      return parts.length ? `${done} ${t("toast_bulk_skipped", { list: new Intl.ListFormat(locale, { type: "conjunction" }).format(parts) })}` : done;
    },
    [t, locale],
  );

  const onVerifyColumn = useCallback(
    async (col: MatrixColumnVM) => {
      if (!snapshot) return;
      const preview = columnVerifyPreview(snapshot.rows, col);
      const skipped = preview.notDone + preview.stale;
      const hidden = filtered && visibleRows.length < snapshot.rows.length;
      const ok = await confirm({
        title: t("verify_column_title", { name: col.name }),
        description: (
          <>
            <p>{t("verify_column_body", { n: preview.eligible })}</p>
            {skipped > 0 && <p>{t("verify_column_skip", { n: skipped })}</p>}
            {hidden && <p>{t("verify_column_hidden")}</p>}
          </>
        ),
        confirmLabel: t("verify_column_confirm"),
        tone: "primary",
      });
      if (!ok) return;
      setBusy(true);
      try {
        const res = await verifyCells(matrixId, { column_id: col.columnId, verified: true });
        await reload();
        say("success", bulkVerifiedText(res));
      } catch (e) {
        say("error", apiErr(e));
      } finally {
        setBusy(false);
      }
    },
    [snapshot, filtered, visibleRows.length, confirm, t, matrixId, reload, say, bulkVerifiedText, apiErr],
  );

  // ── Columns ──
  const onCreateColumn = useCallback(
    async (col: NewColumnIn, run: boolean) => {
      setBusy(true);
      try {
        const res = await addColumn(matrixId, col);
        setAddcol(false);
        await reload();
        if (run) await openRunModal({ kind: "column", column_id: res.column_id }, col.name);
        else say("success", t("toast_column_created", { name: col.name }));
      } catch (e) {
        say("error", apiErr(e));
      } finally {
        setBusy(false);
      }
    },
    [matrixId, reload, openRunModal, say, apiErr, t],
  );

  const onColMenuPick = useCallback(
    async (a: ColMenuAction) => {
      const col = colMenu?.col;
      const anchor = colMenu?.anchor;
      setColMenu(null);
      if (!col) return;
      if (a === "run") await openRunModal({ kind: "column", column_id: col.columnId }, col.name, anchor);
      else if (a === "force") await openRunModal({ kind: "force_column", column_id: col.columnId }, col.name, anchor);
      else if (a === "verify") await onVerifyColumn(col);
      else if (a === "edit") setEditCol(col);
      else if (a === "del") {
        // Frontend second confirmation (§7.2).
        const ok = await confirm({
          title: t("confirm_delete_column_title", { name: col.name }),
          description: t("confirm_delete_column"),
          confirmLabel: t("colmenu_delete"),
        });
        if (!ok) return;
        try {
          await deleteColumn(matrixId, col.columnId);
          await reload();
          say("success", t("toast_column_deleted"));
        } catch (e) {
          say("error", apiErr(e));
        }
      }
    },
    [colMenu, confirm, matrixId, openRunModal, onVerifyColumn, reload, say, apiErr, t],
  );

  // ── S2: rename, delete, add and remove files ──
  const onRename = useCallback(
    async (name: string): Promise<boolean> => {
      try {
        const saved = await renameMatrix(matrixId, name);
        await reload();
        say("success", t("toast_renamed", { name: saved }));
        return true;
      } catch (e) {
        say("error", apiErr(e));
        return false;
      }
    },
    [matrixId, reload, say, apiErr, t],
  );

  // ── S5: export to Excel (any role that can open the matrix; desktop only) ──
  const onExport = useCallback(async () => {
    setExporting(true);
    try {
      await downloadMatrixExport(matrixId, snapshot?.title ?? "");
    } catch (e) {
      say("error", t("toast_export_failed", { reason: apiErr(e) }));
    } finally {
      setExporting(false);
    }
  }, [matrixId, snapshot?.title, say, apiErr, t]);

  const onDeleteMatrix = useCallback(async () => {
    if (!snapshot) return;
    const name = snapshot.title;
    const cells = snapshot.rows.reduce((n, r) => n + Object.values(r.cells).filter(Boolean).length, 0);
    // The delete cancels running and paused runs first (S1).
    const stopping = snapshotRuns(snapshot).filter((r) => r.status === "running" || r.status === "paused").length;
    const ok = await confirm({
      title: t("delete_matrix_title", { name }),
      description: (
        <>
          <p>{t("delete_matrix_body", { cells })}</p>
          {stopping > 0 && <p>{t("delete_matrix_runs", { n: stopping })}</p>}
        </>
      ),
      confirmLabel: t("delete_matrix_confirm"),
    });
    if (!ok) return;
    setBusy(true);
    try {
      await deleteMatrix(matrixId);
      say("success", t("toast_matrix_deleted", { name }));
      onBack();
    } catch (e) {
      say("error", apiErr(e));
    } finally {
      setBusy(false);
    }
  }, [snapshot, confirm, matrixId, onBack, say, apiErr, t]);

  // ── S8: who can see the matrix (its creator; the server re-checks every condition) ──
  const onChangeVisibility = useCallback(async () => {
    if (!snapshot) return;
    const name = snapshot.title;
    const target: MatrixVisibility = snapshot.visibility === "private" ? "space" : "private";
    let preview: MatrixVisibilityPreview;
    try {
      preview = await fetchMatrixVisibility(matrixId);
    } catch (e) {
      say("error", t("visibility_preview_failed", { reason: apiErr(e) }));
      return;
    }
    // Known refusals are said before asking, with what to do about them.
    if (target === "space" && preview.personal_files_count > 0) {
      say(
        "error",
        t("err_visibility_personal_files", { count: preview.personal_files_count, files: preview.personal_files.map((f) => f.name).join(", ") }),
      );
      return;
    }
    if (target === "space" && preview.hidden_personal_files > 0) {
      say("error", t("err_visibility_personal_files_hidden", { count: preview.hidden_personal_files }));
      return;
    }
    const navigation = snapshot.columns.filter((c) => (c.navigationRole ?? "none") !== "none").map((c) => c.name);
    if (target === "private" && navigation.length > 0) {
      say("error", t("err_navigation_columns_block_private", { cols: navigation.join(", ") }));
      return;
    }
    if (target === "private" && preview.active_runs_by_others > 0) {
      say("error", t("err_visibility_runs_active", { count: preview.active_runs_by_others }));
      return;
    }
    const contributors = preview.contributors.join(", ");
    const ok = await confirm({
      title: t(target === "private" ? "visibility_private_title" : "visibility_space_title", { name }),
      description:
        target === "private" ? (
          <>
            <p>{t("visibility_private_body")}</p>
            {preview.contributors_count > 0 && (
              <p data-testid="kb-matrix-visibility-contributors">
                {contributors
                  ? t("visibility_private_contributors", { count: preview.contributors_count, names: contributors })
                  : t("visibility_private_contributors_unnamed", { count: preview.contributors_count })}
              </p>
            )}
          </>
        ) : (
          <p>{t("visibility_space_body")}</p>
        ),
      confirmLabel: t(target === "private" ? "visibility_private_confirm" : "visibility_space_confirm"),
      tone: "primary",
    });
    if (!ok) return;
    setBusy(true);
    try {
      await setMatrixVisibility(matrixId, target);
      await reload();
      say("success", t(target === "private" ? "toast_visibility_private" : "toast_visibility_space", { name }));
    } catch (e) {
      say("error", apiErr(e));
    } finally {
      setBusy(false);
    }
  }, [snapshot, matrixId, confirm, reload, say, apiErr, t]);

  const onAddFiles = useCallback(
    async (fileIds: string[]) => {
      setBusy(true);
      setAddError(null);
      try {
        const res = await addMatrixFiles(matrixId, fileIds);
        setAddingFiles(false);
        await reload();
        say("success", t("toast_files_added", { n: res.added.length }));
      } catch (e) {
        // Stay in the picker with the reason (a cap, a file that is gone or personal) so the choice can be fixed.
        setAddError(apiErr(e));
      } finally {
        setBusy(false);
      }
    },
    [matrixId, reload, say, apiErr, t],
  );

  const onConfirmRemove = useCallback(async () => {
    if (!removing) return;
    setBusy(true);
    try {
      const res = await removeMatrixFiles(
        matrixId,
        removing.rows.map((r) => r.fileId),
      );
      const gone = new Set(res.removed);
      setRemoving(null);
      setSelected((cur) => new Set([...cur].filter((id) => !gone.has(id))));
      await reload();
      say("success", t("toast_files_removed", { n: res.removed.length }));
    } catch (e) {
      // Stay in the dialog with the reason; a refusal (403 with the server's counts) offers no retry.
      const refused = e instanceof MatrixApiError && e.code === "matrix_remove_requires_owner";
      setRemoving((cur) => (cur ? { ...cur, error: apiErr(e), refused } : cur));
    } finally {
      setBusy(false);
    }
  }, [removing, matrixId, reload, say, apiErr, t]);

  const rowControls: MatrixRowControls = useMemo(
    () => ({
      selected,
      onToggle: (fileId, on) =>
        setSelected((cur) => {
          const next = new Set(cur);
          if (on) next.add(fileId);
          else next.delete(fileId);
          return next;
        }),
      onToggleAll: (fileIds, on) =>
        setSelected((cur) => {
          const next = new Set(cur);
          for (const id of fileIds) {
            if (on) next.add(id);
            else next.delete(id);
          }
          return next;
        }),
      onRemove: (row) => setRemoving({ rows: [row], error: null, refused: false }),
    }),
    [selected],
  );

  // ── Keyboard (retain M1 semantics) ──
  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Escape") {
        // §4.8: never blindly close while focus is inside an input (side-panel edit input handles Escape to exit editing).
        if (escTargetIsEditable(e.target)) return;
        // Priority (brief A6): close side panel/panel → clear filter → no action.
        if (panel || addcol || editCol || pendingRun) {
          setPanel(null);
          setAddcol(false);
          setEditCol(null);
          setPendingRun(null);
          return;
        }
        if (filtered) {
          clearFilters();
          return;
        }
        return;
      }
      if (panel || addcol || editCol || pendingRun) return;
      if (!snapshot || !focus) return;
      // Arrows move within the rows shown, in their shown order (brief A6); columns are not clipped.
      const navRows = visibleRows;
      const ri = navRows.findIndex((r) => r.fileId === focus.fileId);
      const ci = snapshot.columns.findIndex((c) => c.columnId === focus.columnId);
      if (ri < 0 || ci < 0) return;
      let nr = ri;
      let nc = ci;
      if (e.key === "ArrowDown") nr = Math.min(navRows.length - 1, ri + 1);
      else if (e.key === "ArrowUp") nr = Math.max(0, ri - 1);
      else if (e.key === "ArrowRight") nc = Math.min(snapshot.columns.length - 1, ci + 1);
      else if (e.key === "ArrowLeft") nc = Math.max(0, ci - 1);
      else if (e.key === "Enter") {
        setPanel(focus);
        return;
      } else return;
      e.preventDefault();
      setFocus({ fileId: navRows[nr].fileId, columnId: snapshot.columns[nc].columnId });
    },
    [snapshot, focus, panel, addcol, editCol, pendingRun, filtered, clearFilters, visibleRows],
  );

  // Document-level Esc (container onKeyDown misses events with focus inside panels; M1 dogfood lesson).
  useEffect(() => {
    if (!panel && !addcol && !editCol) return;
    const onDocKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        // §4.8 lost-input fix: do not blindly close panels when focus is in input/textarea etc.
        if (escTargetIsEditable(e.target)) return;
        setPanel(null);
        setAddcol(false);
        setEditCol(null);
      }
    };
    document.addEventListener("keydown", onDocKey);
    return () => document.removeEventListener("keydown", onDocKey);
  }, [panel, addcol, editCol]);

  const panelColumn = panel && snapshot ? snapshot.columns.find((c) => c.columnId === panel.columnId) : undefined;
  const rerunCredits = useCellRerunCredits(canEdit && panel ? matrixId : null, panel?.fileId, panel?.columnId, panelColumn?.promptVersion, panelColumn?.crossVerify);

  // M4 template-promotion cell statistics (meaningful only with dialog open; needs_review with a value counts as complete):
  // incomplete = missing/queued/running; failed; stale (exclude manually edited cells, because promotion uses edited values).
  const gradStats = useMemo(() => {
    if (!graduating || !snapshot) return { empty: 0, failed: 0, stale: 0 };
    let empty = 0;
    let failed = 0;
    let stale = 0;
    for (const r of snapshot.rows) {
      for (const c of snapshot.columns) {
        const cell = r.cells[c.columnId];
        if (!cell || cell.status === "queued" || cell.status === "running") empty++;
        else if (cell.status === "failed") failed++;
        else if (cell.stale && !cell.edited) stale++;
      }
    }
    return { empty, failed, stale };
  }, [graduating, snapshot]);

  // §4.8: unavailable/deleted matrix (poll/reload 404/403) → neutral empty state + return/retry.
  // (Successful retry restores it; hot-flag mitigation must not falsely claim deletion and cause alarm, S-P2-4.)
  if (unavailable && !snapshot) {
    return (
      <div style={{ padding: 48, textAlign: "center" }} data-testid="kb-matrix-unavailable">
        <div style={{ fontSize: "0.9375rem", fontWeight: 600 }}>{t("matrix_unavailable_title")}</div>
        <div style={{ marginTop: 8, color: "var(--c-ink-3)", fontSize: "0.8125rem" }}>{t("matrix_unavailable_body")}</div>
        <div style={{ marginTop: 16, display: "flex", gap: 8, justifyContent: "center" }}>
          <button type="button" className="kb-btn kb-btn-secondary" onClick={onBack}>
            {t("matrix_unavailable_back")}
          </button>
          <button
            type="button"
            className="kb-btn kb-btn-primary"
            data-testid="kb-matrix-unavailable-retry"
            onClick={() => {
              setUnavailable(false);
              void reload().catch((e: unknown) => setLoadError({ error: e }));
            }}
          >
            {t("matrix_unavailable_retry")}
          </button>
        </div>
      </div>
    );
  }

  if (loadError) {
    return (
      <div style={{ padding: 24, color: "var(--c-err)" }}>
        {t("load_failed_notice")}
        <ErrorDiagnosticsInline error={loadError.error} surface="route" />
        <div style={{ marginTop: 10 }}>
          <button type="button" className="kb-btn kb-btn-secondary" onClick={onBack}>
            {t("back")}
          </button>
        </div>
      </div>
    );
  }

  const panelRow = panel && snapshot ? (snapshot.rows.find((r) => r.fileId === panel.fileId) ?? null) : null;
  const panelCol = panel && snapshot ? (snapshot.columns.find((c) => c.columnId === panel.columnId) ?? null) : null;
  const panelCell = panelRow && panel ? panelRow.cells[panel.columnId] : undefined;
  const citeRow = citePop && snapshot ? (snapshot.rows.find((r) => r.fileId === citePop.fileId) ?? null) : null;
  const citeCell = citeRow && citePop ? citeRow.cells[citePop.columnId] : undefined;
  // Run menu counts mirror the backend selection: FR-9 skips edited cells (FE-P2-7); display only, the estimate is authoritative.
  const menuCounts = runMenuCounts(snapshot?.rows ?? [], snapshot?.columns ?? []);

  // S4: the grid shows the filtered and sorted rows; an empty result gets its own state (the failed view keeps its copy).
  const failedOnly = view.status === "failed" && !view.q.trim() && view.values.length === 0;
  const filterEmpty = !!snapshot && filtered && visibleRows.length === 0;
  const gridSnapshot: MatrixSnapshot | null = snapshot && (filtered || view.sort) ? { ...snapshot, rows: visibleRows } : snapshot;
  const progress = snapshot ? verifiedProgress(snapshot.rows, snapshot.columns) : { verified: 0, total: 0 };
  const readableRows = snapshot ? snapshot.rows.filter((r) => !r.missing).length : 0;
  const colMenuVerifiable = colMenu && snapshot ? columnVerifyPreview(snapshot.rows, colMenu.col).eligible : 0;
  // S2: adding and removing rows is desktop editing; phones stay read-only.
  const editRows = !!snapshot && canEdit && !isMobile;
  // The header's edit actions (⋯, add column, save as template, run) follow the same rule.
  const headerEdit = canEdit && !isMobile;
  const selectedRows = snapshot && selected.size > 0 ? snapshot.rows.filter((r) => selected.has(r.fileId)) : [];

  return (
    <div
      className="mx-app"
      ref={appRef}
      style={{ outline: "none", position: "relative" }}
      tabIndex={0}
      onKeyDown={onKeyDown}
      onClick={() => {
        setRunMenu(false);
        setColMenu(null);
      }}
      data-testid="kb-matrix-app-m2"
    >
      <MatrixHeader
        title={snapshot?.title ?? ""}
        meta={snapshot ? `${t("list_source_selected")} · ${t("meta", { rows: snapshot.rows.length, cols: snapshot.columns.length })}` : ""}
        canEdit={headerEdit}
        canManage={canManage}
        visibility={snapshot?.visibility}
        canChangeVisibility={!!snapshot?.canChangeVisibility}
        onHome={onHome ?? onBack}
        onBack={onBack}
        onRename={onRename}
        onDelete={() => void onDeleteMatrix()}
        onChangeVisibility={() => void onChangeVisibility()}
      >
        {headerEdit && (
          <>
            <button
              type="button"
              className="kb-btn kb-btn-secondary kb-btn-sm"
              data-testid="kb-matrix-addcol-btn"
              onClick={(e) => {
                e.stopPropagation();
                setAddcol(true);
              }}
            >
              <IcPlus s={13} />
              {t("add_column")}
            </button>
            {/* M4 FR-40: template-promotion entry (member+; server 403/422 fallback). */}
            <button
              type="button"
              className="kb-btn kb-btn-secondary kb-btn-sm"
              data-testid="kb-matrix-graduate-btn"
              disabled={!snapshot}
              onClick={(e) => {
                e.stopPropagation();
                setGraduating(true);
              }}
            >
              <IcGrid s={13} />
              {t("grad_entry")}
            </button>
          </>
        )}
        {/* S5: exporting is a read, so viewers get it too; phones have no ⋯ menu, so it is left out there. */}
        {snapshot && !isMobile && (
          <button
            type="button"
            className="kb-btn kb-btn-secondary kb-btn-sm"
            data-testid="kb-matrix-export-btn"
            disabled={exporting}
            aria-busy={exporting || undefined}
            onClick={(e) => {
              e.stopPropagation();
              void onExport();
            }}
          >
            <IcDownload s={13} />
            {exporting ? t("export_excel_busy") : t("export_excel")}
          </button>
        )}
        {/* The page's primary action comes last. */}
        {headerEdit && (
          <div style={{ position: "relative" }}>
            <div className="mx-runbtn">
              <button
                type="button"
                className="mx-run-main"
                disabled={busy}
                data-testid="kb-matrix-run"
                onClick={(e) => {
                  e.stopPropagation();
                  void openRunModal({ kind: "all_pending" }, undefined, e.currentTarget);
                }}
              >
                <IcPlay s={11} />
                {t("run")}
              </button>
              <button
                ref={runCaretRef}
                aria-label={t("run_options")}
                aria-expanded={runMenu}
                type="button"
                className="caret"
                disabled={busy}
                onClick={(e) => {
                  e.stopPropagation();
                  setRunMenuViaKeyboard(e.detail === 0);
                  setRunMenu((v) => !v);
                  setColMenu(null);
                }}
              >
                <IcCaret s={13} />
              </button>
            </div>
            <RunMenu
              open={runMenu}
              onOpenChange={setRunMenu}
              anchor={runCaretRef.current}
              viaKeyboard={runMenuViaKeyboard}
              counts={menuCounts}
              onPick={(v) => {
                setRunMenu(false);
                void openRunModal({ kind: RUN_MENU_SCOPE[v] }, undefined, runCaretRef.current, v === "trial");
              }}
            />
          </div>
        )}
      </MatrixHeader>

        <RunBanners
          key={matrixId}
          runs={runs}
          columns={snapshot?.columns ?? []}
          cancelling={busy}
          onCancel={(runId) => void onCancelRun(runId)}
          onViewFailed={onViewFailed}
        />

      {snapshot?.rows.some(row => Object.values(row.cells).some(cell => (cell?.currentSourceState && cell.currentSourceState !== "eligible") || cell?.sourceCheckState === "source_temporarily_unavailable")) && <p className="mx-inlineerr" role="note">{t("current_source_warning")}</p>}
      {/* S4 review toolbar (mockup ④); phones show the progress only. Keys stay in the toolbar so typing and its
          menus do not move the cell focus. */}
      {snapshot && (
        <div onKeyDown={(e) => e.stopPropagation()}>
          <MatrixToolbar
            view={view}
            columns={snapshot.columns}
            onView={setView}
            shownRows={visibleRows.length}
            totalRows={readableRows}
            progress={progress}
            progressOnly={isMobile}
            filterNote={view.status === "failed" ? t("filter_hint") : undefined}
          />
        </div>
      )}
      {/* S2: bulk bar for the selected rows. */}
      {editRows && selectedRows.length > 0 && (
        <div className="mx-selbar" data-testid="kb-matrix-selbar">
          <span className="count">{t("rows_selected", { n: selectedRows.length })}</span>
          <button
            type="button"
            className="kb-btn kb-btn-secondary kb-btn-sm mx-selbar-remove"
            data-testid="kb-matrix-selbar-remove"
            disabled={busy || selectedRows.length > MATRIX_FILES_PER_CHANGE}
            aria-describedby={selectedRows.length > MATRIX_FILES_PER_CHANGE ? "kb-matrix-selbar-limit" : undefined}
            onClick={() => setRemoving({ rows: selectedRows, error: null, refused: false })}
          >
            {t("rows_remove")}
          </button>
          <button type="button" className="kb-btn kb-btn-ghost kb-btn-sm" onClick={() => setSelected(new Set())}>
            {t("rows_clear")}
          </button>
          {selectedRows.length > MATRIX_FILES_PER_CHANGE && (
            <span id="kb-matrix-selbar-limit" className="hint" role="note">
              {t("rows_remove_limit", { limit: MATRIX_FILES_PER_CHANGE })}
            </span>
          )}
        </div>
      )}

      {!snapshot ? (
        <MatrixSkeleton />
      ) : isMobile ? (
        <MatrixMobile snapshot={snapshot} />
      ) : filterEmpty ? (
        <div className="mx-gridempty" data-testid={failedOnly ? "kb-matrix-failempty" : "kb-matrix-filterempty"}>
          <div className="illus"><IcCheck s={30} /></div>
          <h3>{t(failedOnly ? "fail_empty_title" : "filter_empty_title")}</h3>
          <p className="p">{t(failedOnly ? "fail_empty_body" : "filter_empty_body")}</p>
          <button type="button" className="kb-btn kb-btn-primary" onClick={clearFilters}>
            {t("filter_clear")}
          </button>
        </div>
      ) : (
        <MatrixGrid
          ref={gridRef}
          snapshot={gridSnapshot ?? snapshot}
          focus={focus}
          onCell={onCellOpen}
          onCiteHover={setCitePop}
          onRunCell={canEdit ? onRunCellStable : undefined}
          onColMenu={canEdit ? onColMenuStable : undefined}
          failFilter={view.status === "failed"}
          rowControls={editRows ? rowControls : undefined}
        />
      )}

      {editRows && (
        <button
          type="button"
          className="mx-addrow"
          data-testid="kb-matrix-addfiles-row"
          onClick={() => {
            setAddError(null);
            setAddingFiles(true);
          }}
        >
          <IcPlus s={13} />
          {t("add_files")}
        </button>
      )}

      <div className="mx-gridfoot">
        <span>{snapshot ? t("foot", { rows: snapshot.rows.length }) : ""}</span>
        <span className="grow" />
      </div>

      {citePop && citeRow && citeCell && (
        <CitePopover anchor={citePop.anchor} fileId={citeRow.fileId} fileName={citeRow.fileName} cell={citeCell} />
      )}

      <ColMenu
        open={colMenu !== null}
        onOpenChange={(open) => { if (!open) setColMenu(null); }}
        anchor={colMenu?.anchor ?? null}
        viaKeyboard={colMenu?.viaKeyboard ?? false}
        columnName={colMenu?.col.name ?? ""}
        verifiable={colMenuVerifiable}
        onPick={(a) => void onColMenuPick(a)}
      />

      <div
        className={"mx-panelwrap" + ((panel && panelCell) || addcol || editCol ? " open" : "")}
        onClick={() => {
          setPanel(null);
          setAddcol(false);
          setEditCol(null);
        }}
      >
        <div className="mx-panel-scrim" />
        {addcol && !panel && (
          <AddColumnPanel matrixId={matrixId} creating={busy} onClose={() => setAddcol(false)} onCreate={(c, run) => void onCreateColumn(c, run)} />
        )}
        {editCol && !addcol && !panel && (
          <EditColumnPanel
            col={editCol}
            busy={busy}
            navigationAllowed={snapshot?.visibility !== "private"}
            onClose={() => setEditCol(null)}
            onSave={async (patch) => {
              setBusy(true);
              try {
                const prevPv = editCol.promptVersion;
                const updated = await updateColumn(matrixId, editCol.columnId, patch);
                setEditCol(null);
                await reload();
                say("success", t("toast_column_updated"));
                // §4.2 post-edit column confirmation: prompt_version increments (semantic change) → estimate
                // stale_column → open only when cells_pending>0; "仅对新行生效" = close without requests.
                if (prevPv != null && updated.promptVersion != null && updated.promptVersion > prevPv) {
                  try {
                    const scope: RunScope = { kind: "stale_column", column_id: editCol.columnId };
                    const est = await estimateRun(matrixId, scope);
                    if (est.cells_pending > 0) {
                      setPendingRun({ scope, columnName: updated.name, estimate: est, postEdit: true });
                    }
                  } catch (e2) {
                    // Estimate 402/429 etc.: Toast feedback suffices (rerun entry = Run menu "重跑已过期").
                    say("error", runApiErr(e2));
                  }
                }
              } catch (e) {
                say("error", apiErr(e));
              } finally {
                setBusy(false);
              }
            }}
          />
        )}
        {panel && panelRow && panelCol && panelCell && !addcol && !editCol && (
          <SidePanel
            row={panelRow}
            col={panelCol}
            cell={panelCell}
            canEdit={canEdit}
            saving={busy}
            onClose={() => setPanel(null)}
            onSaveValue={(v) => void onSaveValue(v)}
            onMarkVerified={() => void onMarkVerified()}
            onRerunCell={() => void onPanelRerun()}
            rerunCredits={rerunCredits}
            onKeepAsText={() => void onKeepAsText()}
            onUnverify={() => void onUnverify()}
            onRevertEdited={() => void onRevertEdited()}
            loadHistory={loadHistory}
          />
        )}
      </div>

      {pendingRun && (
        <RunConfirmModal
          scope={pendingRun.scope}
          columnName={pendingRun.columnName}
          columns={snapshot?.columns}
          fileName={(fileId) => snapshot?.rows.find((r) => r.fileId === fileId)?.fileName || undefined}
          estimate={pendingRun.trial ? (pendingRun.trialEstimate ?? null) : pendingRun.estimate}
          trialFiles={trialOffer(pendingRun)}
          trial={!!pendingRun.trial}
          onTrialChange={onTrialChange}
          starting={busy}
          postEdit={pendingRun.postEdit}
          returnFocusTo={pendingRun.trigger}
          onClose={() => setPendingRun(null)}
          onConfirm={() => void confirmRun()}
        />
      )}

      {graduating && snapshot && (
        <GraduateDialog
          matrixId={matrixId}
          defaultName={snapshot.title}
          columns={snapshot.columns}
          filesCount={snapshot.rows.length}
          emptyCells={gradStats.empty}
          failedCells={gradStats.failed}
          staleCells={gradStats.stale}
          onClose={() => setGraduating(false)}
          onDone={(r: GraduateResult) => {
            setGraduating(false);
            say(
              "success",
              r.reused
                ? t("grad_toast_reused", { name: r.name })
                : r.mode === "snapshot"
                  ? t("grad_toast_ok", { name: r.name })
                  : t("grad_toast_tpl_ok", { name: r.name }),
            );
          }}
        />
      )}

      {addingFiles && snapshot && (
        <AddFilesDialog
          matrixName={snapshot.title}
          presentFileIds={new Set(snapshot.rows.map((r) => r.fileId))}
          personalFiles={snapshot.visibility === "private" ? "allowed" : privateMatrices ? "space_matrix" : "unavailable"}
          busy={busy}
          error={addError}
          onClose={() => setAddingFiles(false)}
          onAdd={(ids) => void onAddFiles(ids)}
        />
      )}

      {removing && (
        <RemoveFilesDialog
          rows={removing.rows}
          canManage={canManage}
          hasActiveRun={runs.some((r) => r.status === "running" || r.status === "paused")}
          busy={busy}
          error={removing.error}
          refused={removing.refused}
          onCancel={() => setRemoving(null)}
          onConfirm={() => void onConfirmRemove()}
        />
      )}

      {confirmDialog}
    </div>
  );
}

/** Column editor (FR-4: name/question/hint/**cross_verify**; changing question/hint/
 * cross_verify/options → server prompt_version+1 derives stale). wave-2: type picker
 * displays locked (type cannot change after creation) + select/multi_select options editor. */
function EditColumnPanel({
  col,
  busy,
  navigationAllowed = true,
  onClose,
  onSave,
}: {
  col: MatrixColumnVM;
  busy: boolean;
  /** S8: only-me matrices cannot be used for source discovery, so the purpose is not offered. */
  navigationAllowed?: boolean;
  onClose: () => void;
  onSave: (patch: {
    name?: string;
    question?: string;
    extraction_hint?: string;
    options?: string[] | null;
    cross_verify?: boolean;
    navigation_role?: "none" | "description" | "topics";
  }) => void;
}) {
  const t = useTranslations("kbMatrix");
  const [navigationRole, setNavigationRole] = useState<"none" | "description" | "topics">(col.navigationRole ?? "none");
  const [name, setName] = useState(col.name);
  const [question, setQuestion] = useState(col.question ?? "");
  const [hint, setHint] = useState(col.extractionHint ?? "");
  const [cv, setCv] = useState(!!col.crossVerify);
  const [options, setOptions] = useState<string[]>(col.options ?? []);
  const showOpts = col.type === "select" || col.type === "multi_select";
  const baseOpts = col.options ?? [];
  const optionsChanged = options.length !== baseOpts.length || options.some((o, i) => o !== baseOpts[i]);
  const optionsMissing = showOpts && options.length === 0;
  return (
    <div className="mx-panel addcol" data-testid="kb-matrix-editcol" onClick={(e) => e.stopPropagation()}>
      <div className="mx-panel-head">
        <div style={{ flex: 1 }}>
          <div className="col">{t("editcol_title")}</div>
        </div>
        <button type="button" className="mx-back" aria-label={t("close")} onClick={onClose}>
          <IcX s={16} />
        </button>
      </div>
      <div className="mx-panel-body">
        <div className="mx-seclabel" style={{ marginTop: 0 }}>{t("editcol_name")}</div>
        <input className="mx-inp" value={name} onChange={(e) => setName(e.target.value)} />
        <div className="mx-seclabel">{t("editcol_question")}</div>
        <textarea className="mx-ta" value={question} onChange={(e) => setQuestion(e.target.value)} />
        <div className="mx-seclabel">{t("addcol_hint_label")}</div>
        <textarea className="mx-ta" style={{ minHeight: 64 }} value={hint} onChange={(e) => setHint(e.target.value)} />
        {/* wave-2 §B-a: column type cannot change after creation; locked picker + explanation. */}
        <div className="mx-seclabel">{`${t("type_section")} · ${t("type_locked_suffix")}`}</div>
        <TypePicker8 value={col.type} onChange={() => {}} disabled />
        <div className="hint" style={{ marginTop: 8 }}>{t("type_locked_note")}</div>
        {showOpts && <OptionsEditor value={options} onChange={setOptions} />}
        {/* M3 §4.7: cross-check checkbox (hidden for question columns; FR-39 server 422). */}
        {col.type !== "question" && (
          <label
            style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 10, fontSize: "0.75rem", cursor: "pointer" }}
            data-testid="kb-matrix-editcol-cv"
          >
            <input type="checkbox" checked={cv} onChange={(e) => setCv(e.target.checked)} />
            {t("cv_toggle")}
          </label>
        )}
        {navigationAllowed ? (
          <>
            <label className="mx-label">{t("navigation_role")}<select value={navigationRole} disabled={busy} onChange={(event) => setNavigationRole(event.target.value as "none" | "description" | "topics")}><option value="none">{t("navigation_none")}</option><option value="description">{t("navigation_description")}</option><option value="topics">{t("navigation_topics")}</option></select></label>
            <div className="mx-fallback">{t("navigation_hint")}</div>
          </>
        ) : (
          <div className="mx-fallback" data-testid="kb-matrix-editcol-navigation-private">{t("navigation_private_note")}</div>
        )}
        <div className="mx-fallback" style={{ marginTop: 10 }}>{t("editcol_stale_note")}</div>
      </div>
      <div className="mx-panel-foot">
        <button
          type="button"
          className="kb-btn kb-btn-primary"
          disabled={busy || !name.trim() || optionsMissing}
          onClick={() =>
            onSave({
              ...(name.trim() !== col.name ? { name: name.trim() } : {}),
              ...(question.trim() !== (col.question ?? "") ? { question: question.trim() } : {}),
              ...(hint.trim() !== (col.extractionHint ?? "") ? { extraction_hint: hint.trim() } : {}),
              ...(showOpts && optionsChanged ? { options } : {}),
              ...(cv !== !!col.crossVerify ? { cross_verify: cv } : {}),
              ...(navigationRole !== (col.navigationRole ?? "none") ? { navigation_role: navigationRole } : {}),
            })
          }
        >
          {t("save")}
        </button>
        <button type="button" className="kb-btn kb-btn-ghost" onClick={onClose}>
          {t("cancel")}
        </button>
      </div>
    </div>
  );
}
