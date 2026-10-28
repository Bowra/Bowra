import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ apiFetch: vi.fn(), invalidateGooglePicker: vi.fn() }));
vi.mock("./api-client", () => ({ apiFetch: mocks.apiFetch, readApiError: async () => ({ message: "Rejected", status: 403 }) }));
vi.mock("./google-picker", () => ({ invalidateGooglePicker: mocks.invalidateGooglePicker }));

import { deleteGoogleConnection, disconnectGoogle, saveGoogleConnection, setGoogleConnectionEnabled } from "./google-workspace-api";

afterEach(() => vi.resetAllMocks());

describe("Google connection cache invalidation", () => {
  const mutations = [
    () => saveGoogleConnection({ name: "Google", client_id: "new-client", domain: "example.com", bundles: {}, picker_project_number: "1" }),
    () => setGoogleConnectionEnabled(false),
    () => deleteGoogleConnection(),
    () => disconnectGoogle(),
  ];

  it.each(mutations)("invalidates prepared access after a successful connection mutation", async (mutate) => {
    mocks.apiFetch.mockResolvedValue(new Response(JSON.stringify({ connection: {} }), { status: 200 }));
    await mutate();
    expect(mocks.invalidateGooglePicker).toHaveBeenCalledTimes(1);
  });

  it("keeps the current cache when disconnect is refused", async () => {
    mocks.apiFetch.mockResolvedValue(new Response(null, { status: 403 }));
    await expect(disconnectGoogle()).rejects.toThrow("Rejected");
    expect(mocks.invalidateGooglePicker).not.toHaveBeenCalled();
  });
});
