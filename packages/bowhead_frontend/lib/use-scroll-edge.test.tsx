/**
 * Top-bar scroll edge: set data-scrolled when a main-content scroll container leaves the top, remove it on return;
 * ignore scrolling outside main content (body-mounted overlays) or inside the top bar itself.
 */
import { act, render } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SCROLL_EDGE_SCOPE_ID, useScrollEdge } from "./use-scroll-edge";

function Bar() {
  const ref = useRef<HTMLDivElement>(null);
  useScrollEdge(ref);
  return (
    <div ref={ref} data-testid="bar">
      <div data-testid="inside" />
    </div>
  );
}

function scrollTo(el: Element, top: number) {
  Object.defineProperty(el, "scrollTop", { configurable: true, value: top });
  act(() => {
    el.dispatchEvent(new Event("scroll"));
  });
}

afterEach(() => {
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

describe("useScrollEdge", () => {
  it("主内容区里任一滚动容器离开顶部时写 data-scrolled，都回到顶部时去掉", () => {
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((cb) => {
      cb(0);
      return 1;
    });
    const main = document.createElement("main");
    main.id = SCROLL_EDGE_SCOPE_ID;
    const a = document.createElement("div");
    const b = document.createElement("div");
    main.append(a, b);
    document.body.append(main);
    const { getByTestId } = render(<Bar />, { container: main.appendChild(document.createElement("div")) });
    const bar = getByTestId("bar");
    expect(bar.hasAttribute("data-scrolled")).toBe(false);

    scrollTo(a, 120);
    expect(bar.getAttribute("data-scrolled")).toBe("true");
    scrollTo(b, 40);
    scrollTo(a, 0);
    expect(bar.getAttribute("data-scrolled")).toBe("true");
    scrollTo(b, 0);
    expect(bar.hasAttribute("data-scrolled")).toBe(false);
  });

  it("滚动容器被整个替换（切换会话）后属性复位；值没变时不重复写属性", async () => {
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((cb) => {
      cb(0);
      return 1;
    });
    const main = document.createElement("main");
    main.id = SCROLL_EDGE_SCOPE_ID;
    const pane = document.createElement("div");
    main.append(pane);
    document.body.append(main);
    const { getByTestId } = render(<Bar />, { container: main.appendChild(document.createElement("div")) });
    const bar = getByTestId("bar");
    const set = vi.spyOn(bar, "setAttribute");
    scrollTo(pane, 500);
    scrollTo(pane, 600);
    expect(bar.getAttribute("data-scrolled")).toBe("true");
    expect(set).toHaveBeenCalledTimes(1);

    await act(async () => {
      pane.replaceWith(document.createElement("div"));
      await Promise.resolve();
    });
    expect(bar.hasAttribute("data-scrolled")).toBe(false);
  });

  it("主内容区以外的滚动（挂到 body 的浮层）与顶栏自己里面的滚动不算", () => {
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((cb) => {
      cb(0);
      return 1;
    });
    const main = document.createElement("main");
    main.id = SCROLL_EDGE_SCOPE_ID;
    document.body.append(main);
    const overlay = document.createElement("div");
    document.body.append(overlay);
    const { getByTestId } = render(<Bar />, { container: main.appendChild(document.createElement("div")) });
    scrollTo(overlay, 200);
    scrollTo(getByTestId("inside"), 200);
    expect(getByTestId("bar").hasAttribute("data-scrolled")).toBe(false);
  });
});
