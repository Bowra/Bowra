/**
 * Behavior batch A (docs/design/组件统一-技术设计.md “行为批 A：菜单”): narrow-screen bottom sheets,
 * submenus (desktop keyboard/hover, narrow-screen drill-down), disabled highlight without execution, dangerous-item “…”.
 *
 * jsdom has no layout: sheet positioning, grabber/rounded-list appearance and safe-triangle geometry need real-browser checks
 * (e2e/menu-behavior.spec.ts). Here enforce thresholds, presentation switching, focus and keyboard contracts.
 */
import { useId, useState, type ReactNode } from "react";
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { lockBodyScroll } from "@/lib/body-scroll-lock";

import {
  MENU_SHEET_MEDIA_QUERY,
  MenuFloating,
  MenuItemReason,
  MenuSubmenu,
  menuNeedsSheet,
  SUBMENU_HOVER_DELAY_MS,
  useMenu,
  useMenuItem,
  withConfirmEllipsis,
  type MenuPresentationOption,
} from "./menu";

let narrow = false;

function stubMatchMedia() {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches: query === MENU_SHEET_MEDIA_QUERY ? narrow : false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  );
}

beforeEach(() => {
  narrow = false;
  stubMatchMedia();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

interface ItemSpec {
  label: string;
  disabled?: boolean;
  reason?: string;
  description?: string;
  onSelect?: () => void;
}

function Item({ label, disabled, reason, description, onSelect }: ItemSpec) {
  const props = useMenuItem({ disabled, onSelect, label });
  const reasonId = useId();
  const showReason = disabled && reason;
  return (
    <>
      <button
        type="button"
        className="app-floating-menu-item"
        aria-describedby={showReason ? reasonId : undefined}
        {...props}
      >
        <span>{label}</span>
        {description ? <span data-menu-description="">{description}</span> : null}
      </button>
      {showReason ? <MenuItemReason id={reasonId}>{reason}</MenuItemReason> : null}
    </>
  );
}

function TestMenu({
  items,
  presentation,
  sheetTitle,
  extra,
  label = "Actions",
}: {
  items: ItemSpec[];
  presentation?: MenuPresentationOption;
  sheetTitle?: string;
  extra?: ReactNode;
  label?: string;
}) {
  const [open, setOpen] = useState(false);
  const menu = useMenu({ open, onOpenChange: setOpen, presentation, sheetTitle });
  return (
    <>
      <button type="button" ref={menu.refs.setReference} {...menu.getReferenceProps()}>
        Open
      </button>
      <MenuFloating menu={menu}>
        <div ref={menu.refs.setFloating} {...menu.getFloatingProps({ className: "app-floating-menu", "aria-label": label })}>
          {items.map((item) => (
            <Item key={item.label} {...item} />
          ))}
          {extra}
        </div>
      </MenuFloating>
    </>
  );
}

const labels = (count: number): ItemSpec[] => Array.from({ length: count }, (_, i) => ({ label: `Item ${i + 1}` }));

function openMenu() {
  fireEvent.click(screen.getByRole("button", { name: "Open" }));
  return screen.getByRole("menu");
}

function Option({ label, checked, onPick }: { label: string; checked?: boolean; onPick: (label: string) => void }) {
  const props = useMenuItem({ role: "menuitemradio", checked, label, onSelect: () => onPick(label) });
  return (
    <button type="button" className="app-floating-menu-item" {...props}>
      {label}
    </button>
  );
}

function LanguageSubmenu({ onPick }: { onPick: (label: string) => void }) {
  return (
    <MenuSubmenu label="Language" value="中文" className="app-floating-menu-item" selectedIndex={0}>
      <Option label="中文（简体）" checked onPick={onPick} />
      <Option label="English" onPick={onPick} />
    </MenuSubmenu>
  );
}

describe("门槛判定（已定取舍 8）", () => {
  function container(html: string) {
    const div = document.createElement("div");
    div.innerHTML = html;
    return div;
  }
  const item = (extra = "") => `<button role="menuitem"${extra}>x</button>`;

  it("超过 6 项才算长菜单", () => {
    expect(menuNeedsSheet(container(item().repeat(6)))).toBe(false);
    expect(menuNeedsSheet(container(item().repeat(7)))).toBe(true);
  });

  it("任一项有说明文字、或有子菜单即达门槛；禁用原因不算说明", () => {
    expect(menuNeedsSheet(container(item() + `<button role="menuitem"><span data-menu-description>d</span></button>`))).toBe(true);
    expect(menuNeedsSheet(container(item(' aria-haspopup="menu"')))).toBe(true);
    expect(menuNeedsSheet(container(item() + `<span class="ui-menu-reason">r</span>`))).toBe(false);
    expect(menuNeedsSheet(container(item(' aria-haspopup="dialog"')))).toBe(false);
  });

  it("按角色数菜单项（option、menuitemradio 同样计入）", () => {
    expect(menuNeedsSheet(container(`<div role="option">a</div>`.repeat(4) + `<div role="menuitemradio">b</div>`.repeat(3)))).toBe(true);
  });
});

describe("narrow-screen subscription", () => {
  it("the menu subscribes to the narrow-screen media query", async () => {
    const matchMedia = window.matchMedia as unknown as ReturnType<typeof vi.fn>;
    renderIntl(<TestMenu items={labels(9)} />);
    await act(async () => {});
    expect(matchMedia).toHaveBeenCalledWith(MENU_SHEET_MEDIA_QUERY);
  });
});

describe("底部面板是模态，滚动锁带计数", () => {
  afterEach(() => document.body.style.removeProperty("overflow"));

  it("面板打开时背景 inert、Tab 留在面板里；关闭后解除并回焦", async () => {
    narrow = true;
    renderIntl(
      <>
        <button type="button">背景按钮</button>
        <TestMenu items={labels(8)} />
      </>,
    );
    const trigger = screen.getByRole("button", { name: "Open" });
    trigger.focus();
    fireEvent.click(trigger, { detail: 0 });
    const menu = screen.getByRole("menu");
    await waitFor(() => expect(menu.className).toContain("is-sheet"));
    const background = screen.getByText("背景按钮");
    // jsdom lacks inert; floating-ui falls back to aria-hidden (real browsers use inert; see e2e/menu-behavior.spec.ts).
    const HIDDEN = "[inert], [aria-hidden='true']";
    await waitFor(() => expect(background.closest(HIDDEN)).not.toBeNull());
    const sheet = document.querySelector<HTMLElement>(".ui-menu-sheet")!;
    expect(sheet.closest(HIDDEN)).toBeNull();
    expect(document.querySelector(".ui-menu-sheet-scrim")!.closest(HIDDEN)).toBeNull();
    await waitFor(() => expect(menu.contains(document.activeElement)).toBe(true));
    const tab = fireEvent.keyDown(document.activeElement!, { key: "Tab" });
    expect(tab).toBe(false);
    expect(sheet.contains(document.activeElement)).toBe(true);
    fireEvent.keyDown(document.activeElement!, { key: "Tab", shiftKey: true });
    expect(sheet.contains(document.activeElement)).toBe(true);

    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(background.closest(HIDDEN)).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("浮层形态（短菜单）仍是非模态：背景不 inert、不锁滚动", async () => {
    narrow = true;
    renderIntl(
      <>
        <button type="button">背景按钮</button>
        <TestMenu items={labels(3)} />
      </>,
    );
    const menu = openMenu();
    await act(async () => {});
    expect(menu.className).not.toContain("is-sheet");
    expect(screen.getByText("背景按钮").closest("[inert], [aria-hidden='true']")).toBeNull();
    expect(document.body.style.getPropertyValue("overflow")).toBe("");
  });

  it("面板开着时锁住页面滚动；在已加锁的抽屉里开关面板，抽屉的锁仍在", async () => {
    narrow = true;
    renderIntl(<TestMenu items={labels(8)} />);
    const trigger = screen.getByRole("button", { name: "Open" });

    fireEvent.click(trigger);
    await waitFor(() => expect(document.querySelector(".ui-menu-sheet")).not.toBeNull());
    expect(document.body.style.getPropertyValue("overflow")).toBe("hidden");
    // On narrow screens, globals.css permits body scrolling with !important; the sheet lock needs important to override it.
    expect(document.body.style.getPropertyPriority("overflow")).toBe("important");
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    expect(document.body.style.getPropertyValue("overflow")).toBe("");

    const releaseDrawer = lockBodyScroll();
    fireEvent.click(trigger);
    await waitFor(() => expect(document.querySelector(".ui-menu-sheet")).not.toBeNull());
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    expect(document.body.style.getPropertyValue("overflow")).toBe("hidden");
    releaseDrawer();
    expect(document.body.style.getPropertyValue("overflow")).toBe("");
  });
});

describe("窄屏底部面板", () => {
  it("narrow screen + long menu: bottom sheet (scrim, grab handle, menu with is-sheet, no floating positioning)", async () => {
    narrow = true;
    renderIntl(<TestMenu items={labels(7)} sheetTitle="Actions sheet" />);
    const menu = openMenu();
    await waitFor(() => expect(menu.className).toContain("is-sheet"));
    const sheet = menu.closest(".ui-menu-sheet");
    expect(sheet).not.toBeNull();
    expect(sheet!.querySelector(".ui-menu-sheet-grab")).not.toBeNull();
    expect(sheet!.querySelector(".ui-menu-sheet-title")?.textContent).toBe("Actions sheet");
    expect(document.querySelector(".ui-menu-sheet-scrim")).not.toBeNull();
    expect(menu.style.position).toBe("");
    expect(menu.style.zIndex).toBe("");
  });

  it("短菜单（≤ 6 项、无说明、无子菜单）仍是浮层：宿主不占盒子，菜单照旧定位", async () => {
    narrow = true;
    renderIntl(<TestMenu items={labels(6)} />);
    const menu = openMenu();
    await act(async () => {});
    expect(menu.className).not.toContain("is-sheet");
    expect(menu.closest(".ui-menu-sheet-host")).not.toBeNull();
    expect(document.querySelector(".ui-menu-sheet, .ui-menu-sheet-scrim")).toBeNull();
    expect(menu.style.position).toBe("fixed");
  });

  it("有说明文字的短菜单也改底部面板", async () => {
    narrow = true;
    renderIntl(<TestMenu items={[{ label: "Auto", description: "Plan only when needed" }, { label: "Off" }]} />);
    const menu = openMenu();
    await waitFor(() => expect(menu.className).toContain("is-sheet"));
  });

  it("调用点可以显式覆盖门槛", async () => {
    narrow = true;
    const { unmount } = renderIntl(<TestMenu items={labels(2)} presentation="sheet" />);
    await waitFor(() => expect(openMenu().className).toContain("is-sheet"));
    unmount();
    renderIntl(<TestMenu items={labels(9)} presentation="popover" />);
    const menu = openMenu();
    await act(async () => {});
    expect(menu.className).not.toContain("is-sheet");
  });

  it("on a wide screen a long menu stays a floating menu without a sheet host", async () => {
    renderIntl(<TestMenu items={labels(9)} />);
    const menu = openMenu();
    await act(async () => {});
    expect(menu.className).toBe("ui-menu app-floating-menu");
    expect(menu.closest(".ui-menu-sheet-host, .ui-menu-sheet")).toBeNull();
  });

  it("Esc 关闭并回到触发按钮；点遮罩关闭；按在抓手上不算点外部", async () => {
    narrow = true;
    renderIntl(<TestMenu items={labels(8)} />);
    const trigger = screen.getByRole("button", { name: "Open" });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "Enter" });
    fireEvent.click(trigger, { detail: 0 });
    const menu = screen.getByRole("menu");
    await waitFor(() => expect(menu.className).toContain("is-sheet"));
    await waitFor(() => expect(menu.contains(document.activeElement)).toBe(true));

    fireEvent.pointerDown(document.querySelector(".ui-menu-sheet-grab")!, { button: 0, pointerId: 1, clientY: 100 });
    fireEvent.mouseDown(document.querySelector(".ui-menu-sheet-grab")!);
    expect(screen.queryByRole("menu")).not.toBeNull();

    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));

    fireEvent.click(trigger);
    await waitFor(() => expect(document.querySelector(".ui-menu-sheet-scrim")).not.toBeNull());
    fireEvent.pointerDown(document.querySelector(".ui-menu-sheet-scrim")!);
    fireEvent.click(document.querySelector(".ui-menu-sheet-scrim")!);
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });

  it("抓手下滑超过阈值关闭；拖一点松手弹回", async () => {
    narrow = true;
    renderIntl(<TestMenu items={labels(8)} />);
    const menu = openMenu();
    await waitFor(() => expect(menu.className).toContain("is-sheet"));
    const grab = document.querySelector(".ui-menu-sheet-grab")!;
    const sheet = document.querySelector<HTMLElement>(".ui-menu-sheet")!;

    fireEvent.pointerDown(grab, { button: 0, pointerId: 1, clientY: 100, timeStamp: 0 });
    fireEvent.pointerMove(grab, { pointerId: 1, clientY: 130 });
    expect(sheet.style.transform).toBe("translateY(30px)");
    // Slowly drag a short distance (below the velocity threshold): release bounces back.
    await new Promise((resolve) => setTimeout(resolve, 150));
    fireEvent.pointerUp(grab, { pointerId: 1, clientY: 130, timeStamp: 1000 });
    expect(sheet.style.transform).toBe("");
    expect(screen.queryByRole("menu")).not.toBeNull();

    fireEvent.pointerDown(grab, { button: 0, pointerId: 2, clientY: 100, timeStamp: 2000 });
    fireEvent.pointerMove(grab, { pointerId: 2, clientY: 200 });
    fireEvent.pointerUp(grab, { pointerId: 2, clientY: 200, timeStamp: 3000 });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });
});

/** Open by keyboard (click detail=0 counts as keyboard), wait for first-item focus, then ↓ to “Language”. */
async function focusLanguageItem() {
  openMenu();
  await waitFor(() => expect(document.activeElement?.textContent).toBe("Help"));
  fireEvent.keyDown(document.activeElement!, { key: "ArrowDown" });
  const parent = screen.getByRole("menuitem", { name: /Language/ });
  await waitFor(() => expect(document.activeElement).toBe(parent));
  return parent;
}

const accountMenu = () => document.querySelector<HTMLElement>('[role="menu"][aria-label="Account"]');

describe("子菜单（桌面）", () => {
  function renderAccount(onPick = vi.fn()) {
    renderIntl(
      <TestMenu
        label="Account"
        items={[{ label: "Help" }]}
        extra={<LanguageSubmenu onPick={onPick} />}
      />,
    );
    return onPick;
  }

  it("父项是 menuitem、aria-haspopup=menu，行尾有当前值与 ›", () => {
    renderAccount();
    openMenu();
    const parent = screen.getByRole("menuitem", { name: /Language/ });
    expect(parent.getAttribute("aria-haspopup")).toBe("menu");
    expect(parent.getAttribute("aria-expanded")).toBe("false");
    expect(parent.querySelector(".ui-menu-item-tail")?.textContent).toBe("中文");
    expect(parent.querySelector(".ui-menu-chevron")).not.toBeNull();
  });

  it("→ 打开子菜单并聚焦当前项；← 关闭并回到父项", async () => {
    renderAccount();
    const parent = await focusLanguageItem();
    fireEvent.keyDown(parent, { key: "ArrowRight" });
    const sub = await screen.findByRole("menu", { name: "Language" });
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("menuitemradio", { name: "中文（简体）" })));
    expect(parent.getAttribute("aria-expanded")).toBe("true");
    fireEvent.keyDown(document.activeElement!, { key: "ArrowLeft" });
    await waitFor(() => expect(sub.isConnected).toBe(false));
    await waitFor(() => expect(document.activeElement).toBe(parent));
    expect(accountMenu()).not.toBeNull();
  });

  it("Esc 只关子菜单一级，回到父项；再按 Esc 关整个菜单", async () => {
    renderAccount();
    const parent = await focusLanguageItem();
    fireEvent.keyDown(parent, { key: "ArrowRight" });
    await screen.findByRole("menu", { name: "Language" });
    await waitFor(() => expect(document.activeElement?.getAttribute("role")).toBe("menuitemradio"));
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu", { name: "Language" })).toBeNull());
    expect(accountMenu()).not.toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(parent));
    fireEvent.keyDown(parent, { key: "Escape" });
    await waitFor(() => expect(accountMenu()).toBeNull());
  });

  it("子菜单里的方向键不移动父菜单的高亮", async () => {
    renderAccount();
    const parent = await focusLanguageItem();
    fireEvent.keyDown(parent, { key: "ArrowRight" });
    await screen.findByRole("menu", { name: "Language" });
    await waitFor(() => expect(document.activeElement?.textContent).toBe("中文（简体）"));
    fireEvent.keyDown(document.activeElement!, { key: "ArrowDown" });
    await waitFor(() => expect(document.activeElement?.textContent).toBe("English"));
    expect(screen.queryByRole("menu", { name: "Language" })).not.toBeNull();
  });

  it("子菜单里的首字母不交给父菜单（父菜单有「Help」时按 h 不跳走）", async () => {
    renderAccount();
    const parent = await focusLanguageItem();
    fireEvent.keyDown(parent, { key: "ArrowRight" });
    const sub = await screen.findByRole("menu", { name: "Language" });
    await waitFor(() => expect(sub.contains(document.activeElement)).toBe(true));
    fireEvent.keyDown(document.activeElement!, { key: "h" });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    expect(screen.queryByRole("menu", { name: "Language" })).not.toBeNull();
    expect(sub.contains(document.activeElement)).toBe(true);
  });

  it("悬停 150ms 后才打开", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderAccount();
    openMenu();
    const parent = screen.getByRole("menuitem", { name: /Language/ });
    fireEvent.pointerEnter(parent, { pointerType: "mouse" });
    fireEvent.mouseMove(parent);
    fireEvent.mouseEnter(parent);
    act(() => {
      vi.advanceTimersByTime(SUBMENU_HOVER_DELAY_MS - 20);
    });
    expect(screen.queryByRole("menu", { name: "Language" })).toBeNull();
    act(() => {
      vi.advanceTimersByTime(40);
    });
    expect(screen.queryByRole("menu", { name: "Language" })).not.toBeNull();
  });

  it("在子菜单里选中：执行选择，整个菜单关闭，焦点回到最外层触发按钮", async () => {
    const onPick = renderAccount();
    const trigger = screen.getByRole("button", { name: "Open" });
    const parent = await focusLanguageItem();
    fireEvent.click(parent);
    fireEvent.click(await screen.findByRole("menuitemradio", { name: "English" }));
    expect(onPick).toHaveBeenCalledWith("English");
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });
});

describe("子菜单（窄屏下钻）", () => {
  function renderNarrow(onPick = vi.fn()) {
    narrow = true;
    renderIntl(<TestMenu label="Account" items={[{ label: "Help" }]} extra={<LanguageSubmenu onPick={onPick} />} />);
    return onPick;
  }

  it("有子菜单的菜单在窄屏是底部面板；点父项在同一面板内下钻，顶部「‹ 上一级」", async () => {
    renderNarrow();
    const account = openMenu();
    await waitFor(() => expect(account.className).toContain("is-sheet"));
    fireEvent.click(screen.getByRole("menuitem", { name: /Language/ }));
    const sub = await screen.findByRole("menu", { name: "Language" });
    const sheet = document.querySelector(".ui-menu-sheet")!;
    expect(sheet.contains(sub)).toBe(true);
    expect(sheet.getAttribute("data-drilled")).toBe("true");
    expect(sub.className).toContain("is-drill");
    expect(sub.style.position).toBe("");
    const back = sub.querySelector(".ui-menu-drill-back")!;
    expect(back.textContent).toBe("Account");
    expect(document.querySelectorAll(".ui-menu-sheet").length).toBe(1);
  });

  it("「‹ 上一级」、← 与 Esc 都只返回一级，焦点回到父项", async () => {
    renderNarrow();
    const parent = await focusLanguageItem();
    await waitFor(() => expect(accountMenu()!.className).toContain("is-sheet"));

    fireEvent.click(parent);
    let sub = await screen.findByRole("menu", { name: "Language" });
    fireEvent.click(sub.querySelector(".ui-menu-drill-back")!);
    await waitFor(() => expect(screen.queryByRole("menu", { name: "Language" })).toBeNull());
    expect(document.querySelector(".ui-menu-sheet")!.hasAttribute("data-drilled")).toBe(false);
    await waitFor(() => expect(document.activeElement).toBe(parent));

    fireEvent.keyDown(parent, { key: "ArrowRight" });
    sub = await screen.findByRole("menu", { name: "Language" });
    await waitFor(() => expect(sub.contains(document.activeElement)).toBe(true));
    fireEvent.keyDown(document.activeElement!, { key: "ArrowLeft" });
    await waitFor(() => expect(screen.queryByRole("menu", { name: "Language" })).toBeNull());

    await waitFor(() => expect(document.activeElement).toBe(parent));
    fireEvent.keyDown(parent, { key: "ArrowRight" });
    sub = await screen.findByRole("menu", { name: "Language" });
    await waitFor(() => expect(sub.contains(document.activeElement)).toBe(true));
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu", { name: "Language" })).toBeNull());
    expect(accountMenu()).not.toBeNull();
  });

  it("下钻后在子菜单里选中，整个面板关闭", async () => {
    const onPick = renderNarrow();
    const account = openMenu();
    await waitFor(() => expect(account.className).toContain("is-sheet"));
    fireEvent.click(screen.getByRole("menuitem", { name: /Language/ }));
    fireEvent.click(await screen.findByRole("menuitemradio", { name: "English" }));
    expect(onPick).toHaveBeenCalledWith("English");
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });
});

describe("禁用项", () => {
  const items: ItemSpec[] = [
    { label: "Pin" },
    { label: "Archive", disabled: true, reason: "Running sessions can't be archived" },
    { label: "Delete" },
  ];

  it("disabled items can be highlighted by keyboard, Enter does nothing, the reason is read via aria-describedby", async () => {
    const onSelect = vi.fn();
    items[1].onSelect = onSelect;
    renderIntl(<TestMenu items={items} />);
    const trigger = screen.getByRole("button", { name: "Open" });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    await waitFor(() => expect(document.activeElement?.textContent).toBe("Pin"));
    fireEvent.keyDown(document.activeElement!, { key: "ArrowDown" });
    const archive = screen.getByRole("menuitem", { name: "Archive" });
    await waitFor(() => expect(document.activeElement).toBe(archive));
    expect(archive.getAttribute("aria-disabled")).toBe("true");
    const describedBy = archive.getAttribute("aria-describedby");
    expect(describedBy && document.getElementById(describedBy)?.textContent).toBe("Running sessions can't be archived");
    fireEvent.keyDown(archive, { key: "Enter" });
    fireEvent.click(archive);
    expect(onSelect).not.toHaveBeenCalled();
    expect(screen.getByRole("menu")).toBeTruthy();
  });
});

describe("危险项「…」", () => {
  it("danger items that confirm first end with an ellipsis, added only once", () => {
    expect(withConfirmEllipsis("删除会话")).toBe("删除会话…");
    expect(withConfirmEllipsis("Delete Session")).toBe("Delete Session…");
    expect(withConfirmEllipsis("删除会话…")).toBe("删除会话…");
  });
});
