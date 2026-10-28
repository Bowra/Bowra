/**
 * The sidebar's older sessions arrive as a server calendar day ("2026-10-01"); the client shows the shared
 * list-date absolute form (Agents & Reports Polish Spec §3a): month and day, the year only for other years.
 */
import { describe, expect, it } from "vitest";

import { formatCalendarDay } from "./locale";

const NOW = new Date("2026-10-09T08:00:00Z");

describe("formatCalendarDay", () => {
  it("formats a same-year day as month and day in the interface language", () => {
    expect(formatCalendarDay("2026-10-01", "en-US", NOW, "UTC")).toBe("Oct 1");
    expect(formatCalendarDay("2026-10-01", "zh-CN", NOW, "UTC")).toBe("10月1日");
  });

  it("adds the year only for another year", () => {
    expect(formatCalendarDay("2025-12-30", "en-US", NOW, "UTC")).toBe("Dec 30, 2025");
    expect(formatCalendarDay("2025-12-30", "zh-CN", NOW, "UTC")).toBe("2025年12月30日");
  });

  it("keeps the server's calendar day regardless of the viewer's zone", () => {
    expect(formatCalendarDay("2026-10-01", "en-US", NOW, "Pacific/Honolulu")).toBe("Oct 1");
    expect(formatCalendarDay("2026-10-01", "en-US", NOW, "Asia/Tokyo")).toBe("Oct 1");
  });

  it("ignores other shapes and impossible days", () => {
    expect(formatCalendarDay("6d ago", "en-US", NOW)).toBeNull();
    expect(formatCalendarDay("2026-02-30", "en-US", NOW)).toBeNull();
  });
});
