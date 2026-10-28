import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) =>
    ({
      opening: "Opening Bowhead",
      unavailable: "Bowhead is unavailable",
      retry: "Retry",
    })[key] ?? key,
}));

import { InertAppFallback } from "./inert-app-fallback";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("InertAppFallback", () => {
  it("renders product-shell geometry before showing delayed status", () => {
    vi.useFakeTimers();
    render(<InertAppFallback chrome="product-shell" />);

    expect(document.querySelector(".app-boot-sidebar")).toBeTruthy();
    expect(screen.queryByText("Opening Bowhead")).toBeNull();

    act(() => vi.advanceTimersByTime(300));

    expect(screen.getByText("Opening Bowhead")).toBeTruthy();
  });

  it("renders a retry action only for a recoverable error", () => {
    const retry = vi.fn();
    render(
      <InertAppFallback
        chrome="standalone"
        error="network down"
        onRetry={retry}
      />,
    );

    expect(document.querySelector(".app-boot-sidebar")).toBeNull();
    expect(screen.getByRole("alert").textContent).toContain(
      "Bowhead is unavailable",
    );
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(retry).toHaveBeenCalledOnce();
  });
});
