/**
 * Something a pin points at may have changed outside the pin store: a file was moved to the recycle bin, restored or
 * purged; a report was cleared or deleted; a conversation was deleted or restored. The pinned group reloads on it, so
 * a pin the person can no longer open leaves the sidebar at once and a restored one comes back
 * (docs/sprints/result-pins-2026-11-24/tech-design.md §5.1).
 *
 * Kept apart from `result-pins.ts` so the API helpers can announce a change without importing the pin store.
 */
export const RESULT_TARGETS_CHANGED_EVENT = "bowhead:result-targets-changed";

export function notifyResultTargetsChanged(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(RESULT_TARGETS_CHANGED_EVENT));
}
