import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import LegacyRedirect from "./page";

const replace = vi.fn();

vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ replace }),
}));

describe("legacy personal connections redirect", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    window.history.replaceState(null, "", "/");
  });

  it("opens My Connections", async () => {
    window.history.replaceState(null, "", "/settings/personal-connections/");
    render(<LegacyRedirect />);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/connections/"));
  });

  it("keeps the query and hash of an existing link", async () => {
    window.history.replaceState(null, "", "/settings/personal-connections/?provider=google&status=connected#mcp");
    render(<LegacyRedirect />);
    await waitFor(() =>
      expect(replace).toHaveBeenCalledWith("/connections/?provider=google&status=connected#mcp"),
    );
  });
});
