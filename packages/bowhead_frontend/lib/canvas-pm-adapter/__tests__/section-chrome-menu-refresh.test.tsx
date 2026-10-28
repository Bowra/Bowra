/**
 * Chat workbench and report canvas batch 4b: section menus use the shared menu (useMenu, mounted to body):
 * shared-menu roles, keyboard behavior and focus return; danger items red with “…”; Split Here disabled with its reason when unavailable;
 * narrow touch screens use a bottom sheet (menu tests lock the sheet's own behavior).
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import type { ReportSection } from "@/lib/types";

import { CanvasContextProvider } from "../canvas-context";
import { SectionChrome } from "../extensions/section-chrome";

const SECTIONS: ReportSection[] = [
  { id: "sec_n0", kind: "narrative", markdown: "上一段" } as unknown as ReportSection,
  { id: "sec_n1", kind: "narrative", markdown: "正文" } as unknown as ReportSection,
];

function stubDesktopMatchMedia() {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: (query: string) => ({
      matches: query.includes("hover: hover") || query.includes("pointer: fine"),
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      onchange: null,
      dispatchEvent: () => false,
    }),
  });
}

function renderChrome(opts: { canSplit?: boolean } = {}) {
  const onDelete = vi.fn();
  const onSplit = vi.fn();
  const onMerge = vi.fn();
  const utils = renderIntl(
    <CanvasContextProvider
      sections={SECTIONS}
      canvasId="cv_t"
      isEditMode
      onDeleteSection={onDelete}
      onSplitAtCursor={onSplit}
      canSplitAtCursor={opts.canSplit === undefined ? undefined : () => opts.canSplit as boolean}
      onMergeWithPrev={onMerge}
      onEditBlockWithAI={vi.fn()}
    >
      <SectionChrome sectionId="sec_n1" kind="narrative">
        <p>内容</p>
      </SectionChrome>
    </CanvasContextProvider>,
  );
  const trigger = screen.getByRole("button", { name: "操作菜单 (narrative)" });
  return { ...utils, trigger, onDelete, onSplit, onMerge };
}

afterEach(() => {
  cleanup();
  // @ts-expect-error Restore original jsdom state (matchMedia was absent)
  delete window.matchMedia;
});

describe("section menu uses the shared menu", () => {
  it("挂在 body、共享菜单外壳；危险项标红并带「…」；没有区块编号尾行", async () => {
    stubDesktopMatchMedia();
    const { container, trigger } = renderChrome({ canSplit: true });
    fireEvent.click(trigger);
    const menu = await screen.findByRole("menu");
    expect(container.contains(menu)).toBe(false);
    expect(menu).toHaveClass("ui-menu", "rc-menu-pop");
    expect(menu.style.zIndex).toBe("var(--z-menu)");
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    const del = screen.getByRole("menuitem", { name: "删除…" });
    expect(del).toHaveAttribute("data-danger", "true");
    expect(menu.querySelector(".rc-menu-id")).toBeNull();
    expect(screen.getAllByRole("menuitem").map((el) => el.textContent)).toEqual([
      "AI 改写",
      "向上合并",
      "在此处分割",
      "删除…",
    ]);
  });

  it("键盘：打开时焦点进第一项、方向键移动，Esc 关闭并回到「⋯」", async () => {
    stubDesktopMatchMedia();
    const { trigger } = renderChrome({ canSplit: true });
    fireEvent.click(trigger, { detail: 0 });
    const items = await screen.findAllByRole("menuitem");
    await waitFor(() => expect(document.activeElement).toBe(items[0]));
    fireEvent.keyDown(screen.getByRole("menu"), { key: "ArrowDown" });
    expect(document.activeElement).toBe(items[1]);
    fireEvent.keyDown(items[1], { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("光标不在这一节时「在此处分割」禁用、第二行写出原因，点了不执行；其余项照常", async () => {
    stubDesktopMatchMedia();
    const { trigger, onSplit, onMerge } = renderChrome({ canSplit: false });
    fireEvent.click(trigger);
    const split = await screen.findByRole("menuitem", { name: "在此处分割" });
    expect(split).toHaveAttribute("aria-disabled", "true");
    const reason = document.getElementById(split.getAttribute("aria-describedby") ?? "");
    expect(reason?.textContent).toBe("先在这一节的正文里点一下要分割的位置。");
    fireEvent.click(split);
    expect(onSplit).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("menuitem", { name: "向上合并" }));
    expect(onMerge).toHaveBeenCalledWith("sec_n1");
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });

  it("能分割时照常执行；删除走调用方（确认框在调用方）", async () => {
    stubDesktopMatchMedia();
    const { trigger, onSplit, onDelete } = renderChrome({ canSplit: true });
    fireEvent.click(trigger);
    const split = await screen.findByRole("menuitem", { name: "在此处分割" });
    expect(split).not.toHaveAttribute("aria-disabled");
    fireEvent.click(split);
    expect(onSplit).toHaveBeenCalledWith("sec_n1");
    fireEvent.click(trigger);
    fireEvent.click(await screen.findByRole("menuitem", { name: "删除…" }));
    expect(onDelete).toHaveBeenCalledWith("sec_n1");
  });
});

describe("章节菜单：窄屏触屏", () => {
  function stubNarrowTouch() {
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      configurable: true,
      value: (query: string) => ({
        matches: query.includes("hover: none") || query.includes("pointer: coarse") || query.includes("max-width"),
        media: query,
        addEventListener: () => {},
        removeEventListener: () => {},
        addListener: () => {},
        removeListener: () => {},
        onchange: null,
        dispatchEvent: () => false,
      }),
    });
  }

  it("the persistent “⋯” opens the shared bottom sheet headed by the section summary", async () => {
    stubNarrowTouch();
    renderChrome({ canSplit: true });
    fireEvent.click(screen.getByRole("button", { name: "操作菜单 (narrative)" }));
    const menu = await screen.findByRole("menu");
    await waitFor(() => expect(menu).toHaveClass("is-sheet"));
    expect(document.querySelector(".ui-menu-sheet-scrim")).not.toBeNull();
    expect(document.body.textContent).toContain("正文");
    expect(document.querySelector(".rc-sheet")).toBeNull();
  });
});

