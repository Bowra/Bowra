import { describe, expect, it } from "vitest";

import { resolveSessionTopbarTitle, sessionTopbarMenuTarget } from "./session-topbar-title";

describe("resolveSessionTopbarTitle", () => {
  it("uses the active session list title before a stale runner title", () => {
    expect(resolveSessionTopbarTitle({
      sessionId: "session_new",
      titleOverrides: new Map(),
      remoteItems: [{ id: "session_new", title: "Quarterly revenue analysis" }],
      localItems: [],
      runnerSessionId: "session_old",
      runnerSessionTitle: "Previous session",
    })).toBe("Quarterly revenue analysis");
  });

  it("keeps an optimistic rename visible before the refreshed list arrives", () => {
    expect(resolveSessionTopbarTitle({
      sessionId: "session_1",
      titleOverrides: new Map([["session_1", "Renamed session"]]),
      remoteItems: [{ id: "session_1", title: "Original session" }],
      localItems: [],
      runnerSessionId: "session_1",
      runnerSessionTitle: "Original session",
    })).toBe("Renamed session");
  });

  it("uses the runner title only when it belongs to the active session", () => {
    expect(resolveSessionTopbarTitle({
      sessionId: "session_1",
      titleOverrides: new Map(),
      remoteItems: [{ id: "session_1", title: "session_1" }],
      localItems: [],
      runnerSessionId: "session_1",
      runnerSessionTitle: "New analysis",
    })).toBe("New analysis");
  });

  it("returns no title when the current session has no user-facing title yet", () => {
    expect(resolveSessionTopbarTitle({
      sessionId: "session_1",
      titleOverrides: new Map(),
      remoteItems: [{ id: "session_1", title: "session_1" }],
      localItems: [],
      runnerSessionId: null,
      runnerSessionTitle: null,
    })).toBeUndefined();
  });
});

describe("sessionTopbarMenuTarget", () => {
  it("targets the runner's session when the title names the same session", () => {
    expect(sessionTopbarMenuTarget({ titleSessionId: "s_1", runnerSessionId: "s_1" })).toBe("s_1");
    expect(sessionTopbarMenuTarget({ titleSessionId: null, runnerSessionId: "s_1" })).toBe("s_1");
  });

  it("withholds the menu while the title names a different (pending) session", () => {
    // The runner still holds s_old while s_new is opening: Delete must never hit s_old under s_new's title.
    expect(sessionTopbarMenuTarget({ titleSessionId: "s_new", runnerSessionId: "s_old" })).toBeNull();
  });

  it("has no target without a runner session", () => {
    expect(sessionTopbarMenuTarget({ titleSessionId: "s_1", runnerSessionId: null })).toBeNull();
  });
});
