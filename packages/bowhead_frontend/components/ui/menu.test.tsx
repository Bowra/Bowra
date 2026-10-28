/**
 * Floating-menu interaction contract (component unification step 4, docs/design/组件统一-技术设计.md “Menu”).
 *
 * jsdom lacks layout/inert; calculate positioning directly via floating-ui computePosition and stubbed rectangles
 * (see the “定位” group); inert only enforces menu outside marked subtree, real browsers verify effects.
 */
import { computePosition, type Placement } from "@floating-ui/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";

import { DialogShell } from "./dialog";
import { MenuFloating, menuMiddleware, useMenu, useMenuItem, type MenuItemRole } from "./menu";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

interface ItemSpec {
  label: string;
  disabled?: boolean;
  role?: MenuItemRole;
  checked?: boolean;
  onSelect?: () => void;
}

function Item({ label, disabled, role, checked, onSelect }: ItemSpec) {
  const props = useMenuItem({ disabled, role, checked, onSelect });
  return (
    <button type="button" className="app-floating-menu-item" disabled={disabled} {...props}>
      {label}
    </button>
  );
}

function TestMenu({
  items,
  onOpenChange,
  children,
  selectedIndex,
  returnFocusFallback,
  triggerDisabled,
}: {
  items: ItemSpec[];
  onOpenChange?: (open: boolean) => void;
  children?: React.ReactNode;
  selectedIndex?: number;
  returnFocusFallback?: () => HTMLElement | null;
  triggerDisabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const menu = useMenu({
    open,
    onOpenChange: (next) => {
      onOpenChange?.(next);
      setOpen(next);
    },
    selectedIndex,
    returnFocusFallback,
  });
  return (
    <>
      <button type="button" disabled={triggerDisabled} ref={menu.refs.setReference} {...menu.getReferenceProps()}>
        Actions
      </button>
      <MenuFloating menu={menu}>
        <div
          ref={menu.refs.setFloating}
          {...menu.getFloatingProps({ className: "app-floating-menu", "aria-label": "Actions" })}
        >
          {items.map((item) => (
            <Item key={item.label} {...item} />
          ))}
        </div>
      </MenuFloating>
      {children}
    </>
  );
}

const ABC: ItemSpec[] = [{ label: "Archive" }, { label: "Duplicate" }, { label: "Export" }, { label: "Rename" }];

async function openByKeyboard() {
  // Keyboard Enter/Space click events have detail 0.
  fireEvent.click(screen.getByRole("button", { name: "Actions" }), { detail: 0 });
  const first = screen.getAllByRole("menuitem")[0];
  await waitFor(() => expect(document.activeElement).toBe(first));
  return screen.getByRole("menu");
}

describe("useMenu appearance hook", () => {
  it("adds ui-menu to the container and keeps the caller's class", async () => {
    // UI-refresh batch 3 unifies appearance through .ui-menu; retain caller's legacy class.
    renderIntl(<TestMenu items={ABC} />);
    const menu = await openByKeyboard();
    expect(menu.classList.contains("ui-menu")).toBe(true);
    expect(menu.classList.contains("app-floating-menu")).toBe(true);
  });
});

describe("useMenu keyboard", () => {
  it("focuses the first item after a keyboard open and the container after a pointer open", async () => {
    renderIntl(<TestMenu items={ABC} />);
    await openByKeyboard();

    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());

    fireEvent.click(screen.getByRole("button", { name: "Actions" }), { detail: 1 });
    const menu = screen.getByRole("menu");
    await waitFor(() => expect(document.activeElement).toBe(menu));
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Archive" }));
  });

  it("moves with arrows, wraps at both ends, and jumps with Home/End", async () => {
    renderIntl(<TestMenu items={ABC} />);
    const menu = await openByKeyboard();
    const name = () => document.activeElement?.textContent;

    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(name()).toBe("Duplicate");
    fireEvent.keyDown(menu, { key: "End" });
    expect(name()).toBe("Rename");
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(name()).toBe("Archive");
    fireEvent.keyDown(menu, { key: "ArrowUp" });
    expect(name()).toBe("Rename");
    fireEvent.keyDown(menu, { key: "Home" });
    expect(name()).toBe("Archive");
  });

  it("skips disabled items", async () => {
    renderIntl(
      <TestMenu items={[{ label: "Archive" }, { label: "Duplicate", disabled: true }, { label: "Export" }]} />,
    );
    const menu = await openByKeyboard();

    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(document.activeElement?.textContent).toBe("Export");
    fireEvent.keyDown(menu, { key: "ArrowUp" });
    expect(document.activeElement?.textContent).toBe("Archive");
  });

  it("jumps to the item that starts with the typed character", async () => {
    renderIntl(<TestMenu items={ABC} />);
    const menu = await openByKeyboard();

    fireEvent.keyDown(menu, { key: "r" });
    expect(document.activeElement?.textContent).toBe("Rename");
    fireEvent.keyDown(menu, { key: "e" });
    // Still within 750ms: concatenate "re" to match Rename, never jump to Export.
    expect(document.activeElement?.textContent).toBe("Rename");
  });

  it("uses a roving tabindex so Tab leaves the menu instead of walking every item", async () => {
    renderIntl(<TestMenu items={ABC} />);
    await openByKeyboard();

    const tabbable = screen.getAllByRole("menuitem").filter((item) => item.tabIndex === 0);
    expect(tabbable.map((item) => item.textContent)).toEqual(["Archive"]);
  });
});

describe("useMenu dismissal", () => {
  it("closes on Escape and returns focus to the trigger", async () => {
    renderIntl(<TestMenu items={ABC} />);
    const menu = await openByKeyboard();

    fireEvent.keyDown(menu, { key: "Escape" });

    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Actions" })));
  });

  it("closes on a press outside and stays open on a press inside", async () => {
    renderIntl(
      <TestMenu items={ABC}>
        <p>Outside</p>
      </TestMenu>,
    );
    await openByKeyboard();

    fireEvent.pointerDown(screen.getByRole("menuitem", { name: "Duplicate" }));
    expect(screen.getByRole("menu")).toBeTruthy();

    fireEvent.pointerDown(screen.getByText("Outside"));
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });
});

describe("useMenu selected item", () => {
  it("focuses the selected item instead of the first one after a keyboard open", async () => {
    renderIntl(<TestMenu items={ABC} selectedIndex={2} />);
    fireEvent.click(screen.getByRole("button", { name: "Actions" }), { detail: 0 });

    await waitFor(() => expect(document.activeElement?.textContent).toBe("Export"));
  });

  it("also focuses the selected item after a pointer open", async () => {
    renderIntl(<TestMenu items={ABC} selectedIndex={2} />);
    fireEvent.click(screen.getByRole("button", { name: "Actions" }), { detail: 1 });

    await waitFor(() => expect(document.activeElement?.textContent).toBe("Export"));
  });
});

describe("useMenu return focus fallback", () => {
  function FallbackHarness({ showMenu = true, triggerDisabled = false }: { showMenu?: boolean; triggerDisabled?: boolean }) {
    return (
      <>
        <textarea aria-label="Composer input" />
        {showMenu ? (
          <TestMenu
            items={ABC}
            triggerDisabled={triggerDisabled}
            returnFocusFallback={() => screen.getByRole("textbox", { name: "Composer input" })}
          >
            <p>Outside</p>
          </TestMenu>
        ) : null}
      </>
    );
  }

  it("hands focus to the fallback when the menu unmounts together with its trigger", async () => {
    const { rerender } = render(<FallbackHarness />);
    await openByKeyboard();

    rerender(<FallbackHarness showMenu={false} />);

    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "Composer input" })));
  });

  it("hands focus to the fallback when the trigger is disabled while the menu closes", async () => {
    const { rerender } = render(<FallbackHarness />);
    const menu = await openByKeyboard();

    rerender(<FallbackHarness triggerDisabled />);
    fireEvent.keyDown(menu, { key: "Escape" });

    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "Composer input" })));
  });

  it("does not take focus after a press outside", async () => {
    const { rerender } = render(<FallbackHarness />);
    await openByKeyboard();
    rerender(<FallbackHarness triggerDisabled />);

    fireEvent.pointerDown(screen.getByText("Outside"));
    (document.activeElement as HTMLElement).blur();
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });

    // Outside click moves user attention; never force focus back into input.
    expect(document.activeElement).not.toBe(screen.getByRole("textbox", { name: "Composer input" }));
  });
});

describe("useMenu escape ownership", () => {
  it("stops later native listeners on the React root from also handling the Escape it consumed", async () => {
    // Next mounts React root on document, where ancestor dialog useDismiss also listens; RTL container
    // simulates listener on same node registered later.
    const { container } = renderIntl(<TestMenu items={ABC} />);
    await openByKeyboard();
    const late = vi.fn();
    container.addEventListener("keydown", late);

    const trigger = screen.getByRole("button", { name: "Actions" });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "Escape" });

    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    expect(late).not.toHaveBeenCalled();

    // Closed menu does not own Esc; propagate normally.
    fireEvent.keyDown(trigger, { key: "Escape" });
    expect(late).toHaveBeenCalledTimes(1);
  });
});

describe("useMenu selection", () => {
  it("returns focus to the trigger and closes before running onSelect", async () => {
    const order: string[] = [];
    let focusedDuringSelect: Element | null = null;
    let menuOpenDuringSelect: boolean | null = null;
    renderIntl(
      <TestMenu
        items={[
          {
            label: "Archive",
            onSelect: () => {
              order.push("select");
              focusedDuringSelect = document.activeElement;
              menuOpenDuringSelect = screen.queryByRole("menu") !== null;
            },
          },
        ]}
        onOpenChange={(open) => order.push(open ? "open" : "close")}
      />,
    );
    await openByKeyboard();

    fireEvent.click(screen.getByRole("menuitem", { name: "Archive" }));

    expect(order).toEqual(["open", "close", "select"]);
    expect(focusedDuringSelect).toBe(screen.getByRole("button", { name: "Actions" }));
    // Close requested; DOM unmounts only after this event finishes.
    expect(menuOpenDuringSelect).toBe(true);
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });

  it("gives a dialog opened from a menu item the trigger as its return target", async () => {
    function MenuThatOpensDialog() {
      const [dialogOpen, setDialogOpen] = useState(false);
      return (
        <TestMenu items={[{ label: "Delete report", onSelect: () => setDialogOpen(true) }]}>
          {dialogOpen ? (
            <DialogShell label="Confirm" onClose={() => setDialogOpen(false)}>
              <button type="button">Cancel</button>
            </DialogShell>
          ) : null}
        </TestMenu>
      );
    }
    renderIntl(<MenuThatOpensDialog />);
    await openByKeyboard();

    fireEvent.click(screen.getByRole("menuitem", { name: "Delete report" }));
    const cancel = await screen.findByRole("button", { name: "Cancel" });
    await waitFor(() => expect(document.activeElement).toBe(cancel));
    // Menu unmount focus return must not steal focus from new dialog.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 30));
    });
    expect(document.activeElement).toBe(cancel);

    fireEvent.keyDown(cancel, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Actions" })));
  });

  it("marks checkable items with aria-checked", async () => {
    renderIntl(
      <TestMenu
        items={[
          { label: "Compact", role: "menuitemradio", checked: true },
          { label: "Comfortable", role: "menuitemradio", checked: false },
        ]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Actions" }), { detail: 0 });

    const compact = await screen.findByRole("menuitemradio", { name: "Compact" });
    expect(compact.getAttribute("aria-checked")).toBe("true");
    expect(screen.getByRole("menuitemradio", { name: "Comfortable" }).getAttribute("aria-checked")).toBe("false");
  });
});

describe("useMenu inside a dialog", () => {
  function MenuInDialog({ onDialogClose, onSelect }: { onDialogClose: () => void; onSelect: () => void }) {
    return (
      <DialogShell label="Share" onClose={onDialogClose}>
        <button type="button">Copy link</button>
        <TestMenu items={[{ label: "Archive", onSelect }, { label: "Export" }]} />
      </DialogShell>
    );
  }

  it("is not hidden from the dialog's background marking and can be selected", async () => {
    const onDialogClose = vi.fn();
    const onSelect = vi.fn();
    renderIntl(<MenuInDialog onDialogClose={onDialogClose} onSelect={onSelect} />);
    await openByKeyboard();

    const menu = screen.getByRole("menu");
    expect(menu.closest("[inert], [aria-hidden='true'], [data-floating-ui-inert]")).toBeNull();

    fireEvent.pointerDown(screen.getByRole("menuitem", { name: "Archive" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Archive" }));
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onDialogClose).not.toHaveBeenCalled();
  });

  it("closes only the menu on the first Escape and the dialog on the second", async () => {
    const onDialogClose = vi.fn();
    renderIntl(<MenuInDialog onDialogClose={onDialogClose} onSelect={vi.fn()} />);
    const menu = await openByKeyboard();

    fireEvent.keyDown(menu, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    expect(onDialogClose).not.toHaveBeenCalled();

    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Actions" })));
    fireEvent.keyDown(document.activeElement as Element, { key: "Escape" });
    expect(onDialogClose).toHaveBeenCalledTimes(1);
  });

  it("keeps the dialog open when Escape reaches the document while the menu is open", async () => {
    // Focus outside menu (e.g. Safari click on nonfocusable menu area leaves body) sends keys only to document
    // listener. FloatingTree tells dialog child menu is open; close menu only.
    const onDialogClose = vi.fn();
    renderIntl(<MenuInDialog onDialogClose={onDialogClose} onSelect={vi.fn()} />);
    await openByKeyboard();

    (document.activeElement as HTMLElement).blur();
    fireEvent.keyDown(document, { key: "Escape" });

    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    expect(onDialogClose).not.toHaveBeenCalled();
  });
});

describe("menu positioning", () => {
  function stubViewport(width: number, height: number, visual?: { height: number; offsetTop?: number }) {
    Object.defineProperty(document.documentElement, "clientWidth", { value: width, configurable: true });
    Object.defineProperty(document.documentElement, "clientHeight", { value: height, configurable: true });
    vi.stubGlobal("innerWidth", width);
    vi.stubGlobal("innerHeight", height);
    vi.stubGlobal(
      "visualViewport",
      visual
        ? {
            width,
            height: visual.height,
            offsetLeft: 0,
            offsetTop: visual.offsetTop ?? 0,
            scale: 1,
            addEventListener: () => {},
            removeEventListener: () => {},
          }
        : undefined,
    );
  }

  function element(rect: { x: number; y: number; width: number; height: number }) {
    const el = document.createElement("div");
    el.style.width = `${rect.width}px`;
    el.style.height = `${rect.height}px`;
    // jsdom lacks layout: floating-ui compares CSS width/height with offsetWidth/Height for dimensions.
    Object.defineProperty(el, "offsetWidth", { value: rect.width, configurable: true });
    Object.defineProperty(el, "offsetHeight", { value: rect.height, configurable: true });
    el.getBoundingClientRect = () =>
      ({ ...rect, top: rect.y, left: rect.x, right: rect.x + rect.width, bottom: rect.y + rect.height, toJSON() {} }) as DOMRect;
    document.body.appendChild(el);
    return el;
  }

  async function place(placement: Placement, reference: HTMLElement, floating: HTMLElement) {
    return computePosition(reference, floating, { placement, strategy: "fixed", middleware: menuMiddleware() });
  }

  it("opens a side-placed menu upward when neither side fits on a phone", async () => {
    stubViewport(390, 844);
    const trigger = element({ x: 8, y: 780, width: 374, height: 52 });
    const menu = element({ x: 0, y: 0, width: 256, height: 420 });

    const result = await place("right-end", trigger, menu);

    expect(result.placement.startsWith("top")).toBe(true);
    expect(result.x).toBeGreaterThanOrEqual(8);
    expect(result.x + 256).toBeLessThanOrEqual(390 - 8);
    expect(result.y + 420).toBeLessThanOrEqual(780);
  });

  it("keeps the preferred side on a desktop sidebar", async () => {
    stubViewport(1440, 900);
    // Identity row bottom at viewport bottom: shift handles a few vertical pixels, never flips above for that.
    const trigger = element({ x: 0, y: 831, width: 279, height: 67 });
    const menu = element({ x: 0, y: 0, width: 256, height: 420 });

    const result = await place("right-end", trigger, menu);

    expect(result.placement).toBe("right-end");
  });

  it("clamps into the visual viewport when the soft keyboard pushes the trigger out of view", async () => {
    // Layout viewport height 844; keyboard leaves visible top 400px, trigger at 700px behind keyboard.
    stubViewport(390, 844, { height: 400 });
    const trigger = element({ x: 16, y: 700, width: 120, height: 36 });
    const menu = element({ x: 0, y: 0, width: 236, height: 184 });

    const result = await place("top-start", trigger, menu);

    expect(result.y).toBeGreaterThanOrEqual(8);
    expect(result.y + 184).toBeLessThanOrEqual(400 - 8);
  });

  it("limits the menu height to the available space so it can scroll", async () => {
    stubViewport(390, 500);
    const trigger = element({ x: 16, y: 16, width: 120, height: 36 });
    const menu = element({ x: 0, y: 0, width: 236, height: 900 });

    const result = await place("bottom-start", trigger, menu);

    const maxHeight = Number.parseFloat(menu.style.maxHeight);
    expect(maxHeight).toBeGreaterThan(0);
    expect(result.y).toBeGreaterThanOrEqual(8);
    expect(result.y + maxHeight).toBeLessThanOrEqual(500 - 8);
    expect(menu.style.overflowY).toBe("auto");
  });

  it("sits right above the trigger when a short menu opens upward below its height cap", async () => {
    // 116 device (2026-08-14): plan menu actual height 184px, cap 320; top computed from cap left 142px floating gap.
    stubViewport(1280, 760);
    const trigger = element({ x: 565, y: 692, width: 86, height: 40 });
    const menu = element({ x: 0, y: 0, width: 236, height: 184 });

    const result = await computePosition(trigger, menu, {
      placement: "top-start",
      strategy: "fixed",
      middleware: menuMiddleware({ maxHeight: 320 }),
    });

    expect(result.placement).toBe("top-start");
    expect(result.y + 184).toBe(692 - 6);
    expect(result.x).toBe(565);
  });

  it("caps the menu height when more space is available", async () => {
    stubViewport(390, 900);
    const trigger = element({ x: 16, y: 16, width: 120, height: 36 });
    const menu = element({ x: 0, y: 0, width: 236, height: 900 });

    await computePosition(trigger, menu, {
      placement: "bottom-start",
      strategy: "fixed",
      middleware: menuMiddleware({ maxHeight: 320 }),
    });

    expect(menu.style.maxHeight).toBe("320px");
  });
});

// Layers (design “图层”): real-browser stacking verified in e2e/layer-stacking.spec.ts.
describe("useMenu layer", () => {
  it("sits on the menu layer token by default", async () => {
    render(<TestMenu items={ABC} />);
    const menu = await openByKeyboard();
    expect(menu.style.zIndex).toBe("var(--z-menu)");
  });

  it("lifts above a hand-written overlay whose root layer is higher than the menu layer", async () => {
    render(
      <div style={{ position: "fixed", zIndex: 20000 }}>
        <TestMenu items={ABC} />
      </div>,
    );
    const menu = await openByKeyboard();
    await waitFor(() => expect(menu.style.zIndex).toBe("20001"));
  });
});
