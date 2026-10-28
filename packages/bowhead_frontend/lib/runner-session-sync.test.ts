import { describe, expect, it } from "vitest";

import {
  reconcileRunnerSessionSync,
  shouldIgnoreRunnerSessionOnReset,
} from "./runner-session-sync";

/**
 * Replay frames with actual React effect semantics: the sync effect reruns only when
 * [runner.sessionId, activeSessionId] changes — precisely why the guard got stuck.
 */
function makePage(initial: { runner: string | null; active: string | null }) {
  let runner = initial.runner;
  let active = initial.active;
  let ignoring = false;
  let lastDeps: [string | null, string | null] | null = null;
  const effect = () => {
    if (lastDeps && lastDeps[0] === runner && lastDeps[1] === active) return;
    lastDeps = [runner, active];
    const next = reconcileRunnerSessionSync({ ignoring }, { runnerSessionId: runner, activeSessionId: active });
    ignoring = next.ignoring;
    if (next.adopt) {
      active = next.adopt;
      effect();
    }
  };
  effect(); // mount
  return {
    newWork() {
      ignoring = shouldIgnoreRunnerSessionOnReset(runner);
      active = null;
      effect();
    },
    runnerResets() {
      runner = null;
      effect();
    },
    firstMessageAccepted(sessionId: string) {
      runner = sessionId;
      effect();
    },
    get active() {
      return active;
    },
  };
}

describe("runner → active session sync guard", () => {
  it("adopts the first message's session after New work on an already-clean page", () => {
    // Measured on 116 (2026-09-24): open home, click「新工作」, ask a question — the old guard never unlocks, so the URL never gets
    // ?session=, and the second message creates another session.
    const page = makePage({ runner: null, active: null });
    page.newWork();
    page.firstMessageAccepted("s_new");
    expect(page.active).toBe("s_new");
  });

  it("still refuses to restore the old session in the frame after New work", () => {
    // What the guard must prevent: when active is cleared before runner loses its old ID, do not restore the old session.
    const page = makePage({ runner: "s_old", active: "s_old" });
    page.newWork();
    expect(page.active).toBeNull();
    page.runnerResets();
    expect(page.active).toBeNull();
    page.firstMessageAccepted("s_new");
    expect(page.active).toBe("s_new");
  });

  it("only needs the guard when the runner still holds a session id", () => {
    expect(shouldIgnoreRunnerSessionOnReset(null)).toBe(false);
    expect(shouldIgnoreRunnerSessionOnReset(undefined)).toBe(false);
    expect(shouldIgnoreRunnerSessionOnReset("s_old")).toBe(true);
  });
});
