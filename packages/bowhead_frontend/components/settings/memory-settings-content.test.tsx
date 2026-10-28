import {
  cleanup,
  fireEvent,
  screen,
  within,
  waitFor,
} from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { chooseRowAction } from "@/__tests__/row-actions";
import { ToastContainer } from "@/components/chat/toast-container";
import { _clearAllToasts } from "@/lib/use-toast";

const api = vi.hoisted(() => ({
  approveCandidate: vi.fn(),
  beginAboutYouEditSession: vi.fn(),
  bulkDecideCandidates: vi.fn(),
  deleteAboutYou: vi.fn(),
  deleteEntry: vi.fn(),
  exportMemoryUrl: vi.fn(() => "/api/v1/memory/export"),
  fetchCanonicalMemoryItems: vi.fn(),
  fetchMemoryOverview: vi.fn(),
  listCandidates: vi.fn(),
  listMemoryResets: vi.fn(),
  patchCanonicalMemoryItem: vi.fn(),
  patchAboutYou: vi.fn(),
  pauseDreaming: vi.fn(),
  permanentDeleteAllMemory: vi.fn(),
  permanentDeleteMemoryItem: vi.fn(),
  permanentDeleteMemoryReset: vi.fn(),
  permanentlyClearMemoryTrash: vi.fn(),
  previewMemoryReset: vi.fn(),
  rejectCandidate: vi.fn(),
  releaseAboutYouEditSession: vi.fn(),
  resetMemory: vi.fn(),
  restoreEntry: vi.fn(),
  restoreMemoryReset: vi.fn(),
  resumeDreaming: vi.fn(),
}));

vi.mock("@/lib/memory-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/memory-api")>();
  return { ...actual, ...api };
});

import { MemorySettingsContent } from "./memory-settings-content";

const capabilities = {
  v2_enabled: true,
  v2_ready: true,
  presentation: "v2" as const,
  schema_version: 3,
  kind_policy_version: 1,
  actions: {
    edit: false,
    permanent_delete: false,
    recent_resets: false,
    destructive_operations: false,
  },
  permanent_delete_ready: false,
  permanent_delete_reason: "privacy_deletion_ledger_unavailable",
  reason: null,
};

const overview = {
  schema_version: 2,
  about_you: {
    content: "负责零售分析\n汇报先给结论",
    scope: {
      type: "global" as const,
      space_id: null,
      space_name: null,
      availability: "available" as const,
      label: "仅供你使用 · 所有空间",
    },
    version: "2",
    updated_at: "2026-07-27T08:00:00Z",
    is_locked: false,
    capabilities: {
      edit: true,
      soft_delete: true,
      restore: false,
      permanent_delete: false,
    },
  },
  suggestions: [
    {
      id: "cand_1",
      userId: "usr_1",
      kind: "preference",
      key: null,
      value: "优先看同比",
      reason: "你在对话中多次要求",
      sourceTurnId: "t_1",
      status: "pending" as const,
      decidedBy: null,
      decidedAt: null,
      createdAt: "2026-07-27T08:00:00Z",
      updatedAt: "2026-07-27T08:00:00Z",
      display_value: "优先看同比",
      display_reason: "你在对话中多次要求",
      default_target: {
        type: "space" as const,
        space_id: "ws_retail",
        space_name: "零售分析",
        availability: "available" as const,
        label: "仅供你使用 · 零售分析",
        requires_confirmation: false,
      },
      canonical_state: "pending" as const,
      capabilities: { approve: true, reject: true, reason: null },
    },
  ],
  stats: {
    notes_configured: true,
    saved_entries: 1,
    pending_candidates: 1,
    spaces_represented: 1,
    trash_count: 1,
    recoverable_resets: 0,
  },
  learning: { automatic_learning: "active" as const },
  capabilities,
};

const item = {
  id: "mem_1",
  resource_type: "memory_entry" as const,
  display_type: "work_rule" as const,
  display_value: "汇报时先给结论",
  display_schema: {
    name: "text_v1",
    payload: { content: "汇报时先给结论" },
    editable_fields: [],
  },
  scope: {
    type: "space" as const,
    space_id: "ws_retail",
    space_name: "零售分析",
    availability: "available" as const,
    label: "仅供你使用 · 零售分析",
  },
  provenance: {
    created_from: {
      type: "candidate_approved",
      source_label: "你确认过的一条建议",
      source_available: false,
      source_open_token: null,
    },
    last_modified: {
      actor_type: "user",
      actor_id: "usr_1",
      modified_at: "2026-07-27T08:00:00Z",
    },
  },
  capabilities: {
    edit: true,
    soft_delete: true,
    restore: false,
    permanent_delete: false,
  },
  locked: false,
  created_at: "2026-07-27T08:00:00Z",
  updated_at: "2026-07-27T08:00:00Z",
  deleted_at: null,
  purge_at: null,
  version: "1",
};

describe("MemorySettingsContent", () => {
  beforeEach(() => {
    api.fetchMemoryOverview.mockResolvedValue(overview);
    api.fetchCanonicalMemoryItems.mockResolvedValue({
      items: [item],
      next_cursor: null,
    });
    api.listCandidates.mockResolvedValue({
      items: overview.suggestions,
      next_cursor: null,
    });
    api.listMemoryResets.mockResolvedValue({
      items: [],
      next_cursor: null,
    });
    api.approveCandidate.mockResolvedValue({});
    api.beginAboutYouEditSession.mockResolvedValue({
      edit_token: "edit-token-that-is-long-enough-for-the-api",
      version: "2",
      expires_at: "2026-07-27T08:05:00Z",
    });
    api.rejectCandidate.mockResolvedValue({});
    api.pauseDreaming.mockResolvedValue({ paused: true });
    api.permanentDeleteAllMemory.mockResolvedValue({
      operation_id: "mop_all",
      idempotency_key: "1",
      action: "permanent_delete_all",
      target_id: null,
      status: "completed",
      phase: "finalize",
      cutoff_at: "2026-07-27T08:00:00Z",
      privacy_epoch: 2,
      progress_current: 1,
      progress_total: 1,
      retryable: false,
      last_error_code: null,
      completed_at: "2026-07-27T08:00:01Z",
      counts: { entries: 1 },
      skipped_locked: 0,
      dreaming_paused: true,
      partial_failures: [],
      logical_scrub_completed: true,
      physical_purge_completed: true,
      deletion_receipt_committed: true,
    });
    api.patchCanonicalMemoryItem.mockResolvedValue({
      ...item,
      display_value: "先给建议，再给依据",
      display_schema: {
        ...item.display_schema,
        payload: { content: "先给建议，再给依据" },
      },
      version: "2",
    });
    api.patchAboutYou.mockResolvedValue({
      ...overview.about_you,
      content: "负责全球零售分析",
      version: "3",
    });
    api.releaseAboutYouEditSession.mockResolvedValue(undefined);
    api.resumeDreaming.mockResolvedValue({ paused: false });
    api.deleteEntry.mockResolvedValue({ deleted: "mem_1" });
    api.deleteAboutYou.mockResolvedValue({
      deleted: "about_you",
      recovery_id: "snap_about",
      purge_at: "2026-08-26T08:00:00Z",
      version: "3",
    });
    api.restoreEntry.mockResolvedValue({ id: "mem_1", restored: true });
    api.previewMemoryReset.mockResolvedValue({
      counts: {
        notes: 1,
        entries: 1,
        anchors: 0,
        locked_skipped: 0,
        pending_preserved: 1,
      },
    });
    api.resetMemory.mockResolvedValue({
      snapshot_id: "snap_1",
      affected_memory: 1,
      affected_notes: 1,
      counts: {
        notes: 1,
        entries: 1,
        anchors: 0,
        locked_skipped: 0,
        pending_preserved: 1,
      },
      expires_at: "2026-08-26T08:00:00Z",
      undo_available: true,
    });
  });

  afterEach(() => {
    cleanup();
    _clearAllToasts();
    vi.clearAllMocks();
  });

  it("renders the canonical V2 surface with scope and provenance", async () => {
    renderIntl(<MemorySettingsContent />);

    expect(await screen.findByTestId("memory-v2")).toBeTruthy();
    expect(screen.getByText("你的记忆")).toBeTruthy();
    expect(screen.getAllByText("仅供你使用 · 零售分析").length).toBeGreaterThan(
      0,
    );
    expect(screen.getByText("你确认过的一条建议")).toBeTruthy();
    expect(screen.queryByTestId("btn-add-entry")).toBeNull();
  });

  it("submits the server-provided candidate target", async () => {
    renderIntl(<MemorySettingsContent />);
    await screen.findByTestId("memory-v2");

    fireEvent.click(screen.getByLabelText("保存这条建议"));

    await waitFor(() => {
      expect(api.approveCandidate).toHaveBeenCalledWith(
        "cand_1",
        undefined,
        overview.suggestions[0].default_target,
      );
    });
  });

  it("confirms a global suggestion inside the product UI", async () => {
    const globalSuggestion = {
      ...overview.suggestions[0],
      default_target: {
        ...overview.suggestions[0].default_target,
        type: "global" as const,
        space_id: null,
        space_name: null,
        label: "仅供你使用 · 所有空间",
        requires_confirmation: true,
      },
    };
    api.fetchMemoryOverview.mockResolvedValue({
      ...overview,
      suggestions: [globalSuggestion],
    });
    const nativeConfirm = vi.spyOn(window, "confirm");
    renderIntl(<MemorySettingsContent />);
    await screen.findByTestId("memory-v2");

    fireEvent.click(screen.getByLabelText("保存这条建议"));

    expect(api.approveCandidate).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("alertdialog", {
      name: "保存到所有空间？",
    });
    fireEvent.click(
      within(dialog).getByRole("button", { name: "保存这条建议" }),
    );

    await waitFor(() => {
      expect(api.approveCandidate).toHaveBeenCalledWith(
        "cand_1",
        undefined,
        globalSuggestion.default_target,
      );
    });
    expect(nativeConfirm).not.toHaveBeenCalled();
  });

  it("switches to canonical trash reads", async () => {
    renderIntl(<MemorySettingsContent />);
    await screen.findByTestId("memory-v2");

    fireEvent.click(screen.getByRole("radio", { name: "回收站 1" }));

    await waitFor(() => {
      expect(api.fetchCanonicalMemoryItems).toHaveBeenCalledWith(
        expect.objectContaining({ state: "trashed" }),
      );
    });
  });

  it("edits through the server schema without changing scope", async () => {
    renderIntl(<MemorySettingsContent />);
    await screen.findByTestId("memory-v2");

    await chooseRowAction("「汇报时先给结论」的更多操作", "编辑");
    const field = screen.getByLabelText("记忆内容");
    fireEvent.change(field, { target: { value: "先给建议，再给依据" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      expect(api.patchCanonicalMemoryItem).toHaveBeenCalledWith("mem_1", {
        schema: "text_v1",
        patch: { content: "先给建议，再给依据" },
        version: "1",
      });
    });
    expect(await screen.findByText("先给建议，再给依据")).toBeTruthy();
  });

  it("edits About you through an owner lease", async () => {
    renderIntl(<MemorySettingsContent />);
    await screen.findByTestId("memory-v2");

    fireEvent.click(screen.getByLabelText("编辑关于你"));
    await waitFor(() => {
      expect(api.beginAboutYouEditSession).toHaveBeenCalledWith("2");
    });
    const field = screen.getByLabelText("记忆内容");
    fireEvent.change(field, { target: { value: "负责全球零售分析" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      expect(api.patchAboutYou).toHaveBeenCalledWith({
        content: "负责全球零售分析",
        version: "2",
        editToken: "edit-token-that-is-long-enough-for-the-api",
      });
    });
    expect(await screen.findByText("负责全球零售分析")).toBeTruthy();
  });

  it("keeps the About you lease through the Strict Mode effect replay", async () => {
    renderIntl(
      <StrictMode>
        <MemorySettingsContent />
      </StrictMode>,
    );
    await screen.findByTestId("memory-v2");

    fireEvent.click(screen.getByLabelText("编辑关于你"));

    expect(await screen.findByRole("button", { name: "保存" })).toBeTruthy();
    expect(api.beginAboutYouEditSession).toHaveBeenCalledTimes(1);
    expect(api.releaseAboutYouEditSession).not.toHaveBeenCalled();
  });

  it("moves About you to independently recoverable trash", async () => {
    const nativeConfirm = vi.spyOn(window, "confirm");
    renderIntl(<MemorySettingsContent />);
    await screen.findByTestId("memory-v2");

    fireEvent.click(screen.getByLabelText("将关于你移入回收站"));

    expect(api.deleteAboutYou).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("alertdialog", {
      name: "将「关于你」移入回收站？",
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "移入回收站" }));

    await waitFor(() => {
      expect(api.deleteAboutYou).toHaveBeenCalledWith("2");
    });
    expect(api.fetchMemoryOverview).toHaveBeenCalledTimes(2);
    expect(nativeConfirm).not.toHaveBeenCalled();
  });

  it("uses a distinct filtered empty state and can clear all filters", async () => {
    api.fetchCanonicalMemoryItems
      .mockResolvedValueOnce({ items: [item], next_cursor: null })
      .mockResolvedValueOnce({ items: [], next_cursor: null })
      .mockResolvedValue({ items: [item], next_cursor: null });
    renderIntl(<MemorySettingsContent />);
    await screen.findByTestId("memory-v2");

    fireEvent.change(screen.getByLabelText("按类型筛选"), {
      target: { value: "preference" },
    });

    expect(await screen.findByText("没有符合当前筛选条件的记忆")).toBeTruthy();
    expect(screen.queryByText("还没有已保存的记忆")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "清除筛选" }));

    await waitFor(() => {
      expect(api.fetchCanonicalMemoryItems).toHaveBeenLastCalledWith({
        state: "active",
        query: "",
        // An empty query matches no localized source line (09ebe34b1).
        sourceLabelKeys: [],
        displayType: null,
        scopeType: null,
      });
    });
  });

  it("lists applied filters as removable chips once two are set", async () => {
    api.fetchCanonicalMemoryItems.mockResolvedValue({ items: [item], next_cursor: null });
    renderIntl(<MemorySettingsContent />);
    await screen.findByTestId("memory-v2");

    fireEvent.change(screen.getByLabelText("按类型筛选"), { target: { value: "preference" } });
    // One condition only: no chip row.
    await waitFor(() => expect(api.fetchCanonicalMemoryItems).toHaveBeenLastCalledWith(expect.objectContaining({ displayType: "preference" })));
    expect(screen.queryByRole("list", { name: "已选筛选" })).toBeNull();

    fireEvent.change(screen.getByLabelText("按范围筛选"), { target: { value: "global" } });
    const chips = await screen.findByRole("list", { name: "已选筛选" });
    expect(within(chips).getByText("类型：偏好")).toBeTruthy();
    expect(within(chips).getByText("范围：所有空间")).toBeTruthy();

    // Removing a chip clears the corresponding filter control.
    fireEvent.click(within(chips).getByRole("button", { name: "移除筛选：类型：偏好" }));
    await waitFor(() =>
      expect(api.fetchCanonicalMemoryItems).toHaveBeenLastCalledWith(expect.objectContaining({ displayType: null, scopeType: "global" })),
    );
    expect((screen.getByLabelText("按类型筛选") as HTMLSelectElement).value).toBe("");
    expect(screen.queryByRole("list", { name: "已选筛选" })).toBeNull();
  });

  it("shows a list loading state instead of an incorrect empty state", async () => {
    let resolveTrash:
      | ((value: { items: (typeof item)[]; next_cursor: null }) => void)
      | undefined;
    api.fetchCanonicalMemoryItems
      .mockResolvedValueOnce({ items: [item], next_cursor: null })
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveTrash = resolve;
          }),
      );
    renderIntl(<MemorySettingsContent />);
    await screen.findByTestId("memory-v2");

    fireEvent.click(screen.getByRole("radio", { name: "回收站 1" }));

    expect(await screen.findByRole("status", { name: "正在更新记忆列表…" })).toBeTruthy();
    expect(screen.queryByText("回收站是空的")).toBeNull();
    resolveTrash?.({ items: [], next_cursor: null });
    expect(await screen.findByText("回收站是空的")).toBeTruthy();
  });

  it("restores focus to the About you edit trigger on Escape", async () => {
    renderIntl(<MemorySettingsContent />);
    await screen.findByTestId("memory-v2");

    const trigger = screen.getByLabelText("编辑关于你");
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = await screen.findByRole("dialog", {
      name: "编辑关于你",
    });
    fireEvent.keyDown(dialog, { key: "Escape" });

    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("shows the gate's sentence, not the raw error code, when a write is refused", async () => {
    // ★ Real 116 (1.2.20): adopting a language candidate in settings showed raw Toast text
    // `memory_write_gate_refused` because `MemoryAPIError.message` was detail (=code).
    // Backend returned a written explanation, but this layer discarded it.
    //
    // ★ Mutation: replace memory-v2-content translation with `(error as Error).message` ⇒ this case fails.
    const { MemoryAPIError } = await import("@/lib/memory-api");
    api.approveCandidate.mockRejectedValueOnce(
      new MemoryAPIError(422, "memory_write_gate_refused", {
        detail: { code: "memory_write_gate_refused", reason: "language_is_a_setting" },
      }),
    );
    renderIntl(
      <>
        <MemorySettingsContent />
        <ToastContainer />
      </>,
    );
    await screen.findByTestId("memory-v2");

    // ★ After batch 1c, this is the **only site**: View All drawer (candidates-section) was removed along with
    // inline expansion, taking its duplicate `(error as Error).message` with it.
    fireEvent.click(screen.getAllByRole("button", { name: "保存这条建议" })[0]);

    await screen.findByText(
      "回答语言由设置决定，不作为记忆保存，所以这条没有记进记忆。",
    );
    expect(screen.queryByText("memory_write_gate_refused")).toBeNull();
  });

  it("does not fake a paused state when the request fails", async () => {
    api.pauseDreaming.mockRejectedValueOnce(new Error("network down"));
    renderIntl(
      <>
        <MemorySettingsContent />
        <ToastContainer />
      </>,
    );
    await screen.findByTestId("memory-v2");

    const toggle = screen.getByTestId("memory-v2-learning");
    fireEvent.click(toggle);

    // User-facing failure describes the connection problem, not raw underlying error text.
    await screen.findByText("网络连接中断，请检查网络后重试。");
    expect(screen.queryByText("network down")).toBeNull();
    expect(toggle.getAttribute("aria-checked")).toBe("true");
  });

  it("previews the exact reset scope before clearing", async () => {
    renderIntl(<MemorySettingsContent />);
    await screen.findByTestId("memory-v2");

    fireEvent.click(screen.getByRole("button", { name: "查看范围并清空" }));

    expect(
      await screen.findByRole("heading", { name: "清空已保存记忆？" }),
    ).toBeTruthy();
    expect(
      screen.getByText("待确认建议和内部运行状态不会被清空。"),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "清空已保存记忆" }));

    await waitFor(() => {
      expect(api.resetMemory).toHaveBeenCalledTimes(1);
    });
  });

  it("requires a dedicated confirmation before deleting all memory", async () => {
    const enabledCapabilities = {
      ...capabilities,
      actions: {
        ...capabilities.actions,
        permanent_delete: true,
        destructive_operations: true,
      },
      permanent_delete_ready: true,
      permanent_delete_reason: null,
    };
    api.fetchMemoryOverview.mockResolvedValue({
      ...overview,
      capabilities: enabledCapabilities,
    });
    renderIntl(<MemorySettingsContent />);
    await screen.findByTestId("memory-v2");

    fireEvent.click(screen.getByRole("button", { name: "永久删除全部" }));

    expect(api.permanentDeleteAllMemory).not.toHaveBeenCalled();
    expect(await screen.findByRole("alertdialog")).toBeTruthy();
    expect(
      screen.getByText(
        "原始对话、报告和空间术语有独立的数据生命周期，不会由此删除；自动学习会暂停。",
      ),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "确认永久删除" }));

    await waitFor(() => {
      expect(api.permanentDeleteAllMemory).toHaveBeenCalledTimes(1);
    });
  });
});
