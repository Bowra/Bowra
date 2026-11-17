import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import LegacyRedirect from "./page";

const replace = vi.fn();

vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ replace }),
}));

describe("legacy Team Plugins settings redirect", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    window.history.replaceState(null, "", "/");
  });

  it("opens Team Plugins at its plugins route and keeps the return context", async () => {
    window.history.replaceState(null, "", "/team/settings/capabilities/?capability_return=team#list");
    render(<LegacyRedirect />);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/team/settings/plugins/?capability_return=team#list"));
  });
});
