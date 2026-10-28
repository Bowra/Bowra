"use client";

/**
 * kb-matrix — MatrixGrid: virtualized rows (@tanstack/react-virtual),
 * frozen file column + sticky header, per-cell badges + citation anchor.
 * Verbatim translation of design r2 matrix-grid.jsx. M2 extends the M1
 * three-way cell map (empty/review/done) to the full state set:
 * queued (pale placeholder) / running (shimmer) / failed (red text + dot) / stale (clock marker)
 * / flash (480ms pulse) / aggregate header badge + column-menu caret / empty hover Run this cell.
 */

import { forwardRef, memo, useEffect, useId, useImperativeHandle, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useTranslations } from "next-intl";

import type { MatrixCellVM, MatrixColumnVM, MatrixRowVM, MatrixSnapshot } from "@/lib/kb-matrix/types";
import { cellChoiceList } from "@/lib/kb-matrix/types";

import { IcCaret, IcCheck, IcClock, IcPencil, IcPlay, IcQuote, IcSpin, IcWarn } from "./matrix-icons";

// wave-2: select ◉ / multi-select ⊞ / boolean ⊙ (date retains r2 grid ☷; picker uses 📅, see type-picker).
// M4: export for graduation readiness list/template-drawer fields (glyph authority, mockup review #1).
export const TYPE_GLYPH: Record<string, string> = { string: "Aa", number: "#", currency: "¥", date: "☷", question: "？", select: "◉", multi_select: "⊞", boolean: "⊙" };
const DEF_W: Record<string, number> = { string: 152, number: 132, currency: 150, date: 132, question: 264, select: 150, multi_select: 200, boolean: 120 };
const ROW_H = 44;

/** Whether wave-2 choice cells render chip/boolval (needs_review retains original text, no chip). */
function isChoiceType(type: string): boolean {
  return type === "select" || type === "multi_select" || type === "boolean";
}

function colWidth(c: MatrixColumnVM): number {
  return DEF_W[c.type] ?? 152;
}

export interface CiteHoverInfo {
  anchor: DOMRect;
  fileId: string;
  columnId: string;
}

export interface GridLabels {
  notRun: string;
  noValue: string;
  running: string;
  reviewTooltip: string;
  editedTooltip: string;
  verifiedTooltip: string;
  citeTooltip: string;
  staleTooltip: string;
  failedTooltip: string;
  queued: string;
  failed: string;
  runCell: string;
  /** i18n display for boolean "true"/"false" tokens (variantDisplayValue is pure,
   * not localized; G-P3-10 mapping occurs in renderer here). */
  boolTrue: string;
  boolFalse: string;
  /** multi_select overflow badge +{n} hover tooltip “全部 {n} 项：{list}” (wave-2). */
  moreTip: (n: number, list: string) => string;
}

/** Boolean tokens → Yes/No (other types pass through unchanged). */
function displayValue(value: string, colType: string, labels: GridLabels): string {
  if (colType !== "boolean") return value;
  if (value === "true") return labels.boolTrue;
  if (value === "false") return labels.boolFalse;
  return value;
}

/** wave-2 choice rendering (done): select single chip / multi_select ≤2 chips + `+{n}`
 * overflow badge (full tooltip) / boolean dot badge (No uses muted dot, never red). */
function renderChoiceCell(cell: MatrixCellVM, col: MatrixColumnVM, labels: GridLabels): React.ReactNode {
  if (col.type === "boolean") {
    if (cell.value !== "true" && cell.value !== "false") return <span className="val">—</span>;
    const yes = cell.value === "true";
    return (
      <span className={"mx-boolval " + (yes ? "yes" : "no")}>
        <span className="dot" />
        {yes ? labels.boolTrue : labels.boolFalse}
      </span>
    );
  }
  if (col.type === "multi_select") {
    const chips = cellChoiceList(cell);
    if (!chips.length) return <span className="val">—</span>;
    const shown = chips.slice(0, 2);
    const extra = chips.length - shown.length;
    return (
      <span className="val chips">
        {shown.map((c, i) => (
          <span key={i} className="mx-chipval">{c}</span>
        ))}
        {extra > 0 && (
          <span className="mx-more" title={labels.moreTip(chips.length, chips.join("、"))}>+{extra}</span>
        )}
      </span>
    );
  }
  // select
  if (!cell.value) return <span className="val">—</span>;
  return <span className="mx-chipval">{cell.value}</span>;
}

/** Aggregate column-status badge (design r2 colStatus): running n/total → failed n → not run n → stale dot. */
function colStatus(
  col: MatrixColumnVM,
  rows: MatrixRowVM[],
): { kind: "run"; done: number; total: number } | { kind: "red" | "gray"; n: number } | { kind: "amber" } | null {
  let running = 0;
  let notrun = 0;
  let failed = 0;
  let stale = 0;
  for (const r of rows) {
    const cell = r.cells[col.columnId];
    if (!cell) {
      notrun++;
      continue;
    }
    if (cell.status === "running") running++;
    else if (cell.status === "queued") notrun++;
    else if (cell.status === "failed") failed++;
    else if (cell.stale) stale++;
  }
  const total = rows.length;
  if (running > 0) return { kind: "run", done: total - running - notrun, total };
  if (failed > 0) return { kind: "red", n: failed };
  if (notrun > 0 && notrun < total) return { kind: "gray", n: notrun };
  if (stale > 0) return { kind: "amber" };
  return null;
}

function ColBadge({ st }: { st: ReturnType<typeof colStatus> }) {
  if (!st) return null;
  if (st.kind === "run")
    return (
      <span className="mx-cbadge">
        <IcSpin s={13} />
        <span className="runcount">
          {st.done}/{st.total}
        </span>
      </span>
    );
  if (st.kind === "red") return <span className="mx-cbadge"><span className="num red">{st.n}</span></span>;
  if (st.kind === "gray") return <span className="mx-cbadge"><span className="num gray">{st.n}</span></span>;
  return <span className="mx-cbadge"><span className="dot amber" /></span>;
}

function ColumnHeader({
  col,
  rows,
  onMenu,
}: {
  col: MatrixColumnVM;
  rows: MatrixRowVM[];
  onMenu?: (col: MatrixColumnVM, e: React.MouseEvent) => void;
}) {
  const st = colStatus(col, rows);
  const t = useTranslations("kbMatrix");
  const colMenuLabel = (name: string) => t("colmenu_label", { name });
  const [showQuestion, setShowQuestion] = useState(false);
  const questionId = useId();
  return (
    <div className={"mx-colh" + (col.type === "question" ? " q" : "")} title={col.question || col.name}>
      <span className="tico">{TYPE_GLYPH[col.type] ?? "Aa"}</span>
      <button type="button" className="cname mx-question-trigger" aria-expanded={showQuestion} aria-describedby={showQuestion ? questionId : undefined}
        onClick={() => setShowQuestion(true)} onFocus={() => setShowQuestion(true)}
        onBlur={() => setShowQuestion(false)} onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); setShowQuestion(false); } }}>
        {col.name}
      </button>
      {showQuestion && <div id={questionId} role="tooltip" className="mx-question-text">{col.question || col.name}</div>}
      <span className="mx-colh-badge">
        <ColBadge st={st} />
      </span>
      {onMenu && (
        // A real button (accessibility fix, both appearances): reachable with Tab, opens the column menu with Enter or
        // Space, and is the menu's focus-return target, so Esc lands back here instead of on <body>. Keys stop here
        // so the matrix grid does not also act on Enter or arrows.
        <button
          type="button"
          className="ccaret"
          aria-haspopup="menu"
          aria-label={colMenuLabel(col.name)}
          data-testid={`kb-matrix-colmenu-${col.columnId}`}
          onClick={(e) => {
            e.stopPropagation();
            onMenu(col, e);
          }}
          onKeyDown={(e) => e.stopPropagation()}
        >
          <IcCaret s={13} />
        </button>
      )}
    </div>
  );
}

function FileCell({ row, missingLabel }: { row: MatrixRowVM; missingLabel: string }) {
  return (
    <div className="mx-filecell">
      <span className="fico" style={{ background: row.missing ? "#6B6463" : "#B43A2A" }}>
        {row.missing ? "?" : (row.fileName.split(".").pop() || "DOC").slice(0, 4).toUpperCase()}
      </span>
      <span className="fmeta">
        <span className={"fname" + (row.missing ? " missing" : "")} title={row.missing ? missingLabel : row.fileName}>
          {row.missing ? missingLabel : row.fileName}
        </span>
      </span>
    </div>
  );
}

interface DataCellProps {
  row: MatrixRowVM;
  col: MatrixColumnVM;
  focused: boolean;
  canRun: boolean;
  onClick: (fileId: string, columnId: string) => void;
  onRunCell?: (fileId: string, columnId: string) => void;
  onCiteHover: (info: CiteHoverInfo | null) => void;
  labels: GridLabels;
}

const DataCell = memo(function DataCell({ row, col, focused, canRun, onClick, onRunCell, onCiteHover, labels }: DataCellProps) {
  const citeTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  // Virtualized rows unmount without mouseleave — an armed timer would fire
  // against a stale DOMRect and orphan the popover (impl review FE P1-1).
  useEffect(() => () => clearTimeout(citeTimer.current), []);
  const cell = row.cells[col.columnId];
  const status = cell?.status;
  const isNum = col.type === "currency" || col.type === "number" || col.type === "date";
  const openable = status === "done" || status === "needs_review" || status === "failed";
  const cls = ["mx-cell", "gcell"];
  if (!cell) cls.push("is-empty");
  else cls.push(`is-${status === "needs_review" ? "review" : status}`);
  if (isNum) cls.push("num");
  if (col.type === "question") cls.push("q");
  if (focused) cls.push("is-focus");
  if (cell?.flash) cls.push("is-flash");
  if (cell?.cached) cls.push("is-cached");
  const showCite = status === "done" || status === "needs_review";

  let inner: React.ReactNode;
  if (!cell) inner = <span className="val">{row.extractFailed ? labels.failed : labels.notRun}</span>;
  else if (status === "queued") inner = <span className="val queued">{labels.queued}</span>;
  else if (status === "running") inner = <span className="val">{labels.running}</span>;
  else if (status === "failed") inner = <span className="val failed">{labels.failed}</span>;
  // done + choice uses chip/boolval (needs_review falls through to original text, not chip).
  else if (status === "done" && !cell.value) inner = <span className="val">{labels.noValue}</span>;
  else if (status === "done" && isChoiceType(col.type)) inner = renderChoiceCell(cell, col, labels);
  else inner = <span className="val">{displayValue(cell.value, col.type, labels) || "—"}</span>;

  return (
    <div
      className={cls.join(" ")}
      data-cell={col.columnId}
      role="gridcell"
      tabIndex={openable ? 0 : -1}
      onClick={() => openable && onClick(row.fileId, col.columnId)}
      onKeyDown={(e) => {
        if (openable && e.key === "Enter") {
          // Container onKeyDown reads the (stale) focus state — don't let both
          // handlers race over which cell opens (impl review FE P2-4).
          e.stopPropagation();
          onClick(row.fileId, col.columnId);
        }
      }}
    >
      {(status === "needs_review" || status === "failed" || cell?.stale || cell?.edited) && (
        <span className="mx-badges">
          {status === "needs_review" && (
            <span className="mx-sb review" title={labels.reviewTooltip}>
              <IcWarn s={12} />
            </span>
          )}
          {status === "failed" && (
            <span className="mx-sb failed" title={labels.failedTooltip}>
              <IcWarn s={12} />
            </span>
          )}
          {cell?.stale && (
            <span className="mx-sb stale" title={labels.staleTooltip}>
              <IcClock s={12} />
            </span>
          )}
          {cell?.edited && (
            <span className="mx-sb edited" title={labels.editedTooltip}>
              <IcPencil s={11} />
            </span>
          )}
        </span>
      )}
      {inner}
      {!cell && canRun && onRunCell && (
        <button
          type="button"
          className="mx-cellrun"
          onClick={(e) => {
            e.stopPropagation();
            onRunCell(row.fileId, col.columnId);
          }}
        >
          <IcPlay s={9} />
          {labels.runCell}
        </button>
      )}
      {cell && showCite && (
        <span
          className={"mx-cite" + (cell.verified ? " verified" : "") + (cell.chunkId ? "" : " deg")}
          title={cell.verified ? labels.verifiedTooltip : labels.citeTooltip}
          onMouseEnter={(e) => {
            const anchor = e.currentTarget.getBoundingClientRect();
            citeTimer.current = setTimeout(() => onCiteHover({ anchor, fileId: row.fileId, columnId: col.columnId }), 200);
          }}
          onMouseLeave={() => {
            clearTimeout(citeTimer.current);
            onCiteHover(null);
          }}
        >
          {cell.verified ? <IcCheck s={12} /> : <IcQuote s={12} />}
        </span>
      )}
    </div>
  );
});

export interface MatrixGridProps {
  snapshot: MatrixSnapshot;
  focus: { fileId: string; columnId: string } | null;
  onCell: (fileId: string, columnId: string) => void;
  onCiteHover: (info: CiteHoverInfo | null) => void;
  /** M2: empty-cell hover Run this cell (absent in viewer/readOnly views). */
  onRunCell?: (fileId: string, columnId: string) => void;
  /** M2: column-header menu caret (absent in readOnly view). */
  onColMenu?: (col: MatrixColumnVM, e: React.MouseEvent) => void;
  /** wave-2 View failures: mute nonfailed cells (CSS `.failfilter`; parent filters rows). */
  failFilter?: boolean;
}

/** M3 §4.4: View failures positioning can scroll rows outside virtual window into view. */
export interface MatrixGridHandle {
  scrollToRow(index: number): void;
}

export const MatrixGrid = forwardRef<MatrixGridHandle, MatrixGridProps>(function MatrixGrid(
  { snapshot, focus, onCell, onCiteHover, onRunCell, onColMenu, failFilter }: MatrixGridProps,
  ref,
) {
  const t = useTranslations("kbMatrix");
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [scrolled, setScrolled] = useState(false);
  const rowVirtualizer = useVirtualizer({
    count: snapshot.rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_H,
    overscan: 10,
  });
  useImperativeHandle(
    ref,
    () => ({
      scrollToRow: (index: number) => rowVirtualizer.scrollToIndex(index, { align: "center" }),
    }),
    [rowVirtualizer],
  );
  const template = ["232px", ...snapshot.columns.map((c) => `${colWidth(c)}px`)].join(" ");
  // Stable reference: new inline-object identity each render defeats DataCell memo (same M1 lesson).
  const labels: GridLabels = useMemo(
    () => ({
      notRun: t("cell_not_run"),
      noValue: t("cell_no_value"),
      running: t("cell_running"),
      reviewTooltip: t("tip_review"),
      editedTooltip: t("tip_edited"),
      verifiedTooltip: t("tip_verified"),
      citeTooltip: t("tip_cite"),
      staleTooltip: t("tip_stale"),
      failedTooltip: t("tip_failed"),
      queued: t("cell_queued"),
      failed: t("cell_failed"),
      runCell: t("run_cell"),
      boolTrue: t("bool_true"),
      boolFalse: t("bool_false"),
      moreTip: (n: number, list: string) => t("more_tooltip", { n, list }),
    }),
    [t],
  );

  return (
    <div
      className={"mx-scroll" + (scrolled ? " scrolled" : "") + (failFilter ? " failfilter" : "")}
      ref={scrollRef}
      role="grid"
      data-testid="kb-matrix-grid"
      onScroll={(e) => {
        setScrolled(e.currentTarget.scrollLeft > 2);
        // position:fixed popover holds a one-shot rect — scrolling detaches it
        // from its cell, so close it (impl review FE P1-1 sibling).
        onCiteHover(null);
      }}
    >
      <div className="mx-headrow" style={{ gridTemplateColumns: template }} role="row">
        <div className="mx-colh freeze">
          <span className="cname" style={{ fontWeight: 600 }}>{t("col_file")}</span>
        </div>
        {snapshot.columns.map((c) => (
          <ColumnHeader key={c.columnId} col={c} rows={snapshot.rows} onMenu={onColMenu} />
        ))}
      </div>
      <div className="mx-grid" style={{ height: rowVirtualizer.getTotalSize() }}>
        {rowVirtualizer.getVirtualItems().map((vi) => {
          const row = snapshot.rows[vi.index];
          return (
            <div
              key={row.fileId + ":" + row.recordId}
              className="mx-vrow"
              role="row"
              style={{ gridTemplateColumns: template, transform: `translateY(${vi.start}px)`, height: vi.size }}
            >
              <div className="gcell freeze">
                <FileCell row={row} missingLabel={t("file_missing")} />
              </div>
              {snapshot.columns.map((c) => (
                <DataCell
                  key={c.columnId}
                  row={row}
                  col={c}
                  focused={focus?.fileId === row.fileId && focus?.columnId === c.columnId}
                  canRun={!row.missing && !!onRunCell}
                  onClick={onCell}
                  onRunCell={onRunCell}
                  onCiteHover={onCiteHover}
                  labels={labels}
                />
              ))}
            </div>
          );
        })}
      </div>
    </div>
  );
});
