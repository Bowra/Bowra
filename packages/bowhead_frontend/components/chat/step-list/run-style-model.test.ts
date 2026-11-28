// @vitest-environment jsdom
/**
 * Chat running state R3.1: the three other styles draw the same groups as the list. Chips merge consecutive finished
 * steps of one kind, dots keep every step and note, the timeline places steps on the Turn's own clock.
 */
import { describe, expect, it } from "vitest";

import { buildRunGroups } from "./run-groups";
import { chipsOf, longestSegment, marksOf, segmentsOf, timelineSpan, windowMarks, type RunMarkWords } from "./run-style-model";
import type { StepState, StepView } from "./types";

const words: RunMarkWords = { note: "说明", draft: "初稿", failedTimes: (count) => `失败 ${count} 次` };

function sv(partial: Partial<StepView> & { stepId: string }): StepView {
  return {
    parentStepId: null,
    concurrencyGroupId: null,
    stepType: "tool_run",
    toolName: "bi_query",
    headerZh: "查询数据",
    state: "done" as StepState,
    subagentId: "LEAD",
    ...partial,
  };
}

const failed = (stepId: string, at: number): StepView => sv({
  stepId,
  toolName: "kb_search",
  headerZh: "检索资料",
  label: "pptx",
  state: "failed",
  startedAtMs: at,
  durationMs: 1_000,
  error: { kind: "permanent", uiKind: "permanent", message: "not found", code: "skill_unavailable" },
});

describe("chips", () => {
  it("merge consecutive finished steps of one kind, keep failures and the current step apart", () => {
    const [group] = buildRunGroups({
      steps: [
        sv({ stepId: "q1", label: "门店" }),
        sv({ stepId: "q2", label: "品类" }),
        sv({ stepId: "q3", label: "月份" }),
        failed("f1", 0),
        failed("f2", 0),
        sv({ stepId: "c1", toolName: "python_sandbox", headerZh: "运行代码", state: "active" }),
      ],
      peerNames: {},
    });
    const chips = chipsOf(group, words);
    expect(chips.map((chip) => [chip.tone, chip.count, chip.failures])).toEqual([
      ["done", 3, 0],
      ["fail", 1, 2],
      ["active", 1, 0],
    ]);
    expect(chips[1].sentence).toContain("失败 2 次");
  });

  it("mark a step waiting for confirmation as needing the reader", () => {
    const [group] = buildRunGroups({
      steps: [sv({ stepId: "mail", toolName: "python_sandbox", headerZh: "运行代码", state: "active", confirmation: { requestId: "r1" } as never })],
      peerNames: {},
    });
    expect(chipsOf(group, words)[0].tone).toBe("needs");
  });
});

describe("dots", () => {
  it("keep every step, and windowing keeps failures while counting the rest", () => {
    const steps = Array.from({ length: 10 }, (_, i) => sv({ stepId: `s${i}`, label: `q${i}` }));
    const [group] = buildRunGroups({ steps: [failed("f0", 0), ...steps], peerNames: {} });
    const marks = marksOf(group, words);
    expect(marks).toHaveLength(11);
    const { marks: shown, hiddenBefore } = windowMarks(marks, 4);
    expect(shown.map((mark) => mark.key)).toEqual(["f0", "s6", "s7", "s8", "s9"]);
    expect(hiddenBefore).toBe(6);
  });
});

describe("timeline", () => {
  it("places steps by start and duration, runs the current step to now, and leaves out steps without a start", () => {
    const [group] = buildRunGroups({
      steps: [
        sv({ stepId: "a", startedAtMs: 1_000, durationMs: 9_000 }),
        sv({ stepId: "b", label: "old", startedAtMs: undefined }),
        sv({ stepId: "c", toolName: "python_sandbox", headerZh: "运行代码", state: "active", startedAtMs: 10_000 }),
      ],
      peerNames: {},
    });
    const { startMs, spanMs } = timelineSpan([group], 21_000, true, 1_000);
    expect([startMs, spanMs]).toEqual([1_000, 20_000]);
    const segments = segmentsOf(group, words, startMs, spanMs, 21_000);
    expect(segments.map((segment) => [segment.mark.key, segment.left, segment.width])).toEqual([
      ["a", 0, 45],
      ["c", 45, 55],
    ]);
  });

  it("names the longest finished step only when it took a while", () => {
    const [group] = buildRunGroups({
      steps: [sv({ stepId: "a", startedAtMs: 0, durationMs: 40_000 }), sv({ stepId: "b", label: "x", startedAtMs: 40_000, durationMs: 2_000 })],
      peerNames: {},
    });
    const segments = segmentsOf(group, words, 0, 42_000, 42_000);
    expect(longestSegment(segments)?.mark.key).toBe("a");
    expect(longestSegment(segments, 60_000)).toBeNull();
  });
});
