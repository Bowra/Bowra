"use client";

/**
 * Structured body of the citation source drawer: tables through the shared
 * report table (`ChartTable`), Markdown through the shared safe renderer,
 * charts built from the same source through `ChartBlock`, and everything else
 * as preformatted text that keeps its line breaks.
 *
 * Classification lives in `lib/source-content.ts` (pure, unit-tested).
 */

import { useMemo } from "react";
import { useLocale, useTranslations } from "next-intl";

import { ChartTable } from "@/components/canvas/sections/chart-table";
import { ChartBlock } from "@/components/canvas/sections/chart-block";
import type { ChartSpec } from "@/lib/chart/types";
import { Markdown } from "@/lib/md";
import { parseNumberCell, type CodeRunSection, type SourceBlock, type SourceTable } from "@/lib/source-content";

/** Text excerpts stay bounded like before; tables carry their own row cap. */
export const SOURCE_TEXT_EXCERPT_CHARS = 2000;

/** Report table spec for a parsed source table. Values are raw sandbox/BI output, so the
 *  percent shape is never guessed from column names (`percent_shape_unknown`). */
export function sourceTableSpec(table: SourceTable): ChartSpec {
  const numeric = new Set(table.numericColumns);
  return {
    kind: "table",
    title: "",
    columns: table.columns.map((column) => ({
      key: column,
      header: column,
      type: numeric.has(column) ? "numeric" : "categorical",
      ...(numeric.has(column) ? { percent_shape_unknown: true } : {}),
    })),
    rows: table.rows.map((row) => Object.fromEntries(table.columns.map((column) => [
      column,
      numeric.has(column) ? parseNumberCell(row[column] ?? "") : row[column] ?? "",
    ]))),
    row_count: table.rows.length,
  } as ChartSpec;
}

function SourceTableBlock({ table, label }: { table: SourceTable; label?: string }) {
  const t = useTranslations("source_panel");
  const spec = useMemo(() => sourceTableSpec(table), [table]);
  return (
    <div className="source-panel-block" data-testid="source-panel-table">
      {label ? <div className="source-panel-block-label">{label}</div> : null}
      <ChartTable spec={spec} chromeless exactNumbers />
      {table.totalRows !== undefined && table.totalRows > table.rows.length ? (
        <div className="source-panel-block-note">
          {t("preview_rows_note", { shown: table.rows.length, total: table.totalRows })}
        </div>
      ) : null}
    </div>
  );
}

export function SourceContent({
  blocks,
  truncated = false,
  charts = [],
}: {
  blocks: SourceBlock[];
  /** The payload itself was cut by the server. */
  truncated?: boolean;
  /** Report charts whose source is this citation. */
  charts?: Array<{ id: string; spec: ChartSpec }>;
}) {
  const t = useTranslations("source_panel");
  let cut = truncated;
  const rendered = blocks.map((block, index) => {
    if (block.kind === "table") {
      return <SourceTableBlock key={index} table={block.table} label={block.label} />;
    }
    const excerpt = block.text.slice(0, SOURCE_TEXT_EXCERPT_CHARS);
    if (excerpt.length < block.text.length) cut = true;
    if (block.kind === "markdown") {
      return (
        <div key={index} className="source-panel-md" data-testid="source-panel-markdown">
          <Markdown markdown={excerpt} allowImages={false} linkRel="noopener noreferrer" />
        </div>
      );
    }
    return (
      <div key={index} className="source-panel-block">
        {block.label ? <div className="source-panel-block-label">{block.label}</div> : null}
        <pre className="legacy-source-panel-preview" data-testid="source-panel-text">{excerpt}</pre>
      </div>
    );
  });
  return (
    <>
      {rendered}
      {cut ? <div className="source-panel-md-truncated">{t("preview_truncated")}</div> : null}
      {charts.length > 0 ? (
        <div className="source-panel-block" data-testid="source-panel-charts">
          <div className="source-panel-block-label">{t("related_charts")}</div>
          {charts.map((chart) => (
            <div key={chart.id} className="source-panel-chart">
              {chart.spec.title ? <div className="source-panel-chart-title">{chart.spec.title}</div> : null}
              <ChartBlock spec={chart.spec} chromeless />
            </div>
          ))}
        </div>
      ) : null}
    </>
  );
}

const RUN_LANG_LABEL_KEY: Record<string, string> = {
  python: "lang_label_python",
  sql: "lang_label_sql",
  shell: "lang_label_shell",
};

function formatRanAt(value: string | undefined, locale: string): string | null {
  if (!value) return null;
  const time = new Date(value);
  if (Number.isNaN(time.getTime())) return null;
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(time);
}

/** Run header of the drawer's Run Code view: when it ran and in which language. The run label
 *  is the drawer title already, so it is not repeated here. */
export function SourceRunMeta({ run }: { run: CodeRunSection }) {
  const t = useTranslations("source_panel");
  const tCode = useTranslations("code_block");
  const locale = useLocale();
  const ranAt = formatRanAt(run.ran_at, locale);
  const langKey = RUN_LANG_LABEL_KEY[run.lang];
  return (
    <div className="source-panel-run-meta" data-testid="source-panel-run-meta">
      <div className="source-panel-block-label">{t("run_result")}</div>
      <div className="source-panel-run-sub">
        {langKey ? tCode(langKey) : run.lang}
        {ranAt ? <> · {t("run_ran_at", { time: ranAt })}</> : null}
      </div>
    </div>
  );
}

/** The code behind the run, collapsed under the output it produced. */
export function SourceRunCode({ run }: { run: CodeRunSection }) {
  const t = useTranslations("source_panel");
  const code = run.code || run.sql || "";
  if (!code) return null;
  return (
    <details className="source-panel-block source-panel-run-code" data-testid="source-panel-run-code">
      <summary>{t("run_code")}</summary>
      <pre className="legacy-source-panel-preview"><code>{code}</code></pre>
    </details>
  );
}
