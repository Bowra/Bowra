import { cleanup, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { catalogText, expectRefreshedGlyph } from "@/__tests__/catalog-glyph";
import { renderIntl } from "@/__tests__/intl-render";

const state = vi.hoisted(() => ({ fetchConfig: vi.fn() }));

vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));
vi.mock("@/lib/teams-integration-api", () => ({
  fetchTeamsAdminConfig: () => state.fetchConfig(),
  updateTeamsAdminConfig: vi.fn(),
}));

import { TeamsIntegrationDetail } from "./teams-integration-detail";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("TeamsIntegrationDetail back button glyph", () => {
  it("renders ← before the back label", async () => {
    state.fetchConfig.mockResolvedValue({});
    renderIntl(<TeamsIntegrationDetail />);
    await waitFor(() => expect(state.fetchConfig).toHaveBeenCalled());
    const message = catalogText("zh-CN", "settings.integrations.detail_common.back");
    const back = screen.getByRole("button", { name: (name) => name.endsWith(message) });
    expectRefreshedGlyph(back, "←", "start", message);
  });
});
