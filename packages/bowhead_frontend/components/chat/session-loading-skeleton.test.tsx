/**
 * chat-ux-2026-08 batch E — session hydration skeleton.
 *
 * Measured on 116: DOM ready at 1.27s, but the first chat-content frame took 7.1s for small sessions and ≈19s for large ones.
 * During those intervening seconds, the old UI showed a 740px single-column gray bar, unlike the real left/right split;
 * content arrival caused a layout jump, and the skeleton had no semantics at all.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, act, cleanup } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import zh from "@/messages/zh-CN.json";
import { SessionLoadingSkeleton } from "./session-loading-skeleton";

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

function mount() {
  return render(
    <NextIntlClientProvider locale="zh-CN" messages={zh}>
      <SessionLoadingSkeleton />
    </NextIntlClientProvider>,
  );
}

describe("SessionLoadingSkeleton", () => {
  it("保留 session-loading testid 与 aria-busy（e2e 与无障碍都依赖）", () => {
    mount();
    const root = screen.getByTestId("session-loading");
    expect(root.getAttribute("aria-busy")).toBe("true");
    const label = screen.getByTestId("session-loading-label");
    expect(label.getAttribute("role")).toBe("status");
    expect(label.getAttribute("aria-live")).toBe("polite");
  });

  it("骨架是**分栏**形状——两个 pane，消除内容到达时的重排跳变", () => {
    const { container } = mount();
    expect(container.querySelector(".v2-session-skel-chat")).not.toBeNull();
    expect(container.querySelector(".v2-session-skel-canvas")).not.toBeNull();
  });

  it("有语义文案，不是一堆无意义灰条", () => {
    mount();
    const status = screen.getByRole("status");
    expect(status.getAttribute("aria-live")).toBe("polite");
    expect(status.textContent)
      .toContain("正在载入这个会话");
  });

  it("超过 8 秒换第二句——让「还在转」和「卡住了」可区分", () => {
    mount();
    expect(screen.getByTestId("session-loading-label").textContent)
      .toContain("正在载入这个会话");

    act(() => void vi.advanceTimersByTime(8000));

    expect(screen.getByTestId("session-loading-label").textContent)
      .toContain("内容较多");
  });

  it("8 秒内不提前换（正常路径不该看到第二句）", () => {
    mount();
    act(() => void vi.advanceTimersByTime(7500));
    expect(screen.getByTestId("session-loading-label").textContent)
      .not.toContain("内容较多");
  });
});
