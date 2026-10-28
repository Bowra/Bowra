import { describe, expect, it } from "vitest";

import { creditUsage, formatCreditResetDate } from "@/lib/credit-period";

describe("creditUsage", () => {
  it("reports spent credits and shares against the allowance", () => {
    expect(creditUsage(6_000, 10_000)).toEqual({ used: 4_000, quota: 10_000, usedPct: 40, leftPct: 60 });
    expect(creditUsage(120, 10_000)).toEqual({ used: 9_880, quota: 10_000, usedPct: 99, leftPct: 1 });
  });

  it("clamps grants above the allowance and overdrawn balances", () => {
    expect(creditUsage(12_000, 10_000)).toEqual({ used: 0, quota: 10_000, usedPct: 0, leftPct: 100 });
    expect(creditUsage(-50, 10_000)).toEqual({ used: 10_000, quota: 10_000, usedPct: 100, leftPct: 0 });
  });

  it("is null when the allowance is unknown", () => {
    expect(creditUsage(500, 0)).toBeNull();
    expect(creditUsage(500, -1)).toBeNull();
    expect(creditUsage(500, Number.NaN)).toBeNull();
  });
});

describe("formatCreditResetDate", () => {
  const now = new Date("2026-10-01T03:00:00Z");

  it("formats the day in the requested locale, without a year in the current year", () => {
    expect(formatCreditResetDate("2026-11-01T00:00:00+00:00", "en-US", now, "UTC")).toBe("November 1");
    expect(formatCreditResetDate("2026-11-01T00:00:00+00:00", "zh-CN", now, "UTC")).toBe("11月1日");
    expect(formatCreditResetDate("2026-11-01T00:00:00+00:00", "de-DE", now, "UTC")).toBe("1. November");
    expect(formatCreditResetDate("2026-11-01T00:00:00+00:00", "fr-FR", now, "UTC")).toBe("1 novembre");
  });

  it("adds the year when the reset falls in another year", () => {
    expect(formatCreditResetDate("2027-01-01T00:00:00+00:00", "en-US", new Date("2026-12-20T00:00:00Z"), "UTC")).toBe(
      "January 1, 2027",
    );
  });

  it("uses the viewer's time zone for the calendar day", () => {
    expect(formatCreditResetDate("2026-11-01T00:00:00+00:00", "en-US", now, "America/New_York")).toBe("October 31");
  });

  it("is null for missing or unparseable values", () => {
    expect(formatCreditResetDate(null, "en-US", now)).toBeNull();
    expect(formatCreditResetDate(undefined, "en-US", now)).toBeNull();
    expect(formatCreditResetDate("", "en-US", now)).toBeNull();
    expect(formatCreditResetDate("not-a-date", "en-US", now)).toBeNull();
  });
});
