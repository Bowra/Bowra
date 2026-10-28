"use client";

/**
 * kb-matrix M2 — real matrix (mx_*) main-view orchestration: snapshot loading + polling updates +
 * run flows (toolbar [▶ 运行 ▾] / column menu / single cell) + cost confirmation + run banner + add/edit column
 * + three cell operations (编辑值/标记已核/保留为文本) + single-cell rerun.
 * Component contract stays frozen from M1: grid/side panel consume only MatrixSnapshot (tech design decision 9).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { ErrorDiagnosticsInline, type ShownError, shownError } from "@/components/error-notice";
import { notify } from "@/lib/use-toast";
import { useConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  type NewColumnIn,
  type RunEstimate,
  type RunScope,
  addColumn,
  cancelRun,
  deleteColumn,
  estimateRun,
  patchCell,
  rerunCell,
  startRun,
  updateColumn,
} from "@/lib/kb-matrix/run-api";
import { kbMatrixMutationError } from "@/lib/kb-matrix/mutation-error";
import { kbMatrixRunErrorKey } from "@/lib/kb-matrix/run-error";
import { buildCellFromWire } from "@/lib/kb-matrix/snapshot-api";
import { patchMatrixCell, setMatrixSnapshot, useMatrixSnapshot } from "@/lib/kb-matrix/store";
import type { MatrixColumnVM, MatrixSnapshot } from "@/lib/kb-matrix/types";
import { failedCellCount, rowsWithFailedCell, snapshotRuns } from "@/lib/kb-matrix/types";
import { useIsMobile } from "@/lib/kb-matrix/use-is-mobile";
import { useMatrixPolling } from "@/lib/kb-matrix/use-matrix-poll";

import type { GraduateResult } from "@/lib/kb-matrix/graduate-api";

import { type CiteHoverInfo, type MatrixGridHandle, MatrixGrid } from "./matrix-grid";
import { AddColumnPanel } from "./add-column-panel";
import { CitePopover } from "./cite-popover";
import { type ColMenuAction, ColMenu, RunMenu } from "./column-menu";
import { GraduateDialog } from "./graduate-dialog";
import { IcBack, IcCaret, IcCheck, IcGrid, IcPlay, IcPlus, IcWarn, IcX } from "./matrix-icons";
import { OptionsEditor } from "./options-editor";
import { TypePicker8 } from "./type-picker";
import { MatrixMobile } from "./matrix-mobile";
import { MatrixSkeleton } from "./matrix-skeleton";
import { RunBanners } from "./run-banner";
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
  /** §4.2: confirmation variant after column editing (stale_confirm_* copy; secondary action = 仅对新行生效). */
  postEdit?: boolean;
  /** Focus-return button after close (run button or run/column menu trigger). */
  trigger?: HTMLElement | null;
}

export function MatrixViewM2({ matrixId, onBack }: { matrixId: string; onBack: () => void }) {
  const t = useTranslations("kbMatrix");
  const { confirm, dialog: confirmDialog } = useConfirmDialog();
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
  // wave-2 [查看失败] filtered view (§4.4): only rows containing failed cells + top filter chip.
  const [failFilter, setFailFilter] = useState(false);
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
  const [citePop, setCitePop] = useState<CiteHoverInfo | null>(null);
  const appRef = useRef<HTMLDivElement | null>(null);
  const runCaretRef = useRef<HTMLButtonElement | null>(null);
  const gridRef = useRef<MatrixGridHandle | null>(null);
  const isMobile = useIsMobile();

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
  // M3 §4.5: runBusy retired; active runs no longer disable Run (parallel runs; server
  // 409 matrix_active_runs_cap enforces the cap); disable only for in-flight requests (busy).
  const runs = snapshot ? snapshotRuns(snapshot) : [];

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
  const openRunModal = useCallback(
    async (scope: RunScope, columnName?: string, trigger?: HTMLElement | null) => {
      setPendingRun({ scope, columnName, estimate: null, trigger });
      try {
        const est = await estimateRun(matrixId, scope);
        setPendingRun((cur) => (cur && cur.scope === scope ? { ...cur, estimate: est } : cur));
      } catch (e) {
        setPendingRun(null);
        say("error", runApiErr(e));
      }
    },
    [matrixId, say, runApiErr],
  );

  const confirmRun = useCallback(async () => {
    if (!pendingRun) return;
    setBusy(true);
    try {
      await startRun(matrixId, pendingRun.scope);
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
      // wave-2 §4.4: enter filter mode (rows containing failed cells) + locate/focus the first failed cell. Do not
      // open the side panel automatically (top hint guides clicking a failed cell, then retrying in the side panel); first filtered failed row is index 0.
      if (!snapshot) return;
      const failedRows = rowsWithFailedCell(snapshot.rows);
      if (!failedRows.length) return;
      const row = failedRows[0];
      const columnId = Object.entries(row.cells).find(([, c]) => c?.status === "failed")?.[0];
      if (!columnId) return;
      setFailFilter(true);
      gridRef.current?.scrollToRow(0);
      setFocus({ fileId: row.fileId, columnId });
    },
    [snapshot],
  );

  const exitFailFilter = useCallback(() => setFailFilter(false), []);

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
    [colMenu, confirm, matrixId, openRunModal, reload, say, apiErr, t],
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
        if (failFilter) {
          setFailFilter(false);
          return;
        }
        return;
      }
      if (panel || addcol || editCol || pendingRun) return;
      if (!snapshot || !focus) return;
      // Arrows move within filtered rows (brief A6); columns are not clipped.
      const navRows = failFilter ? rowsWithFailedCell(snapshot.rows) : snapshot.rows;
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
    [snapshot, focus, panel, addcol, editCol, pendingRun, failFilter],
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
  // Match backend expand_targets: FR-9 skips edited failed cells (FE-P2-7).
  const pendingCount = snapshot
    ? snapshot.rows.reduce(
        (acc, r) =>
          acc +
          snapshot.columns.filter((c) => {
            const cell = r.cells[c.columnId];
            return !cell || (cell.status === "failed" && !cell.edited);
          }).length,
        0,
      )
    : 0;
  const totalCells = snapshot ? snapshot.rows.length * snapshot.columns.length : 0;
  // "重跑已过期" menu count (snapshot stale && !edited, display only; estimate is authoritative, §4.1).
  const staleCount = snapshot
    ? snapshot.rows.reduce(
        (acc, r) =>
          acc +
          snapshot.columns.filter((c) => {
            const cell = r.cells[c.columnId];
            return !!cell?.stale && !cell.edited;
          }).length,
        0,
      )
    : 0;

  // wave-2 [查看失败] derived filtered view (computed only in filter mode): failed rows / failed-cell count / empty state.
  const failedRows = snapshot && failFilter ? rowsWithFailedCell(snapshot.rows) : [];
  const failCells = snapshot && failFilter ? failedCellCount(snapshot.rows) : 0;
  const filterEmpty = failFilter && failedRows.length === 0;
  const gridSnapshot: MatrixSnapshot | null = snapshot && failFilter ? { ...snapshot, rows: failedRows } : snapshot;

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
      <div className="mx-topbar">
        <button type="button" className="mx-back" aria-label={t("back")} onClick={onBack}>
          <IcBack s={16} />
        </button>
        <span className="mx-title">{snapshot?.title ?? ""}</span>
        <span className="mx-meta">
          {snapshot ? t("meta", { rows: snapshot.rows.length, cols: snapshot.columns.length }) : ""}
        </span>
        <span className="grow" />
        {canEdit && !isMobile && (
          <div className="acts">
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
                pendingLabel={t("runmenu_pending", { n: pendingCount })}
                forceLabel={t("runmenu_cells", { n: totalCells })}
                staleLabel={t("runmenu_cells", { n: staleCount })}
                onPick={(v) => {
                  setRunMenu(false);
                  void openRunModal({ kind: v === "force" ? "force_all" : v === "stale" ? "stale" : "all_pending" }, undefined, runCaretRef.current);
                }}
              />
            </div>
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
          </div>
        )}
      </div>

        <RunBanners
          key={matrixId}
          runs={runs}
          columns={snapshot?.columns ?? []}
          cancelling={busy}
          onCancel={(runId) => void onCancelRun(runId)}
          onViewFailed={onViewFailed}
        />

      {snapshot?.rows.some(row => Object.values(row.cells).some(cell => (cell?.currentSourceState && cell.currentSourceState !== "eligible") || cell?.sourceCheckState === "source_temporarily_unavailable")) && <p className="mx-inlineerr" role="note">{t("current_source_warning")}</p>}
      {/* wave-2 §4.4: filter-chip bar (仅显示失败 · n 格 / m 行 + 清除过滤). */}
      {failFilter && snapshot && !isMobile && (
        <div className="mx-filterbar" data-testid="kb-matrix-filterbar">
          <span className="mx-fchip">
            <span className="ic"><IcWarn s={12} /></span>
            {t("filter_chip", { n: failCells, m: failedRows.length })}
            <button type="button" className="close" title={t("filter_clear")} onClick={exitFailFilter}>
              <IcX s={12} />
            </button>
          </span>
          {!filterEmpty && <span className="hint">{t("filter_hint")}</span>}
        </div>
      )}

      {!snapshot ? (
        <MatrixSkeleton />
      ) : isMobile ? (
        <MatrixMobile snapshot={snapshot} />
      ) : filterEmpty ? (
        <div className="mx-gridempty" data-testid="kb-matrix-failempty">
          <div className="illus"><IcCheck s={30} /></div>
          <h3>{t("fail_empty_title")}</h3>
          <p className="p">{t("fail_empty_body")}</p>
          <button type="button" className="kb-btn kb-btn-primary" onClick={exitFailFilter}>
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
          failFilter={failFilter}
        />
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
            onKeepAsText={() => void onKeepAsText()}
            onUnverify={() => void onUnverify()}
            onRevertEdited={() => void onRevertEdited()}
          />
        )}
      </div>

      {pendingRun && (
        <RunConfirmModal
          scope={pendingRun.scope}
          columnName={pendingRun.columnName}
          estimate={pendingRun.estimate}
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
  onClose,
  onSave,
}: {
  col: MatrixColumnVM;
  busy: boolean;
  onClose: () => void;
  onSave: (patch: {
    name?: string;
    question?: string;
    extraction_hint?: string;
    options?: string[] | null;
    cross_verify?: boolean;
  }) => void;
}) {
  const t = useTranslations("kbMatrix");
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
