/**
 * Sidebar session dates follow the shared list-date rule (116 dogfood: the sidebar said "1d ago" where other lists
 * said "Yesterday"). The Gateway now sends `updated_at`; calendar days are the viewer's, not UTC.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import enMessages from "@/messages/en-US.json";
import zhMessages from "@/messages/zh-CN.json";

import { SessionRowTime } from "./work-recovery";

// 2026-10-08 03:00 UTC = Oct 7 20:00 in Los Angeles, Oct 8 11:00 in Shanghai.
const NOW = new Date("2026-10-08T03:00:00Z");

function renderTime(
  item: { time: string; updatedAt?: string | null },
  { locale = "en-US", timeZone = "UTC" }: { locale?: "en-US" | "zh-CN"; timeZone?: string } = {},
) {
  return render(
    <NextIntlClientProvider locale={locale} messages={locale === "en-US" ? enMessages : zhMessages} timeZone={timeZone}>
      <SessionRowTime item={item} />
    </NextIntlClientProvider>,
  );
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("SessionRowTime", () => {
  it("labels the previous calendar day Yesterday instead of the legacy 1d ago", () => {
    renderTime({ time: "1d ago", updatedAt: "2026-10-07T01:00:00+00:00" });
    const time = screen.getByText("Yesterday");
    expect(time.tagName).toBe("TIME");
    expect(time.getAttribute("dateTime")).toBe("2026-10-07T01:00:00.000Z");
    expect(screen.queryByText("1d ago")).toBeNull();
  });

  it("uses the viewer's calendar day, not UTC", () => {
    // Oct 7 21:00 UTC: yesterday in UTC, but earlier today in Los Angeles (Oct 7 14:00 vs now Oct 7 20:00).
    const item = { time: "6h ago", updatedAt: "2026-10-07T21:00:00+00:00" };
    renderTime(item, { timeZone: "UTC" });
    expect(screen.getByText("Yesterday")).toBeTruthy();
    cleanup();
    renderTime(item, { timeZone: "America/Los_Angeles" });
    expect(screen.getByText("6h ago")).toBeTruthy();
  });

  it("uses the same copy in Chinese", () => {
    renderTime({ time: "1d ago", updatedAt: "2026-10-07T01:00:00+00:00" }, { locale: "zh-CN" });
    expect(screen.getByText(zhMessages.session.rail_group_yest)).toBeTruthy();
  });

  it("falls back to the legacy time text when an older Gateway sends no timestamp", () => {
    renderTime({ time: "3d ago", updatedAt: null });
    expect(screen.getByText("3d ago")).toBeTruthy();
  });
});
