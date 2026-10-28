/**
 * mobile-tech-design §2.1: pure-function decisions for touch reordering.
 * Moves apply to PM document order (applySectionMove); the existing debounced sync diffs this into reorder.
 * Lock correct calculations and non-silent out-of-bounds handling here.
 */

import { describe, expect, it } from "vitest";

import { planSectionMove } from "./section-move";

const ORDER = ["a", "b", "c"];

describe("planSectionMove", () => {
  it("上移与相邻对调", () => {
    expect(planSectionMove(ORDER, "b", "up")).toEqual(["b", "a", "c"]);
  });

  it("下移与相邻对调", () => {
    expect(planSectionMove(ORDER, "b", "down")).toEqual(["a", "c", "b"]);
  });

  it("首块上移 → null（调用方据此提示, 不静默）", () => {
    expect(planSectionMove(ORDER, "a", "up")).toBeNull();
  });

  it("末块下移 → null", () => {
    expect(planSectionMove(ORDER, "c", "down")).toBeNull();
  });

  it("不在序里 → null", () => {
    expect(planSectionMove(ORDER, "zzz", "up")).toBeNull();
  });

  it("不改动入参数组（纯函数）", () => {
    const input = [...ORDER];
    planSectionMove(input, "b", "up");
    expect(input).toEqual(ORDER);
  });

  it("id 集合不变 —— 因此不触发删除守卫", () => {
    const moved = planSectionMove(ORDER, "b", "down")!;
    expect([...moved].sort()).toEqual([...ORDER].sort());
  });
});
