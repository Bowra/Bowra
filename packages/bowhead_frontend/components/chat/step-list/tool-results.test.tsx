import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";

import { renderIntl } from "@/__tests__/intl-render";
import { ParallelRail } from "./tool-results";

afterEach(() => cleanup());

describe("ParallelRail", () => {
  it("gives the SQL disclosure an accessible name and controlled region", () => {
    renderIntl(
      <ParallelRail
        rows={[
          {
            sql: "SELECT revenue FROM fact_sales",
            meta: "12 行 · 0.6s",
            code: "SELECT revenue FROM fact_sales WHERE region = '华东'",
          },
        ]}
      />,
    );

    const button = screen.getByRole("button", { name: "展开 SQL" });
    const controlledId = button.getAttribute("aria-controls");
    expect(controlledId).toBeTruthy();
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(document.getElementById(controlledId!)).toBeNull();

    fireEvent.click(button);

    expect(screen.getByRole("button", { name: "收起 SQL" })).toBeTruthy();
    expect(button.getAttribute("aria-expanded")).toBe("true");
    expect(document.getElementById(controlledId!)).toBeTruthy();
  });
});
