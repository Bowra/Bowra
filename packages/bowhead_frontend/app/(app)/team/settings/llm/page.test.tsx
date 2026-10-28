import { cleanup, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

const state = vi.hoisted(() => ({
  replace: vi.fn(),
  searchParams: new URLSearchParams("from=setup"),
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => state.searchParams,
}));
vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ replace: state.replace }),
}));

import LegacyTeamModelsRedirect from "./page";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Legacy team models route", () => {
  it("redirects to the canonical models route and preserves query context", async () => {
    renderIntl(<LegacyTeamModelsRedirect />);

    await waitFor(() => {
      expect(state.replace).toHaveBeenCalledWith(
        "/team/settings/models/?from=setup",
      );
    });
  });
});
