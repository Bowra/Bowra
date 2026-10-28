/**
 * mobile-tech-design r1 batch 1: touch-layout branching.
 * - Criterion is hover capability (`(hover: none)` / `(pointer: coarse)`), not screen width; includes tablets
 * - Touch: no drag handle or hover pill; render persistent ⋯ instead; kind gates follow the r3 matrix
 * - Read-only (canEdit=false equivalent: no handler) renders nothing on touch either
 * - The ⋯ menu is the shared menu; on a phone-width touch screen it is the shared bottom sheet
 */

import "@testing-library/jest-dom/vitest";
import { screen, fireEvent, cleanup, waitFor } from "@testing-library/react";
import { renderIntl } from "@/__tests__/intl-render";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SectionChrome } from "../extensions/section-chrome";
import { CanvasContextProvider } from "../canvas-context";
import type { ReportSection } from "@/lib/types";

const SECTIONS: ReportSection[] = [
  { id: "sec_meta", kind: "meta", pills: [] } as unknown as ReportSection,
  { id: "sec_n1", kind: "narrative", markdown: "正文" } as unknown as ReportSection,
];

/** jsdom lacks matchMedia; answer each query precisely to simulate touch phones and desktop devices. */
function stubMatchMedia(touch: boolean) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: (query: string) => ({
      matches: touch
        ? query.includes("hover: none") || query.includes("pointer: coarse") || query.includes("max-width")
        : query.includes("hover: hover") || query.includes("pointer: fine"),
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

function renderChrome(
  kind: string,
  sectionId: string,
  opts: { editable?: boolean; onMove?: (id: string, dir: "up" | "down") => void } = {},
) {
  const editable = opts.editable ?? true;
  return renderIntl(
    <CanvasContextProvider
      sections={SECTIONS}
      canvasId="cv_t"
      isEditMode={editable}
      onDeleteSection={editable ? vi.fn() : undefined}
      onSplitAtCursor={editable ? vi.fn() : undefined}
      onEditBlockWithAI={editable ? vi.fn() : undefined}
      onMoveSection={editable ? (opts.onMove ?? vi.fn()) : undefined}
    >
      <SectionChrome sectionId={sectionId} kind={kind}>
        <p>内容</p>
      </SectionChrome>
    </CanvasContextProvider>,
  );
}

describe("SectionChrome 触屏形态 (mobile r1 §2.2)", () => {
  afterEach(() => {
    cleanup();
    // @ts-expect-error Restore original jsdom state (matchMedia was absent)
    delete window.matchMedia;
  });

  it("触屏 narrative: 常驻 ⋯ 取代拖柄与浮层 pill", () => {
    stubMatchMedia(true);
    const { container } = renderChrome("narrative", "sec_n1");
    expect(container.querySelector(".rc-mdots")).not.toBeNull();
    expect(container.querySelector(".rc-handle")).toBeNull();
    expect(container.querySelector(".rc-actions")).toBeNull();
    expect(container.querySelector(".v2-section-chrome")?.getAttribute("data-chrome-touch"))
      .toBe("on");
  });

  it("桌面 narrative: 维持拖柄 + pill, 不出常驻 ⋯", () => {
    stubMatchMedia(false);
    const { container } = renderChrome("narrative", "sec_n1");
    expect(container.querySelector(".rc-mdots")).toBeNull();
    expect(container.querySelector(".rc-handle")).not.toBeNull();
    expect(container.querySelector(".rc-actions")).not.toBeNull();
    expect(container.querySelector(".v2-section-chrome")?.hasAttribute("data-chrome-touch"))
      .toBe(false);
  });

  it("触屏 ⋯ 点开出菜单, 项序与桌面一致(AI 首项/删除末项带 danger)", () => {
    stubMatchMedia(true);
    renderChrome("narrative", "sec_n1");
    const dots = screen.getByRole("button", { name: /操作菜单/ });
    expect(dots.className).toContain("rc-mdots");
    fireEvent.click(dots);
    expect(dots.className).toContain("on");
    const items = screen.getAllByRole("menuitem");
    expect(items[0].textContent).toContain("AI 改写");
    expect(items[items.length - 1].textContent).toContain("删除");
    expect(items[items.length - 1].className).toContain("danger");
  });

  it("触屏 meta: 骨架件仍零操作入口（kind 门沿用 r3 矩阵）", () => {
    stubMatchMedia(true);
    const { container } = renderChrome("meta", "sec_meta");
    expect(container.querySelector(".rc-mdots")).toBeNull();
    expect(container.querySelector(".rc-handle")).toBeNull();
    expect(container.querySelector(".rc-actions")).toBeNull();
  });

  it("触屏 ⋯ 开的是共享底部面板(非锚定 popover), 项序含上移/下移", async () => {
    stubMatchMedia(true);
    renderChrome("narrative", "sec_n1");
    fireEvent.click(screen.getByRole("button", { name: /操作菜单/ }));
    const menu = await screen.findByRole("menu");
    await waitFor(() => expect(menu).toHaveClass("is-sheet"));
    expect(document.querySelector(".ui-menu-sheet-scrim")).not.toBeNull();
    const labels = screen.getAllByRole("menuitem").map((el) => el.textContent);
    expect(labels[0]).toContain("AI 改写");
    expect(labels).toContain("在此处分割");
    expect(labels).toContain("上移");
    expect(labels).toContain("下移");
    expect(labels[labels.length - 1]).toContain("删除");
  });

  it("触屏上移/下移调 onMoveSection 并带方向", () => {
    stubMatchMedia(true);
    const onMove = vi.fn();
    renderChrome("narrative", "sec_n1", { onMove });
    fireEvent.click(screen.getByRole("button", { name: /操作菜单/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: /上移/ }));
    expect(onMove).toHaveBeenCalledWith("sec_n1", "up");
    fireEvent.click(screen.getByRole("button", { name: /操作菜单/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: /下移/ }));
    expect(onMove).toHaveBeenCalledWith("sec_n1", "down");
  });

  it("真实事件序列(mousedown→click)下面板不自关", () => {
    // Shape observed on device 116: a sheet dismissed on mousedown unmounts before onClick fires;
    // synthetic click without mousedown still passes, so dispatch the entire sequence here.
    stubMatchMedia(true);
    const onMove = vi.fn();
    renderChrome("narrative", "sec_n1", { onMove });
    fireEvent.click(screen.getByRole("button", { name: /操作菜单/ }));
    const item = screen.getByRole("menuitem", { name: /上移/ });
    fireEvent.mouseDown(item);
    expect(screen.queryByRole("menu")).not.toBeNull();
    fireEvent.click(item);
    expect(onMove).toHaveBeenCalledWith("sec_n1", "up");
  });

  it("真实事件序列下点 scrim 才关单", async () => {
    stubMatchMedia(true);
    renderChrome("narrative", "sec_n1");
    fireEvent.click(screen.getByRole("button", { name: /操作菜单/ }));
    await waitFor(() => expect(document.querySelector(".ui-menu-sheet-scrim")).not.toBeNull());
    const scrim = document.querySelector(".ui-menu-sheet-scrim")!;
    fireEvent.mouseDown(scrim);
    fireEvent.click(scrim);
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });

  it("桌面菜单不含上移/下移（桌面用拖柄）", () => {
    stubMatchMedia(false);
    renderChrome("narrative", "sec_n1");
    fireEvent.click(screen.getByRole("button", { name: /操作菜单/ }));
    const labels = screen.getAllByRole("menuitem").map((el) => el.textContent ?? "");
    expect(labels.some((l) => l.includes("上移") || l.includes("下移"))).toBe(false);
    expect(screen.getByRole("menu")).not.toHaveClass("is-sheet");
  });

  it("触屏只读（无 handler / 非编辑态）: 常驻 ⋯ 不漏出", () => {
    stubMatchMedia(true);
    const { container } = renderChrome("narrative", "sec_n1", { editable: false });
    expect(container.querySelector(".rc-mdots")).toBeNull();
    expect(container.querySelector(".rc-handle")).toBeNull();
    expect(container.querySelector(".rc-actions")).toBeNull();
    expect(screen.getByText("内容")).toBeTruthy();
  });
});
