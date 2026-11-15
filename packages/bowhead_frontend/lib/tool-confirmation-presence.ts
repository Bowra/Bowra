"use client";

/**
 * Where inline tool call confirmation cards are on screen, and which card the
 * user asked to be taken to. The notice skips items whose card is visible in the
 * current page (design §6.3 and §9: a card scrolled out of view still counts as
 * waiting), and its View action focuses a card that may only mount after the
 * session has loaded.
 */
import { useEffect, useSyncExternalStore, type RefObject } from "react";

const visible = new Set<string>();
const listeners = new Set<() => void>();
let snapshot: ReadonlySet<string> = new Set();

function publish(): void {
  snapshot = new Set(visible);
  for (const listener of listeners) listener();
}

export function setConfirmationCardVisible(id: string, isVisible: boolean): void {
  if (visible.has(id) === isVisible) return;
  if (isVisible) visible.add(id);
  else visible.delete(id);
  publish();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

const EMPTY: ReadonlySet<string> = new Set();

/** Ids of confirmation cards currently mounted and intersecting the viewport. */
export function useVisibleConfirmationIds(): ReadonlySet<string> {
  return useSyncExternalStore(subscribe, () => snapshot, () => EMPTY);
}

/**
 * Track whether a pending card is on screen. Without IntersectionObserver (old
 * browsers, tests) a mounted card counts as visible.
 */
export function useConfirmationCardPresence(
  id: string,
  ref: RefObject<HTMLElement | null>,
  active: boolean,
): void {
  useEffect(() => {
    const element = ref.current;
    if (!active || !element) return;
    if (typeof IntersectionObserver === "undefined") {
      setConfirmationCardVisible(id, true);
      return () => setConfirmationCardVisible(id, false);
    }
    const observer = new IntersectionObserver((entries) => {
      const entry = entries[entries.length - 1];
      if (entry) setConfirmationCardVisible(id, entry.isIntersecting);
    });
    observer.observe(element);
    return () => {
      observer.disconnect();
      setConfirmationCardVisible(id, false);
    };
  }, [id, ref, active]);
}

/** A View request waits this long for its card to mount (session load, replay). */
const FOCUS_REQUEST_TTL_MS = 60_000;
let focusRequest: { id: string; at: number } | null = null;

export function requestConfirmationFocus(id: string): void {
  focusRequest = { id, at: Date.now() };
}

/** True once for the card the user asked to be taken to. */
export function consumeConfirmationFocus(id: string): boolean {
  if (!focusRequest || focusRequest.id !== id) return false;
  const fresh = Date.now() - focusRequest.at <= FOCUS_REQUEST_TTL_MS;
  focusRequest = null;
  return fresh;
}

export function confirmationCardElement(id: string): HTMLElement | null {
  if (typeof document === "undefined") return null;
  for (const element of document.querySelectorAll<HTMLElement>("[data-tool-confirmation-id]")) {
    if (element.dataset.toolConfirmationId === id) return element;
  }
  return null;
}

/** Scroll a mounted card into view and move focus to it. False when it is not mounted. */
export function focusConfirmationCard(id: string): boolean {
  const element = confirmationCardElement(id);
  if (!element) return false;
  element.scrollIntoView?.({ block: "center", behavior: "smooth" });
  element.focus({ preventScroll: true });
  return true;
}

/**
 * Where the notice docks. A visible chat column offers a slot right above its composer so the
 * notice never covers Send or Stop (116 dogfood 2026-11-15: the bottom-right notice sat on the
 * composer's Stop button). The latest registered slot wins; without one (home, settings, a
 * maximized Results pane) the notice floats at the bottom right.
 */
const slots: HTMLElement[] = [];
const slotListeners = new Set<() => void>();

function publishSlot(): void {
  for (const listener of slotListeners) listener();
}

export function registerConfirmationNoticeSlot(element: HTMLElement): () => void {
  slots.push(element);
  publishSlot();
  return () => {
    const index = slots.lastIndexOf(element);
    if (index >= 0) slots.splice(index, 1);
    publishSlot();
  };
}

function subscribeSlot(listener: () => void): () => void {
  slotListeners.add(listener);
  return () => slotListeners.delete(listener);
}

export function useConfirmationNoticeSlot(): HTMLElement | null {
  return useSyncExternalStore(subscribeSlot, () => slots[slots.length - 1] ?? null, () => null);
}

/** Test helper. */
export function _resetConfirmationPresenceForTesting(): void {
  visible.clear();
  focusRequest = null;
  slots.length = 0;
  publish();
  publishSlot();
}
