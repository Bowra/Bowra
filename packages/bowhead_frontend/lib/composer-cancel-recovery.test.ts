import { beforeEach, describe, expect, it } from "vitest";

import {
  clearCancelRequest,
  deterministicCancelRequestId,
  getOrCreateCancelRequest,
  listPendingCancelRequests,
  compareAndSetCancelRequestState,
} from "./composer-cancel-recovery";

function installMemoryStorage() {
  let values: Record<string, string> = {};
  const storage: Storage = {
    getItem: (key) => values[key] ?? null,
    setItem: (key, value) => { values[key] = String(value); },
    removeItem: (key) => { delete values[key]; },
    clear: () => { values = {}; },
    key: (index) => Object.keys(values)[index] ?? null,
    get length() { return Object.keys(values).length; },
  };
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: storage,
  });
}

describe("composer cancel recovery journal", () => {
  beforeEach(() => {
    installMemoryStorage();
    window.localStorage.clear();
  });

  it("matches the frozen cross-language UUIDv5 golden", () => {
    expect(
      deterministicCancelRequestId("team_1", "user_1", "turn_1", 1),
    ).toBe("eca80362-98ae-5b97-bb9b-fb435f5c27d0");
  });

  it("reuses one cancel request for the same authenticated turn generation", () => {
    const first = getOrCreateCancelRequest("team_1", "user_1", "t_running", 3);
    const retry = getOrCreateCancelRequest("team_1", "user_1", "t_running", 3);
    expect(retry).toEqual(first);
    expect(first.cancelRequestId).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu,
    );
  });

  it("keeps accepted work until terminal and scopes journals by identity", () => {
    const one = getOrCreateCancelRequest("team_1", "user_1", "t_running", 1);
    const two = getOrCreateCancelRequest("team_1", "user_1", "t_running", 2);
    const accepted = compareAndSetCancelRequestState(
      two,
      "accepted_pending_terminal",
    );
    expect(accepted).not.toBeNull();
    expect(listPendingCancelRequests("team_1", "user_1")).toEqual([
      one,
      accepted,
    ]);
    expect(listPendingCancelRequests("team_2", "user_1")).toEqual([]);

    clearCancelRequest(one);
    expect(listPendingCancelRequests("team_1", "user_1")).toEqual([accepted]);
  });

  it("does not recreate a journal cleared by a terminal event", () => {
    const posting = getOrCreateCancelRequest("team_1", "user_1", "t_terminal", 4);
    clearCancelRequest(posting);
    expect(compareAndSetCancelRequestState(
      posting,
      "accepted_pending_terminal",
    )).toBeNull();
    expect(listPendingCancelRequests("team_1", "user_1")).toEqual([]);
  });

  it("rejects a stale revision instead of overwriting a newer state", () => {
    const posting = getOrCreateCancelRequest("team_1", "user_1", "t_stale", 5);
    const reconciling = compareAndSetCancelRequestState(posting, "reconciling");
    expect(reconciling).not.toBeNull();
    expect(compareAndSetCancelRequestState(
      posting,
      "accepted_pending_terminal",
    )).toBeNull();
    expect(listPendingCancelRequests("team_1", "user_1")).toEqual([reconciling]);
  });

  it("converges across tabs without storage coordination", () => {
    const first = getOrCreateCancelRequest("team_1", "user_1", "t_cross_tab", 8);
    window.localStorage.clear();
    const second = getOrCreateCancelRequest("team_1", "user_1", "t_cross_tab", 8);
    expect(second.cancelRequestId).toBe(first.cancelRequestId);
  });

  it("keeps the deterministic Stop usable when journal writes are blocked", () => {
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: {
        getItem: () => null,
        setItem: () => { throw new DOMException("blocked", "SecurityError"); },
        removeItem: () => {},
        clear: () => {},
        key: () => null,
        length: 0,
      } satisfies Storage,
    });
    const posting = getOrCreateCancelRequest("team_1", "user_1", "t_blocked", 7);
    const accepted = compareAndSetCancelRequestState(
      posting,
      "accepted_pending_terminal",
    );
    expect(accepted?.revision).toBe(1);
    clearCancelRequest(accepted!);
    expect(compareAndSetCancelRequestState(
      accepted!,
      "accepted_pending_terminal",
    )).toBeNull();
  });

  it("reconciles and clears the volatile journal when all storage access is blocked", () => {
    const blocked = () => { throw new DOMException("blocked", "SecurityError"); };
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: {
        getItem: blocked,
        setItem: blocked,
        removeItem: blocked,
        clear: blocked,
        key: blocked,
        get length() { return blocked(); },
      } satisfies Storage,
    });
    const posting = getOrCreateCancelRequest("team_1", "user_1", "t_private", 9);
    expect(listPendingCancelRequests("team_1", "user_1")).toEqual([posting]);

    const reconciling = compareAndSetCancelRequestState(posting, "reconciling");
    expect(reconciling?.revision).toBe(1);
    expect(listPendingCancelRequests("team_1", "user_1")).toEqual([reconciling]);

    clearCancelRequest(reconciling!);
    expect(listPendingCancelRequests("team_1", "user_1")).toEqual([]);
    expect(compareAndSetCancelRequestState(
      reconciling!,
      "accepted_pending_terminal",
    )).toBeNull();
  });
});
