/**
 * kb-matrix M3 — store runs[] merge tests (§4.5 terminal stickiness), and step 1 S6 delta fit.
 *
 * Invariant: once observed, terminal runs remain sticky (even if evicted from the server's 5-run window, PRD-P1-1);
 * matrix switching/unmount clears them.
 * Read through public hook (useMatrixSnapshot), using the same subscribe/get path as components.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";

import { applyMatrixDelta, setMatrixSnapshot, useMatrixSnapshot } from "./store";
import type { MatrixCellVM, MatrixRunVM, MatrixSnapshot } from "./types";

function makeRun(runId: string, status: MatrixRunVM["status"]): MatrixRunVM {
  return {
    runId,
    status,
    cellsTotal: 4,
    cellsDone: 2,
    cellsFailed: status === "failed" ? 2 : 0,
    skippedEdited: 0,
    errorCode: null,
    etaS: 10,
    initiatedBy: "u_1",
    scopeKind: "all_pending",
    canCancel: true,
  };
}

function makeSnapshot(runs: MatrixRunVM[], matrixId = "mx_1"): MatrixSnapshot {
  return { matrixId, title: "t", readOnly: false, columns: [], rows: [], seq: 1, runs };
}

function runIds(snap: MatrixSnapshot | null): string[] {
  return (snap?.runs ?? []).map((r) => r.runId);
}

beforeEach(() => {
  vi.useFakeTimers();
  setMatrixSnapshot(null); // Module singleton: clear sticky state between tests.
});

afterEach(() => {
  setMatrixSnapshot(null);
  cleanup();
  vi.useRealTimers();
});

describe("store runs merge (M3 §4.5)", () => {
  it("flush merges delta runs onto the snapshot", () => {
    const { result } = renderHook(() => useMatrixSnapshot());
    act(() => setMatrixSnapshot(makeSnapshot([makeRun("run_a", "running")])));
    act(() => {
      applyMatrixDelta({}, 2, [makeRun("run_a", "running"), makeRun("run_b", "running")]);
      vi.advanceTimersByTime(150); // FLUSH_MS=100
    });
    expect(runIds(result.current)).toEqual(["run_a", "run_b"]);
    expect(result.current?.seq).toBe(2);
  });

  it("sticky retention: terminal run survives server-window eviction", () => {
    const { result } = renderHook(() => useMatrixSnapshot());
    // Terminal A + running B (A becomes sticky).
    act(() => setMatrixSnapshot(makeSnapshot([makeRun("run_a", "failed"), makeRun("run_b", "running")])));
    // Server's 5-run window evicts A: delta contains only B; A must remain sticky in snapshot.
    act(() => {
      applyMatrixDelta({}, 2, [makeRun("run_b", "running")]);
      vi.advanceTimersByTime(150);
    });
    expect(runIds(result.current)).toEqual(["run_b", "run_a"]);

  });

  it("setMatrixSnapshot(null) / matrix switch clears sticky runs", () => {
    const { result } = renderHook(() => useMatrixSnapshot());
    act(() => setMatrixSnapshot(makeSnapshot([makeRun("run_a", "failed")])));
    // Server window no longer lists A: it stays sticky.
    act(() => setMatrixSnapshot(makeSnapshot([makeRun("run_b", "running")])));
    expect(runIds(result.current)).toEqual(["run_b", "run_a"]);

    // Unmount (null) → reload same id: sticky set is cleared.
    act(() => setMatrixSnapshot(null));
    act(() => setMatrixSnapshot(makeSnapshot([makeRun("run_b", "running")])));
    expect(runIds(result.current)).toEqual(["run_b"]);

    // Matrix switch (different matrixId) also clears it.
    act(() => setMatrixSnapshot(makeSnapshot([makeRun("run_c", "failed")])));
    act(() => setMatrixSnapshot(makeSnapshot([makeRun("run_d", "running")], "mx_2")));
    expect(runIds(result.current)).toEqual(["run_d"]);
  });
});

// ── Step 1 S6: deltas merge only into the projection they were computed against ──

function cell(value: string, seq: number): MatrixCellVM {
  return { status: "done", value, variants: [], edited: false, verified: false, seq };
}

function gridSnapshot(digest: string | undefined): MatrixSnapshot {
  return {
    ...makeSnapshot([makeRun("run_a", "running")]),
    projectionDigest: digest,
    columns: [{ columnId: "c1", name: "Amount", type: "question" }],
    rows: [
      { fileId: "f1", fileName: "a.pdf", missing: false, extractFailed: false, recordId: "", approved: false, cells: { c1: cell("old", 1) } },
    ],
  };
}

describe("store delta fit (step 1 S6)", () => {
  it("merges a delta of the same projection and keeps its digest", () => {
    const { result } = renderHook(() => useMatrixSnapshot());
    act(() => setMatrixSnapshot(gridSnapshot("d1")));
    let merged = false;
    act(() => {
      merged = applyMatrixDelta({ "f1:c1": cell("new", 7) }, 7, undefined, "d1");
      vi.advanceTimersByTime(150);
    });
    expect(merged).toBe(true);
    expect(result.current?.rows[0].cells.c1?.value).toBe("new");
    expect(result.current?.seq).toBe(7);
    expect(result.current?.projectionDigest).toBe("d1");
  });

  it.each([
    ["another projection's digest", { "f1:c1": cell("new", 7) }, "d2"],
    ["a row the snapshot does not hold", { "f9:c1": cell("new", 7) }, "d1"],
    ["a column the snapshot does not hold", { "f1:c9": cell("new", 7) }, "d1"],
    ["a malformed key", { f1c1: cell("new", 7) }, "d1"],
  ])("refuses %s and applies nothing", (_case, cells, digest) => {
    const { result } = renderHook(() => useMatrixSnapshot());
    act(() => setMatrixSnapshot(gridSnapshot("d1")));
    let merged = true;
    act(() => {
      merged = applyMatrixDelta(cells, 7, [makeRun("run_a", "succeeded")], digest);
      vi.advanceTimersByTime(150);
    });
    expect(merged).toBe(false);
    expect(result.current?.rows[0].cells.c1?.value).toBe("old");
    expect(result.current?.seq).toBe(1);
    expect(result.current?.runs?.[0].status).toBe("running");
  });

  it("refuses a delta when no snapshot is held", () => {
    expect(applyMatrixDelta({}, 2, [])).toBe(false);
  });
});
