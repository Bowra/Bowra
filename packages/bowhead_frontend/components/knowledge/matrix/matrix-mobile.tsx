"use client";

/**
 * kb-matrix M2 — read-only mobile view (M2 acceptance 8; design r2 matrix-mobile.jsx verbatim
 * Matrix Cards + Source Bottom Sheet from the three screens; reuse the desktop list). No mutation entry.
 */

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Dialog } from "@/components/ui/dialog";

import { useKbChunkContent } from "@/lib/canvas-pm-adapter/kb-chunk-cache";
import type { MatrixCellVM, MatrixColumnVM, MatrixRowVM, MatrixSnapshot } from "@/lib/kb-matrix/types";
import { cellChoiceList } from "@/lib/kb-matrix/types";

import { locateExcerpt } from "./cite-popover";
import { IcCheck, IcChevR, IcClock, IcDoc, IcPencil, IcQuote, IcWarn } from "./matrix-icons";
import { KbSourceDrawer } from "../kb-source-drawer";

/** wave-2: read-only mobile choice-cell rendering (design-wave2 MobileChoiceDetail)—
 * multi_select chips + `+{n}` / single select chip / boolean yes-no badge; empty values fall back to —. */
function renderMobileChoice(
  col: MatrixColumnVM,
  cell: MatrixCellVM,
  labels: { boolTrue: string; boolFalse: string },
): React.ReactNode {
  if (col.type === "boolean") {
    if (cell.value !== "true" && cell.value !== "false") return <span>{cell.value || "—"}</span>;
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
    if (!chips.length) return <span>—</span>;
    const shown = chips.slice(0, 2);
    const extra = chips.length - shown.length;
    return (
      <>
        {shown.map((c, i) => (
          <span key={i} className="mx-chipval">{c}</span>
        ))}
        {extra > 0 && <span className="mx-more">+{extra}</span>}
      </>
    );
  }
  if (!cell.value) return <span>—</span>;
  return <span className="mx-chipval">{cell.value}</span>;
}

interface SheetCtx {
  row: MatrixRowVM;
  col: MatrixColumnVM;
  cell: MatrixCellVM;
}

function MRowVal({
  col,
  cell,
  onOpen,
  labels,
  extractFailed,
}: {
  col: MatrixColumnVM;
  cell: MatrixCellVM | undefined;
  extractFailed?: boolean;
  onOpen: () => void;
  labels: { queued: string; failed: string; boolTrue: string; boolFalse: string };
}) {
  const t = useTranslations("kbMatrix");
  const isNum = col.type === "currency" || col.type === "number" || col.type === "date";
  const isChoice = col.type === "select" || col.type === "multi_select" || col.type === "boolean";
  const clickable = cell && (cell.status === "done" || cell.status === "needs_review" || cell.status === "failed");
  let v: React.ReactNode;
  if (!cell) v = <span style={{ color: "var(--c-ink-3)" }}>{t(extractFailed ? "cell_failed" : "cell_not_run")}</span>;
  else if (cell.status === "running") v = <span>{t("cell_running")}</span>;
  else if (cell.status === "queued") v = <span style={{ color: "var(--c-ink-3)" }}>{labels.queued}</span>;
  else if (cell.status === "failed") v = <span style={{ color: "var(--c-err)" }}>{labels.failed}</span>;
  // done + choice uses chip/boolval (needs_review still shows verbatim text through the else below)
  else if (cell.status === "done" && !cell.value) v = <span>{t("cell_no_value")}</span>;
  else if (cell.status === "done" && isChoice) v = renderMobileChoice(col, cell, labels);
  else v = <span>{cell.value || "—"}</span>;
  return (
    <div className="mx-mrow" role="button" tabIndex={clickable ? 0 : -1} onClick={() => clickable && onOpen()} onKeyDown={(e) => { if (clickable && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); onOpen(); } }}>
      <span className="k">{col.name}</span>
      <span
        className={"v" + (isNum ? " num" : "") + (col.type === "question" ? " q" : "")}
        style={col.type === "multi_select" ? { flexWrap: "wrap" } : undefined}
      >
        {cell?.status === "needs_review" && <span className="mx-sb review" style={{ width: 14, height: 14 }}><IcWarn s={11} /></span>}
        {cell?.stale && <span className="mx-sb stale" style={{ width: 14, height: 14 }}><IcClock s={11} /></span>}
        {cell?.edited && <span className="mx-sb edited" style={{ width: 14, height: 14 }}><IcPencil s={10} /></span>}
        {cell?.verified && <span className="mx-sb edited" style={{ width: 14, height: 14 }}><IcCheck s={11} /></span>}
        {col.type === "question" ? <span className="mx-mval">{v}</span> : v}
        {clickable && (
          <span className="mx-cite" style={{ position: "static", marginLeft: "auto", color: "var(--c-ink-4)" }}>
            <IcQuote s={12} />
          </span>
        )}
      </span>
    </div>
  );
}

function SheetBody({ ctx }: { ctx: SheetCtx }) {
  const t = useTranslations("kbMatrix");
  const chunk = useKbChunkContent(ctx.row.fileId, ctx.cell.chunkId, undefined, { single: true });
  const located = chunk.status === "ready" ? locateExcerpt(chunk.content, ctx.cell.sourceExcerpt) : null;
  const [viewingSource, setViewingSource] = useState(false);
  return (
    <>
      <div style={{ fontSize: "0.75rem", color: "var(--c-ink-3)", marginBottom: 2 }}>{ctx.col.question || ctx.col.name}</div>
      <div className="mx-valbig" style={{ fontSize: "1.1875rem" }}>{ctx.cell.value || t(ctx.cell.status === "failed" ? "cell_failed" : "cell_no_value")}</div>
      {ctx.cell.status === "done" && !ctx.cell.value && <p>{t("cell_no_value_detail")}</p>}
      {ctx.cell.status === "failed" && <p>{t(`fail_${ctx.cell.failReason ?? "llm_invoke_failed"}` as never)}</p>}
      {(ctx.cell.sourceCheckState === "source_temporarily_unavailable" || (ctx.cell.currentSourceState && ctx.cell.currentSourceState !== "eligible")) && <p role="note">{t("current_source_warning")}</p>}
      <div className="mx-seclabel">{t("sec_citation")}</div>
      <div className="mx-quote" style={{ fontSize: "0.75rem" }}>
        {located ? (
          <>
            {located.before}
            <span className="mx-hl">{located.hit}</span>
            {located.after}
          </>
        ) : chunk.status !== "error" && ctx.cell.sourceExcerpt ? (
          <span className="mx-hl">{ctx.cell.sourceExcerpt}</span>
        ) : (
          <span style={{ color: "var(--c-ink-3)" }}>{t("cite_unlocated")}</span>
        )}
      </div>
      <div className="mx-srcmeta">
        <IcDoc s={13} />
        <span>
          {ctx.row.missing ? t("file_missing") : ctx.row.fileName}
          {ctx.cell.page != null ? ` · ${t("page_n", { n: ctx.cell.page })}` : ""}
        </span>
      </div>
      {!ctx.row.missing && (
        <button type="button" className="mx-viewsrc" onClick={() => setViewingSource(true)}>
          {t("view_source")} <IcChevR s={13} />
        </button>
      )}
      {viewingSource && (
        <KbSourceDrawer
          fileId={ctx.row.fileId}
          chunkId={ctx.cell.chunkId}
          highlight={located?.hit}
          onClose={() => setViewingSource(false)}
        />
      )}
    </>
  );
}

export function MatrixMobile({ snapshot }: { snapshot: MatrixSnapshot }) {
  const t = useTranslations("kbMatrix");
  const [sheet, setSheet] = useState<SheetCtx | null>(null);
  const labels = { queued: t("cell_queued"), failed: t("cell_failed"), boolTrue: t("bool_true"), boolFalse: t("bool_false") };
  return (
    <div className="mx-mobile" data-testid="kb-matrix-mobile">
      <div style={{ paddingBottom: 20 }}>
        {snapshot.rows.map((r) => (
          <div className="mx-mcard" key={r.fileId}>
            <div className="ch">
              <span className="fico" style={{ background: r.missing ? "#6B6463" : "#B43A2A" }}>
                {r.missing ? "?" : (r.fileName.split(".").pop() || "DOC").slice(0, 4).toUpperCase()}
              </span>
              <span className="fname">{r.missing ? t("file_missing") : r.fileName}</span>
            </div>
            {snapshot.columns.map((c) => (
              <MRowVal
                key={c.columnId}
                col={c}
                cell={r.cells[c.columnId]}
                extractFailed={r.extractFailed}
                labels={labels}
                onOpen={() => {
                  const cell = r.cells[c.columnId];
                  if (cell) setSheet({ row: r, col: c, cell });
                }}
              />
            ))}
          </div>
        ))}
      </div>
      {/* P2 batch 5: the shared bottom sheet (handle, title bar with Close, swipe down to close).
          The title bar names the column; the full question stays at the top of the body. */}
      {sheet && (
        <Dialog presentation="sheet" title={sheet.col.name} closeLabel={t("close")} onClose={() => setSheet(null)}>
          <SheetBody ctx={sheet} />
        </Dialog>
      )}
    </div>
  );
}
