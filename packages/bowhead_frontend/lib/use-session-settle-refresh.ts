import { useEffect, useRef } from "react";

/**
 * Re-read the session list after a finished turn while its session still reads as active.
 *
 * An @-mentioned Agent's turn releases the session's active marker when its dispatch settles, shortly after
 * `done`, so the refresh right after `done` can still say "In progress" until the next 10 s poll. This bumps the
 * list at 1 s, 2 s, 4 s and 8 s, and stops as soon as the row no longer reads `active` (chat-pipeline changelog
 * 2026-10-21).
 */
export function useSessionSettleRefresh(args: {
  finished: boolean;
  turnId: string | null;
  rowState: string | undefined;
  refreshKey: number;
  refresh: () => void;
}): void {
  const { finished, turnId, rowState, refreshKey, refresh } = args;
  const attempts = useRef<{ turnId: string | null; count: number }>({ turnId: null, count: 0 });
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;
  useEffect(() => {
    if (!finished) return;
    if (attempts.current.turnId !== turnId) attempts.current = { turnId, count: 0 };
    if (rowState !== "active" || attempts.current.count >= 4) return;
    const timer = window.setTimeout(() => {
      attempts.current.count += 1;
      refreshRef.current();
    }, 1000 * 2 ** attempts.current.count);
    return () => window.clearTimeout(timer);
  }, [finished, turnId, rowState, refreshKey]);
}
