import { createHash } from "node:crypto";

import { describe, expect, it } from "vitest";

import grid from "../../../../config/chart_style/series_encoding_grid.json";
import { assignSeriesEncodings } from "./chart-style-presentation";
import { CHART_STYLE_PRESENTATION_POLICY } from "./chart-style-registry";
import { chartThemeBaseForMode, resolveChartThemeForStyle } from "../chart-theme";
import { parseHexColor } from "./color";
import {
  contrastRatio,
  fillPatternGeometry,
  fillPatternPaint,
  paintFillPatternTile,
  patternStrokeFor,
} from "./fill-pattern";
import { mutedOf } from "./presentation";

describe("fill pattern geometry", () => {
  it("draws nothing for solid and distinct marks for every texture", () => {
    expect(fillPatternGeometry("solid")).toEqual({ lines: [], dots: [] });
    expect(fillPatternGeometry("diagonal").lines).toHaveLength(3);
    expect(fillPatternGeometry("diagonal").dots).toHaveLength(0);
    expect(fillPatternGeometry("crosshatch").lines).toHaveLength(6);
    expect(fillPatternGeometry("dots")).toEqual({ lines: [], dots: [[1.5, 1.5, 1.05], [4.5, 4.5, 1.05]] });
  });

  it("tiles seamlessly: every diagonal line leaves the tile on the opposite edges", () => {
    for (const [x1, y1, x2, y2] of fillPatternGeometry("diagonal").lines) {
      expect(x2 - x1).toBe(y1 - y2);
    }
  });

  it("paints the fill first, then strokes the texture in the stroke color", () => {
    const calls: string[] = [];
    const context = {
      fillStyle: "",
      strokeStyle: "",
      lineWidth: 0,
      lineCap: "butt",
      scale: (x: number) => calls.push(`scale:${x}`),
      fillRect: () => calls.push(`fillRect:${context.fillStyle}`),
      beginPath: () => calls.push("begin"),
      moveTo: () => calls.push("move"),
      lineTo: () => calls.push("line"),
      stroke: () => calls.push(`stroke:${context.strokeStyle}`),
      arc: () => calls.push("arc"),
      fill: () => calls.push(`fill:${context.fillStyle}`),
    };
    paintFillPatternTile(context as unknown as CanvasRenderingContext2D, "dots", "#2563A7", "#FFFFFF", 2);
    expect(calls[0]).toBe("scale:2");
    expect(calls[1]).toBe("fillRect:#2563A7");
    expect(calls.filter((call) => call === "fill:#FFFFFF")).toHaveLength(2);
  });

  it("returns the plain color for solid and where no 2D canvas exists", () => {
    expect(fillPatternPaint("#2563A7", "solid", "#FFFFFF")).toBe("#2563A7");
    // jsdom has no canvas backend: color-only rendering instead of a crash.
    expect(fillPatternPaint("#2563A7", "diagonal", "#FFFFFF")).toBe("#2563A7");
  });
});

describe("series encoding assignment", () => {
  it("keeps color only up to six series (product cap) within the measured capacity", () => {
    expect(assignSeriesEncodings({ roles: Array(6).fill("bar"), paletteLength: 16, capacity: 11, colorSafe: false, stacked: true })).toBeNull();
    expect(assignSeriesEncodings({ roles: Array(7).fill("bar"), paletteLength: 16, capacity: 16, colorSafe: false, stacked: true })).not.toBeNull();
  });

  it("keeps every series' color and layers the next texture past the capacity", () => {
    const encodings = assignSeriesEncodings({ roles: Array(13).fill("bar"), paletteLength: 16, capacity: 11, colorSafe: false, stacked: true })!;
    expect(encodings.map((item) => item.paletteIndex)).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
    expect(encodings.map((item) => item.fillPattern)).toEqual([...Array(6).fill("solid"), ...Array(6).fill("diagonal"), "dots"]);
  });

  it("cycles textures per series under color-safe and keeps lines texture-free", () => {
    const encodings = assignSeriesEncodings({
      roles: ["bar", "bar", "bar", "bar", "bar", "line", "area"],
      paletteLength: 6,
      capacity: 6,
      colorSafe: true,
      stacked: false,
    })!;
    expect(encodings.map((item) => item.fillPattern)).toEqual(["solid", "diagonal", "dots", "crosshatch", "solid", "solid", "dots"]);
    // Grouped (not stacked) bars never touch, so no separator eats into them.
    expect(encodings.map((item) => item.boundaryWidthPx)).toEqual([0, 0, 0, 0, 0, 0, 0]);
  });

  it.each([1, 2])("never collapses colors for a low-capacity team palette (capacity %i)", (capacity) => {
    const encodings = assignSeriesEncodings({ roles: Array(5).fill("bar"), paletteLength: 6, capacity, colorSafe: false, stacked: false })!;
    expect(encodings.map((item) => item.paletteIndex)).toEqual([0, 1, 2, 3, 4]);
    expect(encodings[capacity]!.fillPattern).toBe("diagonal");
  });

  it.each([1, 2, 6, 11, 16])("keeps (color, texture) unique past capacity × patterns (capacity %i)", (capacity) => {
    for (const paletteLength of [6, 16]) {
      const all = paletteLength * 4;
      for (const count of [Math.min(Math.min(capacity, paletteLength) * 4 + 1, all), all]) {
        const encodings = assignSeriesEncodings({ roles: Array(count).fill("bar"), paletteLength, capacity, colorSafe: false, stacked: false });
        if (!encodings) continue;
        const pairs = encodings.map((item) => `${item.paletteIndex}:${item.fillPattern}`);
        expect(new Set(pairs).size).toBe(pairs.length);
      }
    }
  });

  it("matches the Python implementation over the shared case grid", () => {
    const lines: string[] = [];
    for (const roleSet of grid.roleSets) {
      for (let count = grid.seriesCounts.from; count <= grid.seriesCounts.to; count += 1) {
        for (const capacity of grid.capacities) {
          for (const paletteLength of grid.paletteLengths) {
            for (const colorSafe of grid.colorSafe) {
              for (const stacked of grid.stacked) {
                const roles = Array.from({ length: count }, (_, index) => roleSet[index % roleSet.length]!);
                const encodings = assignSeriesEncodings({ roles, paletteLength, capacity, colorSafe, stacked });
                // build-config paints palette[i % length]; the resolver must never pick another color.
                if (encodings) expect(encodings.map((item) => item.paletteIndex)).toEqual(roles.map((_, i) => i % paletteLength));
                const body = encodings === null
                  ? "none"
                  : encodings.map((item) =>
                    `${item.paletteIndex},${item.fillPattern},${item.lineDash.join("-")},${item.pointStyle},${item.boundaryWidthPx}`
                  ).join(";");
                lines.push(`${roleSet.join("+")}|${count}|${capacity}|${paletteLength}|${colorSafe ? 1 : 0}|${stacked ? 1 : 0}|${body}`);
              }
            }
          }
        }
      }
    }
    expect(lines).toHaveLength(grid.caseCount);
    expect(createHash("sha256").update(lines.join("\n"), "utf8").digest("hex")).toBe(grid.sha256);
  });
});

describe("pattern stroke contrast", () => {
  it.each(["light", "dark"] as const)("keeps ≥3:1 against muted and unmuted fills (%s)", (mode) => {
    for (const preset of ["bowhead-editorial", "clean-blue", "analytical-neutral"] as const) {
      const theme = resolveChartThemeForStyle(preset, { contrast: "standard", colorVision: "standard" }, chartThemeBaseForMode(mode), mode);
      for (const color of theme.palette) {
        for (const fill of [color, mutedOf(color, theme.surface)]) {
          const stroke = patternStrokeFor(fill, [theme.surface, theme.ink], theme.surface);
          expect(contrastRatio(parseHexColor(stroke)!, parseHexColor(fill)!)).toBeGreaterThanOrEqual(3);
        }
      }
    }
  });

  it("keeps surface strokes on full-strength palette colors and composites translucent fills", () => {
    expect(patternStrokeFor("#2563A7", ["#FFFFFF", "#161310"], "#FFFFFF")).toBe("#FFFFFF");
    // A 20% tint over white is nearly white: the surface stroke would vanish, ink does not.
    expect(patternStrokeFor("#2563A733", ["#FFFFFF", "#161310"], "#FFFFFF")).toBe("#161310");
  });
});

describe("product color-only cap", () => {
  it("ships texture from the 7th series; null falls back to the measured capacity", () => {
    const nonColor = CHART_STYLE_PRESENTATION_POLICY.nonColor as { colorOnlySeriesCap: number | null };
    const input = { roles: Array(7).fill("bar"), paletteLength: 16, capacity: 16, colorSafe: false, stacked: false };
    expect(nonColor.colorOnlySeriesCap).toBe(6);
    expect(assignSeriesEncodings(input)!.map((item) => item.fillPattern)).toEqual([...Array(6).fill("solid"), "diagonal"]);
    nonColor.colorOnlySeriesCap = null;
    try {
      expect(assignSeriesEncodings(input)).toBeNull();
    } finally {
      nonColor.colorOnlySeriesCap = 6;
    }
  });
});
