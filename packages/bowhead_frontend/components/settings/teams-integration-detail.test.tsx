import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { catalogText, expectRefreshedGlyph } from "@/__tests__/catalog-glyph";
import { renderIntl } from "@/__tests__/intl-render";

const state = vi.hoisted(() => ({ fetchConfig: vi.fn(), saveConfig: vi.fn() }));

vi.mock("@/lib/use-app-router", () => ({ useAppRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));
vi.mock("@/lib/teams-integration-api", () => ({
  fetchTeamsAdminConfig: () => state.fetchConfig(),
  updateTeamsAdminConfig: (values: unknown) => state.saveConfig(values),
}));

import { TeamsIntegrationDetail } from "./teams-integration-detail";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("TeamsIntegrationDetail back button glyph", () => {
  it("renders ← before the back label", async () => {
    state.fetchConfig.mockResolvedValue({ configured: false, status: "draft" });
    renderIntl(<TeamsIntegrationDetail />);
    await waitFor(() => expect(state.fetchConfig).toHaveBeenCalled());
    const message = catalogText("zh-CN", "settings.integrations.detail_common.back");
    const back = screen.getByRole("button", { name: (name) => name.endsWith(message) });
    expectRefreshedGlyph(back, "←", "start", message);
  });
});

it("loads the saved status before editing and preserves the existing secret on save", async () => {
  const saved = {configured:true, status:"enabled", entra_tenant_id:"tenant", microsoft_app_id:"app", teams_manifest_id:"manifest",
    sso_connection_name:"connection", sso_audience:"audience", app_secret_configured:true};
  state.fetchConfig.mockResolvedValue(saved);
  state.saveConfig.mockResolvedValue(saved);
  renderIntl(<TeamsIntegrationDetail />);
  const status = await screen.findByRole<HTMLSelectElement>("combobox");
  expect(status.value).toBe("enabled");
  fireEvent.submit(status.closest("form")!);
  await waitFor(() => expect(state.saveConfig).toHaveBeenCalledOnce());
  const payload = state.saveConfig.mock.calls[0][0];
  expect(payload.status).toBe("enabled");
  expect(payload.microsoft_app_id).toBe("app");
  expect(payload).not.toHaveProperty("app_secret");
});
