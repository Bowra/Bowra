/**
 * Space-hydration race regression—reproduced on 116 prod 1.0.18, 2026-07-30.
 *
 * Enter /knowledge/ immediately after login: active space not yet hydrated into space-storage; `scope=space`
 * count request lacks X-Space-Id, Gateway returns intended 400 (kb.py `_require_workspace_context`
 * cross-scope leakage guard). Counts effect depended only on countsTick; catch swallowed 400 as
 * null and **never retried** → Visible to Space Members showed “—” throughout the visit instead of actual
 * count (measured 1 showed “—”), without any error notice.
 *
 * Contracts:
 *   1. Before space settles, **no** scope=space request (avoid that 400); personal/team counts remain normal
 *   2. Automatically refetch when space settles, filling the actual space count
 *   3. Refetch on space switching (never retain old-space count)
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import messagesEn from "@/messages/en-US.json";

const mocks = vi.hoisted(() => ({
  fetchFiles: vi.fn(),
  fetchMe: vi.fn(),
  listDocTypes: vi.fn(),
  activeId: { current: null as string | null },
}));

vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

// Test drives activeId through the pre-hydration null → post-hydration ws_x window.
vi.mock("@/lib/use-space", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/use-space")>();
  return {
    ...actual,
    useSpaceSelection: () => ({
      items: [],
      activeId: mocks.activeId.current,
      loading: false,
      error: null,
      setActiveId: vi.fn(),
      refresh: vi.fn(),
    }),
  };
});

vi.mock("@/lib/kb-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-api")>();
  return { ...actual, fetchKbFiles: (...args: unknown[]) => mocks.fetchFiles(...args) };
});

vi.mock("./kb-me", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./kb-me")>();
  return { ...actual, fetchKbMe: (...args: unknown[]) => mocks.fetchMe(...args) };
});

vi.mock("@/lib/kb-knowledge-tables-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-knowledge-tables-api")>();
  return { ...actual, listKnowledgeTables: (...args: unknown[]) => mocks.listDocTypes(...args) };
});

import { KbFilesManager } from "./kb-files-manager";

/** Real Gateway: scope=space without X-Space-Id → 400. Scope parameter proxies that condition here. */
const TOTALS: Record<string, number> = { personal: 2, space: 1, team: 0 };

function renderManager() {
  return render(
    <NextIntlClientProvider locale="en-US" messages={messagesEn}>
      <KbFilesManager />
    </NextIntlClientProvider>,
  );
}

function scopesRequested(): string[] {
  return mocks.fetchFiles.mock.calls
    .map((c) => (c[0] as { scope?: string; limit?: number } | undefined))
    .filter((p) => p?.limit === 1)
    .map((p) => String(p?.scope));
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.activeId.current = null;
  mocks.fetchMe.mockResolvedValue({ user_id: "u_me", workspace_id: "ws_main", team_id: "team_main", role: "user" });
  mocks.listDocTypes.mockResolvedValue({ items: [] });
  mocks.fetchFiles.mockImplementation(async (params: { scope: string; limit?: number }) => {
    // Faithful Gateway simulation: no active space → empty spaceHeader() → missing X-Space-Id →
    // kb.py `_require_workspace_context` 400. Component uses bare catch for counts, swallowing any thrown value
    // as null; plain Error suffices to express a failed request here.
    if (params.scope === "space" && !mocks.activeId.current) {
      throw new Error("400 workspace context required");
    }
    return { files: [], total: TOTALS[params.scope] ?? 0, has_more: false };
  });
});

afterEach(cleanup);

describe("KbFilesManager · 空间水合竞态", () => {
  it("空间未落定时不发 scope=space 计数请求，personal/team 照常出数", async () => {
    renderManager();
    await waitFor(() => expect(scopesRequested().length).toBeGreaterThan(0));

    expect(scopesRequested()).not.toContain("space");
    expect(scopesRequested()).toEqual(expect.arrayContaining(["personal", "team"]));
  });

  it("空间落定后自动重取，space 计数从占位补成真值", async () => {
    const { rerender } = renderManager();
    await waitFor(() => expect(scopesRequested()).toEqual(expect.arrayContaining(["personal"])));
    // Before hydration there is no space yet: the scope filter offers no space option (never a count of 0).
    fireEvent.click(screen.getByTestId("kb-scope-filter"));
    await screen.findByTestId("kb-scope-filter-personal");
    expect(screen.queryByTestId("kb-scope-filter-space")).toBeNull();

    mocks.activeId.current = "ws_main";
    rerender(
      <NextIntlClientProvider locale="en-US" messages={messagesEn}>
        <KbFilesManager />
      </NextIntlClientProvider>,
    );

    await waitFor(() => expect(scopesRequested()).toContain("space"));
    // The actual count appears.
    await waitFor(() => expect(screen.getByTestId("kb-scope-filter-space").querySelector(".count")).not.toBeNull());
  });

  it("切换空间会重取计数（不停在旧空间）", async () => {
    mocks.activeId.current = "ws_a";
    const { rerender } = renderManager();
    await waitFor(() => expect(scopesRequested()).toContain("space"));
    const before = scopesRequested().filter((s) => s === "space").length;

    mocks.activeId.current = "ws_b";
    rerender(
      <NextIntlClientProvider locale="en-US" messages={messagesEn}>
        <KbFilesManager />
      </NextIntlClientProvider>,
    );

    await waitFor(() =>
      expect(scopesRequested().filter((s) => s === "space").length).toBeGreaterThan(before),
    );
  });
});
