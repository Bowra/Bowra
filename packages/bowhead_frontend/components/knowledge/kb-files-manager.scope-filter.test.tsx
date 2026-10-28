/**
 * Knowledge base scope filter (P2 batch 5, option B).
 *
 * Contracts:
 *   1. The scope segments are one "Scope: X" filter; the first visit opens the current space, a saved
 *      choice is restored, and a saved "space" without a selected space falls back to "All".
 *   2. "All" is offered only when the server supports the cross-scope list (feature-detected); choosing it lists
 *      scope=all with a scope column and remembers the choice.
 *   3. "All" pages by keyset cursor; changing the sort drops the cursors.
 */

import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api-client";
import type { KbListScope } from "@/lib/kb-api";
import messagesEn from "@/messages/en-US.json";

type ListParams = { scope: KbListScope; limit?: number; offset?: number; cursor?: string; order_by?: string; order_dir?: string };

const mocks = vi.hoisted(() => ({
  fetchFiles: vi.fn(),
  fetchMe: vi.fn(),
  loadPref: vi.fn(),
  savePref: vi.fn(),
  activeId: { current: "ws_main" as string | null },
  replace: vi.fn(),
  push: vi.fn(),
}));

vi.mock("@/lib/use-app-router", () => ({
  useAppRouter: () => ({ push: mocks.push, replace: mocks.replace, back: vi.fn() }),
}));


vi.mock("@/lib/use-space", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/use-space")>();
  return {
    ...actual,
    useSpaceSelection: () => ({
      items: [{ id: "ws_main", name: "Retail" }],
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
  return {
    ...actual,
    fetchKbFiles: (...args: unknown[]) => mocks.fetchFiles(...args),
    loadKbFileScopePreference: (...args: unknown[]) => mocks.loadPref(...args),
    saveKbFileScopePreference: (...args: unknown[]) => mocks.savePref(...args),
  };
});

vi.mock("./kb-me", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./kb-me")>();
  return { ...actual, fetchKbMe: (...args: unknown[]) => mocks.fetchMe(...args) };
});

import { KbFilesManager } from "./kb-files-manager";

const TOTALS: Record<KbListScope, number> = { personal: 2, space: 1, team: 3, all: 45 };

function file(i: number, scope: "personal" | "space" | "team") {
  return {
    file_id: `kbf_${scope}_${i}`, filename: `${scope}-${i}.pdf`, mime_type: "application/pdf", size_bytes: 1000,
    scope, scope_id: "x", workspace_id: "ws_main", team_id: "team_main", uploaded_by: "u_me", uploaded_by_name: "Me",
    uploaded_at: "2026-09-01T10:00:00Z", parse_status: "done", parse_progress_pct: null, parse_layer: null, parse_error: null,
    parse_duration_ms: null, chunks_count: 3, cite_count: 0, last_cited_at: null, use_llm_marker: false, doc_type_id: null,
    auto_classified_doc_type_id: null, extracted_rows_status: "na", summary_json: null, version: 1, soft_deleted_at: null,
    artifact_id: `art_${i}`, can_delete: true, can_change_scope: true,
  };
}

/** New server: every list answer carries next_cursor (null on the last page); scope=all pages by cursor. */
function newServer(params: ListParams) {
  if (params.limit === 1) return { files: [], total: TOTALS[params.scope], has_more: false, next_cursor: null };
  const files = params.scope === "all"
    ? [file(1, "team"), file(2, "personal"), file(3, "space")]
    : [file(1, params.scope as "personal" | "space" | "team")];
  const total = TOTALS[params.scope];
  const nextCursor = params.scope === "all" ? `cur:${params.order_by}:${params.order_dir}:${params.cursor ?? params.offset}` : null;
  return { files, total, has_more: total > 20, next_cursor: nextCursor };
}

/** Old server: scope=all is refused by validation; no next_cursor anywhere. */
function oldServer(params: ListParams) {
  if (params.scope === "all") throw new ApiError(422, "validation");
  return { files: params.limit === 1 ? [] : [file(1, params.scope as "personal" | "space" | "team")], total: TOTALS[params.scope], has_more: false };
}

function renderManager(props: Parameters<typeof KbFilesManager>[0] = {}) {
  return render(
    <NextIntlClientProvider locale="en-US" messages={messagesEn}>
      <KbFilesManager {...props} />
    </NextIntlClientProvider>,
  );
}

const listCalls = () => mocks.fetchFiles.mock.calls.map((c) => c[0] as ListParams).filter((p) => p.limit !== 1);
const lastList = () => listCalls().at(-1);

beforeEach(() => {
  vi.clearAllMocks();
  mocks.activeId.current = "ws_main";
  mocks.fetchMe.mockResolvedValue({ user_id: "u_me", workspace_id: "ws_main", team_id: "team_main", role: "user", is_team_admin: false });
  mocks.fetchFiles.mockImplementation(async (params: ListParams) => newServer(params));
  mocks.loadPref.mockResolvedValue(null);
  mocks.savePref.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
});

async function openScopeMenu() {
  fireEvent.click(await screen.findByTestId("kb-scope-filter"), { detail: 0 });
  const menu = await screen.findByTestId("kb-scope-filter-menu");
  expect(menu).toHaveAttribute("role", "menu");
  return menu;
}

describe("KbFilesManager scope filter", () => {
  it("replaces the segments with one filter and opens the current space on the first visit", async () => {
    renderManager();
    await waitFor(() => expect(lastList()?.scope).toBe("space"));
    expect(screen.queryByTestId("kb-scope-personal")).toBeNull();
    expect(screen.queryByTestId("kb-scope-hint")).toBeNull();
    expect(screen.getByTestId("kb-scope-filter")).toHaveTextContent("Scope: Space · Retail");
    // The list waited for the preference: it never listed personal first.
    expect(listCalls().map((p) => p.scope)).toEqual(["space"]);
  });

  it("offers All only when the server supports it, and lists every scope with a scope column", async () => {
    renderManager();
    const menu = await openScopeMenu();
    const all = await within(menu).findByTestId("kb-scope-filter-all");
    expect(all).toHaveAttribute("role", "menuitemradio");
    expect(within(menu).getByTestId("kb-scope-filter-space")).toHaveAttribute("aria-checked", "true");
    expect(all).toHaveTextContent("45");
    expect(all).toHaveAccessibleDescription(messagesEn.knowledge_base.scope_filter.hint_all);

    fireEvent.click(all);
    await waitFor(() => expect(lastList()?.scope).toBe("all"));
    expect(mocks.savePref).toHaveBeenCalledWith("all");
    expect(await screen.findByTestId("kb-col-scope")).toHaveTextContent("Scope");
    const rows = await screen.findAllByTestId("kb-files-row");
    expect(rows.map((r) => r.querySelector(".kb-scope-cell")?.textContent)).toEqual(["Team", "Personal", "Space"]);
    expect(screen.getByTestId("kb-scope-filter")).toHaveTextContent("Scope: All");
  });

  it("hides All on a server without the cross-scope list", async () => {
    mocks.fetchFiles.mockImplementation(async (params: ListParams) => oldServer(params));
    renderManager();
    const menu = await openScopeMenu();
    await waitFor(() => expect(mocks.fetchFiles.mock.calls.some((c) => (c[0] as ListParams).scope === "all")).toBe(true));
    expect(within(menu).queryByTestId("kb-scope-filter-all")).toBeNull();
    expect(within(menu).getAllByRole("menuitemradio", { hidden: true })).toHaveLength(3);
  });

  it("restores the saved scope", async () => {
    mocks.loadPref.mockResolvedValue("team");
    renderManager();
    await waitFor(() => expect(lastList()?.scope).toBe("team"));
    expect(listCalls().map((p) => p.scope)).toEqual(["team"]);
  });

  it("falls back to All when the saved scope is space but no space is selected", async () => {
    mocks.activeId.current = null;
    mocks.loadPref.mockResolvedValue("space");
    renderManager();
    await waitFor(() => expect(lastList()?.scope).toBe("all"));
  });

  it("a scope in the URL wins over the saved one", async () => {
    mocks.loadPref.mockResolvedValue("team");
    renderManager({ initialScope: "personal" });
    await waitFor(() => expect(lastList()?.scope).toBe("personal"));
    expect(mocks.loadPref).not.toHaveBeenCalled();
  });

  it("pages All by cursor and drops the cursor when the sort changes", async () => {
    mocks.loadPref.mockResolvedValue("all");
    renderManager();
    await waitFor(() => expect(lastList()?.scope).toBe("all"));
    fireEvent.click(await screen.findByRole("button", { name: /Next/ }));
    await waitFor(() => expect(lastList()).toMatchObject({ scope: "all", cursor: "cur:uploaded_at:desc:0" }));
    expect(lastList()?.offset).toBeUndefined();

    fireEvent.click(screen.getByText("Filename"));
    await waitFor(() => expect(lastList()).toMatchObject({ order_by: "filename", order_dir: "asc", offset: 0 }));
    expect(lastList()?.cursor).toBeUndefined();
    fireEvent.click(screen.getByRole("button", { name: /Next/ }));
    await waitFor(() => expect(lastList()).toMatchObject({ order_by: "filename", cursor: "cur:filename:asc:0" }));
  });

  it("keeps the scope when the URL loses it (sidebar Knowledge link)", async () => {
    mocks.loadPref.mockResolvedValue("team");
    const { rerender } = renderManager({ viewQuery: "" });
    await waitFor(() => expect(lastList()?.scope).toBe("team"));
    rerender(
      <NextIntlClientProvider locale="en-US" messages={messagesEn}>
        <KbFilesManager viewQuery="tab=files&scope=team&q=x" />
      </NextIntlClientProvider>,
    );
    rerender(
      <NextIntlClientProvider locale="en-US" messages={messagesEn}>
        <KbFilesManager viewQuery="tab=files" />
      </NextIntlClientProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("kb-scope-filter")).toHaveTextContent("Scope: Team"));
    expect(listCalls().map((p) => p.scope)).not.toContain("personal");
  });

  it("keeps the All label and option when the capability probe fails but the list works", async () => {
    mocks.loadPref.mockResolvedValue("all");
    mocks.fetchFiles.mockImplementation(async (params: ListParams) => {
      if (params.scope === "all" && params.limit === 1) throw new ApiError(503, "unavailable");
      return newServer(params);
    });
    renderManager();
    await waitFor(() => expect(lastList()?.scope).toBe("all"));
    expect(screen.getByTestId("kb-scope-filter")).toHaveTextContent("Scope: All");
    const menu = await openScopeMenu();
    expect(within(menu).getByTestId("kb-scope-filter-all")).toHaveAttribute("aria-checked", "true");
  });

  it("writes the restored scope to the URL when the view is URL-backed", async () => {
    mocks.loadPref.mockResolvedValue("team");
    renderManager({ viewQuery: "" });
    await waitFor(() => expect(lastList()?.scope).toBe("team"));
    expect(mocks.replace).toHaveBeenCalledWith(expect.stringContaining("scope=team"), { scroll: false });
  });
});
