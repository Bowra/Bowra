"use client";

/**
 * Knowledge bases (FEATURE_KNOWLEDGE_BASES, PRD §4.11 ⑩ "first visit after the upgrade"): two one-time notices at
 * the top of the directory.
 *
 * - Migration: files were organized by their old scopes (My knowledge, the Space's default, the Team knowledge
 *   base) and access is unchanged. Shown once per person and Team.
 * - Space viewers: they can now only use the Space's knowledge bases (P9). Shown once per person, Team and Space,
 *   only while the person is a viewer of the current Space.
 *
 * "Got it" remembers the dismissal in this browser's local storage. Every storage access is guarded: without
 * storage (private windows, blocked site data) the notices still render and "Got it" hides them for this visit.
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { useSpaceSelection } from "@/lib/use-space";

const STORAGE_PREFIX = "bowhead:knowledge-bases:notice";

export function migrationNoticeKey(userId: string, teamId: string): string {
  return `${STORAGE_PREFIX}:migration:${userId}:${teamId}`;
}

export function viewerNoticeKey(userId: string, teamId: string, spaceId: string): string {
  return `${STORAGE_PREFIX}:space-viewer:${userId}:${teamId}:${spaceId}`;
}

function readDismissed(key: string): boolean {
  try {
    return window.localStorage.getItem(key) === "1";
  } catch {
    return false;
  }
}

function writeDismissed(key: string): void {
  try {
    window.localStorage.setItem(key, "1");
  } catch {
    // Storage unavailable: the notice stays hidden for this visit only.
  }
}

/** One dismissible notice: hidden until storage was read, so a dismissed notice never flashes. */
function useOneTimeNotice(key: string | null): { visible: boolean; dismiss: () => void } {
  const [state, setState] = useState<{ key: string | null; dismissed: boolean | null }>({ key: null, dismissed: null });
  useEffect(() => {
    setState({ key, dismissed: key ? readDismissed(key) : null });
  }, [key]);
  const dismiss = useCallback(() => {
    if (!key) return;
    writeDismissed(key);
    setState({ key, dismissed: true });
  }, [key]);
  return { visible: Boolean(key) && state.key === key && state.dismissed === false, dismiss };
}

export function KbMigrationNotices({
  personalName,
  spaceName,
  teamName,
}: {
  /** Display names of the three defaults (rendered from their homes by the directory namer). */
  personalName: string;
  spaceName: string;
  teamName: string;
}) {
  const t = useTranslations("knowledge_bases.notice");
  const me = useOptionalAuthSession()?.me ?? null;
  const { activeId, items } = useSpaceSelection();
  const role = items.find((space) => space.id === activeId)?.role;
  const userId = me?.id ?? null;
  const teamId = me?.team_id ?? null;
  const migration = useOneTimeNotice(userId && teamId ? migrationNoticeKey(userId, teamId) : null);
  const viewer = useOneTimeNotice(
    userId && teamId && activeId && role === "viewer" ? viewerNoticeKey(userId, teamId, activeId) : null,
  );
  if (!migration.visible && !viewer.visible) return null;
  return (
    <div className="kbd-notices">
      {migration.visible ? (
        <div className="settings-notice settings-notice--info kbd-notice" role="status" data-testid="kb-migration-notice">
          <span className="kbd-notice__text">
            {t("migration", { personal: personalName, space: spaceName, team: teamName })}
          </span>
          <Button size="sm" onClick={migration.dismiss}>
            {t("dismiss")}
          </Button>
        </div>
      ) : null}
      {viewer.visible ? (
        <div className="settings-notice settings-notice--muted kbd-notice" role="status" data-testid="kb-viewer-notice">
          <span className="kbd-notice__text">{t("viewer")}</span>
          <Button size="sm" onClick={viewer.dismiss}>
            {t("dismiss")}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
