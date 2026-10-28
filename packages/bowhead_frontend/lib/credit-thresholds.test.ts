/**
 * credit-billing P1-4—low balance must use a relative ratio aligned with backend/design.
 *
 * Regression: `user-badge.tsx` once used `balance < 1000` (absolute). For pro (quota 5000), this equals 20%,
 * masking the issue; for ultra (quota 30000), only 3.3%—backend `credits_summary` already classified
 * the user as low at 20%, while their badge kept its normal color. A heavy user on 116 with 70% of quota used fell into this gap.
 */

import { describe, expect, it } from "vitest";

import {
  balancePct,
  CRITICAL_BALANCE_PCT,
  isCriticalBalance,
  isLowBalance,
  LOW_BALANCE_PCT,
} from "./credit-thresholds";

describe("低余额阈值与后端/设计对齐", () => {
  it("阈值取值锁死(改动必须同步后端 credits_summary / credit_activation)", () => {
    expect(LOW_BALANCE_PCT).toBe(0.2);
    expect(CRITICAL_BALANCE_PCT).toBe(0.05);
  });

  it("★ ultra 用户在 20% 就判低余额(旧绝对值口径要到 3.3% 才判)", () => {
    // 6000/30000 = 20%—the boundary itself is not low (strictly less than).
    expect(isLowBalance(6000, 30_000)).toBe(false);
    expect(isLowBalance(5999, 30_000)).toBe(true);
    // Old criterion classified 5999 as normal because it far exceeds 1000.
    expect(5999).toBeGreaterThan(1000);
  });

  it("pro 用户口径不变(20% of 5000 == 旧的 1000)", () => {
    expect(isLowBalance(1000, 5000)).toBe(false);
    expect(isLowBalance(999, 5000)).toBe(true);
  });

  it("满额与半额都不算低", () => {
    expect(isLowBalance(30_000, 30_000)).toBe(false);
    expect(isLowBalance(15_000, 30_000)).toBe(false);
  });

  it("critical 是 low 的子集(5% < 20%)", () => {
    expect(isCriticalBalance(1000, 30_000)).toBe(true);
    expect(isLowBalance(1000, 30_000)).toBe(true);
    // 10% range: low but not yet critical.
    expect(isLowBalance(3000, 30_000)).toBe(true);
    expect(isCriticalBalance(3000, 30_000)).toBe(false);
  });

  it("quota <= 0 退化为「余额是否耗尽」, 不产生除零", () => {
    expect(balancePct(0, 0)).toBeNull();
    expect(isLowBalance(0, 0)).toBe(true);
    expect(isCriticalBalance(0, 0)).toBe(true);
    // Positive balance with anomalous quota (not seeded) → no false low-balance warning.
    expect(isLowBalance(500, 0)).toBe(false);
  });

  it("非有限值不炸", () => {
    expect(balancePct(Number.NaN, 30_000)).toBeNull();
    expect(balancePct(100, Number.NaN)).toBeNull();
    expect(isLowBalance(Number.NaN, 30_000)).toBe(false);
  });
});
