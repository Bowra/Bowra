/**
 * Tab-local navigation context, with the persisted preference as its initial fallback.
 * A different tab may update that preference without retargeting this tab's API reads.
 * The server still authorizes every Space request; these keys never grant access.
 */

export const SPACE_LS_KEY = "bowhead.space_id";
export const LEGACY_WORKSPACE_LS_KEY = "bowhead.workspace_id";
const SPACE_SESSION_KEY = "bowhead.space_context";

function currentTeamId(): string {
  return window.localStorage?.getItem?.("bowhead.team_id") ?? "";
}

export function setTabSpaceId(id: string): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage?.setItem?.(SPACE_SESSION_KEY, JSON.stringify({ teamId: currentTeamId(), spaceId: id }));
  } catch {
    // Storage can be disabled; authorized readers still validate the supplied context.
  }
}

export function getActiveSpaceId(): string {
  if (typeof window === "undefined") return "";
  try {
    const cached = JSON.parse(window.sessionStorage?.getItem?.(SPACE_SESSION_KEY) ?? "null");
    if (cached?.teamId === currentTeamId() && typeof cached.spaceId === "string") return cached.spaceId;
  } catch {
    // Ignore unavailable or malformed navigation storage.
  }
  // Freeze the initial preference before another tab can change it.
  const id = (
    window.localStorage?.getItem?.(SPACE_LS_KEY) ??
    window.localStorage?.getItem?.(LEGACY_WORKSPACE_LS_KEY) ??
    ""
  );
  setTabSpaceId(id);
  return id;
}

export function setActiveSpaceId(id: string): void {
  if (typeof window === "undefined") return;
  setTabSpaceId(id);
  try {
    window.localStorage?.setItem?.(SPACE_LS_KEY, id);
    window.localStorage?.setItem?.(LEGACY_WORKSPACE_LS_KEY, id);
  } catch {
    /* Storage full/disabled—silent, matching legacy behavior. */
  }
}

/** Clear space keys for logout/etc. (new + legacy keys + older active_workspace_id leftover). */
export function clearSpaceStorage(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage?.removeItem?.(SPACE_SESSION_KEY);
  } catch {
    // Local and tab storage can fail independently.
  }
  try {
    window.localStorage?.removeItem?.(SPACE_LS_KEY);
    window.localStorage?.removeItem?.(LEGACY_WORKSPACE_LS_KEY);
    window.localStorage?.removeItem?.("bowhead.active_workspace_id");
  } catch {
    /* Storage can be disabled; failed cleanup does not block navigation. */
  }
}

/** Space-context headers (without space, return {}—caller chooses default behavior). */
export function spaceHeader(context?: { workspaceId: string; teamId: string }): Record<string, string> {
  const id = context ? context.workspaceId : getActiveSpaceId();
  // Avoid a static import cycle: team-storage imports setActiveSpaceId for the
  // atomic Team-switch invalidation above.
  const teamId =
    context ? context.teamId : typeof window === "undefined" ? "" : window.localStorage?.getItem?.("bowhead.team_id") ?? "";
  return {
    ...(teamId ? { "X-Team-Id": teamId } : {}),
    ...(id ? { "X-Space-Id": id, "X-Workspace-Id": id } : {}),
  };
}
