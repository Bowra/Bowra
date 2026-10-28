import "@testing-library/jest-dom/vitest";

import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ResultsStateIcon } from "./results-state-icon";

afterEach(cleanup);

describe("ResultsStateIcon", () => {
  it("renders a decorative 20px icon carrying its tone", () => {
    const { container } = render(<ResultsStateIcon tone="error" />);
    const icon = container.querySelector(".results-state-icon");
    expect(icon).toHaveAttribute("data-tone", "error");
    expect(icon).toHaveAttribute("aria-hidden", "true");
    expect(icon?.querySelector("svg")).toHaveAttribute("width", "20");
  });
});
