import { afterEach, describe, expect, it, vi } from "vitest";

import { readReleaseSeen, releaseResultAt, releaseResultUnseen, writeReleaseSeen } from "./agent-release-seen";

const snapshot = { id: "aevs_1", hash: "sha256:x", created_at: "2026-10-18T08:00:00+00:00" };
const workbench = (kind: string, active: typeof snapshot | null = snapshot) => ({
  primary_action: { kind, enabled: true, disabled_reason_codes: [] },
  active_snapshot_identity: active,
});

describe("release result marker", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("waits on Release only for results the Owner must act on", () => {
    for (const kind of ["activate_candidate", "submit_for_review", "resolve_risks", "fix_next_draft", "retry_after_environment_fix"]) {
      expect(releaseResultAt(workbench(kind))).toBe(snapshot.created_at);
    }
    expect(releaseResultAt(workbench("view_comparison"))).toBeNull();
    expect(releaseResultAt(workbench("wait_for_review"))).toBeNull();
    expect(releaseResultAt(workbench("submit_for_review", null))).toBeNull();
  });

  it("treats a result as new until Release was opened at or after it", () => {
    expect(releaseResultUnseen(snapshot.created_at, null)).toBe(true);
    expect(releaseResultUnseen(snapshot.created_at, "2026-10-18T07:59:59+00:00")).toBe(true);
    expect(releaseResultUnseen(snapshot.created_at, snapshot.created_at)).toBe(false);
    expect(releaseResultUnseen(null, null)).toBe(false);
  });

  it("degrades to never seen when storage is unavailable", () => {
    vi.stubGlobal("localStorage", {
      getItem: () => { throw new Error("blocked"); },
      setItem: () => { throw new Error("blocked"); },
    });
    expect(() => writeReleaseSeen("osa_1", snapshot.created_at)).not.toThrow();
    expect(readReleaseSeen("osa_1")).toBeNull();
  });
});
