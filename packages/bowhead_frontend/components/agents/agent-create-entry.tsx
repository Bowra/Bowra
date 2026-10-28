"use client";

import { useEffect } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui/notice";
import { AGENT_CREATION_HANDOFF_KEY } from "@/lib/agent-creation-handoff";
import { clearFeedbackPrefill, peekFeedbackPrefill } from "@/lib/feedback-candidates-api";
import { useAuthSession } from "@/lib/auth/auth-session-context";
import { TEAM_SETTINGS_ROLES, type SettingsTeamRole } from "@/lib/settings-navigation";
import { useAppRouter } from "@/lib/use-app-router";
import { useSpaceSelection } from "@/lib/use-space";

export function AgentCreateEntry() {
  const router = useAppRouter();
  const t = useTranslations("agents_marketplace");
  const session = useAuthSession();
  const canCreate = Boolean(session.me?.capabilities?.subagent_creator);
  const spaces = useSpaceSelection();
  // Drafts live in a Space, so creation can't open without one. Say so here instead of returning to Home,
  // where the creation panel would silently never open.
  const noSpace = !spaces.loading && !spaces.error && !spaces.activeId;
  const role = session.me?.team_role || (session.me?.is_team_admin ? "team_admin" : "member");
  const canManageSpaces = TEAM_SETTINGS_ROLES.includes(role as SettingsTeamRole);
  useEffect(() => {
    if (!canCreate) {
      router.replace("/agents/");
      return;
    }
    if (spaces.loading || noSpace) return;
    let fallbackToUrl = false;
    try {
      const params = new URLSearchParams(window.location.search);
      const handoff = params.get("feedback_prefill") === "1"
        ? peekFeedbackPrefill(params.get("ref") ?? "")
        : null;
      const purpose = handoff?.prefill.kind === "cca_intent"
        ? handoff.prefill.purpose.trim()
        : "";
      sessionStorage.setItem(AGENT_CREATION_HANDOFF_KEY, purpose.slice(0, 2000));
      if (handoff) clearFeedbackPrefill();
    } catch {
      // A browser may deny sessionStorage. Keep the direct creation entry
      // usable without putting the user's purpose text in the URL.
      fallbackToUrl = true;
    }
    router.replace(fallbackToUrl ? "/?agent_create=1" : "/");
  }, [canCreate, router, spaces.loading, noSpace]);
  if (canCreate && noSpace) {
    return (
      <div className="am-page">
        <Notice tone="info" className="am-create-no-space">
          <h2>{t("create_no_space_title")}</h2>
          <p>{t(canManageSpaces ? "create_no_space_body_manage" : "create_no_space_body_member")}</p>
          <div className="am-create-no-space-actions">
            {canManageSpaces ? (
              <Button variant="primary" onClick={() => router.push("/team/settings/spaces/?from=no-space")}>{t("create_no_space_action")}</Button>
            ) : null}
            <Button variant="secondary" onClick={() => router.push("/agents/")}>{t("create_no_space_back")}</Button>
          </div>
        </Notice>
      </div>
    );
  }
  return <div className="am-page"><div className="am-detail-loading" aria-label={t("opening_creator")} /></div>;
}
