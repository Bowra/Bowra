import type { ReactNode } from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("@/components/shell/AppShell", () => ({
  AppShell: ({ children, mobileTitle }: { children: ReactNode; mobileTitle: string }) => (
    <main className="v2-main" data-testid="shell-main" data-title={mobileTitle}>
      {children}
    </main>
  ),
}));
vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: vi.fn() }) }));

import { PersonalCapabilityPage } from "./personal-capability-page";

afterEach(cleanup);

it("puts page content in the shared PageFrame scroller and work column", () => {
  render(
    <PersonalCapabilityPage mobileTitle="Connections">
      <p>long content</p>
    </PersonalCapabilityPage>,
  );
  const shellMain = screen.getByTestId("shell-main");
  expect(shellMain.getAttribute("data-title")).toBe("Connections");
  // body does not scroll and the shell main is a flex column: its direct child shrinks and scrolls itself.
  const scroller = shellMain.firstElementChild as HTMLElement;
  expect(scroller.classList.contains("ui-page-frame-scroll")).toBe(true);
  const column = scroller.firstElementChild as HTMLElement;
  expect(column.classList.contains("ui-page-frame")).toBe(true);
  // Work pages default to the 1120 column (Header, Language & Paging Spec D4).
  expect(column.getAttribute("data-width")).toBe("table");
  expect(column.contains(screen.getByText("long content"))).toBe(true);
});

it("passes the requested content width tier to the column", () => {
  render(
    <PersonalCapabilityPage mobileTitle="Connections" width="form">
      <p>list</p>
    </PersonalCapabilityPage>,
  );
  expect(screen.getByText("list").closest(".ui-page-frame")?.getAttribute("data-width")).toBe("form");
});
