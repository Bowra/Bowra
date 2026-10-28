"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useAppRouter } from "@/lib/use-app-router";
import { agentManagementReturnHref } from "@/lib/agent-navigation";
import { CapabilityLink } from "@/components/capabilities/capability-link";
import { useTranslations } from "next-intl";

import { CreateSubagentCard } from "@/components/canvas/sections/create-subagent-card";
import { useAuthSession } from "@/lib/auth/auth-session-context";
import { getAgentWorkingCopy } from "@/lib/agents-api";
import { ApiError } from "@/lib/api-client";
import { getSubagentManager, type TeamSubagentOwnerManagerDetail } from "@/lib/team-subagents-api";
import { AgentManagement } from "./agent-management";
import { AgentReleaseManager } from "./agent-release-manager";
import { AgentReleaseReview } from "./agent-release-review";
import { AgentBreadcrumb } from "./agent-breadcrumb";

type OwnerEditorState =
  | { kind: "loading" }
  | { kind: "revisioned" }
  | { kind: "legacy"; detail: TeamSubagentOwnerManagerDetail }
  | { kind: "unavailable" }
  | { kind: "error" };

function AgentOwnerManager({ agentId, returnHref }: { agentId: string; returnHref: string }) {
  const router = useAppRouter();
  const t = useTranslations("agents_marketplace");
  const session = useAuthSession();
  const hasSession = Boolean(session.me);
  const managementReady = Boolean(session.me?.capabilities?.subagent_management_v2);
  const [state, setState] = useState<OwnerEditorState>({ kind: "loading" });
  const [retryKey, setRetryKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setState({ kind: "loading" });
    if (!hasSession) return () => { cancelled = true; };
    void getSubagentManager(agentId).then(async (detail) => {
      if (detail.definition_mode === "legacy_mutable") {
        if (!cancelled) setState({ kind: "legacy", detail });
        return;
      }
      if (detail.definition_mode === "revisioned") {
        if (!cancelled) setState({ kind: managementReady ? "revisioned" : "unavailable" });
        return;
      }
      // Rolling deployments may still omit definition_mode from the Owner DTO.
      if (!managementReady) {
        if (!cancelled) setState({ kind: "legacy", detail });
        return;
      }
      try {
        await getAgentWorkingCopy(agentId);
        if (!cancelled) setState({ kind: managementReady ? "revisioned" : "unavailable" });
      } catch (cause) {
        if (!cancelled) setState(cause instanceof ApiError && cause.code === "agent_release_cutover_required"
          ? { kind: "legacy", detail } : { kind: "error" });
      }
    }).catch(() => {
      if (!cancelled) setState({ kind: "error" });
    });
    return () => { cancelled = true; };
  }, [agentId, hasSession, managementReady, retryKey]);

  if (state.kind === "revisioned") return <AgentReleaseManager agentId={agentId} returnHref={returnHref} />;
  if (state.kind === "loading") return <div className="am-page am-detail-page" aria-busy="true"><div className="am-detail-loading" /></div>;
  if (state.kind === "legacy" && state.detail.actions.edit) {
    return <div className="am-page am-release-page">
      <AgentBreadcrumb href={returnHref} label={t("back_to_manage")} />
      <p className="legacy-am-legacy-editor-intro">{t("legacy_editor_intro")}</p>
      <CreateSubagentCard key={agentId} editing={state.detail} onClose={() => router.push(returnHref)} />
    </div>;
  }
  return <div className="am-page am-release-page">
    <AgentBreadcrumb href={returnHref} label={t("back_to_manage")} />
    <div className="am-notice" role="alert">
      <span>{state.kind === "legacy" ? t("legacy_editor_unavailable") : state.kind === "unavailable" ? t("release_management_unavailable") : t("release_load_failed")}</span>
      <button onClick={() => setRetryKey((value) => value + 1)}>{t("retry")}</button>
    </div>
  </div>;
}

export function AgentManagementRoute() {
  const params = useSearchParams();
  const search = params?.toString() ?? (typeof window === "undefined" ? "" : window.location.search);
  const query = new URLSearchParams(search);
  const agentId = query.get("agent_id");
  const candidateId = query.get("release_candidate");
  const router = useAppRouter();
  useEffect(() => {
    if (!agentId && window.location.pathname.replace(/\/+$/, "").endsWith("/agents/manage")) {
      router.replace("/agents/manage/hired/");
    }
  }, [agentId, router]);
  if (candidateId && agentId) return <AgentReleaseReview key={`${agentId}:${candidateId}`} agentId={agentId} candidateId={candidateId} />;
  if (agentId) return <AgentOwnerManager key={agentId} agentId={agentId} returnHref={agentManagementReturnHref(search)} />;
  return <AgentManagement />;
}
