import type { ChartColumn, ChartSpec } from "./types";
import { parseHexColor } from "./color";

/** Only direct measure identities, never inferred category/group positions. */
export function editableColorColumns(spec: ChartSpec): ChartColumn[] {
  if (!["line", "bar", "hbar", "stacked_bar", "area_stacked", "combo", "dual_axis"].includes(spec.kind)
    || spec.views || spec.presentation_plan || spec._export_analytical) return [];
  let columns = spec.columns;
  if (spec.explicit_presentation) {
    if (spec.explicit_presentation.mode !== "plot" || spec.explicit_presentation.group_by?.length) return [];
    const keys = spec.explicit_presentation.axes?.y ?? [];
    columns = columns.filter((column) => keys.includes(column.key));
  } else if (columns.filter((column) => ["date", "categorical", "geo"].includes(column.type)).length > 1) return [];
  return columns.filter((column) => column.type === "numeric");
}

export function colorIndex(value: unknown): number | undefined {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 && value < 16 ? value : undefined;
}

/** Canonical opaque HEX; do not discard an alpha channel or accept arbitrary CSS. */
export function colorHex(value: unknown): string | undefined {
  if (typeof value !== "string" || ![4, 7].includes(value.length) || value.trim() !== value) return undefined;
  const rgb = parseHexColor(value);
  return rgb ? `#${rgb.map((channel) => channel.toString(16).padStart(2, "0")).join("").toUpperCase()}` : undefined;
}
