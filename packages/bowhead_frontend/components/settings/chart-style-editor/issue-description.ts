/**
 * Translate a release-check issue into location + measurement.
 *
 * Server always supplied indices/measurements in `details` (`chart_style_materializer.py:360-440`),
 * but UI rendered only code/path, producing 5 identical
 * “相邻颜色在灰度下不易区分 · colors.dark.categorical” on production, leaving admins without actionable guidance.
 *
 * Reuse this surface's approved design language (Claude Design project `Bowhead` →
 * `team-branding-v2-r2/scripts/validation.js` + `QA-R2.md`):
 *   - Actual X + Required {op} Y, **correct operator per issue**, never a blanket ≥ for every threshold;
 *   - Non-contrast measurements have their own labels (color difference / grayscale difference / color vision), distinct from ratios.
 */

import type { ChartStyleIssue } from "@/lib/team-chart-styles-api";

type Translate = (key: string, values?: Record<string, string | number>) => string;

export interface IssueDescription {
  /** Location such as Light · Color 2; empty string if unlocatable. */
  location: string;
  /** Contrast Actual 2.24:1 · Required ≥ 3:1; empty string without a describable measurement. */
  metric: string;
  /** Parsed colors for swatches; omit if unparseable (no swatch is preferable to the wrong color). */
  swatches: string[];
}

function ratio(value: unknown): string | null {
  return typeof value === "number" ? `${value.toFixed(2)}:1` : null;
}

function delta(value: unknown): string | null {
  return typeof value === "number" ? value.toFixed(3) : null;
}

function indexOf(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

/** `colors.light.categorical.3` → `light`；`colors` → null。 */
export function modeOfIssuePath(path: string): "light" | "dark" | null {
  const segment = path.split(".")[1];
  return segment === "light" || segment === "dark" ? segment : null;
}

/** `colors.light.categorical.3` → 3; null without a trailing index. */
export function trailingIndexOfIssuePath(path: string): number | null {
  const last = path.split(".").pop();
  if (last === undefined || !/^\d+$/.test(last)) return null;
  return Number(last);
}

export function describeIssue(
  issue: ChartStyleIssue,
  t: Translate,
  palettes: { light?: string[] | null; dark?: string[] | null } = {},
): IssueDescription {
  const details = issue.details ?? {};
  const mode = modeOfIssuePath(issue.path);
  const palette = mode ? palettes[mode] ?? null : null;
  const modeLabel = mode ? t(mode === "light" ? "lightMode" : "darkMode") : "";

  const colorAt = (index: number | null): string | null => {
    if (index === null || !palette) return null;
    return palette[index] ?? null;
  };
  const position = (index: number) => t("validation.colorPosition", { index: index + 1 });
  const join = (...parts: Array<string | null>) => parts.filter(Boolean).join(" · ");

  const first = indexOf(details.firstIndex);
  const second = indexOf(details.secondIndex);
  const trailing = trailingIndexOfIssuePath(issue.path);

  switch (issue.code) {
    case "palette.surface_contrast": {
      const color = colorAt(trailing);
      return {
        location: join(modeLabel, trailing === null ? null : position(trailing)),
        metric: t("validation.metricContrast", {
          actual: ratio(details.actual) ?? "—",
          need: ratio(details.minimum) ?? "—",
        }),
        swatches: color ? [color] : [],
      };
    }
    case "palette.pairwise_delta":
    case "palette.adjacent_delta":
    case "palette.adjacent_grayscale": {
      const pair = first === null || second === null
        ? null
        : t("validation.colorPair", { first: first + 1, second: second + 1 });
      const colors = [colorAt(first), colorAt(second)].filter((value): value is string => Boolean(value));
      if (issue.code === "palette.adjacent_grayscale") {
        return {
          location: join(modeLabel, pair),
          metric: t("validation.metricGrayscale", {
            actual: delta(details.actual) ?? "—",
            need: delta(0.08) ?? "—",
          }),
          swatches: colors.length === 2 ? colors : [],
        };
      }
      // Server adjacent_delta warns over the **range** 0.02 <= delta <= 0.06,
      // not a >= 0.06 lower bound—therefore require > 0.06 here, never casually substitute ≥.
      const isRangeWarning = issue.code === "palette.adjacent_delta";
      return {
        location: join(modeLabel, pair),
        metric: t(isRangeWarning ? "validation.metricDeltaAbove" : "validation.metricDeltaAtLeast", {
          actual: delta(details.actual) ?? "—",
          need: delta(isRangeWarning ? 0.06 : details.minimum) ?? "—",
        }),
        swatches: colors.length === 2 ? colors : [],
      };
    }
    case "palette.distinctness": {
      // CIEDE2000 scale (≈10 = clearly different), unlike the 0–1 OKLab deltas above: one decimal.
      const pair = first === null || second === null
        ? null
        : t("validation.colorPair", { first: first + 1, second: second + 1 });
      const colors = [colorAt(first), colorAt(second)].filter((value): value is string => Boolean(value));
      const oneDecimal = (value: unknown) => typeof value === "number" ? value.toFixed(1) : "—";
      return {
        location: join(modeLabel, pair),
        metric: t("validation.metricDistinct", {
          normal: oneDecimal(details.normal),
          cvd: oneDecimal(details.cvd),
          normalNeed: oneDecimal(details.normalMinimum),
          cvdNeed: oneDecimal(details.cvdMinimum),
        }),
        swatches: colors.length === 2 ? colors : [],
      };
    }
    case "palette.cvd_separation": {
      const deficiency = typeof details.deficiency === "string" ? details.deficiency : "";
      const known = ["protan", "deutan", "tritan"].includes(deficiency);
      return {
        location: join(modeLabel, t("validation.wholePalette")),
        metric: t("validation.metricCvd", {
          deficiency: known ? t(`validation.cvd${deficiency.charAt(0).toUpperCase()}${deficiency.slice(1)}`) : deficiency,
          actual: delta(details.actual) ?? "—",
          need: delta(0.045) ?? "—",
        }),
        swatches: [],
      };
    }
    case "palette.count":
      return {
        location: join(modeLabel, t("validation.wholePalette")),
        metric: t("validation.metricCount", {
          actual: typeof details.actual === "number" ? details.actual : "—",
          minimum: typeof details.minimum === "number" ? details.minimum : "—",
          maximum: typeof details.maximum === "number" ? details.maximum : "—",
        }),
        swatches: [],
      };
    case "palette.mode_length_mismatch":
      return {
        location: "",
        metric: t("validation.metricModeLength", {
          light: typeof details.lightCount === "number" ? details.lightCount : "—",
          dark: typeof details.darkCount === "number" ? details.darkCount : "—",
        }),
        swatches: [],
      };
    case "palette.duplicate_color":
      return { location: join(modeLabel, t("validation.wholePalette")), metric: "", swatches: [] };
    default:
      return { location: modeLabel, metric: "", swatches: [] };
  }
}
