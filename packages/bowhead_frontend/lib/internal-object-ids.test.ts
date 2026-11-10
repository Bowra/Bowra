import { describe, expect, it } from "vitest";

import { stripInternalObjectIds } from "./internal-object-ids";
import { prettifyStepLabel } from "./prettify-step-label";

describe("stripInternalObjectIds", () => {
  it.each([
    {
      text: "把已有柱状图「1-2000 各区间质数个数」（section id: sec_4a5c8d7d2b6e）的柱子颜色改成橙色",
      expected: "把已有柱状图「1-2000 各区间质数个数」的柱子颜色改成橙色",
    },
    { text: "Recolor the chart sec_4a5c8d7d2b6e orange", expected: "Recolor the chart orange" },
    { text: "修改图表节 sec_4a5c8d7d2b6e 的颜色", expected: "修改图表节的颜色" },
    { text: "sec_4a5c8d7d2b6e", expected: "" },
    { text: "Use artifact art_3d97f7aeb38a1234 (canvas cv_7e7017bdc1d0)", expected: "Use" },
    // An earlier dispatch label cut at the ID label's colon.
    { text: "把已有柱状图「1-2000 各区间质数个数」（section id", expected: "把已有柱状图「1-2000 各区间质数个数」" },
  ])("removes IDs from $text", ({ text, expected }) => {
    expect(stripInternalObjectIds(text)).toBe(expected);
  });

  it.each([
    "[figure:sec_4a5c8d7d2b6e]",
    "https://example.com/sec_4a5c8d7d2b6e",
    "sec_short and section titles",
    "Revenue by section",
  ])("keeps %s", (text) => {
    expect(stripInternalObjectIds(text)).toBe(text);
  });
});

describe("prettifyStepLabel drops internal IDs from recorded labels", () => {
  it.each([
    { label: "sec_4a5c8d7d2b6e", expected: "" },
    { label: "把已有柱状图「1-2000 各区间质数个数」（section id", expected: "把已有柱状图「1-2000 各区间质数个数」" },
  ])("$label", ({ label, expected }) => {
    expect(prettifyStepLabel(label, "read_chart")).toBe(expected);
  });
});
