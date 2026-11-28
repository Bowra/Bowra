// @vitest-environment node
import { describe, expect, it } from "vitest";

import { parseServerTime } from "./server-time";

describe("parseServerTime", () => {
  it("reads a step time with the server's uniqueness suffix (116, 1.7.77: parsed as Invalid Date before)", () => {
    expect(parseServerTime("2026-11-26T02:05:13.718756+00:00#1382")).toBe(Date.UTC(2026, 10, 26, 2, 5, 13, 718));
  });

  it("reads a plain ISO time and rejects text that is not a time", () => {
    expect(parseServerTime("2026-11-26T02:05:04.812323+00:00")).toBe(Date.UTC(2026, 10, 26, 2, 5, 4, 812));
    expect(parseServerTime("soon")).toBeNull();
  });
});
