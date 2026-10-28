/**
 * Behavior batch B (docs/design/组件统一-技术设计.md “行为批 B：窄屏弹窗与撤销”): undo Toast appearance and
 * touch-swipe dismissal; keyboard and screen-reader users still use the close button.
 */
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { _clearAllToasts, toast } from "@/lib/use-toast";

import { isToastSwipeDismiss, ToastContainer } from "./toast-container";

beforeEach(() => {
  _clearAllToasts();
});

afterEach(() => {
  cleanup();
  _clearAllToasts();
});

function pushUndo(onDismiss = vi.fn(), onAction = vi.fn()) {
  act(() => {
    toast({ kind: "info", undo: true, text: "已删除“周报”", action: { label: "撤销", onAction }, onDismiss });
  });
  return { onDismiss, onAction };
}

function drag(target: Element, dx: number, dy: number, pointerType = "touch") {
  fireEvent.pointerDown(target, { pointerId: 7, pointerType, isPrimary: true, clientX: 100, clientY: 100 });
  fireEvent.pointerMove(target, { pointerId: 7, pointerType, clientX: 100 + dx / 2, clientY: 100 + dy / 2 });
  fireEvent.pointerMove(target, { pointerId: 7, pointerType, clientX: 100 + dx, clientY: 100 + dy });
  fireEvent.pointerUp(target, { pointerId: 7, pointerType, clientX: 100 + dx, clientY: 100 + dy });
}

describe("isToastSwipeDismiss", () => {
  it("向上够远或够快、左右够远或够快时关闭；向下不关", () => {
    expect(isToastSwipeDismiss(0, -40, 400)).toBe(true);
    expect(isToastSwipeDismiss(0, -20, 400)).toBe(false);
    expect(isToastSwipeDismiss(0, -20, 20)).toBe(true);
    expect(isToastSwipeDismiss(90, 5, 400)).toBe(true);
    expect(isToastSwipeDismiss(-90, 5, 400)).toBe(true);
    expect(isToastSwipeDismiss(40, 0, 400)).toBe(false);
    expect(isToastSwipeDismiss(0, 120, 50)).toBe(false);
  });
});

describe("撤销 Toast 样式", () => {
  it("an undo action Toast gets ui-toast--undo (no status marker)", () => {
    renderIntl(<ToastContainer />);
    pushUndo();
    expect(screen.getByTestId("toast-info").className).toContain("ui-toast--undo");
  });
});

describe("触屏轻扫关闭", () => {
  it("an upward swipe dismisses through onDismiss (a deferred commit lands then)", async () => {
    renderIntl(<ToastContainer />);
    const { onDismiss, onAction } = pushUndo();
    drag(screen.getByTestId("toast-info"), 0, -60);
    await waitFor(() => expect(screen.queryByTestId("toast-info")).toBeNull());
    expect(onDismiss).toHaveBeenCalledTimes(1);
    expect(onAction).not.toHaveBeenCalled();
  });

  it("a sideways swipe dismisses", async () => {
    renderIntl(<ToastContainer />);
    pushUndo();
    drag(screen.getByTestId("toast-info"), 120, 4);
    await waitFor(() => expect(screen.queryByTestId("toast-info")).toBeNull());
    act(() => {
      toast({ kind: "info", text: "已复制" });
    });
    drag(screen.getByTestId("toast-info"), -120, 0);
    await waitFor(() => expect(screen.queryByTestId("toast-info")).toBeNull());
  });

  it("拖一点松手弹回、不关；跟手时写 translate", () => {
    renderIntl(<ToastContainer />);
    const { onDismiss } = pushUndo();
    const node = screen.getByTestId("toast-info");
    fireEvent.pointerDown(node, { pointerId: 3, pointerType: "touch", isPrimary: true, clientX: 100, clientY: 100 });
    fireEvent.pointerMove(node, { pointerId: 3, pointerType: "touch", clientX: 100, clientY: 88 });
    expect(node.getAttribute("data-swiping")).toBe("true");
    expect(node.style.translate).toBe("0px -12px");
    fireEvent.pointerUp(node, { pointerId: 3, pointerType: "touch", clientX: 100, clientY: 88, timeStamp: 10_000 });
    // fireEvent cannot set timeStamp: 12px is below the speed criterion's minimum distance, so it is not treated as a fast swipe.
    expect(screen.getByTestId("toast-info")).toBe(node);
    expect(node.hasAttribute("data-swiping")).toBe(false);
    expect(onDismiss).not.toHaveBeenCalled();
  });

  it("a mouse drag does not dismiss", () => {
    renderIntl(<ToastContainer />);
    const first = pushUndo();
    drag(screen.getByTestId("toast-info"), 0, -80, "mouse");
    // Dismissal has an exit animation, leaving the node briefly present: check whether onDismiss was called.
    expect(first.onDismiss).not.toHaveBeenCalled();
    expect(screen.getByTestId("toast-info").hasAttribute("data-swiping")).toBe(false);
  });

  it("关闭按钮与撤销按钮照常可用（键盘与读屏的路径不变）", async () => {
    renderIntl(<ToastContainer />);
    const { onAction } = pushUndo();
    fireEvent.click(screen.getByRole("button", { name: "撤销" }));
    await act(async () => {});
    expect(onAction).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByTestId("toast-info")).toBeNull());
    const second = pushUndo();
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    expect(second.onDismiss).toHaveBeenCalledTimes(1);
  });
});
