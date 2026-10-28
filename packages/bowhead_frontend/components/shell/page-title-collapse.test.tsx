import "@testing-library/jest-dom/vitest";

import { act, cleanup, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "./AppShell";
import { PageStateHeading } from "./PageStateHeading";
import { ProductShell } from "./ProductShell";
import { Topbar } from "./Topbar";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));
vi.mock("./global-controls", () => ({
  ShellGlobalControls: () => null,
}));
vi.mock("@/components/unified-sidebar", () => ({
  UnifiedSidebar: () => <nav data-testid="unified-sidebar" />,
}));
vi.mock("@/components/sessions-drawer", () => ({
  SessionsDrawer: () => null,
  useSessionsDrawer: () => ({ open: vi.fn() }),
}));
vi.mock("@/lib/use-app-router", () => ({
  appNavigationAllowed: () => true,
  navigateAppDocument: vi.fn(),
}));
vi.mock("@/lib/use-session-archive-capability", () => ({
  useSessionArchiveCapability: () => true,
}));

type Rect = Pick<DOMRect, "top" | "bottom" | "width" | "height">;

const observers: FakeIntersectionObserver[] = [];

class FakeIntersectionObserver {
  readonly targets = new Set<Element>();
  constructor(
    readonly callback: IntersectionObserverCallback,
    readonly options?: IntersectionObserverInit,
  ) {
    observers.push(this);
  }
  observe(target: Element) {
    this.targets.add(target);
  }
  unobserve(target: Element) {
    this.targets.delete(target);
  }
  disconnect() {
    this.targets.clear();
  }
  takeRecords() {
    return [];
  }
}

/** Report the observed H1 as on screen or scrolled under the top bar. */
function scrollHeading(isIntersecting: boolean) {
  const observer = observers.filter((item) => item.targets.size).at(-1);
  if (!observer) throw new Error("no heading is observed");
  const [target] = observer.targets;
  act(() => {
    observer.callback(
      [{ isIntersecting, target } as IntersectionObserverEntry],
      observer as unknown as IntersectionObserver,
    );
  });
}

const visibleRect: Rect = { top: 60, bottom: 100, width: 320, height: 40 };

beforeEach(() => {
  observers.length = 0;
  vi.stubGlobal("IntersectionObserver", FakeIntersectionObserver);
  // jsdom has no layout: give real headings a box; PageStateHeading's visually hidden H1 keeps its 1px clip box.
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
    const rect: Rect = this.tagName === "H1"
      ? (this as HTMLElement).style.width === "1px" ? { top: 0, bottom: 1, width: 1, height: 1 } : visibleRect
      : { top: 0, bottom: 0, width: 0, height: 0 };
    return { ...rect, left: 0, right: rect.width, x: 0, y: rect.top, toJSON: () => rect } as DOMRect;
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function renderShell(title: string, children: ReactNode, collapseTopbarTitle = true) {
  return render(
    <ProductShell>
      <AppShell
        mobileTitle={title}
        collapseTopbarTitle={collapseTopbarTitle}
        topbar={<Topbar titleSlot={<span className="title">{title}</span>} />}
      >
        {children}
      </AppShell>
    </ProductShell>,
  );
}

function mobileTitle() {
  return screen.getByTestId("mobile-topbar-title");
}

function desktopTitles() {
  const titles = document.querySelector(".v2-main-head-titles");
  if (!titles) throw new Error("missing desktop titles");
  return titles;
}

describe("top-bar large-title collapse", () => {
  it("hides both bar titles while the page H1 is visible and shows them once it scrolls away", () => {
    renderShell("My reports", <h1>My reports</h1>);

    expect(mobileTitle()).toHaveAttribute("data-collapsed", "true");
    expect(mobileTitle()).toHaveAttribute("aria-hidden", "true");
    expect(desktopTitles()).toHaveAttribute("data-collapsed", "true");

    scrollHeading(false);
    expect(mobileTitle()).not.toHaveAttribute("data-collapsed");
    expect(mobileTitle()).not.toHaveAttribute("aria-hidden");
    expect(mobileTitle()).toHaveTextContent("My reports");
    expect(desktopTitles()).not.toHaveAttribute("data-collapsed");

    scrollHeading(true);
    expect(mobileTitle()).toHaveAttribute("data-collapsed", "true");
  });

  it("measures the H1 below the top chrome", () => {
    renderShell("My reports", <h1>My reports</h1>);

    const observer = observers.find((item) => item.targets.size);
    expect(observer?.options?.rootMargin).toMatch(/^-\d+px 0px 0px 0px$/);
  });

  it("keeps the bar title on pages without an H1", () => {
    renderShell("Files", <div>file list</div>);

    expect(mobileTitle()).not.toHaveAttribute("data-collapsed");
    expect(desktopTitles()).not.toHaveAttribute("data-collapsed");
    expect(observers.some((item) => item.targets.size)).toBe(false);
  });

  it("keeps the bar title when the only H1 is a visually hidden state heading", () => {
    renderShell("Knowledge base", <PageStateHeading>Knowledge base</PageStateHeading>);

    expect(mobileTitle()).not.toHaveAttribute("data-collapsed");
  });

  it("keeps an active session title visible when report content has the same H1", () => {
    renderShell("Quarterly revenue analysis", <h1>Quarterly revenue analysis</h1>, false);

    expect(mobileTitle()).not.toHaveAttribute("data-collapsed");
    expect(desktopTitles()).not.toHaveAttribute("data-collapsed");
    expect(observers.some((item) => item.targets.size)).toBe(false);
  });

  it("restores the title immediately when a large-title page becomes an active session", () => {
    const { rerender } = renderShell("New work", <h1>New work</h1>);
    expect(desktopTitles()).toHaveAttribute("data-collapsed", "true");

    rerender(
      <ProductShell>
        <AppShell
          mobileTitle="Quarterly revenue analysis"
          collapseTopbarTitle={false}
          topbar={<Topbar titleSlot={<span className="title">Quarterly revenue analysis</span>} />}
        >
          <h1>Quarterly revenue analysis</h1>
        </AppShell>
      </ProductShell>,
    );

    expect(mobileTitle()).not.toHaveAttribute("data-collapsed");
    expect(desktopTitles()).not.toHaveAttribute("data-collapsed");
  });

  it("keeps the bar title when the H1 names something else", () => {
    renderShell("Settings", <h1>Profile</h1>);

    expect(mobileTitle()).not.toHaveAttribute("data-collapsed");
  });

  it("treats inline extras after the title as the same heading", () => {
    renderShell("Approvals", <h1>Approvals <span>· 3</span></h1>);

    expect(mobileTitle()).toHaveAttribute("data-collapsed", "true");
  });

  it("follows an H1 that appears after the page loads", async () => {
    const { rerender } = renderShell("Agent overview", <p role="status">Loading…</p>);
    expect(mobileTitle()).not.toHaveAttribute("data-collapsed");

    rerender(
      <ProductShell>
        <AppShell
          mobileTitle="Agent overview"
          topbar={<Topbar titleSlot={<span className="title">Agent overview</span>} />}
        >
          <h1>Agent overview</h1>
        </AppShell>
      </ProductShell>,
    );
    await act(async () => {
      await new Promise((resolve) => window.requestAnimationFrame(() => resolve(null)));
    });

    expect(mobileTitle()).toHaveAttribute("data-collapsed", "true");
    scrollHeading(false);
    expect(mobileTitle()).not.toHaveAttribute("data-collapsed");
  });

  it("keeps the collapsed bar out of the heading outline", () => {
    renderShell("My reports", <h1>My reports</h1>);

    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(mobileTitle().tagName).toBe("DIV");
  });
});
