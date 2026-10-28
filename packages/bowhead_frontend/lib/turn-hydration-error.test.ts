import { describe, expect, it } from "vitest";

import {
  reconcileHydratedErrorAfterDone,
  resolveHydratedTurnError,
} from "./turn-hydration-error";

describe("reconcileHydratedErrorAfterDone", () => {
  it("does not expose Replay when a canvas operation failure is followed by success", () => {
    expect(reconcileHydratedErrorAfterDone({
      source: "canvas_op_failed",
      reasonCode: "section_not_found",
      sectionId: "section_1",
    }, "completed")).toBeNull();
  });

  it("preserves terminal turn failures and unfinished canvas failures", () => {
    const terminal = {
      source: "turn_failed" as const,
      reasonCode: "lead_internal_error",
    };
    const canvas = {
      source: "canvas_op_failed" as const,
      reasonCode: "section_not_found",
    };
    expect(reconcileHydratedErrorAfterDone(terminal, "failed")).toEqual(terminal);
    expect(reconcileHydratedErrorAfterDone(canvas, "failed")).toEqual(canvas);
  });
});

describe("resolveHydratedTurnError", () => {
  it.each(["document_information_incomplete", "document_planning_failed", "document_history_unavailable", "document_request_rejected", "document_routing_invalid", "document_routing_uncertain", "document_routing_source_unavailable"])("retains a document failure from durable state (%s)", (reasonCode) => {
    expect(resolveHydratedTurnError(null, "failed", reasonCode)).toEqual({ source: "turn_failed", reasonCode });
  });
  it("synthesizes a stable failure projection from DB-only terminal state", () => {
    expect(resolveHydratedTurnError(null, "failed", "dispatch_owner_lost"))
      .toEqual({
        source: "turn_failed",
        reasonCode: "lead_internal_error",
      });
  });

  it("renders a DB-only terminal projection abort instead of a blank bubble", () => {
    expect(resolveHydratedTurnError(
      null,
      "aborted",
      "terminal_projection_not_committed",
    )).toEqual({
      source: "turn_failed",
      reasonCode: "terminal_projection_not_committed",
    });
  });

  it("preserves replayed diagnostics and does not invent success errors", () => {
    const replayed = {
      source: "turn_failed" as const,
      reasonCode: "wallclock_exceeded",
    };
    expect(resolveHydratedTurnError(replayed, "failed", "internal"))
      .toBe(replayed);
    expect(resolveHydratedTurnError(null, "completed", null)).toBeNull();
  });
});
