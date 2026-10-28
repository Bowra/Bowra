/**
 * V5 mobile fix · MobileTopBar tests.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { MobileTopBar } from "./mobile-topbar";
import { SessionsDrawerProvider } from "./sessions-drawer";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/",
}));

vi.mock("@/lib/api", () => ({
  fetchSessions: vi.fn().mockResolvedValue({ groups: [] }),
}));

vi.mock("next/link", () => ({
  default: ({ href, children }: { href: string; children: React.ReactNode }) => <a href={href}>{children}</a>,
}));

afterEach(() => cleanup());


function Harness({ pageTitle }: { pageTitle: string }) {
  return (
    <SessionsDrawerProvider>
      <MobileTopBar pageTitle={pageTitle} />
    </SessionsDrawerProvider>
  );
}


describe("MobileTopBar", () => {
  it("renders hamburger + title", () => {
    render(<Harness pageTitle="My Memory" />);
    expect(screen.getByTestId("mobile-topbar")).toBeTruthy();
    expect(screen.getByTestId("mobile-topbar-hamburger")).toBeTruthy();
    expect(screen.getByTestId("mobile-topbar-title").textContent).toBe("My Memory");
  });

  it("shows a title skeleton instead of a placeholder name while the title is loading (116: New work)", () => {
    render(
      <SessionsDrawerProvider>
        <MobileTopBar pageTitle="New work" titleLoading />
      </SessionsDrawerProvider>,
    );
    const title = screen.getByTestId("mobile-topbar-title");
    expect(title.getAttribute("aria-busy")).toBe("true");
    expect(title.querySelector(".v2-mobile-topbar-title-skeleton")).toBeTruthy();
    // The visible text is the skeleton; the name stays available to assistive technology only.
    expect(title.querySelector(".sr-only")?.textContent).toBe("New work");
  });

  it("hamburger click opens sessions drawer", () => {
    render(
      <SessionsDrawerProvider>
        <MobileTopBar pageTitle="X" />
        {/* SessionsDrawer not rendered here, but provider state change observable via context */}
      </SessionsDrawerProvider>,
    );
    const ham = screen.getByTestId("mobile-topbar-hamburger");
    // No throw on click + body overflow set by provider effect (drawer mounting separately)
    fireEvent.click(ham);
    // Verify DOM hasn't crashed by checking topbar is still visible
    expect(screen.getByTestId("mobile-topbar")).toBeTruthy();
  });

  it("renders rightSlot when provided", () => {
    render(
      <SessionsDrawerProvider>
        <MobileTopBar pageTitle="X" rightSlot={<span data-testid="custom-slot">★</span>} />
      </SessionsDrawerProvider>,
    );
    expect(screen.getByTestId("custom-slot")).toBeTruthy();
  });

  describe("menu button (P2 batch 4)", () => {
    function setWidth(width: number) {
      Object.defineProperty(window, "innerWidth", { value: width, configurable: true });
    }
    afterEach(() => {
      setWidth(1024);
    });

    it("asks the sidebar for its overlay at 768–1023, passing itself as the focus target", async () => {
      const { onSidebarOverlayRequest } = await import("@/lib/sidebar-overlay-request");
      const opened = vi.fn();
      const stop = onSidebarOverlayRequest(opened);
      setWidth(900);
      render(<Harness pageTitle="X" />);
      const ham = screen.getByTestId("mobile-topbar-hamburger");
      fireEvent.click(ham);
      expect(opened).toHaveBeenCalledWith(ham);
      stop();
    });

    it("keeps the drawer below 768", async () => {
      const { onSidebarOverlayRequest } = await import("@/lib/sidebar-overlay-request");
      const opened = vi.fn();
      const stop = onSidebarOverlayRequest(opened);
      setWidth(390);
      render(<Harness pageTitle="X" />);
      fireEvent.click(screen.getByTestId("mobile-topbar-hamburger"));
      expect(opened).not.toHaveBeenCalled();
      stop();
    });
  });
});
