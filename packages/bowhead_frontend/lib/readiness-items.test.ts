/**
 * Criteria for how many things can be done.
 *
 * ★ Product premise for this round: **verification is optional, not a gate**. Customers' final LLM deployment locations are unknown;
 * intranets or custom gateways often cannot be verified, which does not imply bad configuration. This layer only answers how many things can be done,
 * never whether usage is allowed; with `attentionCount === 0`, **the entire block is absent** from the page.
 */

import { describe, expect, it } from "vitest";

import {
  attentionBreakdown,
  attentionCount,
  evidenceItemState,
  evidenceItems,
} from "./readiness-items";

function row(overrides: Record<string, unknown> = {}) {
  return {
    role_key: "primary",
    provider_id: "p1",
    provider_display_name: "P1",
    model_id: "m1",
    capability: "chat",
    routing_critical: true,
    declared: true,
    fact_state: "verified",
    status: "ready",
    changed: false,
    ...overrides,
  } as never;
}

function readiness(rows: unknown[]) {
  return {
    as_of: "2026-08-24T00:00:00Z",
    draft_semantic_hash: "a".repeat(64),
    may_apply: true,
    rows,
    blocking: [],
    warnings: [],
    required_acceptances: [],
    migration_acceptance: null,
    external_rows: [],
    baseline_will_invalidate: false,
  } as never;
}

describe("单项状态", () => {
  it.each([
    ["verified", "verified"],
    ["aged", "aged"],
    ["stale", "aged"],
    ["failed", "failed"],
    ["not_testable", "never"],
    ["unknown", "never"],
    ["某个将来才有的值", "never"],
  ])("fact_state=%s → %s", (factState, expected) => {
    expect(evidenceItemState(row({ fact_state: factState }))).toBe(expected);
  });

  it("★ 正在验证时盖过历史事实——不该还显示「14 天前」", () => {
    expect(evidenceItemState(row({ fact_state: "aged", attempt_state: "verifying" })))
      .toBe("running");
    expect(evidenceItemState(row({ fact_state: "aged", attempt_state: "queued" })))
      .toBe("running");
  });

  it("负对照：结束态的 attempt 不盖事实（errored 只是上次尝试出错，依据仍然是依据）", () => {
    expect(evidenceItemState(row({ fact_state: "verified", attempt_state: "errored" })))
      .toBe("verified");
  });
});

describe("需要处理的条数", () => {
  it("★ 全验过时是 0 —— 页面因此整块不出现", () => {
    const items = evidenceItems(readiness([row(), row({ model_id: "m2" })]));

    expect(attentionCount(items)).toBe(0);
  });

  it("已通过与进行中都不算「要你处理」", () => {
    const items = evidenceItems(readiness([
      row({ fact_state: "verified" }),
      row({ model_id: "m2", fact_state: "aged", attempt_state: "verifying" }),
      row({ model_id: "m3", fact_state: "aged" }),
    ]));

    expect(attentionCount(items)).toBe(1);
  });

  it("分组计数供那行提示措辞用", () => {
    const items = evidenceItems(readiness([
      row({ fact_state: "failed" }),
      row({ model_id: "m2", fact_state: "not_testable" }),
      row({ model_id: "m3", fact_state: "aged" }),
      row({ model_id: "m4", fact_state: "stale" }),
      row({ model_id: "m5", fact_state: "verified" }),
    ]));

    expect(attentionBreakdown(items)).toEqual({ failed: 1, needsConfirm: 1, aged: 2 });
  });

  it("readiness 为 null 时不是「0 件事」而是「没有清单」——两者都不出提示", () => {
    expect(evidenceItems(null)).toEqual([]);
    expect(attentionCount(evidenceItems(null))).toBe(0);
  });
});

describe("排序", () => {
  it("★ 先进行中、再失败、没验过、依据旧的，最后已通过", () => {
    const items = evidenceItems(readiness([
      row({ model_id: "verified" }),
      row({ model_id: "aged", fact_state: "aged" }),
      row({ model_id: "never", fact_state: "unknown" }),
      row({ model_id: "failed", fact_state: "failed" }),
      row({ model_id: "running", fact_state: "aged", attempt_state: "verifying" }),
    ]));

    expect(items.map((item) => item.modelId))
      .toEqual(["running", "failed", "never", "aged", "verified"]);
  });

  it("同档内稳定排序（分工名 → 模型名），两次拉取顺序不跳", () => {
    const items = evidenceItems(readiness([
      row({ role_key: "fallback", model_id: "b", fact_state: "aged" }),
      row({ role_key: "fallback", model_id: "a", fact_state: "aged" }),
      row({ role_key: "eval", model_id: "z", fact_state: "aged" }),
    ]));

    expect(items.map((item) => `${item.roleKey}/${item.modelId}`))
      .toEqual(["eval/z", "fallback/a", "fallback/b"]);
  });
});

describe("key 稳定", () => {
  it("同一 provider/model/capability 在两次拉取间是同一个 key", () => {
    const first = evidenceItems(readiness([row({ status: "ready" })]));
    const second = evidenceItems(readiness([row({ status: "warning", observed_at: "2026-08-24T01:00:00Z" })]));

    expect(first[0].key).toBe(second[0].key);
  });

  it("能力不同就是不同项——同一个模型的对话与工具能力各自验证", () => {
    const items = evidenceItems(readiness([row({ capability: "chat" }), row({ capability: "tool" })]));

    expect(new Set(items.map((item) => item.key)).size).toBe(2);
  });
});
