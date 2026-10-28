/**
 * Credit period read model for the topbar credits popover (P2 Sidebar & Topbar spec): usage against the
 * period allowance and the next reset date. Low / critical criteria stay in `lib/credit-thresholds`.
 */

import { balancePct } from "@/lib/credit-thresholds";

export interface CreditUsage {
  /** Credits spent this period; never negative (grants can lift the balance above the allowance). */
  used: number;
  quota: number;
  /** Spent share in whole percent, clamped to 0–100. */
  usedPct: number;
  /** Remaining share in whole percent, clamped to 0–100. */
  leftPct: number;
}

function clampPct(value: number): number {
  return Math.min(100, Math.max(0, Math.round(value * 100)));
}

/** Usage against the allowance, or null when the allowance is unknown (`quota <= 0` or not finite). */
export function creditUsage(balance: number, quota: number): CreditUsage | null {
  const pct = balancePct(balance, quota);
  if (pct === null) return null;
  const used = Math.min(quota, Math.max(0, quota - balance));
  return { used, quota, usedPct: clampPct(used / quota), leftPct: clampPct(pct) };
}

/**
 * The reset date as a localized day without a time ("October 1" / "10月1日"); the year is added only when it
 * differs from the current one. Null for a missing or unparseable value, so callers omit the row instead of
 * inventing a date.
 */
export function formatCreditResetDate(
  iso: string | null | undefined,
  locale: string,
  now: Date = new Date(),
  timeZone?: string,
): string | null {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  const zone = timeZone ? { timeZone } : null;
  const yearOf = (value: Date) => new Intl.DateTimeFormat("en-US", { year: "numeric", ...zone }).format(value);
  const options: Intl.DateTimeFormatOptions = { month: "long", day: "numeric", ...zone };
  if (yearOf(date) !== yearOf(now)) options.year = "numeric";
  try {
    return new Intl.DateTimeFormat(locale, options).format(date);
  } catch {
    return new Intl.DateTimeFormat("en-US", options).format(date);
  }
}
