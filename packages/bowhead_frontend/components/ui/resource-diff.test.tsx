import { cleanup, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

import { ResourceDiff, hasResourceChanges } from "./resource-diff";

describe("ResourceDiff", () => {
  afterEach(cleanup);

  it("groups additions and removals by resource type and expands Plugin Tools", () => {
    renderIntl(
      <ResourceDiff
        input={{
          plugins: [
            {
              kind: "team",
              id: "tcap_1",
              name: "Store pack",
              listing: {},
              version: 3,
              tools: ["web_map"],
              change: "removed",
            },
          ],
          tools: { added: ["send_email"], removed: [] },
          skills: { added: [], removed: ["kpi-method"] },
        }}
      />,
      { locale: "en-US" },
    );
    expect(screen.getByRole("heading", { name: "Plugins" })).toBeTruthy();
    expect(screen.getByText("Store pack")).toBeTruthy();
    expect(screen.getByText("Team Plugin · v3")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Takes away 1 Tool" }));
    expect(screen.getByText("Map Website")).toBeTruthy();
    expect(screen.getByText("Send Email")).toBeTruthy();
    expect(screen.getByText("kpi-method")).toBeTruthy();
  });

  it("renders nothing when no resource changed", () => {
    expect(hasResourceChanges({ plugins: [], tools: { added: [], removed: [] } })).toBe(false);
    const { container } = renderIntl(<ResourceDiff input={{}} />, { locale: "en-US" });
    expect(container.textContent).toBe("");
  });
});
