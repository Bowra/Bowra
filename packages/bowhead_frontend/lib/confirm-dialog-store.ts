"use client";

/**
 * In-app confirmation entry for non-React code (such as report navigation guards), replacing native browser confirmation.
 *
 * `requestConfirm()` queues requests and returns Promise<boolean>; `ConfirmDialogHost`
 * (mounted beside ToastContainer in the authenticated shell) renders `ConfirmDialog` one at a time.
 * Without a host subscriber, immediately cancel: prefer blocking the action to silently allowing it.
 * React components should prefer `useConfirmDialog()`, bypassing this global queue.
 */

export interface ConfirmRequest {
  title: string;
  description?: string;
  confirmLabel: string;
  cancelLabel?: string;
  tone?: "danger" | "primary" | "discard";
}

export interface PendingConfirm extends ConfirmRequest {
  id: number;
}

type Listener = (pending: PendingConfirm | null) => void;

const queue: { request: PendingConfirm; resolve: (ok: boolean) => void; cleanup?: () => void }[] = [];
const listeners = new Set<Listener>();
let seq = 0;

function notify(): void {
  const head = queue[0]?.request ?? null;
  for (const listener of listeners) listener(head);
}

export function requestConfirm(request: ConfirmRequest, signal?: AbortSignal): Promise<boolean> {
  if (signal?.aborted) return Promise.resolve(false);
  if (listeners.size === 0) return Promise.resolve(false);
  return new Promise<boolean>((resolve) => {
    seq += 1;
    const entry = { request: { ...request, id: seq }, resolve, cleanup: () => signal?.removeEventListener("abort", abort) };
    const abort = () => {
      const index = queue.indexOf(entry);
      if (index < 0) return;
      queue.splice(index, 1);
      entry.cleanup();
      resolve(false);
      notify();
    };
    signal?.addEventListener("abort", abort, { once: true });
    queue.push(entry);
    if (queue.length === 1) notify();
  });
}

export function settleConfirm(id: number, ok: boolean): void {
  const index = queue.findIndex((entry) => entry.request.id === id);
  if (index < 0) return;
  const [entry] = queue.splice(index, 1);
  entry.cleanup?.();
  entry.resolve(ok);
  notify();
}

export function subscribeConfirm(listener: Listener): () => void {
  listeners.add(listener);
  listener(queue[0]?.request ?? null);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) {
      // Host unmounted: cancel all pending requests so callers never hang indefinitely.
      for (const entry of queue.splice(0)) { entry.cleanup?.(); entry.resolve(false); }
    }
  };
}
