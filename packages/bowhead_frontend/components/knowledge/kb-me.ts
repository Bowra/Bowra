"use client";

import { appFetch as fetch } from "@/lib/base-path";

/**
 * kb-management · shared "me context" (user/team identity + team-admin).
 * Fetch identity only here; current space always comes from useSpaceSelection (see use-kb-me.ts). The old implementation substituted
 * team_id for workspace_id without a space, with every caller reading localStorage separately; deep-link first frames
 * could not read it, and space switches did not update it.
 */
import type { KbScope } from "@/lib/kb-api";

export interface KbIdentity {
  user_id: string;
  team_id: string;
  is_team_admin: boolean;
}

export interface KbMeContext extends KbIdentity {
  /** Current space; "" = none available yet (block space-scoped writes; never substitute team_id). */
  workspace_id: string;
}

export async function fetchKbMe(): Promise<KbIdentity | null> {
  try {
    const r = await fetch("/api/v1/auth/me");
    if (!r.ok) return null;
    const me = await r.json();
    return {
      user_id: me.id,
      team_id: me.team_id,
      is_team_admin: Boolean(me.is_team_admin),
    };
  } catch {
    return null;
  }
}

/** scope_id for a target scope per backend routing (personal→user, ws→ws, team→team). */
export function scopeIdFor(scope: KbScope, me: KbMeContext): string {
  if (scope === "personal") return me.user_id;
  if (scope === "space") return me.workspace_id;
  return me.team_id;
}
