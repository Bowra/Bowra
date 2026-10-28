import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import LegacyRedirect from "./page";

const replace = vi.fn();

vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ replace }),
}));

describe("legacy skills settings redirect", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    window.history.replaceState(null, "", "/");
  });

  it("opens the Skills page", async () => {
    window.history.replaceState(null, "", "/settings/skills/");
    render(<LegacyRedirect />);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/skills/"));
  });

  it("keeps the Agent repair context from an already-issued link", async () => {
    window.history.replaceState(null, "", "/settings/skills/?from_agent=osa_1&dependency=sample-analysis#top");
    render(<LegacyRedirect />);
    await waitFor(() =>
      expect(replace).toHaveBeenCalledWith("/skills/?from_agent=osa_1&dependency=sample-analysis#top"),
    );
  });
});
