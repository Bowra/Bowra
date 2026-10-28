/**
 * Deep content search respects the current scope — knowledge base UX cleanup PRD FR6.2.
 *
 * Observed on 116: deep search under Personal returned space files, ignoring scope switches. Contract: include the current
 * scope in deep-search requests and search again under the new scope after switching.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import messagesEn from "@/messages/en-US.json";

const mocks = vi.hoisted(() => ({
  fetchFiles: vi.fn(),
  searchKb: vi.fn(),
  fetchMe: vi.fn(),
  listDocTypes: vi.fn(),
}));

vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));


vi.mock("@/lib/use-space", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/use-space")>();
  return {
    ...actual,
    useSpaceSelection: () => ({
      items: [],
      activeId: "ws_main",
      loading: false,
      error: null,
      setActiveId: vi.fn(),
      refresh: vi.fn(),
    }),
  };
});

vi.mock("@/lib/kb-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/kb-api")>();
  return {
    ...actual,
    fetchKbFiles: (...args: unknown[]) => mocks.fetchFiles(...args),
    searchKb: (...args: unknown[]) => mocks.searchKb(...args),
  };
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

beforeEach(() => {
  vi.clearAllMocks();
  mocks.fetchMe.mockResolvedValue({ user_id: "u_me", team_id: "team_main", is_team_admin: false });
  mocks.listDocTypes.mockResolvedValue([]);
  mocks.fetchFiles.mockResolvedValue({ files: [], total: 1, has_more: false });
  mocks.searchKb.mockResolvedValue({ chunks: [], total_candidates: 0, rerank_used: true, latency_ms: 1 });
});

afterEach(cleanup);

function scopeFilters(): unknown[] {
  return mocks.searchKb.mock.calls.map((c) => (c[1] as { scope_filter?: unknown }).scope_filter);
}

describe("KbFilesManager deep search scope", () => {
  it("sends the current scope with deep search and searches again after the scope changes", async () => {
    render(
      <NextIntlClientProvider locale="en-US" messages={messagesEn}>
        <KbFilesManager />
      </NextIntlClientProvider>,
    );
    fireEvent.change(screen.getByPlaceholderText("Search by filename…"), { target: { value: "福州销售数据" } });
    fireEvent.click(screen.getByRole("button", { name: /Deep search/ }));

    // The first visit opens the current space.
    await waitFor(() => expect(scopeFilters()).toContainEqual(["space"]));
    expect(mocks.searchKb.mock.calls.at(-1)?.[1]).toEqual({ scope_filter: ["space"] });

    fireEvent.click(await screen.findByTestId("kb-scope-filter"), { detail: 0 });
    fireEvent.click(await screen.findByTestId("kb-scope-filter-personal"));
    await waitFor(() => expect(scopeFilters()).toContainEqual(["personal"]));
  });
});
