/**
 * ReportHistory — report-agent-authorship α v1 (2026-06-24): collapse-in-place
 * "latest report wins" history accordion.
 *
 * Verbatim translation of the Claude Design spec
 * `docs/sprints/report-agent-authorship-2026-06-24/design/history-collapse.html`.
 *
 * The report canvas is a single ProseMirror doc (ReportView). When a later
 * report-worthy turn supersedes prior reports, those prior sections are filtered
 * OUT of the live PM doc (report-view preprocessSections) and rendered here, ABOVE
 * the current report, as a two-level accordion:
 *   L0  one summary row, scales to any N — "本次会话较早的 N 份报告 · 展开"
 *   L1  bounded scrollable list — one row per prior report ("第 N 轮 · 标题 · N 段")
 *   L2  inline-expand a row → the full old report (dimmed, read-only), reused via
 *       the `renderArchived` render-prop (avoids a report-view ↔ report-history
 *       import cycle).
 *
 * Nothing vanishes (collapse-in-place): prior reports stay visible+recoverable as
 * expandable headers, so this is the review-mandated fix to the "silent vanish"
 * P0 of a hide-and-remove approach.
 */
"use client";

import { useMemo, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import type { ReportSection } from "@/lib/types";
import { isFigureKind, replacedFigureLabel } from "./report-history-split";
import {
  ChartLine as LucideChartLine,
  ChevronRight as LucideChevronRight,
  ChevronUp as LucideChevronUp,
  History as LucideHistory,
  RotateCcw as LucideRotateCcw,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";
import { useSessionTurnOrdinals } from "@/lib/session-turn-ordinals";

/** A prior report = the sections of one superseded turn, with derived meta. */
interface PriorReport {
  turnId: string;
  ordinal: number; // global turn number (1-based, by document order)
  title: string;
  sections: ReportSection[];
}

function sectionText(s: ReportSection): string {
  if ((s.kind === "h1" || s.kind === "eyebrow") && typeof (s as { text?: string }).text === "string") {
    return (s as { text: string }).text;
  }
  // M3 (report-pipeline-reliability): a folded design/poster turn has no h1/eyebrow,
  // so it would show an EMPTY title in the history accordion and look "gone" once
  // superseded (prod s_5675241ee4). Fall back to the design's own title.
  if (s.kind === "design" && typeof (s as { title?: string }).title === "string") {
    return (s as { title: string }).title;
  }
  return "";
}

/**
 * Group superseded sections into per-turn prior reports, in chronological order.
 * The store preserves backend seq order, so first-appearance order == turn order.
 * `allSections` (current + superseded) drives the GLOBAL turn ordinal so a folded
 * turn shows its real "第 N 轮" relative to the whole session.
 */
function groupPriorReports(
  superseded: ReportSection[],
  allSections: ReportSection[],
  sessionOrdinals: ReadonlyMap<string, number> | null = null,
): PriorReport[] {
  // global turn order: distinct turn_id by first appearance across ALL sections
  const ordinalOf = new Map<string, number>();
  let n = 0;
  for (const s of allSections) {
    const t = s.turn_id ?? "";
    if (!ordinalOf.has(t)) ordinalOf.set(t, ++n);
  }
  const order: string[] = [];
  const byTurn = new Map<string, ReportSection[]>();
  for (const s of superseded) {
    const t = s.turn_id ?? "";
    if (!byTurn.has(t)) {
      byTurn.set(t, []);
      order.push(t);
    }
    byTurn.get(t)!.push(s);
  }
  // P2 batch 3: number by the session turn ordinal (same as the chat topbar), but only when
  // every prior report's turn is known to the session; otherwise keep the report-turn numbering for all rows
  // so the list never mixes two counts.
  const useSession = sessionOrdinals !== null && order.every((t) => sessionOrdinals.has(t));
  return order.map((t) => {
    const secs = byTurn.get(t)!;
    const title =
      secs.map(sectionText).find((x) => x.trim().length > 0) ?? "";
    const ordinal = useSession ? sessionOrdinals.get(t)! : (ordinalOf.get(t) ?? 0);
    return { turnId: t, ordinal, title, sections: secs };
  });
}

// ── icons (verbatim from the design spec) ──────────────────────────────
const Chevron = ({ size = 14 }: { size?: number }) => (
  <LucideGlyph icon={LucideChevronRight} size={size} className="ec" />
);
const RowChevron = () => (
  <LucideGlyph icon={LucideChevronRight} size={13} className="rchev" />
);
const RecycleIcon = ({ size = 11 }: { size?: number }) => (
  <LucideGlyph icon={LucideRotateCcw} size={size} />
);
const HistoryIcon = () => (
  <LucideGlyph icon={LucideHistory} size={14} />
);
const CollapseCaret = () => (
  <LucideGlyph icon={LucideChevronUp} size={12} />
);

export interface ReportHistoryProps {
  /** the superseded (prior-report) sections, in document order */
  superseded: ReportSection[];
  /** all sections (current + superseded) — for global turn ordinals */
  allSections: ReportSection[];
  /** render a prior report read-only (report-view injects this to avoid a cycle) */
  renderArchived: (sections: ReportSection[]) => ReactNode;
}

export function ReportHistory({ superseded, allSections, renderArchived }: ReportHistoryProps) {
  const t = useTranslations("canvas");
  const [entryOpen, setEntryOpen] = useState(false);
  const [openTurns, setOpenTurns] = useState<Set<string>>(new Set());
  const sessionOrdinals = useSessionTurnOrdinals();

  const reports = useMemo(
    () => groupPriorReports(superseded, allSections, sessionOrdinals),
    [superseded, allSections, sessionOrdinals],
  );
  if (reports.length === 0) return null;

  const toggleTurn = (turnId: string) =>
    setOpenTurns((prev) => {
      const next = new Set(prev);
      if (next.has(turnId)) next.delete(turnId);
      else next.add(turnId);
      return next;
    });

  return (
    <div className={`hist${entryOpen ? " open" : ""}`} data-testid="report-history">
      {/* L0 — one summary row, scales to any N */}
      <button
        className="hist-entry"
        aria-expanded={entryOpen}
        onClick={() => setEntryOpen((v) => !v)}
      >
        <Chevron />
        <span className="hicon"><HistoryIcon /></span>
        <span className="label">{t.rich("history_entry", { n: reports.length, b: (c) => <b>{c}</b> })}</span>
        <span className="right">
          <span className="superseded"><RecycleIcon /> <span>{t("history_allSuperseded")}</span></span>
          <span className="hint">{entryOpen ? t("history_collapse") : t("history_expand")}</span>
        </span>
      </button>

      {/* L1 — bounded scrollable list (older → newer) */}
      <div className={`hist-list${reports.length > 6 ? " scroll" : ""}`}>
        {reports.map((r) => {
          const open = openTurns.has(r.turnId);
          return (
            <div className={`hitem${open ? " open" : ""}`} key={r.turnId}>
              <button className="hrow" aria-expanded={open} onClick={() => toggleTurn(r.turnId)}>
                <span className="turn">{t("row_turn", { n: r.ordinal })}</span>
                <span className="rt">{r.title}</span>
                <span className="rm">{t("row_sections", { n: r.sections.length })}</span>
                <RowChevron />
              </button>
              {open && (
                <div className="arch">
                  <div className="arch-notice">
                    <span className="ni"><HistoryIcon /></span>
                    <span className="nt">
                      <span>{t("archive_notice")}</span>
                    </span>
                    <button className="arch-collapse" onClick={() => toggleTurn(r.turnId)}>
                      <CollapseCaret /> <span>{t("archive_collapse")}</span>
                    </button>
                  </div>
                  <div className="arch-doc">{renderArchived(r.sections)}</div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── canvas-update-turn B1-2: bottom accordion for "已替换的图表" ─────────────────────
const FigureIcon = () => (
  <LucideGlyph icon={LucideChartLine} size={14} />
);

export interface ReplacedFiguresProps {
  /** superseded sections carrying `_meta.folded_by === "revise"` (figures + companion code), document order */
  figures: ReportSection[];
  /** same render-prop as ReportHistory — read-only nested ReportView */
  renderArchived: (sections: ReportSection[]) => ReactNode;
}

/**
 * One quiet row at the tail of the current report: the figures the latest revision
 * stopped referencing (and their companion code blocks). Reuses the History shell so
 * it reads as "kept for reference", not as a superseded report. Nothing vanishes.
 */
export function ReplacedFigures({ figures, renderArchived }: ReplacedFiguresProps) {
  const t = useTranslations("canvas");
  const [open, setOpen] = useState(false);
  // Count **figure-kind** sections (code blocks are companions, not counted separately); figures include more than chart/image
  // Backend also collapses data_table / html_artifact / datarc_iframe as figures (`_FIGURE_KINDS`).
  const charts = useMemo(() => figures.filter((s) => isFigureKind(s.kind)), [figures]);
  if (figures.length === 0) return null;
  const labels = charts.map(replacedFigureLabel).filter((x) => x.length > 0);
  return (
    <div className={`hist hist-replaced${open ? " open" : ""}`} data-testid="report-replaced-figures">
      <button className="hist-entry" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <Chevron />
        <span className="hicon"><FigureIcon /></span>
        <span className="label">{t.rich("replaced_figures_entry", { n: charts.length || figures.length, b: (c) => <b>{c}</b> })}</span>
        <span className="right">
          <span className="superseded"><RecycleIcon /> <span>{t("replaced_figures_hint")}</span></span>
          <span className="hint">{open ? t("history_collapse") : t("history_expand")}</span>
        </span>
      </button>
      <div className="hist-list">
        <div className="hitem open">
          {labels.length > 0 && (
            <div className="arch-notice">
              <span className="ni"><FigureIcon /></span>
              <span className="nt">{labels.join(" · ")}</span>
            </div>
          )}
          <div className="arch">
            <div className="arch-doc">{renderArchived(figures)}</div>
          </div>
        </div>
      </div>
    </div>
  );
}
