/**
 * Page-level loading skeleton (Page Layout Spec §4): a role=status skeleton, invisible for the first 300ms (is-pending), then visible.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, screen } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";

import { LoadingSkeleton, SKELETON_DELAY_MS } from "./loading-skeleton";

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("LoadingSkeleton", () => {
  it("becomes visible after 300ms and follows the row count", () => {
    renderIntl(<LoadingSkeleton rows={4} />);
    const status = screen.getByRole("status", { name: "加载中…" });
    expect(status.getAttribute("aria-busy")).toBe("true");
    expect(status.className).toContain("ui-skeleton--rows");
    expect(status.className).toContain("is-pending");
    expect(status.querySelectorAll(".ui-skeleton-row")).toHaveLength(4);
    act(() => {
      vi.advanceTimersByTime(SKELETON_DELAY_MS - 1);
    });
    expect(status.className).toContain("is-pending");
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(status.className).not.toContain("is-pending");
  });

  it("设置项页用 form 形态（左标签右控件的骨架行），名称可自定", () => {
    renderIntl(<LoadingSkeleton variant="form" label="正在加载外观设置" />);
    const status = screen.getByRole("status", { name: "正在加载外观设置" });
    expect(status.className).toContain("ui-skeleton--form");
    expect(status.querySelectorAll(".ui-skeleton-row .ui-skeleton-bar")).toHaveLength(6);
  });
});
