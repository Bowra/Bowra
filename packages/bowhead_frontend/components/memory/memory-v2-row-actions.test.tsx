/**
 * “⋯” on Memory entries and Recent Reset rows (appearance batch four): menu names include entry summary / reset time;
 * inline actions live in menus, dangerous items last with confirmation.
 */
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { menuItemNames, openRowMenu } from "@/__tests__/row-actions";

const api = vi.hoisted(() => ({
  fetchMemoryOverview: vi.fn(),
  fetchCanonicalMemoryItems: vi.fn(),
  listMemoryResets: vi.fn(),
  permanentDeleteMemoryItem: vi.fn(),
  restoreMemoryItem: vi.fn(),
  restoreMemoryReset: vi.fn(),
  deleteEntry: vi.fn(),
  restoreEntry: vi.fn(),
  previewMemoryReset: vi.fn(),
  resetMemory: vi.fn(),
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
const item = (id: string, value: string, trashed: boolean) => ({
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
  capabilities: { edit: !trashed, soft_delete: !trashed, restore: trashed, permanent_delete: trashed },
  locked: false,
  created_at: T,
  updated_at: T,
  deleted_at: trashed ? T : null,
  purge_at: trashed ? "2026-10-22T08:00:00Z" : null,
});

beforeEach(() => {
  vi.clearAllMocks();
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
    stats: { notes_configured: false, saved_entries: 1, pending_candidates: 0, spaces_represented: 1, trash_count: 1, recoverable_resets: 1 },
    learning: { automatic_learning: "active" },
    capabilities: {
      v2_enabled: true, v2_ready: true, presentation: "v2", schema_version: 2, kind_policy_version: 1,
      actions: { edit: true, permanent_delete: true, recent_resets: true, destructive_operations: true },
      permanent_delete_ready: true, permanent_delete_reason: null, reason: null,
    },
  });
  api.fetchCanonicalMemoryItems.mockImplementation(async (params: { state?: string } = {}) => ({
    items: params.state === "trashed" ? [item("m_trash", "周报只看前 10 名门店。", true)] : [item("m_live", "图表默认用柱状图。", false)],
    next_cursor: null,
  }));
  api.listMemoryResets.mockResolvedValue({
    items: [{ id: "rs_1", created_at: T, expires_at: "2026-10-22T08:00:00Z", counts: { notes: 0, entries: 2, anchors: 0, locked_skipped: 0, pending_preserved: 0 }, state_counts: {}, remaining_recoverable: 2 }],
  });
});
afterEach(() => cleanup());

describe("记忆页行尾「⋯」", () => {
  it("names the entry menu after the entry summary: Edit, then Move to trash (danger)", async () => {
    renderIntl(<MemoryV2Content />);
    await screen.findByText("图表默认用柱状图。");
    expect(screen.queryByRole("button", { name: "编辑" })).toBeNull();
    const menu = await openRowMenu("「图表默认用柱状图。」的更多操作");
    // Behavior batch B (user decision 2026-09-28): Move to Recycle Bin is undoable, without confirmation or “…”.
    expect(menuItemNames(menu)).toEqual(["编辑", "移入回收站"]);
    expect(within(menu).getByRole("menuitem", { name: "移入回收站" }).className).toContain("is-danger");
  });

  it("names the recent reset menu after the reset time: Restore, then Delete record permanently (danger)", async () => {
    renderIntl(<MemoryV2Content />);
    const trigger = await screen.findByRole("button", { name: /^\d{4}.*的重置记录的更多操作$/ });
    fireEvent.click(trigger);
    const menu = screen.getByRole("menu");
    expect(menuItemNames(menu)).toEqual(["恢复", "永久删除记录…"]);
    expect(within(menu).getByRole("menuitem", { name: "永久删除记录…" }).className).toContain("is-danger");
  });

  it("shows the row menu as busy and closed while a reset restore runs, then re-enables it", async () => {
    let finish: (value: unknown) => void = () => {};
    api.restoreMemoryReset.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    renderIntl(<MemoryV2Content />);
    fireEvent.click(await screen.findByRole("button", { name: /^\d{4}.*的重置记录的更多操作$/ }));
    fireEvent.click(within(screen.getByRole("menu")).getByRole("menuitem", { name: "恢复" }));
    const busy = await screen.findByRole("button", { name: "正在恢复…" });
    expect(busy.getAttribute("aria-busy")).toBe("true");
    expect(api.restoreMemoryReset).toHaveBeenCalledTimes(1);
    finish({ status: "restored", remaining_recoverable: 0 });
    expect(await screen.findByRole("button", { name: /^\d{4}.*的重置记录的更多操作$/ })).toBeTruthy();
  });

  it("offers Restore and Delete permanently for trash entries; permanent deletion confirms first", async () => {
    renderIntl(<MemoryV2Content />);
    await screen.findByText("图表默认用柱状图。");
    fireEvent.click(screen.getByRole("radio", { name: /回收站/ }));
    const menu = await openRowMenu("「周报只看前 10 名门店。」的更多操作");
    expect(menuItemNames(menu)).toEqual(["恢复", "永久删除…"]);
    fireEvent.click(within(menu).getByRole("menuitem", { name: "永久删除…" }));
    expect(await screen.findByRole("alertdialog")).toBeTruthy();
    await waitFor(() => expect(api.permanentDeleteMemoryItem).not.toHaveBeenCalled());
  });
});

describe("记忆页撤销 Toast（行为批 B）", () => {
  beforeEach(() => {
    _clearAllToasts();
    api.deleteEntry.mockResolvedValue({ deleted: "m_live" });
    api.restoreEntry.mockResolvedValue({ id: "m_live", restored: true });
    api.previewMemoryReset.mockResolvedValue({ counts: { notes: 0, entries: 2, anchors: 0, locked_skipped: 0, pending_preserved: 0 } });
    api.resetMemory.mockResolvedValue({
      snapshot_id: "rs_new", affected_memory: 2, affected_notes: 0,
      counts: { notes: 0, entries: 2, anchors: 0, locked_skipped: 0, pending_preserved: 0 },
      expires_at: "2026-10-22T08:00:00Z", undo_available: true,
    });
    api.restoreMemoryReset.mockResolvedValue({ status: "restored", remaining_recoverable: 0 });
  });
  afterEach(() => _clearAllToasts());

  async function moveToTrash(viaMenu = true) {
    renderIntl(<><MemoryV2Content /><ToastContainer /></>);
    await screen.findByText("图表默认用柱状图。");
    if (viaMenu) {
      // Flag on: selection immediately executes, without confirmation.
      const menu = await openRowMenu("「图表默认用柱状图。」的更多操作");
      fireEvent.click(within(menu).getByRole("menuitem", { name: "移入回收站" }));
    } else {
      // Flag off: still confirm first.
      fireEvent.click(screen.getByRole("button", { name: "移入回收站" }));
      const dialog = await screen.findByRole("alertdialog");
      fireEvent.click(within(dialog).getByRole("button", { name: "移入回收站" }));
    }
    await waitFor(() => expect(api.deleteEntry).toHaveBeenCalledTimes(1));
  }

  it("moves to trash without a confirmation dialog", async () => {
    await moveToTrash();
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(api.deleteEntry.mock.calls[0][0]).toBe("m_live");
  });

  it("explains a failed undo and keeps the undo toast for another try", async () => {
    api.restoreEntry.mockRejectedValueOnce(new Error("network"));
    await moveToTrash();
    const toastNode = await screen.findByTestId("toast-info");
    fireEvent.click(within(toastNode).getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(api.restoreEntry).toHaveBeenCalledTimes(1));
    expect(await screen.findByTestId("toast-error")).toBeTruthy();
    // Undo becomes enabled again (not closed as completed), and another click succeeds.
    await waitFor(() => expect((within(toastNode).getByRole("button", { name: "撤销" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(within(toastNode).getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(api.restoreEntry).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("记忆已恢复")).toBeTruthy();
  });

  it("移入回收站失败：错误提示不带状态码，带「复制诊断」操作", async () => {
    const { MemoryAPIError } = await import("@/lib/memory-api");
    api.deleteEntry.mockRejectedValueOnce(new MemoryAPIError(500, "boom", undefined, "0123456789abcdef0123456789abcdef"));
    await moveToTrash();
    const toastNode = await screen.findByTestId("toast-error");
    expect(toastNode.textContent).not.toMatch(/500|boom/);
    expect(within(toastNode).getByRole("button", { name: "复制诊断" })).toBeTruthy();
  });

  it("offers undo after moving to trash; undo calls restore without the pre-delete version and refreshes the list", async () => {
    await moveToTrash();
    const toastNode = await screen.findByTestId("toast-info");
    expect(toastNode.textContent).toContain("已移入回收站");
    expect(toastNode.className).toContain("ui-toast--undo");
    const loads = api.fetchCanonicalMemoryItems.mock.calls.length;
    fireEvent.click(within(toastNode).getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(api.restoreEntry).toHaveBeenCalledTimes(1));
    expect(api.restoreEntry.mock.calls[0]).toEqual(["m_live"]);
    await waitFor(() => expect(api.fetchCanonicalMemoryItems.mock.calls.length).toBeGreaterThan(loads));
    expect(await screen.findByText("记忆已恢复")).toBeTruthy();
  });

  it("offers undo after clearing saved memory; undo restores that clear record", async () => {
    renderIntl(<><MemoryV2Content /><ToastContainer /></>);
    fireEvent.click(await screen.findByRole("button", { name: "查看范围并清空" }));
    const dialog = await screen.findByRole("dialog", { name: /清空已保存记忆/ });
    fireEvent.click(within(dialog).getByRole("button", { name: "清空已保存记忆" }));
    await waitFor(() => expect(api.resetMemory).toHaveBeenCalledTimes(1));
    const toastNode = await screen.findByTestId("toast-info");
    expect(toastNode.textContent).toContain("已清空；30 天内可以恢复。");
    fireEvent.click(within(toastNode).getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(api.restoreMemoryReset).toHaveBeenCalledTimes(1));
    expect(api.restoreMemoryReset.mock.calls[0][0]).toBe("rs_new");
    expect(await screen.findByText("已恢复保存的记忆。")).toBeTruthy();
  });

  async function resetAndUndo() {
    renderIntl(<><MemoryV2Content /><ToastContainer /></>);
    fireEvent.click(await screen.findByRole("button", { name: "查看范围并清空" }));
    const dialog = await screen.findByRole("dialog", { name: /清空已保存记忆/ });
    fireEvent.click(within(dialog).getByRole("button", { name: "清空已保存记忆" }));
    await waitFor(() => expect(api.resetMemory).toHaveBeenCalledTimes(1));
    const toastNode = await screen.findByTestId("toast-info");
    fireEvent.click(within(toastNode).getByRole("button", { name: "撤销" }));
    return toastNode;
  }

  it("retries a failed clear undo with the same idempotency key so it never restores twice", async () => {
    api.restoreMemoryReset.mockRejectedValueOnce(new Error("network"));
    const toastNode = await resetAndUndo();
    await waitFor(() => expect(api.restoreMemoryReset).toHaveBeenCalledTimes(1));
    expect(await screen.findByTestId("toast-error")).toBeTruthy();
    await waitFor(() => expect((within(toastNode).getByRole("button", { name: "撤销" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(within(toastNode).getByRole("button", { name: "撤销" }));
    await waitFor(() => expect(api.restoreMemoryReset).toHaveBeenCalledTimes(2));
    const [first, second] = api.restoreMemoryReset.mock.calls;
    expect(second[0]).toBe("rs_new");
    expect(typeof first[1]).toBe("string");
    expect(second[1]).toBe(first[1]);
  });

  it("reports how many items remain when a clear undo restores only part", async () => {
    api.restoreMemoryReset.mockResolvedValueOnce({ status: "partial", remaining_recoverable: 3 });
    await resetAndUndo();
    expect(await screen.findByText("已保留较新的数据，仍有 3 项需要处理。")).toBeTruthy();
  });
});
