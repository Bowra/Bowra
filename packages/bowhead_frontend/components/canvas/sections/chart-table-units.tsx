import { useTranslations } from "next-intl";
import type { TableColumnDisplay } from "@/lib/chart/table-display";

/** Existing Table unit-note treatment, shared with inspection and static export. */
export function ChartTableUnits({ columns }: { columns: readonly TableColumnDisplay[] }) {
  const t = useTranslations("chart_table");
  const notes = columns.flatMap((column) => column.note === "conversion"
    ? [t("source_scale", { measure: column.measure, source: column.sourceUnit!, displayed: column.displayUnit! })]
    : column.note === "source" ? [t("source_unit", { measure: column.measure, source: column.sourceUnit! })] : []);
  return notes.length ? <p className="v2-chart-table-unit-note text-xs text-muted-foreground" data-chart-unit-note>{notes.join(" · ")}</p> : null;
}
