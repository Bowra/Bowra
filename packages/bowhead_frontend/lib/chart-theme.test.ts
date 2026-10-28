import { describe, expect, it } from "vitest";

import { effectiveChartStyleV2Fixture } from "@/__tests__/chart-style-export-fixture";

import {
  chartThemeBaseForMode,
  resolveChartThemeForEffectiveV2,
  resolveChartThemeForStyle,
} from "./chart-theme";
import { CHART_STYLE_PRESETS } from "./chart/chart-style-registry";

describe("chart style registry", () => {
  it("ships three active immutable preset identities", () => {
    expect(Object.keys(CHART_STYLE_PRESETS)).toEqual([
      "bowhead-editorial",
      "clean-blue",
      "analytical-neutral",
    ]);
    expect(CHART_STYLE_PRESETS["clean-blue"].presetRevision).toBe("2026.10.1");
  });

  it("projects aesthetic geometry without changing chart data", () => {
    const theme = resolveChartThemeForStyle("analytical-neutral");

    expect(theme.palette[0]).toBe("#3F5362");
    expect(theme.lineWidth).toBe(1.5);
    expect(theme.barRadius).toBe(1);
  });

  it("applies accessibility modifiers after the aesthetic preset", () => {
    const theme = resolveChartThemeForStyle("clean-blue", {
      contrast: "high",
      colorVision: "color-safe",
    });

    expect(theme.palette).toEqual([
      "#4477AA",
      "#EE6677",
      "#228833",
      "#CCBB44",
      "#66CCEE",
      "#AA3377",
    ]);
    expect(theme.ink).toBe("#000000");
    expect(theme.gridDensity).toBe("strong");
  });

  it("uses the server-resolved V2 palette and presentation tokens", () => {
    const effective = effectiveChartStyleV2Fixture();
    effective.modeColors.light.categoricalSeriesPalette = [
      "#305F72", "#875A7B", "#4E7651", "#A84B32", "#8B6A37", "#4D5B5C",
    ];
    effective.style.resolvedPresetSnapshot.kindTokens.line = {
      tooltip: "disabled",
      grid: "hidden",
      line: {
        curve: "straight",
        widthPx: 3,
        points: "show",
        pointRadiusPx: 4,
      },
    };

    const theme = resolveChartThemeForEffectiveV2(
      effective,
      chartThemeBaseForMode("light"),
      "light",
    );

    expect(theme.palette[0]).toBe("#305F72");
    expect(theme.lineWidth).toBe(3);
    expect(theme.pointRadius).toBe(4);
    expect(theme.gridDensity).toBe("quiet");
    expect(theme.chartStyleV2?.kindTokens.line).toMatchObject({
      tooltip: "disabled",
      grid: "hidden",
      line: { curve: "straight", points: "show" },
    });
  });
});
