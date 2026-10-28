/**
 * Current Team browser context for FEATURE_MULTI_TEAM_AUTHZ.
 *
 * A Team switch invalidates the selected Space immediately. The server remains
 * authoritative: this key only selects context through X-Team-Id; it never
 * grants access on its own.
 */

import { setActiveSpaceId } from "./space-storage";

export const TEAM_LS_KEY = "bowhead.team_id";
export const TEAM_CHANGED_EVENT = "bowhead:team-changed";

export function getActiveTeamId(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage?.getItem?.(TEAM_LS_KEY) ?? "";
}

export function setActiveTeamId(id: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage?.setItem?.(TEAM_LS_KEY, id);
    // A Space ID is only meaningful inside a Team. Clear it before notifying
    // consumers so no request can reuse the previous Team's Space.
    setActiveSpaceId("");
    window.dispatchEvent(new CustomEvent(TEAM_CHANGED_EVENT, { detail: { teamId: id } }));
  } catch {
    /* storage disabled — server selection still works per request */
  }
}

export function teamHeader(): Record<string, string> {
  const id = getActiveTeamId();
  return id ? { "X-Team-Id": id } : {};
}
