/**
 * Event times from the server. `utc_now_iso` (bowhead_core/types.py) stamps step and event times as
 * `2026-11-26T02:05:13.718756+00:00#1382`: the `#N` suffix is a process-wide counter that keeps keys unique within
 * one microsecond. `Date` cannot read it ("Invalid Date"), so read the ISO part.
 */
const UNIQUENESS_SUFFIX = /#\d+$/;

/** Epoch milliseconds of a server time, or null when it is not one. */
export function parseServerTime(value: string): number | null {
  const ms = Date.parse(value.replace(UNIQUENESS_SUFFIX, ""));
  return Number.isFinite(ms) ? ms : null;
}

/** The same as a `Date`, or null. */
export function serverTimeAsDate(value: string): Date | null {
  const ms = parseServerTime(value);
  return ms === null ? null : new Date(ms);
}
