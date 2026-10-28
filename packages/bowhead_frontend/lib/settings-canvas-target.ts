import { withoutNextRouterHistoryMarkers } from "./url-session-nav";

/** A page selection describes an existing settings surface, not a new domain draft. */
export interface SettingsCanvasTarget {
  page: "workspace_i18n" | "space_general";
  userId: string;
  teamId: string;
  workspaceId: string;
}

const PARAMS = [
  "canvas_settings",
  "canvas_user",
  "canvas_team",
  "canvas_space",
] as const;
const ACTIVE_PARAM = "canvas_settings_active";

export function settingsCanvasKey(target: SettingsCanvasTarget): string {
  return JSON.stringify([
    "settings_page",
    target.page,
    target.userId,
    target.teamId,
    target.workspaceId,
  ]);
}

export function readSettingsCanvasTarget(
  params: URLSearchParams,
): SettingsCanvasTarget | null {
  const page = params.get(PARAMS[0]);
  if (page !== "workspace_i18n" && page !== "space_general") return null;
  const [userId, teamId, workspaceId] = PARAMS.slice(1).map((name) =>
    params.get(name),
  );
  if (
    ![userId, teamId, workspaceId].every(
      (value) => value && value.length <= 256,
    )
  )
    return null;
  return {
    page,
    userId: userId!,
    teamId: teamId!,
    workspaceId: workspaceId!,
  };
}

export function settingsCanvasHref(
  target: SettingsCanvasTarget,
  workRoute = "/",
): string {
  const url = new URL(workRoute, "https://canvas.invalid");
  // The page host is on the conversation root; retain a root conversation's query only.
  const params =
    url.pathname === "/" ? url.searchParams : new URLSearchParams();
  params.set(PARAMS[0], target.page);
  params.set(PARAMS[1], target.userId);
  params.set(PARAMS[2], target.teamId);
  params.set(PARAMS[3], target.workspaceId);
  params.delete(ACTIVE_PARAM);
  return `/?${params}`;
}

export function clearSettingsCanvasTargetInUrl(): void {
  const url = new URL(window.location.href);
  PARAMS.forEach((name) => url.searchParams.delete(name));
  url.searchParams.delete(ACTIVE_PARAM);
  window.history.replaceState(
    withoutNextRouterHistoryMarkers(window.history.state),
    "",
    `${url.pathname}${url.search}${url.hash}`,
  );
}

export function readSettingsCanvasSelection(params: URLSearchParams): boolean {
  return params.get(ACTIVE_PARAM) !== "0";
}

/** Update only the original settings pointer, preserving the entry and other view routes. */
export function writeSettingsCanvasTargetInUrl(target: SettingsCanvasTarget, selected: boolean): void {
  const url = new URL(window.location.href);
  url.searchParams.set(PARAMS[0], target.page);
  url.searchParams.set(PARAMS[1], target.userId);
  url.searchParams.set(PARAMS[2], target.teamId);
  url.searchParams.set(PARAMS[3], target.workspaceId);
  if (selected) url.searchParams.delete(ACTIVE_PARAM);
  else url.searchParams.set(ACTIVE_PARAM, "0");
  if (url.href === window.location.href) return;
  window.history.replaceState(
    withoutNextRouterHistoryMarkers(window.history.state),
    "",
    `${url.pathname}${url.search}${url.hash}`,
  );
}

export function writeSettingsCanvasSelection(selected: boolean): void {
  const url = new URL(window.location.href);
  if (!readSettingsCanvasTarget(url.searchParams)) return;
  if (selected) url.searchParams.delete(ACTIVE_PARAM);
  else url.searchParams.set(ACTIVE_PARAM, "0");
  window.history.replaceState(
    withoutNextRouterHistoryMarkers(window.history.state),
    "",
    `${url.pathname}${url.search}${url.hash}`,
  );
}

export function settingsCanvasTargetMatches(
  target: SettingsCanvasTarget,
  scope: {
    userId: string;
    teamId: string;
    workspaceId: string;
  },
): boolean {
  return (
    target.userId === scope.userId &&
    target.teamId === scope.teamId &&
    target.workspaceId === scope.workspaceId
  );
}
