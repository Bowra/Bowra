"use client";

import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";

// FEATURE_AGENT_DRAFT_TRIAL_V1, from the `/me` capabilities like the release bar switch (rollout register:
// Owner draft trial). Fails closed: without an answer no trial entry, band or answer action appears.
let forcedForTest: boolean | null = null;

/** Test hook: force the capability (null returns to the `/me` answer). */
export function setAgentDraftTrialCapabilityForTest(value: boolean | null): void {
  forcedForTest = value;
}

export function useAgentDraftTrialCapability(): boolean {
  const enabled = useOptionalAuthSession()?.me?.capabilities?.agent_draft_trial === true;
  return forcedForTest ?? enabled;
}
