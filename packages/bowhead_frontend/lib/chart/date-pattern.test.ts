import { describe, expect, it } from "vitest";
import { formatDateLabel, formatDatePattern } from "./format";

describe("bounded date display patterns", () => {
  it.each([
    ["2026-10-28", "yyyyMMdd", "20261028"],
    ["2026-10-28", "yyyymmdd", "20261028"],
    ["20261028", "yyyy-MM-dd", "2026-10-28"],
    ["2026-10-28", "yyyy-mm-dd", "2026-10-28"],
    ["2026-10-28", "yyyy/MM/dd", "2026/10/28"],
    ["2026-10", "yyyyMM", "202610"],
    ["2026", "yy", "26"],
    ["2026-10-28T16:05:09+0800", "yyyyMMdd HH:mm:ss XXX", "20261028 16:05:09 +08:00"],
  ])("formats supplied calendar components without changing their meaning", (raw, pattern, expected) => {
    expect(formatDatePattern(raw, pattern)).toBe(expected);
  });

  it("keeps the source text when its precision cannot satisfy the pattern", () => {
    expect(formatDateLabel("2026-10", "en-US", "yyyyMMdd")).toBe("2026-10");
  });

  it.each([
    ["2026-10", "yyyyMMdd"],
    ["2026", "yyyyMM"],
    ["2026-10-28", "HH:mm"],
    ["2026-10-28T16:05", "ss"],
    ["2026-10-28", "XXX"],
    ["2026-02-30", "yyyyMMdd"],
    ["20260230", "yyyyMMdd"],
    ["2026102801", "yyyyMMdd"],
    ["2026-10-28", "YYYYMMDD"],
    ["2026-10-28", "yyyyMMdd<script>"],
    ["2026-10-28T16:05", "yyyy-mm-dd HH:mm"],
    ["2026-10-28T16:05+9960", "yyyyMMdd"],
    ["2026-10-28", "--"],
  ])("rejects invalid dates, identifiers, missing precision and unsupported tokens", (raw, pattern) => {
    expect(formatDatePattern(raw, pattern)).toBeNull();
  });
});
