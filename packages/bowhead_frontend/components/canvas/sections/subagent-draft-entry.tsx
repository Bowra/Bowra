"use client";

import { useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";

import { AlertCircle, Bot } from "@/components/icons";
import { Button } from "@/components/ui/button";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { CanvasSection, FieldRow, SectionHeading } from "@/components/ui/section-heading";
import { CanvasObjectFooter, CanvasObjectHeader, CanvasObjectSurface, StateBlock } from "@/components/canvas/object";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { TEAM_SETTINGS_ROLES, type SettingsTeamRole } from "@/lib/settings-navigation";
import { listManualSubagentDrafts, type SubagentDraftListItem, type SubagentDraftRequestContext } from "@/lib/subagent-drafts-api";
import { useAppRouter } from "@/lib/use-app-router";

/**
 * A generic create entry must not silently replace one of the owner's open tasks. The tab × closes it (D1).
 * Drafts are saved in a Space: `workspaceId` is empty until the Space resolves, and stays empty when the
 * account has none (`spaceLoading` false), so the entry never asks the server for drafts without one.
 */
export function SubagentDraftEntry({
  onResume, onNewAi, onNewManual, unified = false, requestContext, spaceLoading,
}: {
  onResume: (draftId: string) => void;
  onNewAi: () => void;
  onNewManual: () => void;
  unified?: boolean;
  requestContext: SubagentDraftRequestContext;
  spaceLoading: boolean;
}) {
  const t = useTranslations("create_subagent");
  const tAgents = useTranslations("agents_marketplace");
  const router = useAppRouter();
  const me = useOptionalAuthSession()?.me;
  const role = me?.team_role || (me?.is_team_admin ? "team_admin" : "member");
  const canManageSpaces = TEAM_SETTINGS_ROLES.includes(role as SettingsTeamRole);
  const { teamId, workspaceId } = requestContext;
  const noSpace = !workspaceId && !spaceLoading;
  const locale = useLocale();
  const formatDate = (value: string) => new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
  const [drafts, setDrafts] = useState<SubagentDraftListItem[] | null>(null);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!workspaceId || !teamId) return;
    let cancelled = false;
    void listManualSubagentDrafts({ teamId, workspaceId }).then((items) => {
      if (cancelled) return;
      setDrafts(items);
      setError(false);
    }).catch(() => {
      if (cancelled) return;
      setDrafts(null);
      setError(true);
    });
    return () => { cancelled = true; };
  }, [attempt, teamId, workspaceId]);
  // With no draft to continue there is nothing to choose: start the new draft instead of asking.
  const startNew = unified ? onNewManual : onNewAi;
  const startNewRef = useRef(startNew);
  startNewRef.current = startNew;
  const empty = drafts !== null && drafts.length === 0;
  useEffect(() => {
    if (empty) startNewRef.current();
  }, [empty]);

  if (noSpace) {
    return (
      <CanvasObjectSurface labelledBy="subagent-entry-title">
        <CanvasObjectHeader titleId="subagent-entry-title" icon={<Bot size={14} aria-hidden />} eyebrow={t("canvas_eyebrow")}
          title={t("entry_title")} subtitle={t("entry_description")} />
        <StateBlock icon={<AlertCircle size={20} />} title={tAgents("create_no_space_title")}
          body={tAgents(canManageSpaces ? "create_no_space_body_manage" : "create_no_space_body_member")}
          primaryAction={canManageSpaces
            ? { label: tAgents("create_no_space_action"), onClick: () => router.push("/team/settings/spaces/?from=no-space") }
            : undefined} />
      </CanvasObjectSurface>
    );
  }

  return (
    <CanvasObjectSurface
      labelledBy="subagent-entry-title"
      footer={(
        <CanvasObjectFooter
          secondary={unified ? undefined : [{ label: t("entry_new_manual"), onClick: onNewManual }]}
          primary={unified ? { label: t("canvas_new_draft"), onClick: onNewManual } : { label: t("entry_new_ai"), onClick: onNewAi }}
        />
      )}
    >
      <CanvasObjectHeader titleId="subagent-entry-title" icon={<Bot size={14} aria-hidden />} eyebrow={t("canvas_eyebrow")}
        title={t("entry_title")} subtitle={t("entry_description")} />
      {drafts === null && !error ? <LoadingSkeleton rows={3} label={t("draft_loading")} /> : null}
      {error ? (
        <StateBlock tone="error" icon={<AlertCircle size={20} />} title={t("draft_open_failed_title")} body={t("entry_load_failed")}
          secondaryActions={[{ label: t("entry_retry"), onClick: () => { setError(false); setAttempt((value) => value + 1); } }]} />
      ) : null}
      {drafts && drafts.length > 0 ? (
        <CanvasSection labelledBy="subagent-entry-existing">
          <SectionHeading id="subagent-entry-existing" title={t("entry_existing")} count={drafts.length} />
          <div>
            {drafts.map((draft) => (
              <FieldRow
                key={draft.draft_id}
                title={draft.title || t("entry_untitled")}
                description={[
                  unified ? null : t(draft.source_mode === "manual" ? "entry_source_manual" : "entry_source_ai"),
                  t(`entry_status_${draft.draft_status}` as never),
                  t("entry_updated", { date: formatDate(draft.updated_at) }),
                  t("entry_expires", { date: formatDate(draft.expires_at) }),
                ].filter(Boolean).join(" · ")}
                control={<Button size="sm" onClick={() => onResume(draft.draft_id)}>{t("entry_resume")}</Button>}
              />
            ))}
          </div>
        </CanvasSection>
      ) : null}
    </CanvasObjectSurface>
  );
}
