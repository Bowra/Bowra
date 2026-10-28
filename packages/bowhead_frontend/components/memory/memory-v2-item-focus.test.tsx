/**
 * Settings > Memory deep link `?item=<id>`: scroll to and highlight that memory,
 * page forward when it is not on the first page, and explain a missing one.
 */
import { act, cleanup, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

const api = vi.hoisted(() => ({
  fetchMemoryOverview: vi.fn(),
  fetchCanonicalMemoryItems: vi.fn(),
  listMemoryResets: vi.fn(),
}));
vi.mock("@/lib/memory-api", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  ...api,
}));

import { ToastContainer } from "@/components/chat/toast-container";
import { _clearAllToasts } from "@/lib/use-toast";

import { MemoryV2Content } from "./memory-v2-content";

const T = "2026-09-22T08:00:00Z";
const SCOPE = { type: "global", space_id: null, space_name: null, availability: "available", label: "所有空间" };
const item = (id: string, value: string) => ({
  id,
  resource_type: "memory_entry",
  display_type: "preference",
  display_value: value,
  display_schema: { name: "text", payload: {}, editable_fields: ["value"] },
  scope: SCOPE,
  provenance: {
    created_from: { type: "chat", source_label: "对话", source_available: false, source_open_token: null },
    last_modified: { actor_type: null, actor_id: null, modified_at: null },
  },
  capabilities: { edit: true, soft_delete: true, restore: false, permanent_delete: false },
  locked: false,
  created_at: T,
  updated_at: T,
  deleted_at: null,
  purge_at: null,
});

const scrollIntoView = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  _clearAllToasts();
  Element.prototype.scrollIntoView = scrollIntoView;
  api.fetchMemoryOverview.mockResolvedValue({
    schema_version: 2,
    about_you: {
      content: "",
      scope: SCOPE,
      version: "0",
      updated_at: null,
      is_locked: false,
      capabilities: { edit: true, soft_delete: false, restore: false, permanent_delete: false },
    },
    suggestions: [],
    stats: { notes_configured: false, saved_entries: 2, pending_candidates: 0, spaces_represented: 1, trash_count: 0, recoverable_resets: 0 },
    learning: { automatic_learning: "active" },
    capabilities: {
      v2_enabled: true, v2_ready: true, presentation: "v2", schema_version: 2, kind_policy_version: 1,
      actions: { edit: true, permanent_delete: true, recent_resets: true, destructive_operations: true },
      permanent_delete_ready: true, permanent_delete_reason: null, reason: null,
    },
  });
  api.fetchCanonicalMemoryItems.mockResolvedValue({
    items: [item("mem_a", "图表默认用柱状图。"), item("mem_b", "周报只看前 10 名门店。")],
    next_cursor: null,
  });
  api.listMemoryResets.mockResolvedValue({ items: [] });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  window.history.replaceState(null, "", "/");
});

describe("Settings > Memory item deep link", () => {
  it("scrolls to and highlights the linked memory, then clears the highlight", async () => {
    window.history.replaceState(null, "", "/settings/memory/?item=mem_b");
    renderIntl(<MemoryV2Content />);

    const row = await screen.findByTestId("memory-v2-item-mem_b");
    await waitFor(() => expect(row.className).toContain("is-focus-target"));
    expect(screen.getByTestId("memory-v2-item-mem_a").className).not.toContain("is-focus-target");
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    expect(scrollIntoView.mock.contexts[0]).toBe(row);

    await waitFor(() => expect(row.className).not.toContain("is-focus-target"), { timeout: 4000 });
  });

  it("loads further pages until the linked memory appears", async () => {
    api.fetchCanonicalMemoryItems.mockImplementation(async (params: { cursor?: string | null } = {}) => (
      params.cursor
        ? { items: [item("mem_late", "按季度对比时用同比。")], next_cursor: null }
        : { items: [item("mem_a", "图表默认用柱状图。")], next_cursor: "c1" }
    ));
    window.history.replaceState(null, "", "/settings/memory/?item=mem_late");
    renderIntl(<MemoryV2Content />);

    const row = await screen.findByTestId("memory-v2-item-mem_late");
    await waitFor(() => expect(row.getAttribute("data-highlighted")).toBe("true"));
    expect(api.fetchCanonicalMemoryItems).toHaveBeenCalledWith(expect.objectContaining({ cursor: "c1" }));
  });

  it("explains that a deleted or unknown memory is unavailable without highlighting anything", async () => {
    window.history.replaceState(null, "", "/settings/memory/?item=mem_deleted");
    renderIntl(<><MemoryV2Content /><ToastContainer /></>);

    expect(await screen.findByText("这条记忆已不可用，可能已被删除或移入回收站。")).toBeTruthy();
    expect(document.querySelector(".is-focus-target")).toBeNull();
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it("does nothing without an item parameter", async () => {
    window.history.replaceState(null, "", "/settings/memory/");
    renderIntl(<><MemoryV2Content /><ToastContainer /></>);

    await screen.findByTestId("memory-v2-item-mem_a");
    await act(async () => { await Promise.resolve(); });
    expect(document.querySelector(".is-focus-target")).toBeNull();
    expect(screen.queryByText("这条记忆已不可用，可能已被删除或移入回收站。")).toBeNull();
  });
});
