"use client";

/**
 * tool-results.tsx — the expanded-body renderers for a <Step> (§3 of the
 * block-model-spec). One per backend tool shape:
 *
 *   ParallelRail  → bi_query batch (concurrent SQL lanes)
 *   Hits          → web_search ranked results
 *   Chunks        → kb_search passage previews
 *   Code          → SQL / Python / stdout (raw text, optional lang tag)
 *   ResultTable   → single-query inline preview table
 *
 * All class/token-driven (consume `.ag-*` / `--c-*`); the v2 replacement for
 * the legacy inline-styled StepContentToolRun + CodeBlock path.
 */
import { type ReactNode, useId, useState } from "react";
import { useTranslations } from "next-intl";
import { Chevron, StatusGlyph, type StepStatus } from "./block-primitives";

/* ── code (SQL / Python / stdout) ─────────────────────────────────────── */
export interface CodeProps {
  code: string;
  /** language tag shown top-right (e.g. "SQL" / "Python"). */
  lang?: string;
  /** show the streaming caret at the end. */
  caret?: boolean;
}

/** chat-ux-2026-08 batch A — collapse beyond this many lines. On 116, one python_sandbox
 *  stdout stream measured hundreds of lines; `.code` previously had **no height cap**, rendering all 500 lines
 *  — one major cause of the left column growing to 6664px in a single turn. */
const CODE_COLLAPSE_LINES = 8;

export function Code({ code, lang, caret }: CodeProps) {
  const tStep = useTranslations("step_list");
  const [showAll, setShowAll] = useState(false);
  // Count string lines instead of measuring DOM: deterministic, testable, independent of fonts/width.
  // Do not collapse while streaming (caret): users are watching output arrive, and truncation is most disruptive then.
  const collapsible = !caret && code.split("\n").length > CODE_COLLAPSE_LINES;
  const clipped = collapsible && !showAll;

  return (
    <div className={`codewrap${clipped ? " is-clipped" : ""}`}>
      <pre className="code">
        {lang ? <span className="lang">{lang}</span> : null}
        {code}
        {/* dogfood-0708 #3: streaming cursor gets its own `stream-cursor` class (not generic `.caret`, which collided with the chevron). */}
        {caret ? <span className="stream-cursor" aria-hidden /> : null}
      </pre>
      {collapsible && (
        <button
          type="button"
          className="code-more"
          onClick={() => setShowAll((v) => !v)}
          aria-expanded={showAll}
        >
          {showAll ? tStep("collapse") : tStep("code_show_all")}
        </button>
      )}
    </div>
  );
}

/* ── parallel SQL rail (bi_query batch) ───────────────────────────────── */
export interface ParallelRailRow {
  /** lane label — SQL summary / sub-query text. */
  sql: ReactNode;
  /** right-aligned meta — "234 rows" / "0.6s". */
  meta?: ReactNode;
  status?: StepStatus;
  /** the lane's full SQL — when present, a chevron reveals it inline. */
  code?: string;
}

/** One lane of a bi_query batch, with an optional inline SQL reveal (legacy
 *  SubstepRow parity — see one lane's SQL without leaving the row). */
function ParallelRailLane({ row }: { row: ParallelRailRow }) {
  const tStep = useTranslations("step_list");
  const [open, setOpen] = useState(false);
  const hasCode = Boolean(row.code);
  const codeId = useId();
  return (
    <div className="slane">
      <div className="srow">
        <StatusGlyph status={row.status ?? "done"} />
        <span className="srow-sql">{row.sql}</span>
        {row.meta != null && <span className="srow-meta">{row.meta}</span>}
        {hasCode && (
          <button
            type="button"
            className="srow-chev"
            onClick={() => setOpen((o) => !o)}
            aria-expanded={open}
            aria-controls={codeId}
            aria-label={open ? tStep("collapse_sql") : tStep("expand_sql")}
          >
            <Chevron open={open} />
          </button>
        )}
      </div>
      {open && hasCode && (
        <div className="slane-code" id={codeId}>
          <Code code={row.code as string} lang="SQL" />
        </div>
      )}
    </div>
  );
}

export function ParallelRail({ rows }: { rows: ParallelRailRow[] }) {
  const tChat = useTranslations("chat");
  return (
    <div className="prail">
      <span className="prail-tag">{tChat("parallel")}</span>
      {rows.map((r, i) => (
        <ParallelRailLane key={i} row={r} />
      ))}
    </div>
  );
}

/* ── web_search hits ──────────────────────────────────────────────────── */
export interface HitItem {
  title: ReactNode;
  src: ReactNode;
  page?: number | string;
}

export function Hits({ hits }: { hits: HitItem[] }) {
  return (
    <div className="hits">
      {hits.map((h, i) => (
        <div className="hit" key={i}>
          <div className="hit-t">
            <b>
              {i + 1}. {h.title}
            </b>
          </div>
          <div className="hit-s">
            {h.src}
            {h.page ? ` · p.${h.page}` : ""}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ── kb_search chunks ─────────────────────────────────────────────────── */
export interface ChunkItem {
  src: ReactNode;
  text: ReactNode;
}

export function Chunks({ chunks }: { chunks: ChunkItem[] }) {
  return (
    <div className="chunks">
      {chunks.map((c, i) => (
        <div className="chunk" key={i}>
          <div className="chunk-src">{c.src}</div>
          <div className="chunk-tx">{c.text}</div>
        </div>
      ))}
    </div>
  );
}

/* ── inline result table (single-query preview) ───────────────────────── */
export interface ResultTableProps {
  cols: ReactNode[];
  rows: ReactNode[][];
  /** footer line — e.g. "showing 50 of 1,204". */
  foot?: ReactNode;
  /** indices of numeric columns (right-aligned, tabular-nums). */
  nums?: number[];
}

export function ResultTable({ cols, rows, foot, nums = [] }: ResultTableProps) {
  return (
    <div>
      <table className="tbl">
        <thead>
          <tr>
            {cols.map((c, i) => (
              <th key={i} className={nums.includes(i) ? "n" : undefined}>
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {r.map((v, j) => (
                <td key={j} className={nums.includes(j) ? "n" : undefined}>
                  {v}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {foot != null && <div className="tbl-foot">{foot}</div>}
    </div>
  );
}
