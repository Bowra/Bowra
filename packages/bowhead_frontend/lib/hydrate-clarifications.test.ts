import { describe, expect, it } from "vitest";

import {
  clarificationId,
  resolveLiveClarifications,
  retainPendingClarifications,
} from "./hydrate-clarifications";

const CLAR = { clarification_id: "c1", questions: [{ question: "q", options: ["a"], type: "multi_select" }] };

describe("resolveLiveClarifications (dogfood-0708 F1)", () => {
  it("authority probe 只保留后端仍 pending 的 request", () => {
    const legacy = { request_id: "c2" };
    expect(clarificationId(CLAR)).toBe("c1");
    expect(clarificationId(legacy)).toBe("c2");
    expect(
      retainPendingClarifications([CLAR, legacy], new Set(["c2"])),
    ).toEqual([legacy]);
  });

  it("F1: 仍暂停(clarification_pending)但 done 已 stamp response_kind → 卡保留（不被 proxy 误杀）", () => {
    // Exact F1 production shape: paused-turn done{clarification_pending} also carries response_kind=canvas.
    const out = resolveLiveClarifications({
      clarifications: [CLAR],
      resolvedClarIds: new Set(),
      final: undefined,
      responseKind: "canvas",          // Old proxy would wrongly infer turnEnded.
      lastDoneStatus: "clarification_pending",
    });
    expect(out).toHaveLength(1);
    expect((out[0] as typeof CLAR).clarification_id).toBe("c1");
  });

  it("resume 到完成：末次 done=completed → 全丢（不复活旧卡）", () => {
    const out = resolveLiveClarifications({
      clarifications: [CLAR],
      resolvedClarIds: new Set(),
      final: { some: "payload" },
      responseKind: "canvas",
      lastDoneStatus: "completed",
    });
    expect(out).toEqual([]);
  });

  it("dismissed / failed / aborted 终态 → 全丢", () => {
    for (const st of ["clarification_dismissed", "failed", "aborted", "error"]) {
      expect(
        resolveLiveClarifications({
          clarifications: [CLAR], resolvedClarIds: new Set(),
          final: undefined, responseKind: null, lastDoneStatus: st,
        }),
      ).toEqual([]);
    }
  });

  it("仍暂停但该卡已 resolve → 丢这张，留其它未答的", () => {
    const c2 = { clarification_id: "c2", questions: [] };
    const out = resolveLiveClarifications({
      clarifications: [CLAR, c2],
      resolvedClarIds: new Set(["c1"]),
      final: undefined, responseKind: "canvas",
      lastDoneStatus: "clarification_pending",
    });
    expect(out).toHaveLength(1);
    expect((out[0] as typeof c2).clarification_id).toBe("c2");
  });

  it("F1 review 回归守卫：dismiss(换个问法)的卡刷新不复活", () => {
    // Dismiss now emits clarification.dismissed → page.tsx adds it to resolvedClarIds (discard set).
    // Key: lastDoneStatus stays clarification_pending (dismiss emits no done); relying only on
    // pausedOnClarification wrongly retains the card, so the discard set covers it. Old responseKind proxy incidentally blocked it;
    // after F1 removed that proxy, this explicit path must take over.
    const out = resolveLiveClarifications({
      clarifications: [CLAR],
      resolvedClarIds: new Set(["c1"]), // ← From clarification.dismissed event.
      final: undefined, responseKind: "canvas",
      lastDoneStatus: "clarification_pending",
    });
    expect(out).toEqual([]);
  });

  it("老事件无 done status：final/responseKind proxy 仍作兜底（drop）", () => {
    expect(
      resolveLiveClarifications({
        clarifications: [CLAR], resolvedClarIds: new Set(),
        final: { x: 1 }, responseKind: null, lastDoneStatus: null,
      }),
    ).toEqual([]);
    // No terminal signal → retain (genuinely running/paused).
    expect(
      resolveLiveClarifications({
        clarifications: [CLAR], resolvedClarIds: new Set(),
        final: undefined, responseKind: null, lastDoneStatus: null,
      }),
    ).toHaveLength(1);
  });
});
