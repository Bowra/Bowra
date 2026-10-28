import type { ReactNode } from "react";
import { cleanup, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

import ConnectionsPage from "./page";

vi.mock("@/components/capabilities/personal-capability-page", () => ({
  PersonalCapabilityPage: ({ children, mobileTitle }: { children: ReactNode; mobileTitle: string }) => (
    <>
      <div data-testid="bar-title">{mobileTitle}</div>
      {children}
    </>
  ),
}));
vi.mock("@/components/settings/google-connection-card", () => ({
  GoogleConnectionCard: () => <h2>Google</h2>,
}));
vi.mock("@/components/settings/personal-mcp-settings", () => ({
  PersonalMcpSettings: () => <h2>Personal MCP</h2>,
}));

afterEach(cleanup);

it.each(["en-US", "zh-CN", "de-DE", "fr-FR"] as const)(
  "titles My Connections once with the top-bar text as its only H1 (%s)",
  (locale) => {
    renderIntl(<ConnectionsPage />, { locale });
    const headings = screen.getAllByRole("heading", { level: 1 });
    // The large-title pattern hides the bar title only while an H1 with the same text is on screen.
    expect(headings).toHaveLength(1);
    expect(headings[0].textContent).toBe(screen.getByTestId("bar-title").textContent);
    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(2);
  },
);
