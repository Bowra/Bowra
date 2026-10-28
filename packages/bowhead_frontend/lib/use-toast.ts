"use client";

/**
 * Global Toast data layer (module subscription, rendered by ToastContainer).
 *
 * Duration (docs/design/组件统一-技术设计.md “动效”):
 * - Default disappears after 4 seconds;
 * - With actions (Undo, Retry), at least 8 seconds;
 * - Errors (kind: "error") never auto-dismiss; users close manually;
 * - `durationMs <= 0` is persistent; pause timers on hover or focus inside Toast (pause/resume).
 *
 * Persistent errors must not stack indefinitely: identical visible error text merges with count (not with actions or
 * `onDismiss`, whose individual callbacks must survive); at most `MAX_ERROR_TOASTS` visible errors,
 * dismissing oldest on overflow. Other kinds neither merge nor have a limit.
 */

import { useEffect, useState, type ReactNode } from "react";

export type ToastKind = "info" | "warning" | "error" | "success";

export interface ToastAction {
  label: string;
  onAction: () => void | Promise<void>;
}

export interface Toast {
  id: string;
  kind: ToastKind;
  text: ReactNode;
  durationMs?: number;
  action?: ToastAction;
  /**
   * Called on Toast dismissal without successful action (timeout/manual close). Deferred-commit operations
   * (hide first, actually delete after Undo window) commit here, persisting exactly when Undo disappears.
   */
  onDismiss?: () => void;
  /** Number of identical merged errors (present only when ≥ 2). Written only by this module. */
  count?: number;
  /**
   * Undo Toast (behavior batch B, Menus Spec §5): completed undoable operation. Rendered as
   * Deleted “name” Undo, without status marker. These Toasts also carry `action` (Undo).
   */
  undo?: boolean;
}

export const DEFAULT_TOAST_DURATION_MS = 4000;
export const ACTION_TOAST_MIN_DURATION_MS = 8000;
/** Maximum concurrently displayed error Toasts. */
export const MAX_ERROR_TOASTS = 3;

/** Resolve actual duration from rules; 0 means no automatic dismissal. */
export function resolveToastDuration(
  input: Pick<Toast, "kind" | "durationMs" | "action">,
): number {
  if (input.kind === "error") return 0;
  const requested = input.durationMs ?? DEFAULT_TOAST_DURATION_MS;
  if (requested <= 0) return 0;
  return input.action
    ? Math.max(requested, ACTION_TOAST_MIN_DURATION_MS)
    : requested;
}

type Listener = (toasts: Toast[]) => void;

const _toasts: Toast[] = [];
const _listeners: Set<Listener> = new Set();
let _seq = 0;
const _timers = new Map<
  string,
  {
    handle: number | null;
    remainingMs: number;
    startedAt: number;
  }
>();

function _notify(): void {
  // Snapshot avoids mutation during iteration (autodismiss timer may fire mid-render).
  const snapshot = _toasts.slice();
  for (const l of _listeners) {
    try { l(snapshot); } catch { /* noop */ }
  }
}

function _mkId(): string {
  _seq += 1;
  // Date.now()+counter — stable across React strict-mode double-render.
  return `t_${Date.now()}_${_seq}`;
}

function mergeableError(input: Omit<Toast, "id">): input is Omit<Toast, "id"> & { text: string | number } {
  return (
    input.kind === "error" &&
    !input.action &&
    !input.onDismiss &&
    (typeof input.text === "string" || typeof input.text === "number")
  );
}

/** Push a toast. Returns its id (so caller may dismiss early). */
export function toast(input: Omit<Toast, "id" | "count">): string {
  if (mergeableError(input)) {
    const index = _toasts.findIndex(
      (existing) => mergeableError(existing) && existing.text === input.text,
    );
    if (index >= 0) {
      const existing = _toasts[index];
      _toasts[index] = { ...existing, count: (existing.count ?? 1) + 1 };
      _notify();
      return existing.id;
    }
  }
  const id = _mkId();
  const t: Toast = { ...input, id, durationMs: resolveToastDuration(input) };
  _toasts.push(t);
  if (t.kind === "error") {
    const errors = _toasts.filter((item) => item.kind === "error");
    if (errors.length > MAX_ERROR_TOASTS) {
      // Remove first, then notify once; evicted Toasts still invoke onDismiss normally.
      for (const dropped of errors.slice(0, errors.length - MAX_ERROR_TOASTS)) dismissToast(dropped.id);
      return id;
    }
  }
  _notify();
  if (typeof window !== "undefined" && t.durationMs && t.durationMs > 0) {
    _timers.set(id, {
      handle: null,
      remainingMs: t.durationMs,
      startedAt: 0,
    });
    scheduleToast(id);
  }
  return id;
}

/** Kind/text-only notifications, for components accepting `(kind, text)` callbacks. */
export type NotifyFn = (kind: ToastKind, text: ReactNode) => void;

export const notify: NotifyFn = (kind, text) => {
  toast({ kind, text });
};

/**
 * Remove a toast by id (no-op if already removed). `reason: "action"` means the
 * toast's action completed, so `onDismiss` is not called.
 */
export function dismissToast(
  id: string,
  reason: "dismiss" | "action" = "dismiss",
): void {
  clearToastTimer(id);
  const i = _toasts.findIndex((t) => t.id === id);
  if (i < 0) return;
  const [removed] = _toasts.splice(i, 1);
  _notify();
  if (reason !== "action") {
    try { removed.onDismiss?.(); } catch { /* noop */ }
  }
}

export function pauseToast(id: string): void {
  const timer = _timers.get(id);
  if (!timer || timer.handle === null) return;
  window.clearTimeout(timer.handle);
  timer.handle = null;
  timer.remainingMs = Math.max(
    0,
    timer.remainingMs - (Date.now() - timer.startedAt),
  );
}

export function resumeToast(id: string): void {
  const timer = _timers.get(id);
  if (!timer || timer.handle !== null) return;
  if (timer.remainingMs <= 0) {
    dismissToast(id);
    return;
  }
  scheduleToast(id);
}

/** Clear all toasts without running `onDismiss` (used by tests). */
export function _clearAllToasts(): void {
  for (const id of [..._timers.keys()]) clearToastTimer(id);
  _toasts.length = 0;
  _notify();
}

/** Subscribe React component to the toast store. */
export function useToasts(): Toast[] {
  const [items, setItems] = useState<Toast[]>(() => _toasts.slice());
  useEffect(() => {
    const cb: Listener = (snapshot) => setItems(snapshot);
    _listeners.add(cb);
    // initial sync — toasts pushed before mount must show
    setItems(_toasts.slice());
    return () => {
      _listeners.delete(cb);
    };
  }, []);
  return items;
}

function scheduleToast(id: string): void {
  const timer = _timers.get(id);
  if (!timer || timer.handle !== null) return;
  timer.startedAt = Date.now();
  timer.handle = window.setTimeout(
    () => dismissToast(id),
    timer.remainingMs,
  );
}

function clearToastTimer(id: string): void {
  const timer = _timers.get(id);
  if (!timer) return;
  if (timer.handle !== null) window.clearTimeout(timer.handle);
  _timers.delete(id);
}
