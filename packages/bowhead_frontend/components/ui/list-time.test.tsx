/**
 * Shared list date: one rule for reports, files, notifications and agent management (Polish Spec §3a, D8).
 */
import { screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

import { ListTime } from "./list-time";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-09T12:00:00Z"));
});

afterEach(() => {
  vi.useRealTimers();
});

describe("ListTime", () => {
  it("shows relative time within a week with the full time on hover", () => {
    renderIntl(<ListTime value="2026-10-04T12:00:00Z" />, { locale: "en-US" });
    const time = screen.getByText("5d ago");
    expect(time.tagName).toBe("TIME");
    expect(time.getAttribute("dateTime")).toBe("2026-10-04T12:00:00.000Z");
    expect(time.getAttribute("title")).toMatch(/Oct 4, 2026/);
  });

  it("switches to month and day after seven days, without a numeric date", () => {
    renderIntl(<ListTime value="2026-09-25T12:00:00Z" />, { locale: "en-US" });
    expect(screen.getByText("Sep 25")).toBeTruthy();
    expect(screen.queryByText("9/25/2026")).toBeNull();
  });

  it("uses the interface language", () => {
    renderIntl(<ListTime value="2026-09-25T12:00:00Z" />, { locale: "zh-CN" });
    expect(screen.getByText("9月25日")).toBeTruthy();
  });

  it("shows the empty placeholder for a missing value", () => {
    renderIntl(<ListTime value={null} />, { locale: "en-US" });
    expect(screen.getByText("—")).toBeTruthy();
  });
});
