/**
 * Behavior batch B (docs/design/组件统一-技术设计.md “行为批 B：窄屏弹窗与撤销”): at viewport < 640,
 * form dialogs become bottom sheets; confirmations remain centered cards.
 *
 * jsdom lacks layout: bottom anchoring, rounding, sticky actions and safe area need real browsers (e2e/dialog-sheet.spec.ts).
 * Enforce criteria, presentation switching, swipe dismissal, busy/leave confirmation, focus, keyboard-avoidance calculations and wiring.
 */
import { useState } from "react";
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

import { ConfirmDialog } from "./confirm-dialog";
import { Dialog, DialogShell, dialogNeedsSheet } from "./dialog";
import { PromptDialog } from "./prompt-dialog";
import { NARROW_SHEET_MEDIA_QUERY, SHEET_DISMISS_DISTANCE, sheetViewportFrom } from "./sheet";

let narrow = false;
const mediaListeners = new Set<(event: { matches: boolean }) => void>();
const matchMediaSpy = vi.fn((query: string) => ({
  matches: query === NARROW_SHEET_MEDIA_QUERY ? narrow : false,
  media: query,
  onchange: null,
  addEventListener: (_: string, listener: (event: { matches: boolean }) => void) => mediaListeners.add(listener),
  removeEventListener: (_: string, listener: (event: { matches: boolean }) => void) => mediaListeners.delete(listener),
  addListener: vi.fn(),
  removeListener: vi.fn(),
  dispatchEvent: vi.fn(),
}));

function setNarrow(value: boolean) {
  narrow = value;
  act(() => {
    for (const listener of mediaListeners) listener({ matches: value });
  });
}

beforeEach(() => {
  narrow = false;
  mediaListeners.clear();
  matchMediaSpy.mockClear();
  vi.stubGlobal("matchMedia", matchMediaSpy);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  document.body.removeAttribute("style");
});

function FormDialog({
  onClose = () => {},
  busy = false,
  presentation,
  variant,
}: {
  onClose?: () => void;
  busy?: boolean;
  presentation?: "auto" | "center" | "sheet";
  variant?: "center" | "drawer";
}) {
  return (
    <Dialog
      title="添加连接"
      onClose={onClose}
      busy={busy}
      presentation={presentation}
      variant={variant}
      footer={(
        <>
          <button type="button">取消</button>
          <button type="button">保存</button>
        </>
      )}
    >
      <label>
        连接名称
        <input name="name" />
      </label>
      <label>
        服务地址
        <input name="url" />
      </label>
    </Dialog>
  );
}

const sheetOf = () => document.querySelector<HTMLElement>(".ui-dialog-sheet");
const panelOf = () => screen.getByRole("dialog");

function swipe(target: Element, distance: number, elapsed = 400, pointerId = 1) {
  fireEvent.pointerDown(target, { button: 0, pointerId, clientY: 100, timeStamp: 0 });
  fireEvent.pointerMove(target, { pointerId, clientY: 100 + distance });
  fireEvent.pointerUp(target, { pointerId, clientY: 100 + distance, timeStamp: elapsed });
}

describe("dialogNeedsSheet（Narrow Spec「弹窗」）", () => {
  function panelWith(html: string) {
    const panel = document.createElement("div");
    panel.innerHTML = html;
    document.body.append(panel);
    return panel;
  }
  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("标题 + 说明 + 两个按钮的确认框仍是卡片", () => {
    expect(dialogNeedsSheet(panelWith("<h3>删除？</h3><p>无法恢复</p><button>取消</button><button>删除</button>"), 800)).toBe(false);
  });

  it("单个输入（重命名）仍是卡片；附加一个开关的确认框也是", () => {
    expect(dialogNeedsSheet(panelWith("<input name='n'><button>取消</button><button>确定</button>"), 800)).toBe(false);
    expect(dialogNeedsSheet(panelWith("<p>确认？</p><input type='checkbox'><button>好</button>"), 800)).toBe(false);
  });

  it("两个以上字段改面板", () => {
    expect(dialogNeedsSheet(panelWith("<input><textarea></textarea>"), 800)).toBe(true);
    expect(dialogNeedsSheet(panelWith("<input><select><option>a</option></select>"), 800)).toBe(true);
  });

  it("同名单选或 radiogroup 整组算一个字段", () => {
    expect(dialogNeedsSheet(panelWith("<input type='radio' name='s'><input type='radio' name='s'><input type='radio' name='s'>"), 800)).toBe(false);
    expect(
      dialogNeedsSheet(panelWith("<div role='radiogroup'><button role='radio'></button><button role='radio'></button></div>"), 800),
    ).toBe(false);
    expect(
      dialogNeedsSheet(panelWith("<div role='radiogroup'><button role='radio'></button></div><input name='x'>"), 800),
    ).toBe(true);
  });

  it("隐藏的控件不算字段", () => {
    expect(dialogNeedsSheet(panelWith("<input><input type='file' style='display:none'><input hidden><input type='hidden'>"), 800)).toBe(false);
  });

  it("带表格 / 列表框改面板", () => {
    expect(dialogNeedsSheet(panelWith("<table><tr><td>a</td></tr></table>"), 800)).toBe(true);
    expect(dialogNeedsSheet(panelWith("<div role='listbox'></div>"), 800)).toBe(true);
  });

  it("内容超过一屏（面板或其中的滚动区装不下）改面板", () => {
    const panel = panelWith("<p>长内容</p>");
    Object.defineProperty(panel, "scrollHeight", { configurable: true, value: 1200 });
    Object.defineProperty(panel, "clientHeight", { configurable: true, value: 700 });
    expect(dialogNeedsSheet(panel, 800)).toBe(true);

    const other = panelWith("<div class='body' style='overflow-y:auto'>x</div>");
    const body = other.querySelector<HTMLElement>(".body")!;
    Object.defineProperty(body, "scrollHeight", { configurable: true, value: 900 });
    Object.defineProperty(body, "clientHeight", { configurable: true, value: 300 });
    expect(dialogNeedsSheet(other, 800)).toBe(true);

    const tall = panelWith("<p>x</p>");
    tall.getBoundingClientRect = () => ({ height: 790 }) as DOMRect;
    expect(dialogNeedsSheet(tall, 800)).toBe(true);
  });
});

describe("窄屏形态", () => {
  it("wide viewport: only adds a boxless host and stays a centered dialog", () => {
    renderIntl(<FormDialog />);
    expect(document.querySelector(".ui-dialog-sheet-host")).not.toBeNull();
    expect(sheetOf()).toBeNull();
    expect(panelOf().hasAttribute("data-dialog-presentation")).toBe(false);
    expect(panelOf().querySelector("h3.settings-dialog__title")).not.toBeNull();
  });

  it("narrow viewport: a form dialog is a bottom sheet with a grabber, a header and a persistent close button", () => {
    narrow = true;
    renderIntl(<FormDialog />);
    const sheet = sheetOf();
    expect(sheet).not.toBeNull();
    expect(sheet!.querySelector(":scope > .ui-dialog-sheet-grab")).not.toBeNull();
    expect(panelOf().getAttribute("data-dialog-presentation")).toBe("sheet");
    expect(panelOf().closest("[data-settings-dialog-portal]")!.getAttribute("data-dialog-presentation")).toBe("sheet");
    const head = panelOf().querySelector(".ui-dialog-sheet-head")!;
    const title = document.getElementById(panelOf().getAttribute("aria-labelledby")!)!;
    expect(head.contains(title)).toBe(true);
    expect(title.textContent).toBe("添加连接");
    expect(screen.getByRole("button", { name: "关闭" })).toBeTruthy();
  });

  it("确认框与重命名框在窄屏仍是居中卡片", () => {
    narrow = true;
    const view = renderIntl(
      <ConfirmDialog title="删除这份报告？" description="删除后无法恢复。" confirmLabel="删除" onCancel={() => {}} onConfirm={() => {}} />,
    );
    expect(sheetOf()).toBeNull();
    expect(document.querySelector(".ui-dialog-sheet-host")).not.toBeNull();
    view.unmount();
    renderIntl(<PromptDialog title="重命名" label="名称" confirmLabel="保存" onCancel={() => {}} onConfirm={() => {}} />);
    expect(sheetOf()).toBeNull();
  });

  it("调用点覆盖：center 的表单不改面板；sheet 的确认框改面板；抽屉默认是面板", () => {
    narrow = true;
    const first = renderIntl(<FormDialog presentation="center" />);
    expect(sheetOf()).toBeNull();
    expect(matchMediaSpy).not.toHaveBeenCalled();
    first.unmount();
    const second = renderIntl(
      <Dialog title="说明" onClose={() => {}} presentation="sheet">
        <p>只有文字</p>
      </Dialog>,
    );
    expect(sheetOf()).not.toBeNull();
    second.unmount();
    renderIntl(
      <Dialog title="邀请成员" variant="drawer" onClose={() => {}}>
        <p>x</p>
      </Dialog>,
    );
    expect(sheetOf()).not.toBeNull();
  });

  it("overlay=none 的调用点不参与：不包宿主", () => {
    narrow = true;
    renderIntl(
      <DialogShell onClose={() => {}} overlay="none" label="浮卡" panelClassName="card">
        <input />
        <input />
      </DialogShell>,
    );
    expect(document.querySelector(".ui-dialog-sheet, .ui-dialog-sheet-host")).toBeNull();
    expect(screen.getByRole("dialog").hasAttribute("data-dialog-presentation")).toBe(false);
    expect(matchMediaSpy).not.toHaveBeenCalled();
  });

  it("自写弹窗（DialogShell）同样按内容判定：抓手在面板外、算面板内", () => {
    narrow = true;
    renderIntl(
      <DialogShell onClose={() => {}} label="上传" backdropClassName="kb-overlay" panelClassName="kb-modal">
        <input />
        <textarea />
        <div className="kb-modal-foot"><button type="button">上传</button></div>
      </DialogShell>,
    );
    const sheet = sheetOf()!;
    expect(sheet.querySelector(":scope > .ui-dialog-sheet-grab")).not.toBeNull();
    expect(sheet.querySelector(":scope > .kb-modal")).toBe(panelOf());
  });

  it("旋转（宽↔窄）时面板与已填内容不重新挂载", () => {
    narrow = true;
    renderIntl(<FormDialog />);
    const input = screen.getByRole("textbox", { name: "连接名称" }) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "北极九章 生产" } });
    setNarrow(false);
    expect(sheetOf()).toBeNull();
    expect(screen.getByRole("textbox", { name: "连接名称" })).toBe(input);
    setNarrow(true);
    expect(sheetOf()).not.toBeNull();
    expect((screen.getByRole("textbox", { name: "连接名称" }) as HTMLInputElement).value).toBe("北极九章 生产");
    expect(screen.getByRole("textbox", { name: "连接名称" })).toBe(input);
  });
});

describe("下滑关闭", () => {
  beforeEach(() => {
    narrow = true;
  });

  it("按住抓手下滑超过阈值关闭；距离不够且慢则弹回", async () => {
    const onClose = vi.fn();
    renderIntl(<FormDialog onClose={onClose} />);
    const grab = document.querySelector(".ui-dialog-sheet-grab")!;
    fireEvent.pointerDown(grab, { button: 0, pointerId: 1, clientY: 100, timeStamp: 0 });
    fireEvent.pointerMove(grab, { pointerId: 1, clientY: 130 });
    expect(sheetOf()!.style.transform).toBe("translateY(30px)");
    // Slowly drag a short distance (below velocity threshold; fireEvent cannot set timeStamp, so really wait): release bounces back.
    await new Promise((resolve) => setTimeout(resolve, 150));
    fireEvent.pointerUp(grab, { pointerId: 1, clientY: 130, timeStamp: 1000 });
    expect(onClose).not.toHaveBeenCalled();
    expect(sheetOf()!.style.transform).toBe("");
    swipe(grab, SHEET_DISMISS_DISTANCE + 8, 1000, 2);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("慢慢拖过 72px 也关闭（按距离，不靠速度）", async () => {
    const onClose = vi.fn();
    renderIntl(<FormDialog onClose={onClose} />);
    const grab = document.querySelector(".ui-dialog-sheet-grab")!;
    fireEvent.pointerDown(grab, { button: 0, pointerId: 5, clientY: 100 });
    fireEvent.pointerMove(grab, { pointerId: 5, clientY: 100 + SHEET_DISMISS_DISTANCE + 4 });
    // 76px / 250ms ≈ 0.3px/ms, below velocity threshold: only distance criterion can dismiss.
    await new Promise((resolve) => setTimeout(resolve, 250));
    fireEvent.pointerUp(grab, { pointerId: 5, clientY: 100 + SHEET_DISMISS_DISTANCE + 4 });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("拖动中途变成 busy（例如提交开始）：松手不关、弹回", () => {
    const onClose = vi.fn();
    let setBusy: (value: boolean) => void = () => {};
    function Toggle() {
      const [busy, update] = useState(false);
      setBusy = update;
      return <FormDialog onClose={onClose} busy={busy} />;
    }
    renderIntl(<Toggle />);
    const grab = document.querySelector(".ui-dialog-sheet-grab")!;
    fireEvent.pointerDown(grab, { button: 0, pointerId: 6, clientY: 100 });
    fireEvent.pointerMove(grab, { pointerId: 6, clientY: 300 });
    expect(sheetOf()!.style.transform).toBe("translateY(200px)");
    act(() => setBusy(true));
    fireEvent.pointerUp(grab, { pointerId: 6, clientY: 300 });
    expect(onClose).not.toHaveBeenCalled();
    expect(sheetOf()!.style.transform).toBe("");
  });

  it("标题栏也能下滑关闭；按在关闭按钮上不开始拖动", () => {
    const onClose = vi.fn();
    renderIntl(<FormDialog onClose={onClose} />);
    const head = document.querySelector(".ui-dialog-sheet-head")!;
    swipe(head, 120);
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("busy 时下滑、关闭按钮、Esc、点遮罩都不能关", () => {
    const onClose = vi.fn();
    renderIntl(<FormDialog onClose={onClose} busy />);
    const grab = document.querySelector(".ui-dialog-sheet-grab")!;
    // Busy sheets do not follow grabber dragging.
    fireEvent.pointerDown(grab, { button: 0, pointerId: 9, clientY: 100 });
    fireEvent.pointerMove(grab, { pointerId: 9, clientY: 300 });
    expect(sheetOf()!.style.transform).toBe("");
    fireEvent.pointerUp(grab, { pointerId: 9, clientY: 300 });
    swipe(grab, 200);
    expect(sheetOf()!.style.transform).toBe("");
    expect((screen.getByRole("button", { name: "关闭" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.keyDown(panelOf(), { key: "Escape" });
    const backdrop = document.querySelector("[data-settings-dialog-portal]")!;
    fireEvent.pointerDown(backdrop);
    fireEvent.click(backdrop);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("有离开确认的弹窗：下滑走原来的确认流程，面板不关、回到原位", async () => {
    function Guarded() {
      const [open, setOpen] = useState(true);
      const [asking, setAsking] = useState(false);
      if (!open) return null;
      return (
        <>
          <FormDialog onClose={() => setAsking(true)} />
          {asking ? (
            <ConfirmDialog
              title="放弃未保存的内容？"
              confirmLabel="放弃"
              onCancel={() => setAsking(false)}
              onConfirm={() => setOpen(false)}
            />
          ) : null}
        </>
      );
    }
    renderIntl(<Guarded />);
    swipe(document.querySelector(".ui-dialog-sheet-grab")!, 200);
    const confirm = await screen.findByRole("dialog", { name: "放弃未保存的内容？" });
    expect(sheetOf()).not.toBeNull();
    expect(sheetOf()!.style.transform).toBe("");
    // Confirmation itself is a centered card above the sheet.
    expect(confirm.hasAttribute("data-dialog-presentation")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "放弃未保存的内容？" })).toBeNull());
    expect(sheetOf()).not.toBeNull();
  });
});

describe("焦点", () => {
  beforeEach(() => {
    narrow = true;
  });

  it("面板是模态：打开后焦点在面板里，Tab 首尾循环，关闭后回到触发按钮", async () => {
    function Opener() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>打开</button>
          {open ? <FormDialog onClose={() => setOpen(false)} /> : null}
        </>
      );
    }
    renderIntl(<Opener />);
    const trigger = screen.getByRole("button", { name: "打开" });
    trigger.focus();
    fireEvent.click(trigger);
    await waitFor(() => expect(sheetOf()).not.toBeNull());
    // Opening as sheet focuses the sheet itself (no input autofocus, avoiding immediate mobile keyboard opening).
    await waitFor(() => expect(document.activeElement).toBe(panelOf()));
    const close = screen.getByRole("button", { name: "关闭" });
    const save = screen.getByRole("button", { name: "保存" });
    save.focus();
    fireEvent.keyDown(save, { key: "Tab" });
    expect(document.activeElement).toBe(close);
    fireEvent.keyDown(close, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(save);
    fireEvent.keyDown(save, { key: "Escape" });
    await waitFor(() => expect(sheetOf()).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("面板打开期间锁住页面滚动，关闭后恢复", () => {
    const view = renderIntl(<FormDialog />);
    expect(document.body.style.getPropertyValue("overflow")).toBe("hidden");
    expect(document.body.style.getPropertyPriority("overflow")).toBe("important");
    view.unmount();
    expect(document.body.style.getPropertyValue("overflow")).toBe("");
  });
});

describe("软键盘避让", () => {
  it("iOS：布局视口不变、可见区域变矮时，按被挡住的高度抬起；Android resizes-content 为 0；缩放中不避让", () => {
    expect(sheetViewportFrom({ layoutHeight: 844, visualHeight: 500, offsetTop: 0, scale: 1 })).toEqual({ keyboardInset: 344, viewportHeight: 500 });
    expect(sheetViewportFrom({ layoutHeight: 844, visualHeight: 500, offsetTop: 100, scale: 1 })).toEqual({ keyboardInset: 244, viewportHeight: 500 });
    expect(sheetViewportFrom({ layoutHeight: 500, visualHeight: 500, offsetTop: 0, scale: 1 })).toEqual({ keyboardInset: 0, viewportHeight: 500 });
    // Address-bar expansion/collapse differs by only tens of pixels, not a keyboard.
    expect(sheetViewportFrom({ layoutHeight: 844, visualHeight: 790, offsetTop: 0, scale: 1 }).keyboardInset).toBe(0);
    expect(sheetViewportFrom({ layoutHeight: 844, visualHeight: 300, offsetTop: 0, scale: 2 })).toEqual({ keyboardInset: 0, viewportHeight: null });
  });

  it("面板按 visualViewport 抬起、收短，并把获得焦点的输入框滚到可见处", () => {
    narrow = true;
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
    vi.stubGlobal("visualViewport", viewport);
    vi.stubGlobal("innerHeight", 844);
    vi.stubGlobal("requestAnimationFrame", (fn: FrameRequestCallback) => {
      fn(0);
      return 1;
    });
    const scrollIntoView = vi.fn();
    HTMLElement.prototype.scrollIntoView = scrollIntoView;
    renderIntl(<FormDialog />);
    const sheet = sheetOf()!;
    expect(sheet.style.getPropertyValue("--ui-sheet-keyboard-inset")).toBe("0px");
    expect(sheet.hasAttribute("data-keyboard")).toBe(false);

    const input = screen.getByRole("textbox", { name: "服务地址" });
    viewport.height = 450;
    act(() => {
      input.focus();
      for (const fn of listeners.get("resize") ?? []) fn();
    });
    expect(sheetOf()!.style.getPropertyValue("--ui-sheet-keyboard-inset")).toBe("394px");
    expect(sheetOf()!.style.getPropertyValue("--ui-sheet-viewport-height")).toBe("450px");
    expect(sheetOf()!.getAttribute("data-keyboard")).toBe("open");
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "nearest" });
    expect(scrollIntoView.mock.contexts.at(-1)).toBe(input);

    viewport.height = 844;
    act(() => {
      for (const fn of listeners.get("resize") ?? []) fn();
    });
    expect(sheetOf()!.hasAttribute("data-keyboard")).toBe(false);
  });
});
