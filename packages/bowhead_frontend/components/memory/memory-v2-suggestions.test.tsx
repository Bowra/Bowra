/**
 * Batch 1c · memory-page Awaiting confirmation area: inline expansion + bulk selection.
 *
 * This version removed old View all N suggestions overlay. Old approach had three problems,
 * each enforced by a corresponding case below:
 * ① Server gave only 3, rest required overlay; ② overlay used **separate components** (layout/actions/bulk differed);
 * ③ overlay fetched again, allowing two lists on same page to disagree.
 */
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";
import { ToastContainer } from "@/components/chat/toast-container";
import { _clearAllToasts } from "@/lib/use-toast";

const api = vi.hoisted(() => ({
  fetchMemoryOverview: vi.fn(),
  fetchCanonicalMemoryItems: vi.fn(),
  listMemoryResets: vi.fn(),
  bulkDecideCandidates: vi.fn(),
  approveCandidate: vi.fn(),
  rejectCandidate: vi.fn(),
}));

vi.mock("@/lib/memory-api", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  ...api,
}));

import { MemoryV2Content } from "./memory-v2-content";

function _cand(i: number) {
  return {
    id: `c${i}`,
    kind: "preference",
    key: `k${i}`,
    value: `v${i}`,
    display_value: `建议内容第 ${i} 条`,
    display_reason: null,
    canonical_state: "pending",
    default_target: { label: "所有空间", requires_confirmation: false },
    capabilities: { approve: true },
    expires_at: null,
  };
}

const SUGGESTIONS = Array.from({ length: 7 }, (_v, i) => _cand(i + 1));

// ⚠ This repo's vitest has no automatic cleanup: without manual cleanup, previous-case DOM remains in document,
// so `getByTestId` reports multiple matches, appearing like duplicate component rendering but actually uncleaned tests.
afterEach(() => {
  cleanup();
  _clearAllToasts();
});

beforeEach(() => {
  vi.clearAllMocks();
  api.fetchMemoryOverview.mockResolvedValue({
    schema_version: 2,
    about_you: {
      content: "",
      scope: { type: "global", space_id: null, space_name: null, availability: "available", label: "所有空间" },
      version: "0",
      updated_at: null,
      is_locked: false,
      capabilities: { edit: true, soft_delete: false, restore: false, permanent_delete: false },
    },
    suggestions: SUGGESTIONS,
    stats: {
      notes_configured: false,
      saved_entries: 0,
      pending_candidates: SUGGESTIONS.length,
      spaces_represented: 1,
      trash_count: 0,
      recoverable_resets: 0,
    },
    learning: { automatic_learning: "active" },
    capabilities: {
      v2_enabled: true,
      v2_ready: true,
      presentation: "v2",
      schema_version: 2,
      kind_policy_version: 1,
      actions: { edit: true, permanent_delete: false, recent_resets: false, destructive_operations: false },
      permanent_delete_ready: false,
      permanent_delete_reason: null,
      reason: null,
    },
  });
  api.fetchCanonicalMemoryItems.mockResolvedValue({ items: [], next_cursor: null });
  api.listMemoryResets.mockResolvedValue({ items: [] });
  api.bulkDecideCandidates.mockResolvedValue({ succeeded: 0, failed: 0, items: [] });
});

async function _render() {
  renderIntl(
    <>
      <MemoryV2Content />
      <ToastContainer />
    </>,
  );
  await waitFor(() => expect(screen.getByText("建议内容第 1 条")).toBeTruthy());
}

describe("待你确认 · 就地展开", () => {
  it("默认只露 3 条，展开按钮说清楚还剩几条", async () => {
    await _render();
    expect(screen.queryByText("建议内容第 4 条")).toBeNull();
    // Expand remaining 4: count must be **remaining**, not total, or user expects 7 new items.
    expect(screen.getByTestId("suggestions-expand").textContent).toContain("4");
  });

  it("★ 展开不再拉一次数据 —— 同一页上只有一份列表", async () => {
    await _render();
    const callsBefore = api.fetchMemoryOverview.mock.calls.length;
    fireEvent.click(screen.getByTestId("suggestions-expand"));
    await waitFor(() => expect(screen.getByText("建议内容第 7 条")).toBeTruthy());
    expect(api.fetchMemoryOverview.mock.calls.length).toBe(callsBefore);
    // Old drawer opening called listCandidates("pending", 50): page showed 3, drawer 50,
    // potentially inconsistent lists. Whole component should now have no second candidate data source.
  });

  it("进入选择模式后，逐条动作让位给勾选", async () => {
    await _render();
    fireEvent.click(screen.getByTestId("suggestions-expand"));
    fireEvent.click(screen.getByTestId("suggestions-enter-select"));
    await waitFor(() => expect(screen.getByTestId("suggestion-check-c1")).toBeTruthy());
    // Coexisting action sets leave no clear meaning for clicking ✓ then selecting the same item.
    expect(screen.queryByLabelText("保存这条建议")).toBeNull();
  });
});

describe("批量：两个动作的代价不对称", () => {
  it("★★ 批量忽略必须先二次确认，不能直接打出去", async () => {
    await _render();
    fireEvent.click(screen.getByTestId("suggestions-expand"));
    fireEvent.click(screen.getByTestId("suggestions-enter-select"));
    fireEvent.click(await screen.findByTestId("suggestion-check-c1"));
    fireEvent.click(screen.getByTestId("suggestions-bulk-ignore"));

    // Ignore writes 30-day verbatim suppression: one click suppresses N topics for a month, invisible to user.
    // Old drawer's Reject all ×N directly submitted with **one click and no confirmation**.
    expect(api.bulkDecideCandidates).not.toHaveBeenCalled();
    expect(screen.getByText(/忽略这 1 条/)).toBeTruthy();
  });

  it("★★ 批量保存不设确认 —— 它是可逆的，拦一道只是白费一次点击", async () => {
    await _render();
    fireEvent.click(screen.getByTestId("suggestions-expand"));
    fireEvent.click(screen.getByTestId("suggestions-enter-select"));
    fireEvent.click(await screen.findByTestId("suggestion-check-c1"));
    fireEvent.click(screen.getByTestId("suggestions-bulk-approve"));

    await waitFor(() =>
      expect(api.bulkDecideCandidates).toHaveBeenCalledWith(["c1"], "approve"),
    );
  });

  it("部分失败要如实说，不能报成全成", async () => {
    api.bulkDecideCandidates.mockResolvedValue({ succeeded: 1, failed: 2, items: [] });
    await _render();
    fireEvent.click(screen.getByTestId("suggestions-expand"));
    fireEvent.click(screen.getByTestId("suggestions-enter-select"));
    fireEvent.click(await screen.findByTestId("suggestion-check-c1"));
    fireEvent.click(screen.getByTestId("suggestions-bulk-approve"));
    // Reporting N processed when some failed makes user think list is cleared,
    // then mistake remaining items for new arrivals.
    await waitFor(() => expect(screen.getByText(/2 条没成功/)).toBeTruthy());
  });
});

describe("失败提示不透出后端原文", () => {
  it("接口报错时按状态给出说明，不显示后端给开发者的英文 detail", async () => {
    const { MemoryAPIError } = await import("@/lib/memory-api");
    api.fetchMemoryOverview.mockRejectedValueOnce(new MemoryAPIError(404, "entry not found or locked"));
    renderIntl(<MemoryV2Content />);
    await waitFor(() => expect(screen.getByTestId("memory-v2-error").textContent).toContain("这条记忆已不存在"));
    expect(document.body.textContent).not.toContain("entry not found or locked");
  });

  it("网络中断（非接口错误）时说明是连接问题", async () => {
    api.fetchMemoryOverview.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    renderIntl(<MemoryV2Content />);
    await waitFor(() => expect(screen.getByTestId("memory-v2-error").textContent).toContain("网络连接中断"));
    expect(document.body.textContent).not.toContain("Failed to fetch");
  });
});
