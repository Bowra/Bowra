/**
 * The memory receipt hook reads one REST projection (`GET /memory/turn-receipt`).
 *
 * ★ Live state after Undo/approval must equal what a refresh reads from the server, so each case
 * compares the live hook state with a second, archived mount fed the server's derived receipt.
 */

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { PendingCandidate, TurnMemoryReceipt } from "@/lib/memory-api";

const api = vi.hoisted(() => ({
  getTurnMemoryReceipt: vi.fn(),
  deleteEntry: vi.fn(),
  approveCandidate: vi.fn(),
}));
vi.mock("@/lib/memory-api", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  getTurnMemoryReceipt: (...args: unknown[]) => api.getTurnMemoryReceipt(...args),
  deleteEntry: (...args: unknown[]) => api.deleteEntry(...args),
  approveCandidate: (...args: unknown[]) => api.approveCandidate(...args),
}));

// eslint-disable-next-line import/first
import {
  mergeLocallyUndone,
  useMemoryCandidates,
} from "@/lib/use-memory-candidates";

afterEach(() => {
  api.getTurnMemoryReceipt.mockReset();
  api.deleteEntry.mockReset();
  api.approveCandidate.mockReset();
});

function receipt(over: Partial<TurnMemoryReceipt> = {}): TurnMemoryReceipt {
  return {
    schema_version: 1,
    turn_id: "t_1",
    explicit_request: null,
    pending_candidates: [],
    entries: [],
    ...over,
  };
}

const ENTRY = {
  id: "mem_1",
  kind: "preference",
  key: "ranking_chart_type",
  display_value: "排名图用横向条形图",
  origin: "remember_tool" as const,
  state: "remembered" as const,
  undoable: true,
  candidate_id: null,
};

describe("useMemoryCandidates receipt", () => {
  it("★★ Undo marks the entry undone, exactly like the refreshed server receipt", async () => {
    api.getTurnMemoryReceipt.mockResolvedValueOnce(
      receipt({
        entries: [ENTRY],
        explicit_request: { source: "remember_tool", outcome: "remembered", reason: null },
      }),
    );
    api.deleteEntry.mockResolvedValue({ deleted: "mem_1" });
    const live = renderHook(() => useMemoryCandidates("t_1", "archived"));
    await waitFor(() => expect(live.result.current.entries).toHaveLength(1));
    await act(async () => {
      expect(await live.result.current.undoEntry("mem_1")).toBe(true);
    });
    expect(api.deleteEntry).toHaveBeenCalledWith("mem_1");

    api.getTurnMemoryReceipt.mockResolvedValueOnce(
      receipt({
        entries: [{ ...ENTRY, state: "undone", undoable: false }],
        explicit_request: { source: "remember_tool", outcome: "remembered", reason: null },
      }),
    );
    const refreshed = renderHook(() => useMemoryCandidates("t_1", "archived"));
    await waitFor(() => expect(refreshed.result.current.entries).toHaveLength(1));
    expect(live.result.current.entries).toEqual(refreshed.result.current.entries);
    expect(live.result.current.explicitRequest).toEqual(
      refreshed.result.current.explicitRequest,
    );
  });

  it("a failed Undo keeps the entry remembered", async () => {
    api.getTurnMemoryReceipt.mockResolvedValueOnce(receipt({ entries: [ENTRY] }));
    api.deleteEntry.mockRejectedValue(new Error("boom"));
    const { result } = renderHook(() => useMemoryCandidates("t_1", "archived"));
    await waitFor(() => expect(result.current.entries).toHaveLength(1));
    await act(async () => {
      expect(await result.current.undoEntry("mem_1")).toBe(false);
    });
    expect(result.current.entries[0].state).toBe("remembered");
  });

  it("approval adds the candidate-origin entry a refresh would return", async () => {
    const candidate = {
      id: "cand_1",
      kind: "preference",
      key: "fiscal_year",
      display_value: "财年从7月开始",
      default_target: { type: "global", requires_confirmation: false },
      capabilities: { approve: true, reject: true, reason: null },
    } as unknown as PendingCandidate;
    api.getTurnMemoryReceipt.mockResolvedValueOnce(receipt({ pending_candidates: [candidate] }));
    api.approveCandidate.mockResolvedValue({ ...candidate, status: "approved", approved_entry_id: "mem_9" });
    const { result } = renderHook(() => useMemoryCandidates("t_1", "archived"));
    await waitFor(() => expect(result.current.items).toHaveLength(1));
    await act(async () => {
      await result.current.decide("cand_1", "remember");
    });
    expect(result.current.items).toEqual([]);
    expect(result.current.entries).toEqual([
      {
        id: "mem_9",
        kind: "preference",
        key: "fiscal_year",
        display_value: "财年从7月开始",
        origin: "candidate",
        state: "remembered",
        undoable: true,
        candidate_id: "cand_1",
      },
    ]);
  });

  it("★ a stale poll cannot flip a locally undone entry back to remembered", async () => {
    vi.useFakeTimers();
    try {
      // Every poll answers with the pre-undo state, as a response already in flight would.
      api.getTurnMemoryReceipt.mockResolvedValue(receipt({ entries: [ENTRY] }));
      api.deleteEntry.mockResolvedValue({ deleted: "mem_1" });
      const { result } = renderHook(() => useMemoryCandidates("t_1", "live"));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2600);
      });
      expect(result.current.entries[0].state).toBe("remembered");
      await act(async () => {
        await result.current.undoEntry("mem_1");
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(13000);
      });
      expect(api.getTurnMemoryReceipt.mock.calls.length).toBeGreaterThanOrEqual(3);
      expect(result.current.entries).toEqual([
        { ...ENTRY, state: "undone", undoable: false },
      ]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("mergeLocallyUndone only touches entries undone in this view", () => {
    const other = { ...ENTRY, id: "mem_2" };
    expect(mergeLocallyUndone([ENTRY, other], new Set(["mem_1"]))).toEqual([
      { ...ENTRY, state: "undone", undoable: false },
      other,
    ]);
  });

  it("an endpoint error shows nothing", async () => {
    api.getTurnMemoryReceipt.mockRejectedValue(new Error("down"));
    const { result } = renderHook(() => useMemoryCandidates("t_1", "archived"));
    await waitFor(() => expect(api.getTurnMemoryReceipt).toHaveBeenCalled());
    expect(result.current.items).toEqual([]);
    expect(result.current.entries).toEqual([]);
    expect(result.current.explicitRequest).toBeNull();
  });
});
