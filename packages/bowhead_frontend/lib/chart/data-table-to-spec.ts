/**
 * `data_table` section → `ChartSpec`, routing through **the same** table renderer (`ChartTable`).
 *
 * Why consolidate: reports previously had two renderers for one table. Each has capabilities the other lacks,
 * so combine them rather than merely delete one:
 *
 * | Unique to `ChartTable`             | Unique to `DataTableSection`                  |
 * | --------------------------------- | --------------------------------------------- |
 * | Collapse/expand all (fetch rows; no fake expansion on failure) | Markdown inside cells |
 * | No sorting while collapsed (avoids false Top10) | `totalRow` totals row                 |
 * | Sorting / virtual scrolling (≥200 rows) | 🐍 Sandbox badge                           |
 * | Column-level percent/unit/date formats | Legacy `{columns:string[],rows:any[][]}` adaptation |
 * | Export auto-fetch + `aria-busy`    | —                                             |
 *
 * 116 production measurements (2026-09-08): **10 `data_table` sections**, latest on 8-29;
 * **five of six** right-side capabilities never used (subCaption/totalRow/badge/artifact/markdown/legacy all 0),
 * only `caption` appears in all ten — directly mapped to `spec.title`.
 * `totalRow` even has **no producer in composer** (only `export_pptx` reads it).
 * Thus union cost is small in practice; still migrate every right-side capability without removal
 * (moving capabilities ≠ losing capabilities).
 *
 * ★ Consolidation also fixes a real issue: `DataTableSection` renders all `rows.map` **without truncation**,
 * while sandbox CSV previews can contain hundreds of rows. `ChartTable` adds collapsing + virtual scrolling automatically.
 */

import type { ChartColumn, ChartSpec } from "@/lib/chart/types";
import type { ReportSection, TableCol, TableRow } from "@/lib/types";

type DataTableSection = Extract<ReportSection, { kind: "data_table" }>;

/** Totals-row marker key: `ChartTable` uses it for `<tr class="total">`. */
export const TOTAL_ROW_FLAG = "_total";

/**
 * 0.39.8 #4 — pre-0.39.8 `canvas_composer` wrote columns as `string[]` and rows as
 * `unknown[][]`; rendering with `{key,header}` / `row[col.key]` yields empty headers + all `—`.
 * Persisted old sections cannot be changed, requiring render-time adaptation.
 *
 * Retain this adapter for supported historical section shapes. A current deployment using
 * only the new shape does not establish that all persisted sections have migrated.
 */
export function normalizeLegacyTableShape(
  columns: unknown,
  rows: unknown,
): { columns: TableCol[]; rows: TableRow[] } | null {
  const legacyCols = Array.isArray(columns) && columns.length > 0 && typeof columns[0] === "string";
  const legacyRows = Array.isArray(rows) && rows.length > 0 && Array.isArray(rows[0]);
  if (!legacyCols && !legacyRows) return null;
  const names = (legacyCols ? (columns as string[]) : (columns as TableCol[]).map((c) => c.key)) ?? [];
  // ★ Legacy column names are strings without numeric metadata — infer from data. **Look beyond first row**,
  //   not just at first row: real production section `sec_00f08ec49602` has row[0] `["", 9905]`,
  //   so first-row-only detection classifies col_a as numeric (old `peekNumeric` was written for this).
  const peekNumeric = (name: string, index: number): boolean => {
    for (const r of (rows as unknown[]) ?? []) {
      const v = Array.isArray(r) ? (r as unknown[])[index] : (r as Record<string, unknown>)?.[name];
      if (v === null || v === undefined || v === "") continue;
      return typeof v === "number";
    }
    return false;
  };
  const outCols: TableCol[] = names.map((name, i) => ({
    key: name,
    header: name,
    numeric: peekNumeric(name, i),
  }));
  const outRows: TableRow[] = ((rows as unknown[]) ?? []).map((r) => {
    if (!Array.isArray(r)) return r as TableRow;
    const row: Record<string, unknown> = {};
    names.forEach((name, i) => {
      row[name] = (r as unknown[])[i];
    });
    return row as TableRow;
  });
  return { columns: outCols, rows: outRows };
}

export interface DataTableRendering {
  spec: ChartSpec;
  /** 🐍 Badge: mark sandbox-produced tables (`meta.source === "python_sandbox"`). */
  sandboxBadge: boolean;
  /** Original download filename; null without artifact (falls back to CSV export). */
  downloadFilename: string | null;
}

/** `data_table` section → rendering input. Pure function, no DOM/window reads. */
export function dataTableToRendering(section: DataTableSection): DataTableRendering {
  const legacy = normalizeLegacyTableShape(section.columns as unknown, section.rows as unknown);
  const cols: TableCol[] = legacy ? legacy.columns : (section.columns ?? []);
  const baseRows: TableRow[] = legacy ? legacy.rows : (section.rows ?? []);
  // Append flagged totals row. `ChartTable` recognizes only this key, never assumes the last row is totals
  // — that would render actual data as totals when no totals row exists.
  const rows: TableRow[] = section.totalRow
    ? [...baseRows, { ...section.totalRow, [TOTAL_ROW_FLAG]: true } as TableRow]
    : baseRows;

  const columns: ChartColumn[] = cols.map((c) => ({
    key: c.key,
    header: c.header ?? c.key,
    // `data_table` uses boolean `numeric`; `ChartSpec` uses `type`. Both express the same thing
    // (right alignment + numeric formatting); adapt once here.
    type: c.numeric ? "numeric" : "categorical",
    format: c.format, unit: c.unit, currency_code: c.currency_code,
    display: c.display, percent_scale: c.percent_scale,
  }));

  return {
    spec: {
      kind: "table",
      title: section.caption,
      subtitle: section.subCaption,
      columns,
      rows,
      row_count: rows.length,
      // With artifact, open-data / download uses the original file (same existing `ChartTable` semantics).
      data_artifact_id: section.meta?.artifact_id,
    } as ChartSpec,
    sandboxBadge: section.meta?.source === "python_sandbox",
    downloadFilename: section.meta?.filename ?? null,
  };
}
