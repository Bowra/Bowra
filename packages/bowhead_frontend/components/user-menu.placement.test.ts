/**
 * Account menu in mobile drawers must stay within the viewport (UI redesign P0-5, docs/design/界面改版-Apple设计方向.md).
 *
 * Menu placement is `right-end`; the mobile-drawer trigger almost fills 390px width, leaving no room on either side.
 * Previously flip only swapped left/right, while shift only corrected vertically for side placements; most of the menu overflowed to the right.
 * Placement now uses `useMenu`'s general configuration (`menuMiddleware`: flip tries above/below if neither side fits).
 * jsdom lacks layout; use floating-ui computePosition and stub rectangles to calculate the account menu's
 * actual placement/middleware; real-browser evidence (2026-09-25, local dev, 390×844):
 * menu fits entirely above its trigger; desktop 1440×900 still opens right of the sidebar.
 */
import { computePosition } from "@floating-ui/react";
import { afterEach, describe, expect, it } from "vitest";

import { menuMiddleware } from "./ui/menu";
import { USER_MENU_OFFSET, USER_MENU_PLACEMENT } from "./user-menu";

function stubViewport(width: number, height: number) {
  Object.defineProperty(document.documentElement, "clientWidth", { value: width, configurable: true });
  Object.defineProperty(document.documentElement, "clientHeight", { value: height, configurable: true });
}

function box(rect: { x: number; y: number; width: number; height: number }) {
  const el = document.createElement("div");
  el.style.width = `${rect.width}px`;
  el.style.height = `${rect.height}px`;
  Object.defineProperty(el, "offsetWidth", { value: rect.width, configurable: true });
  Object.defineProperty(el, "offsetHeight", { value: rect.height, configurable: true });
  el.getBoundingClientRect = () =>
    ({ ...rect, top: rect.y, left: rect.x, right: rect.x + rect.width, bottom: rect.y + rect.height, toJSON() {} }) as DOMRect;
  document.body.appendChild(el);
  return el;
}

function placeAccountMenu(trigger: HTMLElement, menu: HTMLElement) {
  return computePosition(trigger, menu, {
    placement: USER_MENU_PLACEMENT,
    strategy: "fixed",
    middleware: menuMiddleware({ offset: USER_MENU_OFFSET }),
  });
}

afterEach(() => {
  document.body.innerHTML = "";
});

describe("账户菜单定位", () => {
  it("左右都放不下时向上展开，且完整留在视口内", async () => {
    stubViewport(390, 844);
    // Identity row at drawer bottom (drawer width min(320px, 100vw-48px), but menu width 256px + 8px gap fits on neither side).
    const trigger = box({ x: 12, y: 776, width: 318, height: 56 });
    const menu = box({ x: 0, y: 0, width: 256, height: 430 });

    const result = await placeAccountMenu(trigger, menu);

    expect(result.placement.startsWith("top")).toBe(true);
    expect(result.x).toBeGreaterThanOrEqual(8);
    expect(result.x + 256).toBeLessThanOrEqual(390 - 8);
    expect(result.y + 430).toBeLessThanOrEqual(776);
  });

  it("桌面侧栏仍在右侧展开", async () => {
    stubViewport(1440, 900);
    // Identity-row position measured in real browser at 1440×900: bottom almost touches viewport bottom (898px), less than 8px from the edge.
    // Do not flip above merely for a few pixels of vertical shortfall; shift corrects that axis.
    const trigger = box({ x: 0, y: 831, width: 279, height: 67 });
    const menu = box({ x: 0, y: 0, width: 256, height: 430 });

    const result = await placeAccountMenu(trigger, menu);

    expect(result.placement).toBe("right-end");
  });
});
