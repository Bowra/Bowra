/**
 * feedback-0713 #13 / F5: guard decisions for URL ?session= soft navigation (two app/page.tsx
 * effects share this decision core, extracted as pure function for regression coverage; final review P1's repeated
 * dead clicks on the same notification came from an incorrect guard here).
 *
 * - "mark-handled": sid + report intent already match current view (rail/URL write echo) → no hydration,
 *   but record target (otherwise visiting another session then returning gets wrongly deduped). Page URL writes always remove Next
 *   history markers (see `withoutNextRouterHistoryMarkers`), so each own write echoes back
 *   here: if view still matches written target, echo is mark-handled; if view changes before echo arrives, echo becomes
 *   stale snapshot rejected by live URL comparison.
 * - "sync-report": same session's report intent changes → sync report/chat view only, no session replay.
 * - "ignore": no sid, or same sid + report target already hydrating/handled (prevents double hydration).
 * - "hydrate": actual soft-navigation target (notification click/external deep link) → handlePickSessionFromUrl.
 */
export type UrlSessionNavAction = "ignore" | "mark-handled" | "sync-report" | "hydrate";

export function urlSessionTargetKey(sid: string, reportIntent: boolean): string {
  return JSON.stringify([sid, reportIntent]);
}

export function resolveUrlSessionNav(args: {
  sid: string | null;
  reportIntent: boolean;
  activeSessionId: string | null;
  activeReportIntent: boolean;
  lastRequestedTarget: string | null;
  /**
   * **Actual current** `?session=` in `window.location` (not React snapshot).
   *
   * Page maintains URL through `window.history.replaceState`; Next syncs these writes into
   * `useSearchParams` asynchronously (transition), so this tick's `sid` may still hold **previous** URL.
   * Snapshot differing from live URL is stale; no decision based on it is trustworthy.
   */
  liveSid: string | null;
  /** Actual current `report=1` in `window.location`. With `liveSid`, detects stale snapshots. */
  liveReportIntent: boolean;
  /**
   * “新工作” reset in flight (canonical clean URL not yet observed).
   */
  resetInFlight: boolean;
}): UrlSessionNavAction {
  if (!args.sid) return "ignore";
  // Native replaceState maintains the URL while the React snapshot can lag by a tick.
  // Hydrating stale sid sends user to a session they never selected.
  // Comparing sid alone insufficient: switching report/chat views, previous `report=1` write echo arrives one tick late,
  // mistaken as URL requesting report view, reverting switch just made.
  if (args.sid !== args.liveSid || args.reportIntent !== args.liveReportIntent) return "ignore";
  // During reset, accept **no** URL-derived session, not merely the one just closed.
  // Old implementation ignored only session being closed, allowing **previous** session in stale snapshot through.
  if (args.resetInFlight && args.activeSessionId === null) return "ignore";
  if (args.sid === args.activeSessionId) {
    if (args.reportIntent === args.activeReportIntent) return "mark-handled";
    // URL target still equals last hydrated target ⇒ **view state** changed this tick, URL did not:
    // Shared-report read-only session hydration forces `?session=X` into report view; URL sync writes
    // `report=1` later. Syncing as URL requesting chat view triggers another report-forcing hydration,
    // looping forever (Results stuck on “正在加载成果…”). Actual back/forward changes target, unaffected.
    if (urlSessionTargetKey(args.sid, args.reportIntent) === args.lastRequestedTarget) return "ignore";
    return "sync-report";
  }
  if (urlSessionTargetKey(args.sid, args.reportIntent) === args.lastRequestedTarget) return "ignore";
  return "hydrate";
}

/**
 * When page writes `activeSessionId` back to URL, does live URL point to an **unhandled** external navigation target?
 *
 * Notification/deep link/back-forward changes URL session; Next syncs searchParams through transition, guard
 * effect waits for snapshot before opening it. If earlier hydration finishes meanwhile (activeSessionId becomes old
 * target), sync effect writing old target overwrites new one, so snapshot never sees it: user's notification is swallowed.
 * Therefore, if live URL session is neither current session nor page's last written target, and this was not user
 * actively switching sessions in page, defer write. Guard opens new target from live URL (sync afterwards).
 */
export function urlHasPendingForeignSession(args: {
  liveSid: string | null;
  activeSessionId: string | null;
  lastWrittenSid: string | null;
  userSwitch: boolean;
}): boolean {
  if (!args.liveSid || !args.activeSessionId || args.userSwitch) return false;
  return args.liveSid !== args.activeSessionId && args.liveSid !== args.lastWrittenSid;
}

/**
 * Page guard effect entry: URL target comes from Next snapshot (`useSearchParams`), staleness from browser's
 * live URL. Read `session` and `report=1` from both URLs, preventing caller passing snapshot as live.
 */
export function resolveUrlSessionNavAgainstLocation(args: {
  snapshot: Pick<URLSearchParams, "get">;
  location: string;
  activeSessionId: string | null;
  activeReportIntent: boolean;
  lastRequestedTarget: string | null;
  resetInFlight: boolean;
}): UrlSessionNavAction {
  const live = new URL(args.location).searchParams;
  return resolveUrlSessionNav({
    sid: args.snapshot.get("session"),
    reportIntent: args.snapshot.get("report") === "1",
    activeSessionId: args.activeSessionId,
    activeReportIntent: args.activeReportIntent,
    lastRequestedTarget: args.lastRequestedTarget,
    liveSid: live.get("session"),
    liveReportIntent: live.get("report") === "1",
    resetInFlight: args.resetInFlight,
  });
}

/**
 * **External** navigation to clean homepage (back/forward to `/`, elsewhere `router.push("/")`), with page still showing
 * (or opening) session ⇒ page returns to “新工作”. URL identity includes session: address bar `/` cannot
 * continue displaying s2.
 *
 * Page's own `/` write (“新工作” / current-session archive reset) excluded: reset clears `handledTarget`
 * (page's adopted URL target) before URL write, so already null when echo arrives. Snapshot and live URL must both be clean;
 * only one clean means this tick's snapshot is stale.
 */
export function isExternalCleanUrlNavigation(args: {
  snapshotSid: string | null;
  liveSid: string | null;
  handledTarget: string | null;
}): boolean {
  return !args.snapshotSid && !args.liveSid && args.handledTarget !== null;
}

/**
 * Page's next action after handling external navigation to clean `/`.
 *
 * - "done": already returned to “新工作”.
 * - "restore-session-url": user canceled (or pending send needs confirmation first), page stays in original session ⇒ restore current
 *   history entry to session URL (replaceState: history length unchanged, cost is adjacent duplicate session entries,
 *   making next Back appear unresponsive).
 * - "release-target": unfinished session opening canceled ⇒ release adopted target, address stays `/`; otherwise any subsequent
 *   effect rerun treats same `/` as external navigation and resets again.
 * - "leave": address returns to a session during confirmation (e.g. user presses Back) ⇒ write nothing, guard handles that URL.
 */
export type CleanUrlNavigationOutcome = "done" | "restore-session-url" | "release-target" | "leave";

export function cleanUrlNavigationOutcome(args: {
  result: "created" | "cancelled";
  viewSessionId: string | null;
  liveSid: string | null;
}): CleanUrlNavigationOutcome {
  if (args.liveSid) return "leave";
  if (args.result === "created") return "done";
  return args.viewSessionId ? "restore-session-url" : "release-target";
}

export function syncActiveSessionSearchParams(args: {
  current: URLSearchParams;
  activeSessionId: string | null;
  reportLinkSessionId: string | null;
  clearSessionScopedResults: boolean;
}): URLSearchParams {
  const next = new URLSearchParams(args.current);
  if (args.activeSessionId) {
    next.set("session", args.activeSessionId);
    if (args.reportLinkSessionId === args.activeSessionId) {
      next.set("report", "1");
    } else {
      next.delete("report");
    }
  } else {
    next.delete("session");
    next.delete("report");
  }
  if (args.clearSessionScopedResults) {
    next.delete("results");
    next.delete("result");
    next.delete("mode");
    next.delete("tab");
    next.delete("focus");
  }
  return next;
}

/**
 * Remove Next App Router's internal history markers (`__NA`, `_N`, `__PRIVATE_NEXTJS_INTERNALS_TREE`),
 * preserving other fields. With `__NA`, Next treats `history.replaceState` / `pushState` as its own
 * internal write and passes through; remembered router URL (source of `useSearchParams`/`usePathname`) stays old;
 * navigating to that old value later sees unchanged string, so observing effects never rerun (deep links/notifications fail), while Next
 * next commits router state by writing old URL back to address bar. Removing markers makes Next's patch sync router and copy internal markers
 * back into entry (Next still handles back/forward, no full-page refresh).
 *
 * **Every page-owned history write must pass through this** (`url-history-writes.test.ts` guards by source scan).
 *
 * Assumption: Next installs patch in effect after AppRouter mounts; page writes must occur after that.
 * Child effects run before parent effects, so first-frame writes bypass patch; protected pages currently mount beneath
 * `AuthenticatedAppBoundary`, whose initial auth loading guarantees page effects run after patch installation.
 */
export function withoutNextRouterHistoryMarkers(state: unknown): Record<string, unknown> {
  if (!state || typeof state !== "object") return {};
  const rest: Record<string, unknown> = { ...(state as Record<string, unknown>) };
  delete rest.__NA;
  delete rest._N;
  delete rest.__PRIVATE_NEXTJS_INTERNALS_TREE;
  return rest;
}
