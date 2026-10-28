/**
 * feedback-0713 F1 — FormattingToolbar captured-selection defense regression lock.
 *
 * Confirmed real-device cause: formatting triggers auto-save → PATCH round-trip → store update →
 * setContent rebuilds entire doc → toolbar's captured {from,to} shifts in new doc → next
 * formatting applies to wrong range (font size affects one character) or collapses to no-op (serif silently ineffective).
 * Two-layer fix:
 *   L1 report-view setContent effect defers while selection overlay is open (see report-view);
 *   L2 this component validates captured text against doc before restoreSel; on mismatch closes toolbar and refuses formatting.
 * This locks L2: predicate semantics (real PM doc) + component wiring (bail on mismatch / apply normally on match).
 */
import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import zhMessages from "@/messages/zh-CN.json";
import { getSchema } from "@tiptap/core";
import { Node as PMNode } from "@tiptap/pm/model";
import StarterKit from "@/lib/canvas-pm-adapter/starter-kit";
import type { Editor } from "@tiptap/react";

import { renderIntl } from "@/__tests__/intl-render";
import { resetAiRewriteDrafts } from "@/lib/ai-rewrite-drafts";
import { FormattingToolbar, capturedSelectionStillValid, isSelectionToolbarShortcut } from "./formatting-toolbar";

afterEach(() => cleanup());

// ── Predicate: text consistency checks on a real PM doc ─────────────────────────────────

const schema = getSchema([StarterKit]);
const docWith = (text: string) =>
  PMNode.fromJSON(schema, {
    type: "doc",
    content: [{ type: "paragraph", content: [{ type: "text", text }] }],
  });

describe("capturedSelectionStillValid", () => {
  it("同一 doc 同一选区 → 有效", () => {
    const doc = docWith("近30天各渠道销售额呈现分化趋势");
    const text = doc.textBetween(1, 8, "\n");
    expect(capturedSelectionStillValid(doc, { from: 1, to: 8, text })).toBe(true);
  });

  it("doc 被重建后同位置是别的文字 → 失配", () => {
    const oldDoc = docWith("近30天各渠道销售额呈现分化趋势");
    const captured = { from: 1, to: 8, text: oldDoc.textBetween(1, 8, "\n") };
    const rebuilt = docWith("完全不同的另一段报告叙述文本内容");
    expect(capturedSelectionStillValid(rebuilt, captured)).toBe(false);
  });

  it("doc 变短导致位置越界 → 失配 (textBetween 抛错路径)", () => {
    const oldDoc = docWith("近30天各渠道销售额呈现分化趋势");
    const captured = { from: 1, to: 16, text: oldDoc.textBetween(1, 16, "\n") };
    const shorter = docWith("短");
    expect(capturedSelectionStillValid(shorter, captured)).toBe(false);
  });

  it("重建后同位置文本恰好一致 → 有效 (套用的就是用户选的字, 无害)", () => {
    const oldDoc = docWith("近30天各渠道销售额呈现分化趋势");
    const captured = { from: 1, to: 8, text: oldDoc.textBetween(1, 8, "\n") };
    const rebuilt = docWith("近30天各渠道销售额呈现分化趋势");
    expect(capturedSelectionStillValid(rebuilt, captured)).toBe(true);
  });
});

// ── Component wiring: mismatch bails (close toolbar without formatting) / match applies normally ──────────────────

const PARA = "近30天各渠道销售额呈现分化趋势";

function makeMockEditor(initialText: string) {
  const handlers: Record<string, Array<() => void>> = {};
  const chainCalls: string[] = [];
  // Collect all chain commands in chainCalls; run() completes one chain
  const chain: Record<string, unknown> = new Proxy(
    {},
    {
      get: (_t, prop: string) => {
        if (prop === "run") return () => true;
        return () => {
          chainCalls.push(prop);
          return chain;
        };
      },
    },
  );
  let docText = initialText;
  const editor = {
    on: (ev: string, h: () => void) => {
      (handlers[ev] ||= []).push(h);
    },
    off: vi.fn(),
    isActive: () => false,
    getAttributes: () => ({}),
    chain: () => chain,
    state: {
      // PM text positions start at 1 (paragraph opening token occupies 0); textBetween adjusts accordingly
      selection: {
        from: 1,
        to: 1 + initialText.length,
        empty: false,
        $from: {
          depth: 1,
          node: () => ({ type: { name: "section_narrative" } }),
        },
      },
      doc: {
        textBetween: (from: number, to: number) => docText.slice(from - 1, to - 1),
      },
    },
    view: {
      dom: document.createElement("div"),
      coordsAtPos: () => ({ left: 0, right: 10, top: 0, bottom: 10 }),
      // Batch 4b: return focus to editor when Esc is pressed within toolbar
      focus: vi.fn(),
    },
  };
  return {
    editor: editor as unknown as Editor,
    fireSelectionUpdate: () => handlers["selectionUpdate"]?.forEach((h) => h()),
    setDocText: (t: string) => {
      docText = t;
    },
    chainCalls,
  };
}

function renderToolbar(mock: ReturnType<typeof makeMockEditor>) {
  const onClose = vi.fn();
  renderIntl(
    <FormattingToolbar
      editor={mock.editor}
      open={true}
      isEditMode={true}
      isStreaming={false}
      isAIRewriting={false}
      onOpen={vi.fn()}
      onClose={onClose}
    />,
  );
  act(() => mock.fireSelectionUpdate());
  return { onClose };
}

describe("FormattingToolbar restoreSel 防御层", () => {
  it("doc 未变: 格式动作照常应用到 captured range", () => {
    const mock = makeMockEditor(PARA);
    const { onClose } = renderToolbar(mock);
    fireEvent.click(screen.getByRole("button", { name: "加粗 (B)" }));
    expect(mock.chainCalls).toContain("setTextSelection");
    expect(mock.chainCalls).toContain("toggleBold");
    expect(onClose).not.toHaveBeenCalled();
  });

  it("doc 在脚下被换(同位置不同文字): 拒绝套用并收起工具栏", () => {
    const mock = makeMockEditor(PARA);
    const { onClose } = renderToolbar(mock);
    mock.setDocText("完全不同的另一段报告叙述文本内容拉长一点");
    fireEvent.click(screen.getByRole("button", { name: "加粗 (B)" }));
    expect(mock.chainCalls).not.toContain("toggleBold");
    expect(mock.chainCalls).not.toContain("setTextSelection");
    expect(onClose).toHaveBeenCalled();
  });

  it("dropdown 路径同样受防御: 字体选项在 doc 换掉后不套用", () => {
    const mock = makeMockEditor(PARA);
    const { onClose } = renderToolbar(mock);
    fireEvent.click(screen.getByRole("button", { name: "文字样式" }));
    fireEvent.click(screen.getByRole("button", { name: "字体" }));
    mock.setDocText("完全不同的另一段报告叙述文本内容拉长一点");
    fireEvent.click(screen.getByRole("menuitem", { name: "衬线" }));
    expect(mock.chainCalls).not.toContain("setFontFamily");
    expect(onClose).toHaveBeenCalled();
  });
});

describe("FormattingToolbar 字体下拉菜单", () => {
  it("键盘打开进第一项、方向键移动，Esc 关闭并回到下拉按钮", async () => {
    const mock = makeMockEditor(PARA);
    renderToolbar(mock);
    fireEvent.click(screen.getByRole("button", { name: "文字样式" }));
    const trigger = screen.getByRole("button", { name: "字体" });
    fireEvent.click(trigger, { detail: 0 });
    const [first, second] = screen.getAllByRole("menuitem");
    await waitFor(() => expect(document.activeElement).toBe(first));
    fireEvent.keyDown(screen.getByRole("menu"), { key: "ArrowDown" });
    expect(document.activeElement).toBe(second);

    fireEvent.keyDown(second!, { key: "Escape" });

    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("点菜单外面关闭", async () => {
    const mock = makeMockEditor(PARA);
    renderToolbar(mock);
    fireEvent.click(screen.getByRole("button", { name: "文字样式" }));
    fireEvent.click(screen.getByRole("button", { name: "字体" }));
    expect(screen.getByRole("menu")).toBeTruthy();

    fireEvent.pointerDown(document.body);

    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });

  it("菜单挂在工具栏节点里——报告浮层管理器按工具栏节点判定点外部", () => {
    const mock = makeMockEditor(PARA);
    renderToolbar(mock);
    fireEvent.click(screen.getByRole("button", { name: "文字样式" }));
    fireEvent.click(screen.getByRole("button", { name: "字体" }));

    const toolbar = screen.getByRole("toolbar");
    expect(toolbar.contains(screen.getByRole("menu"))).toBe(true);
  });
});

describe("选区自定义改写输入的草稿（第 4a 批）", () => {
  afterEach(() => {
    resetAiRewriteDrafts();
  });

  function renderWithAI(sectionId: string) {
    const mock = makeMockEditor(PARA);
    // Section containing selection: resolved ancestor chain contains a section node with sectionId
    (mock.editor.state.doc as unknown as { resolve: (pos: number) => unknown }).resolve = () => ({
      depth: 1,
      node: (d: number) => (d === 1
        ? { attrs: { sectionId }, type: { name: "section_narrative" } }
        : { attrs: {}, type: { name: "doc" } }),
    });
    const onAICustom = vi.fn();
    renderIntl(
      <FormattingToolbar
        editor={mock.editor}
        open={true}
        isEditMode={true}
        isStreaming={false}
        isAIRewriting={false}
        onOpen={vi.fn()}
        onClose={vi.fn()}
        onAIAction={vi.fn()}
        onAICustom={onAICustom}
        draftScope="s_1"
      />,
    );
    act(() => mock.fireSelectionUpdate());
    return { onAICustom };
  }
  const input = () => screen.getByRole("textbox", { name: /怎么改写/ }) as HTMLInputElement;

  it("keeps the typed instruction after Esc closes it and Rewrite reopens it; sending clears it", () => {
    const { onAICustom } = renderWithAI("sec_a");
    fireEvent.click(screen.getByRole("button", { name: "改写" }));
    fireEvent.change(input(), { target: { value: "换成管理层口吻" } });
    fireEvent.keyDown(input(), { key: "Escape" });
    expect(screen.queryByRole("textbox", { name: /怎么改写/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "改写" }));
    expect(input().value).toBe("换成管理层口吻");
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(onAICustom).toHaveBeenCalledWith("换成管理层口吻", { from: 1, to: 1 + PARA.length });
    cleanup();
    renderWithAI("sec_a");
    fireEvent.click(screen.getByRole("button", { name: "改写" }));
    expect(input().value).toBe("");
  });
});

describe("选区工具栏（第 4b 批）：三组、「Aa」收纳字体、⌥⏎ 键盘入口", () => {
  function renderFull(mock: ReturnType<typeof makeMockEditor>, open = true) {
    const props = {
      editor: mock.editor,
      isEditMode: true,
      isStreaming: false,
      isAIRewriting: false,
      onOpen: vi.fn(),
      onClose: vi.fn(),
      onAIAction: vi.fn(),
    };
    const ui = (isOpen: boolean) => (
      <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
        <FormattingToolbar {...props} open={isOpen} />
      </NextIntlClientProvider>
    );
    const utils = render(ui(open));
    act(() => mock.fireSelectionUpdate());
    return { ...utils, rerenderOpen: (isOpen: boolean) => utils.rerender(ui(isOpen)) };
  }
  const buttonNames = () =>
    Array.from(screen.getByRole("toolbar").querySelectorAll("button.rc-sel-btn")).map((b) => b.getAttribute("aria-label"));
  const altEnter = (mock: ReturnType<typeof makeMockEditor>, extra: Record<string, unknown> = {}) =>
    fireEvent.keyDown(mock.editor.view.dom, { key: "Enter", altKey: true, ...extra });

  it("isSelectionToolbarShortcut：只认不带 ⌘ / Ctrl / Shift 的 Alt+Enter；输入法组字中不算", () => {
    const base = { key: "Enter", altKey: true, metaKey: false, ctrlKey: false, shiftKey: false };
    expect(isSelectionToolbarShortcut(base)).toBe(true);
    expect(isSelectionToolbarShortcut({ ...base, altKey: false })).toBe(false);
    expect(isSelectionToolbarShortcut({ ...base, metaKey: true })).toBe(false);
    expect(isSelectionToolbarShortcut({ ...base, ctrlKey: true })).toBe(false);
    expect(isSelectionToolbarShortcut({ ...base, shiftKey: true })).toBe(false);
    expect(isSelectionToolbarShortcut({ ...base, key: "a" })).toBe(false);
    expect(isSelectionToolbarShortcut({ ...base, isComposing: true })).toBe(false);
    expect(isSelectionToolbarShortcut({ ...base, keyCode: 229 })).toBe(false);
  });

  it("puts the AI group first and folds the three font dropdowns into Aa, rendered inside the toolbar node when opened", () => {
    const mock = makeMockEditor(PARA);
    renderFull(mock);
    expect(buttonNames()).toEqual(["改写", "缩短", "正式", "加粗 (B)", "斜体 (I)", "加链接", "文字样式"]);
    const aa = screen.getByRole("button", { name: "文字样式" });
    expect(aa).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("button", { name: "字体" })).toBeNull();
    fireEvent.click(aa);
    expect(aa).toHaveAttribute("aria-expanded", "true");
    const group = screen.getByRole("group", { name: "文字样式" });
    expect(aa.getAttribute("aria-controls")).toBe(group.id);
    expect(screen.getByRole("toolbar").contains(screen.getByRole("button", { name: "字体" }))).toBe(true);
    expect(screen.getByRole("button", { name: "字号" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "字重" })).toBeTruthy();
    expect(screen.getByRole("toolbar")).toHaveAttribute("aria-keyshortcuts", "Alt+Enter");
  });

  it("moves focus from the body into the first toolbar button on Alt+Enter; arrow keys move between buttons and wrap", () => {
    const mock = makeMockEditor(PARA);
    renderFull(mock);
    altEnter(mock);
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "改写" }));
    fireEvent.keyDown(document.activeElement!, { key: "ArrowRight" });
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "缩短" }));
    fireEvent.keyDown(document.activeElement!, { key: "ArrowLeft" });
    fireEvent.keyDown(document.activeElement!, { key: "ArrowLeft" });
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "文字样式" }));
  });

  it("does not take focus on Alt+Enter during IME composition", () => {
    const mock = makeMockEditor(PARA);
    renderFull(mock);
    altEnter(mock, { isComposing: true });
    expect(document.activeElement).toBe(document.body);
  });

  it("returns focus to the body on Esc inside the toolbar and leaves focus alone otherwise", () => {
    const mock = makeMockEditor(PARA);
    const focus = vi.fn();
    (mock.editor.view as unknown as { focus: () => void }).focus = focus;
    renderFull(mock);
    altEnter(mock);
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    expect(focus).toHaveBeenCalledTimes(1);
    (document.activeElement as HTMLElement).blur();
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(focus).toHaveBeenCalledTimes(1);
  });

  // Batch 4b remainder: same Esc behavior in both browsers — close open font dropdown only; otherwise close toolbar without reopening for unchanged selection.
  // Report overlay manager closes the whole toolbar in document capture phase; a listener at the same point checks whether Esc reaches it.
  function overlayManagerStandIn() {
    const reached = vi.fn();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") reached();
    };
    document.addEventListener("keydown", onKey, true);
    return { reached, dispose: () => document.removeEventListener("keydown", onKey, true) };
  }

  it("closes only the open font dropdown on Esc, returning focus to its button without closing the toolbar", async () => {
    const mock = makeMockEditor(PARA);
    const focus = vi.fn();
    (mock.editor.view as unknown as { focus: () => void }).focus = focus;
    renderFull(mock);
    fireEvent.click(screen.getByRole("button", { name: "文字样式" }));
    const family = screen.getByRole("button", { name: "字体" });
    fireEvent.click(family);
    await waitFor(() => expect(screen.getByRole("menu", { name: "字体" })).toBeTruthy());
    const standIn = overlayManagerStandIn();
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu", { name: "字体" })).toBeNull());
    expect(document.activeElement).toBe(family);
    expect(standIn.reached).not.toHaveBeenCalled();
    expect(focus).not.toHaveBeenCalled();
    expect(screen.getByRole("toolbar")).toBeTruthy();
    // Second Esc: dropdown is closed; overlay manager closes toolbar and returns focus to body text
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    expect(standIn.reached).toHaveBeenCalledTimes(1);
    expect(focus).toHaveBeenCalledTimes(1);
    standIn.dispose();
  });

  it("does not reopen for the same selection after Esc closes it; a new selection opens it again", () => {
    const mock = makeMockEditor(PARA);
    const onOpen = vi.fn();
    const props = {
      editor: mock.editor,
      isEditMode: true,
      isStreaming: false,
      isAIRewriting: false,
      onOpen,
      onClose: vi.fn(),
      onAIAction: vi.fn(),
    };
    const ui = (isOpen: boolean) => (
      <NextIntlClientProvider locale="zh-CN" messages={zhMessages}>
        <FormattingToolbar {...props} open={isOpen} />
      </NextIntlClientProvider>
    );
    const { rerender } = render(ui(true));
    act(() => mock.fireSelectionUpdate());
    expect(onOpen).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(document.body, { key: "Escape" });
    rerender(ui(false)); // Overlay manager closes it
    act(() => mock.fireSelectionUpdate());
    expect(onOpen).toHaveBeenCalledTimes(1);
    const selection = mock.editor.state.selection as unknown as { from: number; to: number };
    selection.to -= 2;
    act(() => mock.fireSelectionUpdate());
    expect(onOpen).toHaveBeenCalledTimes(2);
  });
});
