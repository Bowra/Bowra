/**
 * design r2 (report-edit-affordance) action-matrix gate:
 * - Drag handles/action clusters/⋮ menus belong only to text-based body blocks (narrative/deck/callout, + design)
 * - Structural nodes such as meta render no action entries (in-place edits only)
 * - First menu item = AI Rewrite; dangerous Delete last, separated by a divider
 * r3 §5.1/5.2/5.3 additions:
 * - Content atomic kinds (image, etc.) retain a delete-only menu, without a drag handle
 * - Chrome button mousedown preventDefault (preserve PM caret, required for splitting)
 * - text-based node spec draggable + rc-handle data-drag-handle (make drag handles functional)
 */

import { screen, fireEvent, cleanup } from "@testing-library/react";
import { renderIntl } from "@/__tests__/intl-render";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SectionChrome, kindHasDeleteMenu } from "../extensions/section-chrome";
import { ALL_TEXT_BASED_EXTENSIONS } from "../extensions/text-based";
import { CanvasContextProvider } from "../canvas-context";
import type { ReportSection } from "@/lib/types";

const SECTIONS: ReportSection[] = [
  { id: "sec_meta", kind: "meta", pills: [] } as unknown as ReportSection,
  { id: "sec_n1", kind: "narrative", markdown: "正文" } as unknown as ReportSection,
  { id: "sec_img", kind: "image", url: "x.png" } as unknown as ReportSection,
];

function renderChrome(kind: string, sectionId: string) {
  return renderIntl(
    <CanvasContextProvider
      sections={SECTIONS}
      canvasId="cv_t"
      isEditMode
      onDeleteSection={vi.fn()}
      onSplitAtCursor={vi.fn()}
      onEditBlockWithAI={vi.fn()}
    >
      <SectionChrome sectionId={sectionId} kind={kind}>
        <p>内容</p>
      </SectionChrome>
    </CanvasContextProvider>,
  );
}

describe("SectionChrome 操作矩阵门 (design r2)", () => {
  afterEach(cleanup);

  it("narrative: 渲染拖柄 + 操作簇, 菜单含 AI 首项与危险删除项", () => {
    const { container } = renderChrome("narrative", "sec_n1");
    expect(container.querySelector(".rc-handle")).not.toBeNull();
    expect(container.querySelector(".rc-actions")).not.toBeNull();

    const kebab = screen.getByRole("button", { name: /操作菜单/ });
    fireEvent.click(kebab);
    const items = screen.getAllByRole("menuitem");
    expect(items[0].textContent).toContain("AI 改写");
    const del = items[items.length - 1];
    expect(del.textContent).toContain("删除");
    expect(del.className).toContain("danger");
    expect(document.querySelector(".rc-menu-pop")).not.toBeNull();
    expect(document.querySelector(".rc-msep")).not.toBeNull();
  });

  it("meta: 骨架件不渲染拖柄/操作簇/菜单", () => {
    const { container } = renderChrome("meta", "sec_meta");
    expect(container.querySelector(".rc-handle")).toBeNull();
    expect(container.querySelector(".rc-actions")).toBeNull();
    expect(screen.queryByRole("button", { name: /操作菜单 \(meta\)/ })).toBeNull();
    // Preserve children and container invariants
    expect(container.querySelector(".v2-section-chrome")).not.toBeNull();
    expect(screen.getByText("内容")).toBeTruthy();
  });

  it("image (r3 §5.1): 仅删除菜单——无拖柄, 菜单只有危险删除项", () => {
    const { container } = renderChrome("image", "sec_img");
    expect(container.querySelector(".rc-handle")).toBeNull();
    // Cluster exists only for ⋮, without an AI button
    expect(container.querySelector(".rc-action-btn.ai")).toBeNull();
    const kebab = screen.getByRole("button", { name: /操作菜单 \(image\)/ });
    fireEvent.click(kebab);
    const items = screen.getAllByRole("menuitem");
    expect(items).toHaveLength(1);
    expect(items[0].textContent).toContain("删除");
    expect(items[0].className).toContain("danger");
    // A single-item menu has no divider
    expect(document.querySelector(".rc-msep")).toBeNull();
  });

  it("kindHasDeleteMenu: guard toast 指路判据与菜单渲染一致", () => {
    for (const k of ["narrative", "deck", "callout", "design", "image", "html_artifact", "trend_chart"]) {
      expect(kindHasDeleteMenu(k), k).toBe(true);
    }
    for (const k of ["meta", "eyebrow", "h1", "kpi_row", "chart", "sources"]) {
      expect(kindHasDeleteMenu(k), k).toBe(false);
    }
  });

  it("mousedown preventDefault (r3 §5.2): 菜单按钮与菜单项不夺 PM 光标", () => {
    renderChrome("narrative", "sec_n1");
    const kebab = screen.getByRole("button", { name: /操作菜单/ });
    const md1 = fireEvent.mouseDown(kebab);
    expect(md1).toBe(false); // preventDefault → fireEvent returns false
    fireEvent.click(kebab);
    const item = screen.getAllByRole("menuitem")[0];
    expect(fireEvent.mouseDown(item)).toBe(false);
  });

  it("拖柄做真 (r3 §5.3): text-based node spec draggable + rc-handle 带 data-drag-handle+draggable", () => {
    for (const ext of ALL_TEXT_BASED_EXTENSIONS) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      expect((ext as any).config.draggable, (ext as any).config.name).toBe(true);
    }
    const { container } = renderChrome("narrative", "sec_n1");
    const handle = container.querySelector(".rc-handle");
    expect(handle?.hasAttribute("data-drag-handle")).toBe(true);
    // PM automatically marks only leaves without contentDOM as draggable; content-bearing NodeViews require an explicit mark,
    // or browsers never initiate drag, making the handle decorative again (116 evidence).
    expect(handle?.getAttribute("draggable")).toBe("true");
  });
});
