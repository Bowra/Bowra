"use client";

/**
 * `data_table` section — now just an adapter wrapper around `ChartTable`.
 *
 * Before consolidation this was the **second table renderer** (198 lines, its own `<table>` and action bar).
 * Each renderer had capabilities the other lacked, so they were merged into their union rather than deleting one:
 * the right-column capabilities (cell markdown / totals row / 🐍 badge / legacy-shape adaptation) all moved into
 * `ChartTable` + `lib/chart/data-table-to-spec.ts`; see that file's header for criteria and tradeoffs.
 *
 * ★ Consolidation also fixed a real issue: the old implementation rendered every `rows.map` row **without truncation**, while sandbox
 * CSV previews can have hundreds of rows. Routing through `ChartTable` automatically gives collapsed display (10 rows) + expand all +
 * virtual scrolling at ≥200 rows.
 *
 * ⚠ No chart-kind switcher: `data_table` is a table exactly as returned by the tool; switching it to a pie chart is meaningless
 * (`ChartTable`'s switcher prop is optional, so omit it here).
 */

import { useMemo } from "react";

import { dataTableToRendering } from "@/lib/chart/data-table-to-spec";
import type { ReportSection } from "@/lib/types";

import { ChartTable } from "./chart-table";

export type DataTableSectionData = Extract<ReportSection, { kind: "data_table" }>;

export function DataTableSection({
  data,
  surface = "canvas",
}: {
  data: DataTableSectionData;
  surface?: "canvas" | "chat" | "export";
}) {
  // memo: although `ChartTable` now resets by **content signature** (rerenders with new wrapper objects no longer
  // clear expanded state), mapping again on every render would still be wasted work.
  const rendering = useMemo(() => dataTableToRendering(data), [data]);
  if (!data.rows?.length) return null;
  return (
    <ChartTable
      spec={rendering.spec}
      surface={surface}
      sectionId={data.id}
      cellMarkdown
      sandboxBadge={rendering.sandboxBadge}
      downloadFilename={rendering.downloadFilename}
    />
  );
}
