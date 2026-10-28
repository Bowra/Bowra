import { beforeEach, describe, expect, it, vi } from "vitest";
import { clarificationResumeFailure, readHydratedClarificationAuthority, resolveLiveClarifications } from "./hydrate-clarifications";
import { resolveHydratedTurnError } from "./turn-hydration-error";

const list = vi.hoisted(() => vi.fn());
vi.mock("./api", () => ({ listClarificationsForTurn: list }));
const failure = {
  request_id: "c_original", turn_id: "t_original", execution_generation: 1,
  code: "clarification_resume_unavailable" as const,
  recovery_action: "copy_diagnostics" as const,
};

beforeEach(() => list.mockReset());

describe("saved answer recovery on reload", () => {
  it("reads the paused original Turn after resolution removed its last card", async () => {
    list.mockResolvedValue({ items: [], resume_failure: failure });
    const candidates = resolveLiveClarifications({
      clarifications: [{ request_id: failure.request_id }], resolvedClarIds: new Set([failure.request_id]),
      final: undefined, responseKind: "chat", lastDoneStatus: "clarification_pending",
    });
    expect(candidates).toEqual([]);
    const result = await readHydratedClarificationAuthority("t_original", candidates, "clarification_pending", 1);
    expect(list).toHaveBeenCalledOnce();
    expect(result.clarifications).toEqual([]);
    expect(resolveHydratedTurnError(result.resumeFailure, "clarification_pending", "needs_clarification")).toEqual({
      source: "clarification_resume", reasonCode: failure.code, recoveryAction: "copy_diagnostics",
    });
  });

  it("does not probe completed Turns or restart execution", async () => {
    expect(await readHydratedClarificationAuthority("t_original", [], "completed", 2)).toEqual({ clarifications: [], resumeFailure: null });
    expect(list).not.toHaveBeenCalled();
  });

  it("does not promote stale generations, other Turns or unknown failure codes", () => {
    expect(clarificationResumeFailure(failure, "t_other", 1)).toBeNull();
    expect(clarificationResumeFailure(failure, "t_original", 2)).toBeNull();
    expect(clarificationResumeFailure({ ...failure, code: "internal details" } as unknown as typeof failure, "t_original", 1)).toBeNull();
  });
});
