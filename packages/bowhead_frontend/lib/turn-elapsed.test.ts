/**
 * turn-elapsed — elapsed time must not lie under clock skew.
 *
 * ★Caught by dogfood on device 116; **never reproducible locally**: dev frontend/backend share a machine, so skew is always 0.
 * The server was measured 31.4 seconds ahead of the browser, so `Date.now() - startedAt` stayed negative for the first half minute.
 * The old `Math.max(0, …)` clamped it to 0: the scrolling band showed "0 秒" throughout an actual 19-second turn.
 */
import { afterEach, describe, expect, it } from "vitest";

import { _resetTurnElapsedAnchorsForTesting, turnElapsedMs, elapsedAnchorKeyOf } from "./turn-elapsed";

afterEach(() => _resetTurnElapsedAnchorsForTesting());

const SKEW = 31_400; // Measured value on 116.

describe("turnElapsedMs", () => {
  it("时钟同源时就是普通差值", () => {
    const now = 1_700_000_000_000;
    expect(turnElapsedMs(now - 5_000, now)).toBe(5_000);
  });

  it("★ 服务器时钟快 31 秒：不能冻在 0，必须从看到的那一刻开始走", () => {
    // Mutation: revert to `Math.max(0, now - startedAt)` → this case must fail (continually returns 0).
    const t0 = 1_700_000_000_000;
    const startedAt = t0 + SKEW; // Server timestamp lies in the browser's future.
    expect(turnElapsedMs(startedAt, t0)).toBe(0); // The first observation is naturally 0.
    expect(turnElapsedMs(startedAt, t0 + 1_000)).toBe(1_000);
    expect(turnElapsedMs(startedAt, t0 + 19_000)).toBe(19_000); // The old implementation still returned 0 here.
    expect(turnElapsedMs(startedAt, t0 + 125_000)).toBe(125_000);
  });

  it("锚点按 startedAt 记，不同轮各算各的", () => {
    const t0 = 1_700_000_000_000;
    turnElapsedMs(t0 + SKEW, t0);
    // Turn two starts 5 minutes later, also with a future timestamp.
    const t1 = t0 + 300_000;
    expect(turnElapsedMs(t1 + SKEW, t1)).toBe(0);
    expect(turnElapsedMs(t1 + SKEW, t1 + 3_000)).toBe(3_000);
    // Turn one's anchor was not overwritten.
    expect(turnElapsedMs(t0 + SKEW, t0 + 310_000)).toBe(310_000);
  });

  it("startedAt 缺失 → 0，不炸", () => {
    expect(turnElapsedMs(null)).toBe(0);
    expect(turnElapsedMs(undefined)).toBe(0);
    expect(turnElapsedMs(0)).toBe(0);
  });

  it("锚点表有界（长会话不无限增长）", () => {
    const t0 = 1_700_000_000_000;
    for (let i = 0; i < 100; i += 1) turnElapsedMs(t0 + i, t0);
    // Check only that it does not throw and still computes; capacity details stay private.
    expect(turnElapsedMs(t0 + 99, t0 + 1_000)).toBeGreaterThanOrEqual(0);
  });
});

describe("★★★ 同一轮里 startedAt 会换钟——秒数不许因此重锚", () => {
  // 📏Measured on 116 v1.1.6 (user reported "一上来就十秒"): within one turn, the display went
  //    `0 → 1 → **0** → 1 → 2`, moving backward.
  // 🔍Root cause: `use-turn-runner.ts:447` writes `Date.now()` (browser clock) on submission,
  //    then `:2194` overwrites it with `Date.parse(started_at)` (server clock) upon turn_started.
  //    Two values = two keys = two anchors.
  // Mutation: make `turnElapsedMs` ignore key (anchor by startedAt again) → this group must fail.
  const TURN = "t-abc";

  it("服务器**快**：换钟后不许跳回 0", () => {
    const t0 = 1_700_000_000_000;
    const browserStartedAt = t0;                 // Browser clock at submission.
    expect(turnElapsedMs(browserStartedAt, t0, TURN)).toBe(0);
    expect(turnElapsedMs(browserStartedAt, t0 + 1_500, TURN)).toBe(1_500);
    // turn_started arrives, switching to the server clock (32.5 seconds ahead, measured on 116).
    const serverStartedAt = t0 + 32_500;
    expect(turnElapsedMs(serverStartedAt, t0 + 2_000, TURN)).toBe(2_000);  // The old implementation returned 0 here.
    expect(turnElapsedMs(serverStartedAt, t0 + 9_000, TURN)).toBe(9_000);
  });

  it("★ 服务器**慢**：换钟后不许一下跳到 skew（user 报的那条）", () => {
    const t0 = 1_700_000_000_000;
    const browserStartedAt = t0;
    expect(turnElapsedMs(browserStartedAt, t0 + 1_000, TURN)).toBe(1_000);
    // Server 10 seconds behind ⇒ plain subtraction gives 11 seconds when only 1 second actually elapsed.
    const serverStartedAt = t0 - 10_000;
    expect(turnElapsedMs(serverStartedAt, t0 + 1_200, TURN)).toBe(1_200);
  });

  it("不同 turn 各算各的（别把上一轮的锚点串过来）", () => {
    const t0 = 1_700_000_000_000;
    expect(turnElapsedMs(t0, t0 + 5_000, "turn-1")).toBe(5_000);
    // Turn two only starts now: count from 0 without inheriting turn-1's anchor.
    expect(turnElapsedMs(t0 + 60_000, t0 + 60_000, "turn-2")).toBe(0);
  });
});

describe("★★★ key 必须带 executionGeneration（同轮澄清 resume 不许漏上一代的墙上时间）", () => {
  // Independent review caught `use-turn-runner.ts:1229` resetting startedAt on **the same turnId**;
  // its comment explicitly forbids leaking duration from the paused generation. A turnId-only key would count
  // the user's minutes answering clarification toward generation two.
  // Mutation: have `elapsedAnchorKeyOf` return only turnId (drop generation) → this case fails.
  it("第二代从 0 起算，不继承第一代的锚点", () => {
    const t0 = 1_700_000_000_000;
    const gen1 = elapsedAnchorKeyOf("T", 1);
    expect(turnElapsedMs(t0, t0 + 20_000, gen1)).toBe(20_000);   // Generation one ran for 20 seconds.
    // The user answers clarification 3 minutes later → a new generation.
    const resumeAt = t0 + 200_000;
    const gen2 = elapsedAnchorKeyOf("T", 2);
    expect(turnElapsedMs(resumeAt, resumeAt, gen2)).toBe(0);      // With only turnId, this would be 200000.
    expect(turnElapsedMs(resumeAt, resumeAt + 4_000, gen2)).toBe(4_000);
  });

  it("同一代内换钟仍然共用锚点（别把上一条修过头）", () => {
    const t0 = 1_700_000_000_000;
    const k = elapsedAnchorKeyOf("T2", 1);
    expect(turnElapsedMs(t0, t0 + 1_000, k)).toBe(1_000);
    expect(turnElapsedMs(t0 + 32_500, t0 + 2_000, k)).toBe(2_000);  // Switch to the server clock without reanchoring.
  });

  it("没有 turnId 时回落 null（调用方拿 startedAt 当 key）", () => {
    expect(elapsedAnchorKeyOf(null, 3)).toBeNull();
    expect(elapsedAnchorKeyOf("T3", null)).toBe("T3#0");
  });
});
