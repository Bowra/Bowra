"use client";

import dynamic from "next/dynamic";
import { useMemo } from "react";
import { useTranslations } from "next-intl";
import type { ChartSpec } from "@/lib/chart/types";

const ChartBlock = dynamic(
  () => import("@/components/canvas/sections/chart-block").then(module => module.ChartBlock),
  { ssr: false, loading: () => <div className="ad-chart-loading" aria-hidden="true" /> },
);

/**
 * The product chart renderer in its `embed` frame (Chart Embed Spec D4/D5): the panel is the only frame and owns the
 * visible title (`titleId`); the chart draws no title row, kind switch or ⋯, and renders 「查看精确数值」 itself with
 * the full category names when the bars use short ones.
 */
export function AgentDashboardChart({ title, titleId, rows }: {
  title: string;
  titleId: string;
  rows: Array<{ id: string; label: string; chartLabel?: string; count: number }>;
}) {
  const t = useTranslations("agents_marketplace");
  const spec = useMemo<ChartSpec>(() => ({
    kind: "hbar", title, row_count: rows.length,
    columns: [
      { key: "category", header: title, type: "categorical" },
      { key: "entries", header: t("dashboard_agents"), type: "numeric" },
    ],
    rows: rows.map(row => ({ category: row.chartLabel ?? row.label, entries: row.count })),
    show_legend: false, show_data_labels: true,
  }), [title, rows, t]);
  // Keyed by the short name the bar shows, so the table names stay right whatever order the rows are drawn in.
  const fullLabels = useMemo(() => Object.fromEntries(rows.map(row => [row.chartLabel ?? row.label, row.label])), [rows]);
  return <div className="ad-native-chart">
    <ChartBlock spec={spec} surface="canvas" chrome="embed" labelledBy={titleId} exactValueLabels={fullLabels} />
  </div>;
}
