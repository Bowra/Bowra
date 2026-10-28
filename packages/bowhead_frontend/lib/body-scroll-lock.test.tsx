/**
 * Counted page-scroll lock: drawers, dialogs and menu bottom sheets share one lock; unlocking one never removes another's lock.
 * Previously dialogs used FloatingOverlay lockScroll, which unconditionally cleared body overflow on unlock; opening and closing a dialog inside a mobile drawer
 * therefore removed the drawer's lock.
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { DialogShell } from "@/components/ui/dialog";

import { lockBodyScroll } from "./body-scroll-lock";

const overflow = () => document.body.style.getPropertyValue("overflow");
const priority = () => document.body.style.getPropertyPriority("overflow");

afterEach(() => {
  cleanup();
  document.body.removeAttribute("style");
});

describe("lockBodyScroll", () => {
  it("加锁 hidden；最后一个持有者释放时才恢复加锁前的值", () => {
    document.body.style.setProperty("overflow", "scroll");
    const a = lockBodyScroll();
    const b = lockBodyScroll({ compensate: true });
    expect(overflow()).toBe("hidden");
    a();
    expect(overflow()).toBe("hidden");
    a();
    expect(overflow()).toBe("hidden");
    b();
    expect(overflow()).toBe("scroll");
  });

  it("补偿（滚动条宽度变量）单独计数，随最后一个要补偿的持有者撤掉", () => {
    const drawer = lockBodyScroll();
    const dialog = lockBodyScroll({ compensate: true });
    expect(document.body.style.getPropertyValue("--floating-ui-scrollbar-width")).not.toBe("");
    dialog();
    expect(document.body.style.getPropertyValue("--floating-ui-scrollbar-width")).toBe("");
    expect(overflow()).toBe("hidden");
    drawer();
    expect(overflow()).toBe("");
  });

  it("抽屉开着时开关一个带遮罩的弹窗（DialogShell），抽屉的锁仍在", () => {
    const drawer = lockBodyScroll();
    expect(document.body.style.getPropertyPriority("overflow")).toBe("important");
    const view = render(
      <DialogShell onClose={() => {}} label="确认">
        <button type="button">确定</button>
      </DialogShell>,
    );
    expect(overflow()).toBe("hidden");
    view.unmount();
    expect(overflow()).toBe("hidden");
    drawer();
    expect(overflow()).toBe("");
  });

  it("单独打开的弹窗也写 important（窄屏 globals.css 以 !important 放开 body 滚动，不带 important 压不住），关掉后恢复", () => {
    const view = render(
      <DialogShell onClose={() => {}} label="确认">
        <button type="button">确定</button>
      </DialogShell>,
    );
    expect(overflow()).toBe("hidden");
    expect(priority()).toBe("important");
    view.unmount();
    expect(overflow()).toBe("");
    expect(priority()).toBe("");
  });

  it("弹窗类持有者的写法：hidden !important，并保留滚动条补偿", () => {
    const release = lockBodyScroll({ compensate: true });
    expect(overflow()).toBe("hidden");
    expect(priority()).toBe("important");
    expect(document.body.style.getPropertyValue("--floating-ui-scrollbar-width")).toMatch(/^\d+px$/);
    release();
    expect(overflow()).toBe("");
    expect(document.body.style.getPropertyValue("--floating-ui-scrollbar-width")).toBe("");
  });

  it("嵌套：抽屉里开弹窗，先关弹窗再关抽屉；或先关抽屉再关弹窗——锁都保持到最后一个持有者释放，再恢复原值", () => {
    document.body.style.setProperty("overflow", "auto");
    const drawer = lockBodyScroll();
    const dialog = lockBodyScroll({ compensate: true });
    dialog();
    expect([overflow(), priority()]).toEqual(["hidden", "important"]);
    drawer();
    expect([overflow(), priority()]).toEqual(["auto", ""]);

    const drawer2 = lockBodyScroll();
    const dialog2 = lockBodyScroll({ compensate: true });
    drawer2();
    expect([overflow(), priority()]).toEqual(["hidden", "important"]);
    expect(document.body.style.getPropertyValue("--floating-ui-scrollbar-width")).not.toBe("");
    dialog2();
    expect([overflow(), priority()]).toEqual(["auto", ""]);
    expect(document.body.style.getPropertyValue("--floating-ui-scrollbar-width")).toBe("");
  });

  it("加锁前原本带 important 的值，解锁后连同优先级一起恢复", () => {
    document.body.style.setProperty("overflow", "clip", "important");
    const release = lockBodyScroll({ compensate: true });
    expect([overflow(), priority()]).toEqual(["hidden", "important"]);
    release();
    expect([overflow(), priority()]).toEqual(["clip", "important"]);
  });
});
