import { describe, expect, it } from "vitest";

import { getSteerTarget } from "./steer-guard";

const base = {
  sseMode: "sse" as const,
  running: true,
  turnId: "t_active",
  activeWorkspaceId: "ws_1",
  activeSessionId: "s_active",
  runnerSessionId: "s_active",
  allowRunnerSessionClaim: false,
};

describe("getSteerTarget", () => {
  it("accepts steer only for the active running turn in the active session", () => {
    expect(getSteerTarget(base)).toEqual({
      ok: true,
      workspaceId: "ws_1",
      sessionId: "s_active",
      turnId: "t_active",
    });
  });

  it("rejects stale runner session ids after navigation or refresh races", () => {
    expect(getSteerTarget({
      ...base,
      activeSessionId: "s_new",
      runnerSessionId: "s_old",
    })).toEqual({ ok: false, reason: "session_mismatch" });
  });

  it("rejects completed turns so drafts are not silently promoted into new tasks", () => {
    expect(getSteerTarget({ ...base, running: false })).toEqual({
      ok: false,
      reason: "not_running",
    });
    expect(getSteerTarget({ ...base, turnId: null })).toEqual({
      ok: false,
      reason: "not_running",
    });
  });

  it("rejects missing workspace/session context", () => {
    expect(getSteerTarget({ ...base, activeWorkspaceId: null })).toEqual({
      ok: false,
      reason: "missing_context",
    });
    expect(getSteerTarget({ ...base, activeSessionId: null })).toEqual({
      ok: false,
      reason: "missing_context",
    });
  });

  it("accepts the accepted Home turn before page session state catches up", () => {
    expect(getSteerTarget({
      ...base,
      activeSessionId: null,
      allowRunnerSessionClaim: true,
    })).toEqual({
      ok: true,
      workspaceId: "ws_1",
      sessionId: "s_active",
      turnId: "t_active",
    });
  });

  it("keeps the runner session fenced while New chat resets the page", () => {
    expect(getSteerTarget({
      ...base,
      activeSessionId: null,
      allowRunnerSessionClaim: false,
    })).toEqual({
      ok: false,
      reason: "missing_context",
    });
  });
});
