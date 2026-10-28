import { describe, expect, it } from "vitest";

import zh from "@/messages/zh-CN.json";
import type { ChartStyleIssue } from "@/lib/team-chart-styles-api";

import { describeIssue, modeOfIssuePath, trailingIndexOfIssuePath } from "./issue-description";

const NS = zh.settings.appearance.chartStyleV2 as Record<string, unknown>;

/** A next-intl-shaped `t`: walks the dotted path and interpolates {placeholders}. */
function t(key: string, values: Record<string, string | number> = {}): string {
  const raw = key.split(".").reduce<unknown>(
    (node, part) => (node && typeof node === "object" ? (node as Record<string, unknown>)[part] : undefined),
    NS,
  );
  if (typeof raw !== "string") throw new Error(`missing message: ${key}`);
  return raw.replace(/\{(\w+)\}/g, (_, name: string) => String(values[name] ?? `{${name}}`));
}

function issue(code: string, path: string, details: Record<string, unknown> = {}): ChartStyleIssue {
  return { code, path, severity: "warning", details };
}

const PALETTES = {
  light: ["#503291", "#00A3B2", "#E61E50", "#0F69AF", "#149B5F", "#EB3C96"],
  dark: ["#C5B7FF", "#2DBECD", "#E61E50", "#0F69AF", "#149B5F", "#EB3C96"],
};

describe("issue path helpers", () => {
  it("reads the mode and the trailing colour index", () => {
    expect(modeOfIssuePath("colors.light.categorical.3")).toBe("light");
    expect(modeOfIssuePath("colors.dark.categorical")).toBe("dark");
    expect(modeOfIssuePath("colors")).toBeNull();
    expect(trailingIndexOfIssuePath("colors.light.categorical.3")).toBe(3);
    expect(trailingIndexOfIssuePath("colors.light.categorical")).toBeNull();
  });
});

describe("describeIssue", () => {
  // The production complaint: five rows reading exactly the same. The server had
  // the indices all along — see spike.md §5.
  it("两条只差下标的同码告警必须渲染出不同的文本", () => {
    const first = describeIssue(
      issue("palette.adjacent_grayscale", "colors.dark.categorical", { firstIndex: 1, secondIndex: 2, actual: 0.0067 }),
      t,
      PALETTES,
    );
    const second = describeIssue(
      issue("palette.adjacent_grayscale", "colors.dark.categorical", { firstIndex: 4, secondIndex: 5, actual: 0.0132 }),
      t,
      PALETTES,
    );

    expect(first.location).not.toBe(second.location);
    expect(first.metric).not.toBe(second.metric);
    expect(first.location).toContain("第 2 与第 3 个颜色");
    expect(second.location).toContain("第 5 与第 6 个颜色");
  });

  it("对比度用比值，并带上模式与序号", () => {
    const described = describeIssue(
      issue("palette.surface_contrast", "colors.light.categorical.1", { minimum: 3, actual: 2.243946 }),
      t,
      PALETTES,
    );

    expect(described.location).toBe("浅色模式 · 第 2 个颜色");
    expect(described.metric).toBe("对比度 实际 2.24:1 · 需要 ≥ 3.00:1");
    expect(described.swatches).toEqual(["#00A3B2"]);
  });

  // QA-R2 §E: operators are per-issue. adjacent_delta is a *range* warning
  // (0.02 <= d <= 0.06), so writing ≥ here would state the opposite of the rule.
  it("adjacent_delta 是区间告警，写「>」而不是「≥」", () => {
    const described = describeIssue(
      issue("palette.adjacent_delta", "colors.light.categorical", { firstIndex: 0, secondIndex: 1, actual: 0.045 }),
      t,
      PALETTES,
    );

    expect(described.metric).toContain("需要 > 0.060");
    expect(described.metric).not.toContain("≥");
  });

  it("pairwise_delta 是下限，写「≥」", () => {
    const described = describeIssue(
      issue("palette.pairwise_delta", "colors.light.categorical", {
        firstIndex: 0,
        secondIndex: 2,
        minimum: 0.02,
        actual: 0.018,
      }),
      t,
      PALETTES,
    );

    expect(described.metric).toBe("色差 实际 0.018 · 需要 ≥ 0.020");
  });

  it("每种度量各带自己的标签，不与对比度混淆", () => {
    const grayscale = describeIssue(
      issue("palette.adjacent_grayscale", "colors.dark.categorical", { firstIndex: 0, secondIndex: 1, actual: 0.0067 }),
      t,
      PALETTES,
    ).metric;
    const cvd = describeIssue(
      issue("palette.cvd_separation", "colors.light.categorical", { deficiency: "deutan", actual: 0.0343 }),
      t,
      PALETTES,
    ).metric;

    expect(grayscale.startsWith("灰度差")).toBe(true);
    expect(cvd.startsWith("绿色觉")).toBe(true);
    expect(grayscale).not.toContain("对比度");
    expect(cvd).not.toContain("对比度");
  });

  it("色卡数量是区间，不套单一 operator", () => {
    const described = describeIssue(
      issue("palette.count", "colors.light.categorical", { minimum: 6, maximum: 8, actual: 1 }),
      t,
      PALETTES,
    );

    expect(described.metric).toBe("实际 1 个 · 需要 6–8 个");
    expect(described.metric).not.toContain("≥");
  });

  it("模式数量不一致时两个模式都点名", () => {
    const described = describeIssue(
      issue("palette.mode_length_mismatch", "colors", { lightCount: 6, darkCount: 8 }),
      t,
      PALETTES,
    );

    expect(described.metric).toBe("浅色 6 个 · 深色 8 个");
  });

  it("拿不到色值时只给序号，绝不画一个可能是错的色块", () => {
    const described = describeIssue(
      issue("palette.surface_contrast", "colors.dark.categorical.1", { minimum: 3, actual: 1.8 }),
      t,
      { dark: null },
    );

    expect(described.location).toBe("深色模式 · 第 2 个颜色");
    expect(described.swatches).toEqual([]);
  });

  it("未知 code 不抛错，退化成只给模式", () => {
    const described = describeIssue(issue("palette.something_new", "colors.light.categorical"), t, PALETTES);

    expect(described.location).toBe("浅色模式");
    expect(described.metric).toBe("");
  });
});

describe("distinctness issues", () => {
  it("locates the pair and reports both vision measurements on the CIEDE2000 scale", () => {
    const described = describeIssue(
      issue("palette.distinctness", "colors.dark.categorical", {
        firstIndex: 0,
        secondIndex: 1,
        normal: 4.21,
        cvd: 2.3,
        normalMinimum: 10,
        cvdMinimum: 7,
      }),
      t,
      PALETTES,
    );

    expect(described.location).toBe("深色模式 · 第 1 与第 2 个颜色");
    expect(described.metric).toBe("色差 实际 4.2（色觉障碍模拟 2.3）· 需要 ≥ 10.0（≥ 7.0）");
    expect(described.swatches).toEqual(["#C5B7FF", "#2DBECD"]);
  });
});
