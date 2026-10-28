/**
 * dingsiyan #11 (review test #8) — direct clarification tombstone unit tests.
 *
 * Previously only covered indirectly by use-turn-runner.hitl-replay, with no eviction/dismissed/unmark coverage.
 */
import { beforeEach, describe, expect, it } from "vitest";

import {
  _resetClarificationTombstonesForTest,
  getAnsweredClarification,
  isClarificationAnswered,
  markClarificationAnswered,
  unmarkClarificationAnswered,
} from "./clarification-tombstone";

beforeEach(() => _resetClarificationTombstonesForTest());

describe("clarification-tombstone", () => {
  it("mark → isAnswered / getAnswered 返回值", () => {
    markClarificationAnswered("c1", { values: ["报告A", "报告B"] });
    expect(isClarificationAnswered("c1")).toBe(true);
    expect(getAnsweredClarification("c1")).toEqual({ values: ["报告A", "报告B"] });
    expect(isClarificationAnswered("nope")).toBe(false);
    expect(getAnsweredClarification("nope")).toBeNull();
  });

  it("null/空 requestId 是 no-op（不崩、不误标）", () => {
    markClarificationAnswered(null, { values: ["x"] });
    markClarificationAnswered("", { values: ["x"] });
    markClarificationAnswered(undefined, { values: ["x"] });
    expect(isClarificationAnswered(null)).toBe(false);
    expect(isClarificationAnswered("")).toBe(false);
  });

  it("unmark 撤销乐观标记（resolve 失败 → 卡片复活可重试）", () => {
    markClarificationAnswered("c1", { values: ["A"] });
    unmarkClarificationAnswered("c1");
    expect(isClarificationAnswered("c1")).toBe(false);
    unmarkClarificationAnswered("nope"); // Missing → no-op without crashing.
    unmarkClarificationAnswered(null);
  });

  it("dismissed 标记也算已答（防复活），但携带 dismissed 字段", () => {
    markClarificationAnswered("c1", { values: [], dismissed: true });
    expect(isClarificationAnswered("c1")).toBe(true);
    expect(getAnsweredClarification("c1")?.dismissed).toBe(true);
  });

  it("同 key 重复 mark 幂等（reducer double-invoke 安全）", () => {
    markClarificationAnswered("c1", { values: ["A"] });
    markClarificationAnswered("c1", { values: ["A"] });
    expect(getAnsweredClarification("c1")).toEqual({ values: ["A"] });
  });

  it("FIFO 128 驱逐：超上限时最老条目被逐出", () => {
    for (let i = 0; i < 128; i++) {
      markClarificationAnswered(`c${i}`, { values: [String(i)] });
    }
    expect(isClarificationAnswered("c0")).toBe(true);
    // The 129th entry → evict oldest c0.
    markClarificationAnswered("c128", { values: ["128"] });
    expect(isClarificationAnswered("c0")).toBe(false);
    expect(isClarificationAnswered("c1")).toBe(true);
    expect(isClarificationAnswered("c128")).toBe(true);
  });

  it("已存在 key 的更新不触发驱逐（不占新槽）", () => {
    for (let i = 0; i < 128; i++) {
      markClarificationAnswered(`c${i}`, { values: [String(i)] });
    }
    // Updating existing c0 must not evict any entry.
    markClarificationAnswered("c0", { values: ["updated"] });
    expect(isClarificationAnswered("c0")).toBe(true);
    expect(isClarificationAnswered("c1")).toBe(true); // Still present.
    expect(getAnsweredClarification("c0")).toEqual({ values: ["updated"] });
  });
});
