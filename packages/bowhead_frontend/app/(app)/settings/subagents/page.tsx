"use client";

// Agent Marketplace M1: legacy bookmarks land in the unified governance view
// only while its short-lived rollout gate is active.
import { useEffect } from "react";

import { useAuthSession } from "@/lib/auth/auth-session-context";
import { useAppRouter } from "@/lib/use-app-router";

export default function LegacyRedirect() {
  const router = useAppRouter();
  const session = useAuthSession();
  useEffect(() => {
    if (session.status !== "authenticated" || !session.me) return;
    const query = new URLSearchParams(window.location.search);
    if (query.get("agent_id")) {
      router.replace(`/agents/manage/?${query}`);
      return;
    }
    if (!session.me.capabilities?.subagent_management_v2) {
      router.replace("/team/settings/subagents/");
      return;
    }
    const canGovernTeam = session.me.is_team_admin
      || session.me.team_role === "owner"
      || session.me.team_role === "team_admin";
    router.replace(canGovernTeam ? "/agents/manage/governance/" : "/agents/manage/");
  }, [router, session.me, session.status]);
  return null;
}
