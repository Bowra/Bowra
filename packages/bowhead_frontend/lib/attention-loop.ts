/**
 * Shared pieces of the client attention loop: polling cadence, the one browser-notification
 * permission offer, and the notification count the loop publishes to the bell.
 *
 * Without FEATURE_NOTIFICATION_REACH the turn-completion notifier and the bell keep their own
 * timers and only share the permission helpers. With it, `AttentionLoop` runs one timer for turn
 * completions, the notification count and urgent items, and the bell reads the count from here.
 */

import type { NotificationCountDTO } from "@/lib/api";
import { unprefixAppPath } from "@/lib/base-path";

export const VISIBLE_POLL_MS = 15_000;
export const HIDDEN_POLL_MS = 60_000;
/** Kept from the turn-completion notifier so an answer given before the loop existed still counts. */
export const OFFER_KEY = "bowhead.completion-notifications.offer";
export const TURN_CHANNEL = "bowhead-turn-completions";
export const NOTIFICATION_CHANNEL = "bowhead-notification-alerts";

/** The session the user is looking at, when the chat page is in the foreground. */
export function viewedSessionId(): string | null {
  if (typeof document === "undefined" || document.visibilityState !== "visible") return null;
  if (unprefixAppPath(window.location.pathname) !== "/") return null;
  return new URLSearchParams(window.location.search).get("session");
}

export function readOffer(): string | null {
  try {
    return window.localStorage.getItem(OFFER_KEY);
  } catch {
    return null;
  }
}

export function writeOffer(value: "granted" | "dismissed"): void {
  try {
    window.localStorage.setItem(OFFER_KEY, value);
  } catch {
    // Private windows may block storage; the offer can then show again later.
  }
}

export function notificationsAvailable(): boolean {
  return typeof window !== "undefined" && window.isSecureContext && "Notification" in window;
}

/** Whether a browser notification may be shown now: background tab, secure context, permission granted. */
export function browserNotificationAllowed(): boolean {
  return notificationsAvailable() && document.visibilityState === "hidden" && Notification.permission === "granted";
}

/** Whether to offer browser notifications: never asked in this browser and not decided yet. */
export function permissionOfferDue(): boolean {
  return notificationsAvailable() && Notification.permission === "default" && readOffer() === null;
}

export function openBroadcastChannel(name: string, onMessage: (value: string) => void): BroadcastChannel | null {
  try {
    if (typeof BroadcastChannel === "undefined") return null;
    const channel = new BroadcastChannel(name);
    channel.addEventListener("message", (event: MessageEvent) => {
      if (typeof event.data === "string") onMessage(event.data);
    });
    return channel;
  } catch {
    return null;
  }
}

// ── The notification count the loop publishes ─────────────────────────────

export type NotificationCountResult = { ok: true; counts: NotificationCountDTO } | { ok: false; error: unknown };

const countListeners = new Set<(result: NotificationCountResult) => void>();
let latestCount: NotificationCountResult | null = null;
let refreshNow: (() => void) | null = null;

/** Receive every count the loop fetches; the latest one, if any, is delivered immediately. */
export function subscribeNotificationCount(listener: (result: NotificationCountResult) => void): () => void {
  countListeners.add(listener);
  if (latestCount) listener(latestCount);
  return () => {
    countListeners.delete(listener);
  };
}

export function publishNotificationCount(result: NotificationCountResult): void {
  latestCount = result;
  for (const listener of countListeners) listener(result);
}

/** The running loop registers how to tick early (window focus, a change the user just made). */
export function registerAttentionRefresh(refresh: (() => void) | null): void {
  refreshNow = refresh;
  if (refresh === null) latestCount = null;
}

export function requestAttentionRefresh(): void {
  refreshNow?.();
}
