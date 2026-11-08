"use client";

import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";

/**
 * FEATURE_NOTIFICATION_REACH, as `/me` reports it: one attention loop for turn completions and
 * notifications, panel actions, Team labels and browser notifications for urgent items. False
 * until `/me` loads, so the previous behavior stays in place when it is unknown.
 */
export function useNotificationReachCapability(): boolean {
  return useOptionalAuthSession()?.me?.capabilities?.notification_reach === true;
}
