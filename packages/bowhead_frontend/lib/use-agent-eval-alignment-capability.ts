"use client";

import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";

// FEATURE_AGENT_EVAL_ALIGNMENT, from the `/me` capabilities like the other deployment switches the
// browser needs (rollout register: Coach alignment loop). Fails closed: without an answer nothing of
// the loop shows.
let forcedForTest: boolean | null = null;

/** Test hook: force the capability (null returns to the `/me` answer). */
export function setAgentEvalAlignmentCapabilityForTest(value: boolean | null): void {
  forcedForTest = value;
}

export function useAgentEvalAlignmentCapability(): boolean {
  const enabled = useOptionalAuthSession()?.me?.capabilities?.agent_eval_alignment === true;
  return forcedForTest ?? enabled;
}
