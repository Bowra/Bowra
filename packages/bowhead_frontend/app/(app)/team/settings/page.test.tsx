import { cleanup, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

const state = vi.hoisted(() => ({
  replace: vi.fn(),
}));

vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ replace: state.replace }),
}));

import TeamSettingsPage from "./page";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Team settings index", () => {
  it("redirects the settings index to the people page", async () => {
    renderIntl(<TeamSettingsPage />);

    await waitFor(() => {
      expect(state.replace).toHaveBeenCalledWith("/team/settings/people/");
    });
  });

});
