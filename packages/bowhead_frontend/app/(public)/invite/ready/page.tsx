"use client";

import {
 Suspense, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";

import { roleKey } from "@/lib/invite-role";
import {
  BusyLabel,
  CircleCheckIcon,
  PublicBanner,
  PublicShell,
  PublicSteps,
  PublicSummary,
  useBrandedTranslations,
} from "@/components/public-shell";
import { fetchWorkspaces } from "@/lib/api";
import { fetchTeams, setLastTeam } from "@/lib/auth-api";
import { readInviteResult, type InviteRegisterResult } from "@/lib/invite-api";
import { switchActiveSpace } from "@/lib/use-space";
import { appDocumentHref } from "@/lib/use-app-router";

export default function InviteReadyPage() {
  return (
    <Suspense fallback={null}>
      <InviteReadyInner />
    </Suspense>
  );
}

function InviteReadyInner() {
  const t = useBrandedTranslations("invite");
  const searchParams = useSearchParams();
  const state = searchParams.get("state");
  const [result, setResult] = useState<InviteRegisterResult | null>(null);
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  const [workspaceName, setWorkspaceName] = useState<string | null>(null);
  const [loadingWorkspace, setLoadingWorkspace] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const stored = readInviteResult();
    setResult(stored);
    const storedWorkspaceId = stored?.workspace?.id || null;
    const storedWorkspaceName = stored?.workspace?.name || null;
    setWorkspaceId(storedWorkspaceId);
    setWorkspaceName(storedWorkspaceName);

    const needsWorkspace = (
      state === "workspace_ready"
      || stored?.next_step === "workspace_ready"
      || stored?.invite_type === "workspace"
    );
    if (!needsWorkspace || storedWorkspaceId) return;

    let cancelled = false;
    setLoadingWorkspace(true);
    fetchWorkspaces()
      .then((r) => {
        if (cancelled) return;
        const target = r.items.find((item) => item.id === r.active_id) || r.items[0] || null;
        if (target) {
          setWorkspaceId(target.id);
          setWorkspaceName(target.name);
        }
        setError(r.error ? t("ready.space_list_error") : null);
      })
      .catch(() => {
        if (!cancelled) setError(t("ready.space_list_error"));
      })
      .finally(() => {
        if (!cancelled) setLoadingWorkspace(false);
      });
    return () => {
      cancelled = true;
    };
  }, [state, t]);

  const isWorkspaceReady = useMemo(
    () => state === "workspace_ready" || result?.next_step === "workspace_ready" || result?.invite_type === "workspace",
    [result, state],
  );

  const enterWorkspace = () => {
    void continueInInvitedTeam("/");
  };

  const continueInInvitedTeam = async (path: string) => {
    setLoadingWorkspace(true);
    setError(null);
    try {
      const targetTeamId = result?.team?.id || null;
      if (targetTeamId) {
        const teams = await fetchTeams();
        if (teams.active_team_id !== targetTeamId) {
          await setLastTeam(targetTeamId);
        }
      }
      if (workspaceId) switchActiveSpace(workspaceId);
      window.location.replace(appDocumentHref(path));
    } catch {
      setError(t("ready.continue_error"));
    } finally {
      setLoadingWorkspace(false);
    }
  };

  if (isWorkspaceReady) {
    return (
      <PublicShell
        width="invite"
        steps={<PublicSteps current={3} label={t("progress.step_3.title")} />}
        status={{ tone: "success", icon: <CircleCheckIcon /> }}
        title={t("ready.space_title")}
        subtitle={workspaceName
          ? t("ready.space_lead_named", { name: workspaceName })
          : t("ready.space_lead")}
      >
        <PublicSummary
          label={t("ready.summary_aria")}
          rows={[
            { term: t("summary.status"), value: t("ready.access_granted") },
            {
              term: t("summary.space"),
              value: workspaceName || workspaceId || t("ready.available_in_bowhead"),
            },
            { term: t("summary.role"), value: t(roleKey(result?.workspace?.role)) },
          ]}
        />
        {error ? <PublicBanner tone="warning" focusKey={error}>{error}</PublicBanner> : null}
        <div className="pub-actions">
          <button
            className="pub-button pub-button--primary"
            type="button"
            onClick={enterWorkspace}
            disabled={loadingWorkspace}
            aria-busy={loadingWorkspace || undefined}
          >
            <BusyLabel busy={loadingWorkspace} idle={t("ready.enter_space")} pending={t("ready.checking_space")} />
          </button>
        </div>
        <p className="pub-note">{t("ready.space_permission_note")}</p>
      </PublicShell>
    );
  }

  return (
    <PublicShell
      width="invite"
      steps={<PublicSteps current={3} label={t("progress.step_3.title")} />}
      status={{ tone: "success", icon: <CircleCheckIcon /> }}
      title={t("ready.team_title")}
      subtitle={t("ready.team_lead")}
    >
      <PublicSummary
        label={t("ready.summary_aria")}
        rows={[
          { term: t("summary.status"), value: t("ready.team_joined") },
          { term: t("summary.space"), value: t("ready.pending_admin_grant") },
          { term: t("ready.data_access"), value: t("ready.managed_separately") },
        ]}
      />
      <div>
        <p className="pub-help">{t("ready.next_steps_title")}</p>
        <ul className="pub-list" aria-label={t("ready.next_steps_aria")}>
          <li>{t("ready.complete_profile")}</li>
          <li>{t("ready.ask_for_space")}</li>
          <li>{t("ready.keep_permissions_separate")}</li>
        </ul>
      </div>
      {error ? <PublicBanner tone="warning" focusKey={error}>{error}</PublicBanner> : null}
      <div className="pub-actions">
        <button
          className="pub-button pub-button--primary"
          type="button"
          onClick={() => void continueInInvitedTeam("/")}
          disabled={loadingWorkspace}
          aria-busy={loadingWorkspace || undefined}
        >
          <BusyLabel busy={loadingWorkspace} idle={t("ready.continue_to_bowhead")} pending={t("ready.continue_to_bowhead")} />
        </button>
      </div>
      <p className="pub-note">{t("ready.team_permission_note")}</p>
    </PublicShell>
  );
}
