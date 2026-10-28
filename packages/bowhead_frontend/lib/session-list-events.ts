export const SESSION_LIST_CHANGED_EVENT = "bowhead:sessions-changed";

export function notifySessionListChanged(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(SESSION_LIST_CHANGED_EVENT));
}
