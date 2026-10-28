import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({
  mutationObserverConstructed: vi.fn(),
}));

vi.mock("@/lib/brand-runtime", () => ({
  getBrandSnapshot: vi.fn(),
  subscribeBrandSnapshot: vi.fn(),
}));

import { PublicBrandProvider } from "./brand-provider";

class MutationObserverMock {
  constructor() {
    state.mutationObserverConstructed();
  }

  disconnect() {}

  observe() {}

  takeRecords(): MutationRecord[] {
    return [];
  }
}

afterEach(() => {
  cleanup();
  state.mutationObserverConstructed.mockReset();
  vi.unstubAllGlobals();
});

describe("PublicBrandProvider", () => {
  it("provides public branding without owning document metadata", () => {
    vi.stubGlobal("MutationObserver", MutationObserverMock);

    render(
      <PublicBrandProvider>
        <main>Public page</main>
      </PublicBrandProvider>,
    );

    expect(state.mutationObserverConstructed).not.toHaveBeenCalled();
  });
});
