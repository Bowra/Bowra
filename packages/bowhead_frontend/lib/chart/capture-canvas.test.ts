import { describe, expect, test } from "vitest";

import { POINT_LABEL_KEEP_ALL_KEY } from "./build-config";
import { withAllPointLabels } from "./capture-canvas";

describe("withAllPointLabels", () => {
  test("the capture is drawn while the chart keeps every point label, then the chart redraws back", () => {
    const events: string[] = [];
    const chart: Record<string, unknown> & { update: (mode?: string) => void } = {
      update: (mode) => events.push(`update:${mode}:${String(chart[POINT_LABEL_KEEP_ALL_KEY] ?? "off")}`),
    };
    const canvas = {} as HTMLCanvasElement;
    const result = withAllPointLabels(
      canvas,
      () => {
        events.push(`draw:${String(chart[POINT_LABEL_KEEP_ALL_KEY])}`);
        return "pixels";
      },
      () => chart,
    );
    expect(result).toBe("pixels");
    expect(events).toEqual(["update:none:true", "draw:true", "update:none:off"]);
    expect(POINT_LABEL_KEEP_ALL_KEY in chart).toBe(false);
  });

  test("the flag is cleared even when drawing throws", () => {
    const chart: Record<string, unknown> & { update: () => void } = { update: () => {} };
    expect(() =>
      withAllPointLabels({} as HTMLCanvasElement, () => {
        throw new Error("tainted");
      }, () => chart),
    ).toThrow("tainted");
    expect(POINT_LABEL_KEEP_ALL_KEY in chart).toBe(false);
  });

  test("a canvas without a chart instance is drawn as is", () => {
    expect(withAllPointLabels({} as HTMLCanvasElement, () => 1, () => undefined)).toBe(1);
  });
});
