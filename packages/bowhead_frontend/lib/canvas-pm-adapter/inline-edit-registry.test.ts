/**
 * Batch B (report-edit-audit P1-2) — bookkeeping for pending inline edits.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  clearPendingInlineEdit,
  commitPendingInlineEdits,
  pendingInlineEditCount,
  setPendingInlineEdit,
  subscribePendingInlineEdits,
} from "./inline-edit-registry";

afterEach(() => {
  commitPendingInlineEdits(); // Clear state (commit deletes entries individually)
});

describe("inline-edit-registry", () => {
  it("set/clear 维护计数并通知订阅者", () => {
    const seen: number[] = [];
    const off = subscribePendingInlineEdits(() => seen.push(pendingInlineEditCount()));
    setPendingInlineEdit("a", () => {});
    setPendingInlineEdit("b", () => {});
    clearPendingInlineEdit("a");
    off();
    expect(seen).toEqual([1, 2, 1]);
    expect(pendingInlineEditCount()).toBe(1);
  });

  it("clear 不存在的 key 不通知", () => {
    const listener = vi.fn();
    const off = subscribePendingInlineEdits(listener);
    clearPendingInlineEdit("ghost");
    off();
    expect(listener).not.toHaveBeenCalled();
  });

  it("commit 执行全部回调并清账", () => {
    const order: string[] = [];
    setPendingInlineEdit("x", () => order.push("x"));
    setPendingInlineEdit("y", () => order.push("y"));
    commitPendingInlineEdits();
    expect(order).toEqual(["x", "y"]);
    expect(pendingInlineEditCount()).toBe(0);
  });

  it("commit 回调抛异常也要清账（finally）", () => {
    setPendingInlineEdit("boom", () => {
      throw new Error("boom");
    });
    setPendingInlineEdit("ok", () => {});
    expect(() => commitPendingInlineEdits()).toThrow("boom");
    // finally removed boom; ok did not run but remains (exception interrupted the loop) → commit again to finish
    commitPendingInlineEdits();
    expect(pendingInlineEditCount()).toBe(0);
  });
});
