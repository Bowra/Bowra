/**
 * The viewer's time zone: every time shown in the product follows the browser's IANA zone, never a fixed
 * deployment zone. Persisted timestamps stay UTC ISO strings; only display converts.
 *
 * Server rendering and static export have no viewer, so they render with {@link FALLBACK_TIME_ZONE} and the
 * client switches to the real zone right after hydration (see {@link useViewerTimeZone}).
 */

import { useSyncExternalStore } from "react";

/** Used when the browser reports no zone or one `Intl` rejects, and during server rendering. */
export const FALLBACK_TIME_ZONE = "UTC";

/** Request header that tells the gateway which zone to compute day boundaries in (session list groups). */
export const TIME_ZONE_HEADER = "X-Bowhead-Time-Zone";

/** True when `Intl` accepts `value` as a time zone. */
export function isValidTimeZone(value: unknown): value is string {
  if (typeof value !== "string" || !value.trim()) return false;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: value });
    return true;
  } catch {
    return false;
  }
}

function browserTimeZone(): string | undefined {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone;
  } catch {
    return undefined;
  }
}

/** The browser's IANA zone, or {@link FALLBACK_TIME_ZONE} when it is missing or invalid. */
export function detectViewerTimeZone(detect: () => string | undefined = browserTimeZone): string {
  let zone: string | undefined;
  try {
    zone = detect();
  } catch {
    zone = undefined;
  }
  return isValidTimeZone(zone) ? zone : FALLBACK_TIME_ZONE;
}

/**
 * The viewer-zone request header. Sessions use it for day groups; chat sends use it so the model's
 * "today" for that turn (and its resumes) follows the viewer instead of the deployment clock.
 */
export function viewerTimeZoneHeaders(): Record<string, string> {
  return { [TIME_ZONE_HEADER]: viewerTimeZone() };
}

let cachedZone: string | null = null;

/** {@link detectViewerTimeZone}, resolved once per page load (the zone does not change while a tab is open). */
export function viewerTimeZone(): string {
  if (typeof window === "undefined") return FALLBACK_TIME_ZONE;
  cachedZone ??= detectViewerTimeZone();
  return cachedZone;
}

/** Test hook: forget the cached zone so the next read detects it again. */
export function resetViewerTimeZoneForTests(): void {
  cachedZone = null;
}

const subscribe = () => () => {};
const serverSnapshot = () => FALLBACK_TIME_ZONE;

/**
 * The viewer's zone as React state. Hydration reads the server snapshot ({@link FALLBACK_TIME_ZONE}), so
 * statically exported HTML never mismatches; React then re-renders with the browser's zone. Client-only
 * mounts read the browser's zone directly.
 */
export function useViewerTimeZone(): string {
  return useSyncExternalStore(subscribe, viewerTimeZone, serverSnapshot);
}
