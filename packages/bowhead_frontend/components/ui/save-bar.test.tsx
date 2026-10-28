/**
 * Save bar appears only after changes (appearance decision 17; page-by-page acceptance A “保存栏合同”):
 * show only with unsaved changes; Discard/Save invoke their respective handlers; no repeated submissions while saving;
 * mount into a provided slot; guard beforeunload with unsaved changes (guard=false retains the page's original guard).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";

import { renderIntl } from "@/__tests__/intl-render";
import zhMessages from "@/messages/zh-CN.json";

import { SaveBar, SaveBarSlotContext } from "./save-bar";

/** Rerenderable wrapper: use the same language context on every render. */
const wrap = (ui: ReactElement) => <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>{ui}</NextIntlClientProvider>;
function renderWrapped(ui: ReactElement) {
  const result = render(wrap(ui));
  return { rerender: (next: ReactElement) => result.rerender(wrap(next)) };
}

afterEach(() => {
  cleanup();
});

describe("SaveBar", () => {
  it("没有改动时不出现，有改动才出现", () => {
    const { rerender } = renderWrapped(<SaveBar dirty={false} onSave={vi.fn()} onDiscard={vi.fn()} />);
    expect(screen.queryByRole("region", { name: "有未保存的更改" })).toBeNull();
    rerender(<SaveBar dirty onSave={vi.fn()} onDiscard={vi.fn()} />);
    const bar = screen.getByRole("region", { name: "有未保存的更改" });
    expect(bar.className).toContain("ui-savebar");
    expect(bar.className).toContain("is-inline");
  });

  it("「放弃」「保存」各自调用处理函数", () => {
    const onSave = vi.fn();
    const onDiscard = vi.fn();
    renderIntl(<SaveBar dirty onSave={onSave} onDiscard={onDiscard} />);
    fireEvent.click(screen.getByRole("button", { name: "放弃" }));
    expect(onDiscard).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  it("保存中：保存按钮忙碌且禁用、放弃禁用，改动已提交后仍显示直到保存结束", () => {
    const onSave = vi.fn();
    renderIntl(<SaveBar dirty={false} saving onSave={onSave} onDiscard={vi.fn()} />);
    const save = screen.getByRole("button", { name: "保存中…" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    expect(save.getAttribute("aria-busy")).toBe("true");
    expect((screen.getByRole("button", { name: "放弃" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(save);
    expect(onSave).not.toHaveBeenCalled();
  });

  it("校验未通过时只禁用保存", () => {
    renderIntl(<SaveBar dirty saveDisabled onSave={vi.fn()} onDiscard={vi.fn()} />);
    expect((screen.getByRole("button", { name: "保存" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "放弃" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("有插槽时用门户挂进插槽", () => {
    const slot = document.createElement("div");
    slot.className = "ui-savebar-slot";
    document.body.appendChild(slot);
    renderIntl(
      <SaveBarSlotContext.Provider value={slot}>
        <SaveBar dirty onSave={vi.fn()} onDiscard={vi.fn()} />
      </SaveBarSlotContext.Provider>,
    );
    const bar = screen.getByRole("region", { name: "有未保存的更改" });
    expect(bar.parentElement).toBe(slot);
    expect(bar.className).not.toContain("is-inline");
    slot.remove();
  });

  it("有未保存改动时拦 beforeunload；guard=false 与没有改动时不拦", () => {
    const fire = () => {
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    };
    const { rerender } = renderWrapped(<SaveBar dirty onSave={vi.fn()} onDiscard={vi.fn()} />);
    expect(fire()).toBe(true);
    rerender(<SaveBar dirty guard={false} onSave={vi.fn()} onDiscard={vi.fn()} />);
    expect(fire()).toBe(false);
    rerender(<SaveBar dirty={false} onSave={vi.fn()} onDiscard={vi.fn()} />);
    expect(fire()).toBe(false);
  });

  describe("窄屏贴视口底边：占位块、软键盘避让、正在输入的字段不被挡住", () => {
    const listeners = new Map<string, Set<() => void>>();
    const viewport = {
      height: 844,
      offsetTop: 0,
      scale: 1,
      addEventListener: (type: string, fn: () => void) => {
        if (!listeners.has(type)) listeners.set(type, new Set());
        listeners.get(type)!.add(fn);
      },
      removeEventListener: (type: string, fn: () => void) => listeners.get(type)?.delete(fn),
    };
    const fireViewport = (type: string) => listeners.get(type)?.forEach((fn) => fn());
    let style: HTMLStyleElement;
    let content: HTMLElement;
    let slot: HTMLElement;
    let input: HTMLInputElement;
    const barTop = 790;
    beforeEach(() => {
      listeners.clear();
      viewport.height = 844;
      vi.stubGlobal("visualViewport", viewport);
      vi.stubGlobal("innerHeight", 844);
      vi.stubGlobal("requestAnimationFrame", (fn: FrameRequestCallback) => {
        fn(0);
        return 1;
      });
      // Narrow-screen bar is fixed (jsdom loads no stylesheets; add an equivalent rule for getComputedStyle).
      style = document.createElement("style");
      style.textContent = ".ui-savebar-slot > .ui-savebar { position: fixed; }";
      document.head.appendChild(style);
      content = document.createElement("section");
      content.className = "settings-content";
      input = document.createElement("input");
      content.appendChild(input);
      slot = document.createElement("div");
      slot.className = "ui-savebar-slot";
      content.appendChild(slot);
      document.body.appendChild(content);
      vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
        if (this.classList.contains("ui-savebar")) return { top: barTop, bottom: 844, height: 54, left: 0, right: 390, width: 390, x: 0, y: barTop, toJSON: () => ({}) } as DOMRect;
        if (this === input) return { top: 780, bottom: 816, height: 36, left: 0, right: 390, width: 390, x: 0, y: 780, toJSON: () => ({}) } as DOMRect;
        return { top: 0, bottom: 0, height: 0, left: 0, right: 0, width: 0, x: 0, y: 0, toJSON: () => ({}) } as DOMRect;
      });
    });
    afterEach(() => {
      vi.unstubAllGlobals();
      vi.restoreAllMocks();
      style.remove();
      content.remove();
    });
    const renderInSlot = () =>
      renderIntl(
        <SaveBarSlotContext.Provider value={slot}>
          <SaveBar dirty onSave={vi.fn()} onDiscard={vi.fn()} />
        </SaveBarSlotContext.Provider>,
      );

    it("插槽里的占位块与栏同高（内容区最后一项不被盖住，不靠 :has()）", () => {
      renderInSlot();
      const spacer = slot.querySelector(".ui-savebar-spacer") as HTMLElement;
      expect(spacer.getAttribute("aria-hidden")).toBe("true");
      expect(spacer.style.height).toBe("54px");
      expect(spacer.nextElementSibling?.classList.contains("ui-savebar")).toBe(true);
    });

    it("软键盘弹起时栏抬到键盘上方，收起后回到底边", () => {
      renderInSlot();
      const bar = screen.getByRole("region", { name: "有未保存的更改" });
      expect(bar.style.getPropertyValue("--savebar-keyboard-inset")).toBe("0px");
      act(() => {
        viewport.height = 500;
        fireViewport("resize");
      });
      expect(bar.style.getPropertyValue("--savebar-keyboard-inset")).toBe("344px");
      act(() => {
        viewport.height = 844;
        fireViewport("resize");
      });
      expect(bar.style.getPropertyValue("--savebar-keyboard-inset")).toBe("0px");
    });

    it("获得焦点的字段底边低于栏顶时把页面滚上去，栏外的焦点与栏内按钮不滚", () => {
      const scrollBy = vi.fn();
      vi.stubGlobal("scrollBy", scrollBy);
      renderInSlot();
      scrollBy.mockClear();
      act(() => {
        input.focus();
        input.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
      });
      // Field bottom 816 − bar top 790 + gap 8
      expect(scrollBy).toHaveBeenCalledWith({ top: 34 });
      scrollBy.mockClear();
      act(() => {
        screen.getByRole("button", { name: "放弃" }).focus();
      });
      expect(scrollBy).not.toHaveBeenCalled();
    });
  });

});
