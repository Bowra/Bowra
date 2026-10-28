/**
 * Batch B (report-edit-audit P1-2) — bookkeeping for uncommitted atomic-section inline edits.
 *
 * `InlineEditableText` (eyebrow/h1/KPI/callout labels, etc.) is uncontrolled contentEditable,
 * **committed only on blur** (onChange → updateAttributes → PM transaction). Do not call
 * updateAttributes on every keystroke: payload changes rerender NodeView, React rewrites contentEditable
 * content, and the caret jumps (exactly why the original author chose blur commits).
 *
 * During typing, PM doc knows nothing: dirty=0 → status falsely says "已保存", beforeunload allows exit,
 * and leaving edit mode skips flush; editing only a KPI then refreshing silently loses edits (walkthrough §7 evidence).
 *
 * This registry covers that gap: register pending input when its value differs from the committed value,
 * then commit all before flush (synchronously dispatch updateAttributes, so editor.getJSON() immediately includes new values).
 */

const _pending = new Map<string, () => void>();
const _listeners = new Set<() => void>();

function _notify(): void {
  for (const listener of _listeners) listener();
}

/** Input value ≠ committed value → register; commit callbacks must read current DOM values and call onChange. */
export function setPendingInlineEdit(key: string, commit: () => void): void {
  _pending.set(key, commit);
  _notify();
}

/** Clear on blur commit / restoring the original value / component unmount. */
export function clearPendingInlineEdit(key: string): void {
  if (_pending.delete(key)) _notify();
}

export function pendingInlineEditCount(): number {
  return _pending.size;
}

/** Call at the start of flushNow: synchronously commit all pending inline edits to PM doc.
 *  commit clears itself; clear defensively again (onChange may not run when values are equal). */
export function commitPendingInlineEdits(): void {
  const entries = [...(_pending.entries())];
  for (const [key, commit] of entries) {
    try {
      commit();
    } finally {
      _pending.delete(key);
    }
  }
  if (entries.length > 0) _notify();
}

/** useSyncExternalStore subscription (status bars/navigation guards respond to pending-count changes). */
export function subscribePendingInlineEdits(listener: () => void): () => void {
  _listeners.add(listener);
  return () => {
    _listeners.delete(listener);
  };
}
