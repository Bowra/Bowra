/**
 * agent-feedback-loop batch 5 · receipt data fetching.
 *
 * Enforce **whether acknowledged cards reappear**: fetching is idempotent, but acknowledgement and listing can race;
 * server still counts notification unread before mark_read persists. Without local tombstones, a late list response
 * reinserts the just-dismissed card, defeating its purpose: once seen, never bother the user again.
 */

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HttpStatusError } from "@/lib/api-client";

const listFeedbackReceipts = vi.fn();
const markNotificationRead = vi.fn();

vi.mock("@/lib/feedback-candidates-api", () => ({
  listFeedbackReceipts: (...a: unknown[]) => listFeedbackReceipts(...a),
}));
vi.mock("@/lib/api", () => ({
  markNotificationRead: (...a: unknown[]) => markNotificationRead(...a),
}));

// eslint-disable-next-line import/first
import { useFeedbackReceipts } from "@/lib/use-feedback-receipts";

function _r(id: string) {
  return {
    notification_id: id,
    candidate_id: `fbc_${id}`,
    target_label: "Example Data Analyst",
    final_state: "accepted" as const,
    decided_at: "2026-09-11T03:20:00+00:00",
    mine: true,
  };
}

afterEach(() => {
  listFeedbackReceipts.mockReset();
  markNotificationRead.mockReset();
});

describe("useFeedbackReceipts", () => {
  it("关掉时一个请求都不发（关闸/会话未就绪）", () => {
    listFeedbackReceipts.mockResolvedValue({ items: [_r("n1")] });
    renderHook(() => useFeedbackReceipts(false, "t_1"));
    expect(listFeedbackReceipts).not.toHaveBeenCalled();
  });

  it("挂载即取数", async () => {
    listFeedbackReceipts.mockResolvedValue({ items: [_r("n1")] });
    const { result } = renderHook(() => useFeedbackReceipts(true, "t_1"));
    await waitFor(() => expect(result.current.items).toHaveLength(1));
  });

  it("取数失败不炸卡片（关闸时端点 404）", async () => {
    listFeedbackReceipts.mockRejectedValue(new Error("404"));
    const { result } = renderHook(() => useFeedbackReceipts(true, "t_1"));
    await waitFor(() => expect(listFeedbackReceipts).toHaveBeenCalled());
    expect(result.current.items).toEqual([]);
  });

  it("签收成功即移除", async () => {
    listFeedbackReceipts.mockResolvedValue({ items: [_r("n1"), _r("n2")] });
    markNotificationRead.mockResolvedValue(undefined);
    const { result } = renderHook(() => useFeedbackReceipts(true, "t_1"));
    await waitFor(() => expect(result.current.items).toHaveLength(2));
    await act(async () => {
      await result.current.acknowledge("n1");
    });
    expect(markNotificationRead).toHaveBeenCalledWith("n1");
    expect(result.current.items.map((r) => r.notification_id)).toEqual(["n2"]);
  });

  it("签收遇临时故障时保留卡片可重试", async () => {
    listFeedbackReceipts.mockResolvedValue({ items: [_r("n1")] });
    markNotificationRead.mockRejectedValue(new HttpStatusError("notification read", new Response(null, { status: 500 })));
    const { result } = renderHook(() => useFeedbackReceipts(true, "t_1"));
    await waitFor(() => expect(result.current.items).toHaveLength(1));
    let ok = true;
    await act(async () => {
      ok = await result.current.acknowledge("n1");
    });
    expect(ok).toBe(false);
    expect(result.current.items).toHaveLength(1);
  });

  it.each([404, 503])(
    "★ 签收永久失败（%i）照样撕掉卡片，否则用户撕不掉",
    async (status) => {
      // Real trigger: operations disables notification center with `BOWHEAD_NOTIFICATION_CENTER=0` ⇒
      // `POST /notifications/{id}/read` immediately returns 404 (routes/notifications.py::_store).
      // Treating it as retryable brings it back on every fetch, **permanently pinning the card in every chat**,
      // reproducing [[feedback_soft_failure_swallowed_as_transient]] exactly.
      listFeedbackReceipts.mockResolvedValue({ items: [_r("n1"), _r("n2")] });
      markNotificationRead.mockRejectedValue(new HttpStatusError("notification read", new Response(null, { status })));
      const { result, rerender } = renderHook(
        ({ key }: { key: string }) => useFeedbackReceipts(true, key),
        { initialProps: { key: "t_1" } },
      );
      await waitFor(() => expect(result.current.items).toHaveLength(2));
      let ok = false;
      await act(async () => {
        ok = await result.current.acknowledge("n1");
      });
      expect(ok).toBe(true);
      expect(result.current.items.map((r) => r.notification_id)).toEqual(["n2"]);
      // Tombstone must survive next fetch: server still reports n1 unread.
      rerender({ key: "t_2" });
      await waitFor(() => expect(listFeedbackReceipts).toHaveBeenCalledTimes(2));
      await waitFor(() =>
        expect(result.current.items.map((r) => r.notification_id)).toEqual(["n2"]),
      );
    },
  );

  it("★ 签收后重新取数，服务端仍报未读也不把卡变回来", async () => {
    listFeedbackReceipts.mockResolvedValue({ items: [_r("n1")] });
    markNotificationRead.mockResolvedValue(undefined);
    const { result, rerender } = renderHook(
      ({ key }: { key: string }) => useFeedbackReceipts(true, key),
      { initialProps: { key: "t_1" } },
    );
    await waitFor(() => expect(result.current.items).toHaveLength(1));
    await act(async () => {
      await result.current.acknowledge("n1");
    });
    // Server has not persisted read_at yet (or listing began before acknowledgement): same item returns.
    // ★ Add **new** n2 as second-response beacon: waiting only for `toHaveBeenCalledTimes(2)` is insufficient
    //   because it passes before `.then`; items are momentarily empty and a removed-tombstone mutation still passes
    //   (observed survivor). Wait for actual n2 rendering to prove this fetch's setItems has landed.
    listFeedbackReceipts.mockResolvedValue({ items: [_r("n1"), _r("n2")] });
    rerender({ key: "t_2" });
    await waitFor(() =>
      expect(result.current.items.map((r) => r.notification_id)).toContain("n2"),
    );
    expect(result.current.items.map((r) => r.notification_id)).toEqual(["n2"]);
  });
});
