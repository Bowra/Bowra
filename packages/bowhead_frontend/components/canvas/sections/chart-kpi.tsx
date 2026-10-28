"use client";

import { useChartTheme } from "@/lib/chart-theme";
import {
  formatObservedPercentCell,
  formatObservedValue,
  kpiValueLines,
  axisUnitOf,
  measureDisplayName,
  resolvePercentColumn,
  resolvePercentScale,
} from "@/lib/chart/format";
import type { ChartSpec } from "@/lib/chart/types";

/**
 * KPI card group for a single row of multiple numeric columns. Pure DIV layout matches product cards, without Chart.js.
 *
 * Trigger: spec.kind === "kpi" (infer_chart_spec emits this for n=1 + all numeric).
 * Each numeric column of spec.rows[0] becomes a card.
 */
export function ChartKPI({ spec, switcher, embedded = false }: {
  spec: ChartSpec;
  switcher?: React.ReactNode;
  /** Chart Embed Spec D4: inside a host panel — no own title row or card frame (the host gives both). */
  embedded?: boolean;
}) {
  const theme = useChartTheme();
  const row = (spec.rows ?? [])[0] ?? {};
  const numCols = spec.columns.filter((c) => c.type === "numeric");

  if (!numCols.length) return null;

  return (
    <div className={`v2-chart-kpi${embedded ? " is-embedded" : ""}`} role="group" aria-label={spec.title}>
      {/* Batch 1 2.4 (1-F): this component alone owns title + actions; chart-block no longer adds duplicate titles/frames. */}
      {!embedded && (spec.title || switcher) ? (
        <div className="v2-chart-kpi-title-row">
          {spec.title ? <div className="v2-chart-kpi-title">{spec.title}</div> : null}
          {switcher ? <div className="v2-chart-kpi-actions">{switcher}</div> : null}
        </div>
      ) : null}
      <div className="v2-chart-kpi-grid">
        {numCols.map((c) => {
          const v = row[c.key];
          const num = typeof v === "number" ? v : null;
          // The unabridged readout shares ordinary column precision with Table and hover.
          const isPercent = resolvePercentColumn(c);
          const unit = axisUnitOf(c);
          // KPI is single-row, so column shape cannot be inferred; explicit percent_scale / name families still apply,
          // required for attainment rates: single-value heuristics classify 1.02 as points and render "1.0%".
          const display = isPercent
            ? formatObservedPercentCell(typeof v === "string" ? v : num, resolvePercentScale([num], c), c.display)
            : formatObservedValue(typeof v === "string" ? v : num, unit, c.format, true, c.display, c.currency_code);
          // Reuse the existing compact/main and ordinary/secondary layout, including explicit integer precision.
          const { main, exact, swapped, ems } = kpiValueLines(num, display, {
            unit,
            format: c.format,
            display: c.display,
            currency_code: c.currency_code,
            isPercent: Boolean(isPercent),
            header: c.source_header || c.header,
          });
          return (
            <div key={c.key} className="v2-chart-kpi-cell">
              <div className="v2-chart-kpi-label" style={{ color: theme.inkSoft }}>
                {measureDisplayName(c)}
              </div>
              <div
                className="v2-chart-kpi-value"
                data-kpi-swapped={swapped ? "" : undefined}
                // Fit fallback (option 1d): CSS shrinks the font only when this estimate cannot fit the cell.
                style={{ color: theme.ink, "--kpi-ems": ems } as React.CSSProperties}
              >
                {main}
              </div>
              {exact !== null ? (
                <div className="v2-chart-kpi-exact" style={{ color: theme.inkSoft }}>
                  {exact}
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
      {spec.subtitle ? (
        <div className="v2-chart-kpi-subtitle" style={{ color: theme.inkSoft }}>
          {spec.subtitle}
        </div>
      ) : null}
    </div>
  );
}
