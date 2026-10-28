"use client";

import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";

/** Team settings › Models › assignments: where the evaluation (judge) model is chosen. */
export const JUDGE_MODEL_SETTINGS_PATH = "/team/settings/models/assignments/";

/**
 * Whether the signed-in person can configure team models (team owners and team administrators, the
 * same roles the model settings API admits). Only they are offered a link to fix a missing judge;
 * everyone else gets the plain message (design agent-release-beginner-experience §8.2).
 */
export function useCanConfigureModels(): boolean {
  const me = useOptionalAuthSession()?.me;
  return Boolean(me && (me.is_team_admin || me.team_role === "owner" || me.team_role === "team_admin"));
}
