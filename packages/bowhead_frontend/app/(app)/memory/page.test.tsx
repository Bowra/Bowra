import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { replace } = vi.hoisted(() => ({ replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace, back: vi.fn() }),
}));

afterEach(() => {
  cleanup();
  vi.resetModules();
  replace.mockReset();
  window.__BOWHEAD_BASE_PATH__ = "";
  window.history.replaceState(null, "", "/");
});

async function renderRedirect() {
  const { default: LegacyMemoryRedirect } = await import("./page");
  render(<LegacyMemoryRedirect />);
}

describe("legacy /memory/ redirect", () => {
  it.each([
    ["/memory/", "/settings/memory/"],
    ["/memory/?item=mem_1", "/settings/memory/?item=mem_1"],
    ["/memory/?item=mem_1#learned", "/settings/memory/?item=mem_1#learned"],
  ])("opens Settings > Memory from %s at the root entry", async (location, expected) => {
    window.__BOWHEAD_BASE_PATH__ = "";
    window.history.replaceState(null, "", location);
    await renderRedirect();
    await waitFor(() => expect(replace).toHaveBeenCalledWith(expected));
  });

  it("hands Next Router the unprefixed route under the /bowhead entry", async () => {
    window.__BOWHEAD_BASE_PATH__ = "/bowhead";
    window.history.replaceState(null, "", "/bowhead/memory/?item=mem_1");
    await renderRedirect();
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/settings/memory/?item=mem_1"));
  });
});
