import { describe, expect, it } from "vitest";

import { spreadBackgrounds, type Rgb } from "./in-place";

const yellow: Rgb = [255, 255, 0];

describe("spreadBackgrounds", () => {
  it("gives a dense character its neighbours' highlight when the colour shows between its strokes", () => {
    // 同 比 增 长: 增 has so many strokes that yellow is not its column's main colour.
    const spread = spreadBackgrounds([yellow, yellow, null, yellow], (index) => index === 2);
    expect(spread).toEqual([yellow, yellow, yellow, yellow]);
  });

  it("leaves a character next to a highlight plain when the colour does not show behind it", () => {
    const spread = spreadBackgrounds([null, yellow, yellow, null], () => false);
    expect(spread).toEqual([null, yellow, yellow, null]);
  });

  it("reaches a run of dense characters one by one from the highlight beside it", () => {
    const spread = spreadBackgrounds([yellow, null, null, null], (index) => index < 3);
    expect(spread).toEqual([yellow, yellow, yellow, null]);
  });
});
