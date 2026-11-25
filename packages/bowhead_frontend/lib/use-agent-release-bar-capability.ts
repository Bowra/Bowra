"use client";

import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";

// FEATURE_AGENT_RELEASE_BAR, from the `/me` capabilities like the other deployment switches the browser
// needs (rollout register: Agent release bar). Fails closed: without an answer the workbench keeps its
// release cards.
let forcedForTest: boolean | null = null;

/** Test hook: force the capability (null returns to the `/me` answer). */
export function setAgentReleaseBarCapabilityForTest(
  value: boolean | null,
): void {
  forcedForTest = value;
}

export function useAgentReleaseBarCapability(): boolean {
  const enabled =
    useOptionalAuthSession()?.me?.capabilities?.agent_release_bar === true;
  return forcedForTest ?? enabled;
}
