/**
 * kb-matrix M3 — polling liveness tests (§4.5 multi-run liveness rewrite, review G-P1-1).
 *
 * Key behavior:
 * - Active run set shrinks but remains nonempty → **continue polling** after final reload (copying M2 terminal wrap kills
 *   polling for remaining runs);
 * - Set empties → reload once, then stop timer;
 * - 404/403 (matrix deleted/removed from space) → stop timer + null snapshot + onUnavailable (FE-P2-11 +
 *   S-P3-4 dead-polling fix).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";

import { MatrixApiError } from "./api-error";
import { fetchMatrixDelta, fetchMatrixSnapshot } from "./snapshot-api";
import { setMatrixSnapshot, useMatrixSnapshot } from "./store";
import type { MatrixRunVM, MatrixSnapshot } from "./types";
import { useMatrixPolling } from "./use-matrix-poll";

vi.mock("./snapshot-api", () => ({
  fetchMatrixDelta: vi.fn(),
  fetchMatrixSnapshot: vi.fn(),
}));

const mockDelta = vi.mocked(fetchMatrixDelta);
const mockSnap = vi.mocked(fetchMatrixSnapshot);

const POLL_MS = 50;

function makeRun(runId: string, status: MatrixRunVM["status"]): MatrixRunVM {
  return {
    runId,
    status,
    cellsTotal: 4,
    cellsDone: 1,
    cellsFailed: 0,
    skippedEdited: 0,
    errorCode: null,
    etaS: 10,
    initiatedBy: "u_1",
  };
}

function makeSnapshot(runs: MatrixRunVM[], seq = 1): MatrixSnapshot {
  return { matrixId: "mx_1", title: "t", readOnly: false, columns: [], rows: [], seq, pollMs: POLL_MS, runs };
}

function delta(runs: MatrixRunVM[], seq = 2) {
  return { kind: "delta" as const, delta: { seq, cells: {}, runs } };
}

beforeEach(() => {
  vi.useFakeTimers();
  setMatrixSnapshot(null);
});

afterEach(() => {
  setMatrixSnapshot(null);
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("useMatrixPolling liveness (M3 §4.5)", () => {
  it("active set shrink (A done, B still running) → reload + KEEP polling", async () => {
    const initial = makeSnapshot([makeRun("run_a", "running"), makeRun("run_b", "running")]);
    mockDelta.mockResolvedValueOnce(delta([makeRun("run_a", "succeeded"), makeRun("run_b", "running")]));
    mockSnap.mockResolvedValue(makeSnapshot([makeRun("run_a", "succeeded"), makeRun("run_b", "running")], 2));
    renderHook(() => useMatrixPolling("mx_1", initial));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS + 10);
    });
    expect(mockDelta).toHaveBeenCalledTimes(1);
    expect(mockSnap).toHaveBeenCalledTimes(1); // Set shrinks → one full final reload.

    // Remaining runs are active: chain must continue (G-P1-1: copying M2 return would stop it here).
    mockDelta.mockResolvedValueOnce(delta([makeRun("run_b", "running")], 3));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS + 10);
    });
    expect(mockDelta).toHaveBeenCalledTimes(2);
    expect(mockSnap).toHaveBeenCalledTimes(1); // No further shrink → no repeated reload.
  });

  it("active set empty → final reload then stop", async () => {
    const initial = makeSnapshot([makeRun("run_a", "running")]);
    mockDelta.mockResolvedValueOnce(delta([makeRun("run_a", "succeeded")]));
    mockSnap.mockResolvedValue(makeSnapshot([makeRun("run_a", "succeeded")], 2));
    renderHook(() => useMatrixPolling("mx_1", initial));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS + 10);
    });
    expect(mockDelta).toHaveBeenCalledTimes(1);
    expect(mockSnap).toHaveBeenCalledTimes(1);

    // Timer stopped: further ticks do not fetch delta.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS * 4);
    });
    expect(mockDelta).toHaveBeenCalledTimes(1);
  });

  it("404 in tick → stop polling + snapshot null + onUnavailable (FE-P2-11)", async () => {
    const initial = makeSnapshot([makeRun("run_a", "running")]);
    const onUnavailable = vi.fn();
    mockDelta.mockRejectedValue(new MatrixApiError(404, "unknown", "matrix not found", {}));
    const store = renderHook(() => useMatrixSnapshot());
    act(() => setMatrixSnapshot(initial));
    renderHook(() => useMatrixPolling("mx_1", initial, onUnavailable));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS + 10);
    });
    expect(mockDelta).toHaveBeenCalledTimes(1);
    expect(onUnavailable).toHaveBeenCalledTimes(1);
    expect(store.result.current).toBeNull(); // Null snapshot → view renders unavailable state.

    // Stop timer: no retries.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS * 4);
    });
    expect(mockDelta).toHaveBeenCalledTimes(1);
  });

  it("403 in reload → onUnavailable + no throw (被移出空间同永久错误, S-P3-4)", async () => {
    const onUnavailable = vi.fn();
    mockSnap.mockRejectedValue(new MatrixApiError(403, "forbidden", "not a member", {}));
    const { result } = renderHook(() => useMatrixPolling("mx_1", null, onUnavailable));
    await act(async () => {
      await result.current.reload(); // Do not propagate exception.
    });
    expect(onUnavailable).toHaveBeenCalledTimes(1);
  });

  it("other errors keep the silent-retry loop (kb-files-manager 范式)", async () => {
    const initial = makeSnapshot([makeRun("run_a", "running")]);
    mockDelta.mockRejectedValueOnce(new Error("network"));
    mockDelta.mockResolvedValueOnce(delta([makeRun("run_a", "running")], 3));
    renderHook(() => useMatrixPolling("mx_1", initial));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS + 10);
    });
    expect(mockDelta).toHaveBeenCalledTimes(1);
    // Silent retry: continue on next tick.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS + 10);
    });
    expect(mockDelta).toHaveBeenCalledTimes(2);
  });
});

it("idle matrices reproject revoked sources even if the matrix seq has not changed", async () => {
  const before = makeSnapshot([], 5);
  before.rows = [{ fileId: "f", fileName: "Private", missing: false, extractFailed: false, recordId: "", approved: false, cells: {} }];
  const after = { ...before, rows: [{ ...before.rows[0], fileName: "", missing: true }] };
  mockDelta.mockResolvedValue({ kind: "full", snapshot: after });
  const store = renderHook(() => useMatrixSnapshot());
  act(() => setMatrixSnapshot(before));
  renderHook(() => useMatrixPolling("mx_1", before));
  await act(async () => { await vi.advanceTimersByTimeAsync(30_010); });
  expect(mockDelta).toHaveBeenCalledTimes(1);
  expect(store.result.current?.rows[0]).toMatchObject({ fileName: "", missing: true });
});

it("discard a reload response after leaving the matrix, including late action callbacks", async () => {
  let resolve!: (snapshot: MatrixSnapshot) => void;
  mockSnap.mockReturnValueOnce(new Promise((r) => { resolve = r; }));
  const view = renderHook(() => useMatrixPolling("mx_1", null));
  const observer = renderHook(() => useMatrixSnapshot());
  let pending!: Promise<void>;
  act(() => { pending = view.result.current.reload(); });
  const oldReload = view.result.current.reload;
  view.unmount();
  const current = { ...makeSnapshot([]), matrixId: "mx_other", title: "New space" };
  act(() => setMatrixSnapshot(current));
  await act(async () => { resolve(makeSnapshot([])); await pending; });
  expect(observer.result.current?.matrixId).toBe("mx_other");
  await act(async () => { await oldReload(); });
  expect(mockSnap).toHaveBeenCalledTimes(1);
});
