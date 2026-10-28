/**
 * Sprint λ F5 (R2-a) — peer_dispatched upsert dedup tests.
 *
 * Backend `peer_dispatched` SSE emits 3× per peer from subagent.py:694/1390/1505
 * (known ρ Phase E artifact). FE must collapse to 1 entry per subagent_id, or
 * fall back to peerName when no subagent_id available.
 */
import { describe, it, expect } from "vitest";
import type { PeerDispatchInfo } from "@/lib/types";

/**
 * Simulates the upsert reducer logic from use-turn-runner.ts:peer_dispatched.
 * Pure function so we can test without React.
 */
function applyPeerDispatched(
  existing: PeerDispatchInfo[],
  payload: { peer_name?: string; subagent_id?: string; parent_ctx_summary?: string },
): PeerDispatchInfo[] {
  const peerName = String(payload.peer_name ?? "");
  const subagentId = payload.subagent_id != null ? String(payload.subagent_id) : undefined;
  const summary = String(payload.parent_ctx_summary ?? "");
  if (!peerName && !subagentId) return existing;
  const matchIdx = existing.findIndex((p) =>
    subagentId && p.subagentId
      ? p.subagentId === subagentId
      : p.peerName === peerName,
  );
  if (matchIdx >= 0) {
    const old = existing[matchIdx];
    return [
      ...existing.slice(0, matchIdx),
      {
        ...old,
        parentCtxSummary: summary || old.parentCtxSummary,
        subagentId: subagentId ?? old.subagentId,
      },
      ...existing.slice(matchIdx + 1),
    ];
  }
  return [
    ...existing,
    {
      peerName: peerName || subagentId!,
      dispatchedAt: 1000,
      parentCtxSummary: summary,
      subagentId,
    },
  ];
}

describe("λ F5 — peersDispatched upsert dedup", () => {
  it("3× same subagent_id collapses to 1 entry (backend 3-emit artifact)", () => {
    let state: PeerDispatchInfo[] = [];
    const payload = {
      peer_name: "compute",
      subagent_id: "sub-abc",
      parent_ctx_summary: "compute report",
    };
    state = applyPeerDispatched(state, payload);
    state = applyPeerDispatched(state, payload);
    state = applyPeerDispatched(state, payload);
    expect(state).toHaveLength(1);
    expect(state[0].subagentId).toBe("sub-abc");
    expect(state[0].peerName).toBe("compute");
  });

  it("distinct subagent_id keeps both entries", () => {
    let state: PeerDispatchInfo[] = [];
    state = applyPeerDispatched(state, {
      peer_name: "compute", subagent_id: "sub-1", parent_ctx_summary: "first",
    });
    state = applyPeerDispatched(state, {
      peer_name: "compute", subagent_id: "sub-2", parent_ctx_summary: "second",
    });
    expect(state).toHaveLength(2);
    expect(state.map((p) => p.subagentId).sort()).toEqual(["sub-1", "sub-2"]);
  });

  it("upsert refreshes summary on second emit (latest non-empty wins)", () => {
    let state: PeerDispatchInfo[] = [];
    state = applyPeerDispatched(state, {
      peer_name: "compute", subagent_id: "sub-x", parent_ctx_summary: "v1",
    });
    state = applyPeerDispatched(state, {
      peer_name: "compute", subagent_id: "sub-x", parent_ctx_summary: "v2 updated",
    });
    expect(state).toHaveLength(1);
    expect(state[0].parentCtxSummary).toBe("v2 updated");
  });

  it("P1-Eng-1 fix: empty summary on 2nd emit does NOT clobber non-empty old summary", () => {
    let state: PeerDispatchInfo[] = [];
    state = applyPeerDispatched(state, {
      peer_name: "compute", subagent_id: "sub-x", parent_ctx_summary: "initial summary",
    });
    state = applyPeerDispatched(state, {
      peer_name: "compute", subagent_id: "sub-x",  // no parent_ctx_summary
    });
    expect(state).toHaveLength(1);
    expect(state[0].parentCtxSummary).toBe("initial summary");
  });

  it("missing subagent_id falls back to peerName dedup", () => {
    let state: PeerDispatchInfo[] = [];
    const payload = { peer_name: "kb-analyst", parent_ctx_summary: "kb search" };
    state = applyPeerDispatched(state, payload);
    state = applyPeerDispatched(state, payload);
    expect(state).toHaveLength(1);
    expect(state[0].peerName).toBe("kb-analyst");
    expect(state[0].subagentId).toBeUndefined();
  });

  it("empty peer_name + empty subagent_id is no-op", () => {
    const state: PeerDispatchInfo[] = [];
    const result = applyPeerDispatched(state, {});
    expect(result).toBe(state);  // unchanged
  });

  it("first entry without subagentId, second with same peer_name + subagentId → upsert", () => {
    // Edge case: mock scenarios may emit peer_name first without subagent_id,
    // then real SSE adds subagent_id later. Should merge.
    let state: PeerDispatchInfo[] = [];
    state = applyPeerDispatched(state, {
      peer_name: "compute", parent_ctx_summary: "initial",
    });
    state = applyPeerDispatched(state, {
      peer_name: "compute", subagent_id: "sub-late",
      parent_ctx_summary: "with id",
    });
    expect(state).toHaveLength(1);
    expect(state[0].subagentId).toBe("sub-late");
  });
});
