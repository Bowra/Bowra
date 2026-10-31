import { formatExactSourceNumber } from "../source-content";
import {
  axisUnitOf, formatGrainLabel, formatObservedPercentCell, formatObservedValue,
  formatSourceValue, isDisplayableUnit, quantityUnitOf, resolvePercentColumn,
  resolvePercentScale, sourceUnitColumns, tableHeaderWithUnit,
} from "./format";
import type { ChartColumn } from "./types";

export interface TableColumnDisplay {
  header: string;
  measure: string;
  sourceUnit?: string;
  displayUnit?: string;
  quantityIdentity?: string;
  note?: "source" | "conversion";
  formatValue(value: unknown): string;
}

/** Resolve headers, values and disclosure from the same existing Table policy. */
export function tableColumnDisplays(
  columns: readonly ChartColumn[], rows: readonly Record<string, string | number | null>[], locale: string, exactNumbers = false,
): Map<string, TableColumnDisplay> {
  const preserved = sourceUnitColumns(columns, locale);
  return new Map<string, TableColumnDisplay>(columns.filter((column) => column.type === "numeric").map((column) => {
    const measure = formatGrainLabel(column.header, locale);
    const sourceUnit = axisUnitOf(column);
    const percent = resolvePercentColumn(column);
    const scale = percent ? resolvePercentScale(rows.map((row) => row[column.key]), column) : null;
    const quantity = quantityUnitOf(column);
    const preserve = exactNumbers || preserved.has(column.key);
    const base = column.currency_code && /^[¥￥$€£]$/.test(quantity.base ?? "") ? column.currency_code : quantity.base;
    const displayUnit = preserve ? sourceUnit : base;
    return [column.key, {
      measure, sourceUnit, displayUnit, quantityIdentity: quantity.identity,
      header: exactNumbers || percent ? measure : tableHeaderWithUnit(measure, sourceUnit, preserve, column.currency_code),
      note: !percent && isDisplayableUnit(sourceUnit) ? exactNumbers ? "source" : !preserve && displayUnit !== sourceUnit ? "conversion" : undefined : undefined,
      formatValue(value: unknown) {
        const numeric = typeof value === "number" ? value : null;
        const supplied = typeof value === "string" ? value : numeric;
        if (exactNumbers) return typeof value === "string" ? value : formatExactSourceNumber(numeric);
        if (scale) return formatObservedPercentCell(supplied, scale, column.display);
        return preserve
          ? formatSourceValue(supplied, sourceUnit, false, column.display)
          : formatObservedValue(supplied, sourceUnit, column.format, false, column.display, column.currency_code);
      },
    }];
  }));
}

/** Qualify only isolated, proven source-unit markers; preserve arbitrary prose. */
export function qualifyTableUnitText(text: string, columns: Iterable<TableColumnDisplay>, sourceLabel: (unit: string) => string): string {
  const displays = [...columns];
  const unit = displays[0]?.sourceUnit;
  if (!unit || !displays.length || displays.some((column) => column.sourceUnit !== unit || column.quantityIdentity !== displays[0].quantityIdentity) || !displays.some((column) => column.note === "conversion")) return text;
  const label = sourceLabel(unit);
  for (const [left, right] of [["（", "）"], ["(", ")"]]) {
    if (text.endsWith(`${left}${unit}${right}`)) return `${text.slice(0, -(unit.length + left.length + right.length))}${left}${label}${right}`;
  }
  return text.split(/([;；])/).map((segment) => {
    const trimmed = segment.trim();
    const match = /^(?:单位\s*[:：]|Units?\s*:|Einheit\s*:|Unité\s*:)\s*(.+)$/i.exec(trimmed);
    return match?.[1] === unit ? segment.replace(trimmed, label) : segment;
  }).join("");
}
