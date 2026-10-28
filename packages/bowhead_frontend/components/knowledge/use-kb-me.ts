"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { useSpaceSelection } from "@/lib/use-space";

import { type KbIdentity, type KbMeContext, fetchKbMe } from "./kb-me";

/**
 * Knowledge base identity + current space. Null until identity arrives: callers must show loading, not interpret it as no permission.
 *
 * Reuse /auth/me already fetched by the app shell (AuthSessionProvider). Previously each caller fetched separately,
 * making every upload-dialog opening wait for /auth/me again and disabling Start Upload for several seconds on a slow server.
 */
export function useKbMeState() {
  const { activeId } = useSpaceSelection();
  const sessionMe = useOptionalAuthSession()?.me ?? null;
  const [fetched, setFetched] = useState<KbIdentity | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const retry = useCallback(() => { setFailed(false); setAttempt((value) => value + 1); }, []);
  useEffect(() => {
    if (sessionMe) return;
    setFailed(false);
    let cancelled = false;
    fetchKbMe().then((id) => {
      if (!cancelled) { setFetched(id); setFailed(id === null); }
    }).catch(() => { if (!cancelled) setFailed(true); });
    return () => {
      cancelled = true;
    };
  }, [sessionMe, attempt]);
  const identity = useMemo<KbIdentity | null>(
    () =>
      sessionMe
        ? { user_id: sessionMe.id, team_id: sessionMe.team_id, is_team_admin: Boolean(sessionMe.is_team_admin) }
        : fetched,
    [sessionMe, fetched],
  );
  const me = useMemo(() => (identity ? { ...identity, workspace_id: activeId ?? "" } : null), [identity, activeId]);
  return { me, status: identity ? "ready" : failed ? "failed" : "loading", retry } as const;
}

/** Compatibility projection for consumers that only require the identity. */
export function useKbMe(): KbMeContext | null {
  return useKbMeState().me;
}
