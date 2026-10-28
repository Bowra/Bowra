"use client";

import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import type { MeUser } from "@/lib/auth-api";

/** Whether the access-request flow is on (server-resolved `/me` capability). */
export function accessRequestsEnabledFrom(me: Pick<MeUser, "capabilities"> | null | undefined): boolean {
  return me?.capabilities?.access_requests === true;
}

/**
 * Whether the access-request UI is on for the signed-in user. Reads the shared auth session
 * only; outside the authenticated app root (or before `/me` has loaded) it is off.
 */
export function useAccessRequestsEnabled(): boolean {
  return accessRequestsEnabledFrom(useOptionalAuthSession()?.me);
}
