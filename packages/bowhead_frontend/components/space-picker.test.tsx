/**
 * SpacePicker dropdown interaction/positioning contracts (component unification step 4, ui/menu behavior).
 *
 * Position: expand downward in Topbar (screen top), flip above if needed (decouple R6 dogfood:
 * always opening upward flew offscreen); near the right edge, shift the entire menu left inside screen (measured on 116).
 * jsdom lacks layout; compute directly with floating-ui computePosition and stubbed rectangles.
 */

import { computePosition } from "@floating-ui/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { menuMiddleware } from "@/components/ui/menu";
import { registerSubagentDraftNavigationGuard } from "@/lib/canvas-navigation-guard";

import { SPACE_PICKER_PLACEMENT, SpacePicker, spacePickerMenuOptions } from "./space-picker";

vi.mock("next-intl", () => ({
  // Trigger accessible name includes current space (picker_aria); find button by space name.
  useTranslations: () => (k: string, values?: { name?: string }) =>
    values?.name ? `${k}:${values.name}` : k,
}));

const spaceMocks = vi.hoisted(() => ({
  setActiveId: vi.fn(),
  items: [
    { id: "ws_1", name: "万达门店", desc: "" },
    { id: "ws_2", name: "华东区", desc: "" },
    { id: "ws_3", name: "华南区", desc: "" },
  ] as Array<{ id: string; name: string; desc: string }>,
}));

vi.mock("@/lib/use-space", () => ({
  useSpace: () => ({
    items: spaceMocks.items,
    activeId: "ws_2",
    activeName: "华东区",
    loading: false,
    error: null,
    setActiveId: spaceMocks.setActiveId,
    refresh: vi.fn(),
  }),
}));

beforeEach(() => {
  spaceMocks.setActiveId.mockReset();
  spaceMocks.items = [
    { id: "ws_1", name: "万达门店", desc: "" },
    { id: "ws_2", name: "华东区", desc: "" },
    { id: "ws_3", name: "华南区", desc: "" },
  ];
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function trigger() {
  return screen.getByRole("button", { name: /华东区/ });
}

async function openByKeyboard() {
  fireEvent.click(trigger(), { detail: 0 });
  const menu = await screen.findByRole("menu");
  await waitFor(() => expect(menu.contains(document.activeElement)).toBe(true));
  return menu;
}

describe("SpacePicker menu", () => {
  it("focuses the active space on a keyboard open and moves with the arrow keys", async () => {
    render(<SpacePicker compact />);
    const menu = await openByKeyboard();

    const items = screen.getAllByRole("menuitemradio");
    expect(document.activeElement).toBe(items[1]);
    expect(items[1].getAttribute("aria-checked")).toBe("true");
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(document.activeElement).toBe(items[2]);
    fireEvent.keyDown(menu, { key: "Home" });
    expect(document.activeElement).toBe(items[0]);
  });

  it("switches space on Enter and returns focus to the chip", async () => {
    render(<SpacePicker compact />);
    const menu = await openByKeyboard();

    fireEvent.keyDown(menu, { key: "ArrowUp" });
    fireEvent.click(document.activeElement as HTMLElement);

    await waitFor(() => expect(spaceMocks.setActiveId).toHaveBeenCalledWith("ws_1"));
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    expect(document.activeElement).toBe(trigger());
  });

  it("keeps the current space when the draft editor cancels leaving", async () => {
    const unregister = registerSubagentDraftNavigationGuard({ confirmLeave: async () => false });
    try {
      render(<SpacePicker compact />);
      await openByKeyboard();
      fireEvent.click(screen.getAllByRole("menuitemradio")[0]);
      await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
      expect(spaceMocks.setActiveId).not.toHaveBeenCalled();
    } finally {
      unregister();
    }
  });

  it("closes on Escape and returns focus to the chip", async () => {
    render(<SpacePicker compact />);
    const menu = await openByKeyboard();

    fireEvent.keyDown(menu, { key: "Escape" });

    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger()));
    expect(spaceMocks.setActiveId).not.toHaveBeenCalled();
  });

  it("closes on a press outside", async () => {
    render(
      <>
        <p>Outside</p>
        <SpacePicker compact />
      </>,
    );
    fireEvent.click(trigger());
    expect(await screen.findByRole("menu")).toBeTruthy();

    fireEvent.pointerDown(screen.getByText("Outside"));

    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });

  it("keeps typing in the search box and reaches the list with ArrowDown", async () => {
    spaceMocks.items = Array.from({ length: 10 }, (_, index) => ({
      id: `ws_${index + 1}`,
      name: index === 1 ? "华东区" : `Space ${index + 1}`,
      desc: "",
    }));
    render(<SpacePicker compact />);
    fireEvent.click(trigger());
    const search = await screen.findByRole("searchbox");
    search.focus();

    // Letters must not be intercepted by typeahead; leave Home/End to the cursor.
    fireEvent.keyDown(search, { key: "s" });
    fireEvent.keyDown(search, { key: "End" });
    expect(document.activeElement).toBe(search);

    fireEvent.keyDown(search, { key: "ArrowDown" });
    expect(document.activeElement?.getAttribute("role")).toBe("menuitemradio");
  });

  it("keeps focus in the search box while filtering moves the current space in the list", async () => {
    // Current space is fifth; typing “华” makes it first filtered result. Never pull focus onto a menu item.
    spaceMocks.items = Array.from({ length: 10 }, (_, index) => ({
      id: index === 4 ? "ws_2" : `ws_x${index}`,
      name: index === 4 ? "华东区" : `Space ${index + 1}`,
      desc: "",
    }));
    render(<SpacePicker compact />);
    fireEvent.click(trigger());
    const search = await screen.findByRole("searchbox");
    await new Promise((resolve) => setTimeout(resolve, 20));
    search.focus();

    for (const value of ["华", "华东", "华", ""]) {
      fireEvent.change(search, { target: { value } });
      await new Promise((resolve) => setTimeout(resolve, 20));
      expect(document.activeElement).toBe(search);
    }
    expect(screen.getAllByRole("menuitemradio")).toHaveLength(10);

    // After filtering, arrows still enter filtered results from search.
    fireEvent.change(search, { target: { value: "华" } });
    await new Promise((resolve) => setTimeout(resolve, 20));
    fireEvent.keyDown(search, { key: "ArrowDown" });
    expect(document.activeElement?.textContent).toContain("华东区");
  });
});

describe("SpacePicker menu positioning", () => {
  function stubViewport(width: number, height: number) {
    Object.defineProperty(document.documentElement, "clientWidth", { value: width, configurable: true });
    Object.defineProperty(document.documentElement, "clientHeight", { value: height, configurable: true });
    vi.stubGlobal("innerWidth", width);
    vi.stubGlobal("innerHeight", height);
    vi.stubGlobal("visualViewport", undefined);
  }

  function element(rect: { x: number; y: number; width: number; height: number }) {
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

  async function place(chip: HTMLElement, menu: HTMLElement) {
    return computePosition(chip, menu, {
      placement: SPACE_PICKER_PLACEMENT,
      strategy: "fixed",
      middleware: menuMiddleware(spacePickerMenuOptions(true)),
    });
  }

  it("stays inside the right edge when the chip sits near it", async () => {
    // 116: chip on Topbar right; old left = chip.left made the 260px menu extend offscreen.
    stubViewport(1280, 800);
    const chip = element({ x: 1200, y: 14, width: 72, height: 22 });
    const menu = element({ x: 0, y: 0, width: 260, height: 200 });

    const result = await place(chip, menu);

    expect(result.x + 260).toBeLessThanOrEqual(1280 - 8);
    expect(result.x).toBeGreaterThanOrEqual(8);
    expect(Number.parseFloat(menu.style.maxWidth)).toBeGreaterThanOrEqual(260);
  });

  it("opens downward from the top bar", async () => {
    stubViewport(1280, 800);
    const chip = element({ x: 200, y: 14, width: 72, height: 22 });
    const menu = element({ x: 0, y: 0, width: 260, height: 200 });

    const result = await place(chip, menu);

    expect(result.placement).toBe("bottom-start");
    expect(result.y).toBe(36 + 6);
  });

  it("prefers opening downward when both sides have room", async () => {
    stubViewport(1280, 800);
    const chip = element({ x: 200, y: 300, width: 72, height: 22 });
    const menu = element({ x: 0, y: 0, width: 260, height: 200 });

    const result = await place(chip, menu);

    expect(result.placement).toBe("bottom-start");
  });

  it("opens upward when the chip is at the bottom of the screen", async () => {
    stubViewport(1280, 800);
    const chip = element({ x: 20, y: 740, width: 72, height: 22 });
    const menu = element({ x: 0, y: 0, width: 260, height: 200 });

    const result = await place(chip, menu);

    expect(result.placement.startsWith("top")).toBe(true);
    expect(result.y + 200).toBe(740 - 6);
  });

  it("caps the height at 360px", async () => {
    stubViewport(1280, 1200);
    const chip = element({ x: 200, y: 14, width: 72, height: 22 });
    const menu = element({ x: 0, y: 0, width: 260, height: 1000 });

    await place(chip, menu);

    expect(menu.style.maxHeight).toBe("360px");
  });
});

// Behavior batch A: the menu has bottom keyboard hints (desktop-pointer CSS only), spaces with descriptions carry markers.
describe("SpacePicker 键盘提示条与说明标记（行为批 A）", () => {
  it("shows the keyboard hint bar and marks spaces that have a description", async () => {
    spaceMocks.items = [
      { id: "ws_1", name: "万达门店", desc: "商超 / 卖场数据" },
      { id: "ws_2", name: "华东区", desc: "" },
    ];
    render(<SpacePicker />);
    const menu = await openByKeyboard();
    const hints = menu.querySelector(".ui-menu-hints");
    expect(hints).not.toBeNull();
    expect(hints!.getAttribute("aria-hidden")).toBe("true");
    expect(hints!.textContent).toBe("↑↓hint_select↵hint_switcheschint_close");
    expect(menu.querySelector("[data-menu-description]")).not.toBeNull();
  });
});
