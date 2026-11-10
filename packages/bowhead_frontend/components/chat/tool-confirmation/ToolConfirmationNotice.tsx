"use client";

/**
 * ToolConfirmationNotice — the non-blocking reminder for confirmations waiting
 * outside the card the user is looking at (design §6.3, decision D3).
 *
 * Sources, merged by confirmation id:
 *   · `GET /api/v1/tool-confirmations` (owner-only, all kinds), read once on mount
 *     and then polled while any known turn has an active step or anything waits;
 *   · pending confirmations already in the turn store. SSE keeps running in a
 *     hidden tab while polling pauses, so this updates the count at once.
 *
 * Items whose card is on screen are skipped; the rest are grouped by session with
 * a View action. The tab title carries "(n) " while anything waits. A polite live
 * region, no modal, no focus trap.
 */
import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { viewedSessionId } from "@/lib/attention-loop";
import { useAuthSession } from "@/lib/auth/auth-session-context";
import { setTitleAttentionCount } from "@/lib/page-title-store";
import { listToolConfirmations, type ToolConfirmationSummary } from "@/lib/tool-confirmation-api";
import {
  focusConfirmationCard,
  requestConfirmationFocus,
  useVisibleConfirmationIds,
} from "@/lib/tool-confirmation-presence";
import {
  knownConfirmationOutcome,
  useHasAnyActiveStep,
  usePendingConfirmations,
} from "@/lib/turn-store";
import { useAppRouter } from "@/lib/use-app-router";
import { useConfirmationQueuePoll } from "@/lib/use-confirmation-queue-poll";

interface WaitingConfirmation {
  id: string;
  /** Null when only this tab's turn store knows it (not yet in a server read). */
  sessionId: string | null;
  sessionTitle: string | null;
}

interface NoticeGroup {
  sessionId: string | null;
  sessionTitle: string | null;
  ids: string[];
}

export function ToolConfirmationNotice() {
  const t = useTranslations("toolConfirmation");
  const router = useAppRouter();
  const { me } = useAuthSession();
  const viewerId = me?.id ?? null;
  const storePending = usePendingConfirmations();
  const anyActiveStep = useHasAnyActiveStep();
  const visibleIds = useVisibleConfirmationIds();
  const [server, setServer] = useState<{ items: ToolConfirmationSummary[]; startedAt: number }>({
    items: [],
    startedAt: 0,
  });
  const [mountCheck, setMountCheck] = useState(true);
  const readStartedAtRef = useRef(0);

  const waiting = new Map<string, WaitingConfirmation>();
  for (const item of server.items) {
    // The turn store already holds this decision: the server read is older than the event.
    if (knownConfirmationOutcome(item.id)) continue;
    waiting.set(item.id, { id: item.id, sessionId: item.session_id, sessionTitle: item.session_title });
  }
  for (const pending of storePending) {
    const { confirmation } = pending;
    if (!viewerId || confirmation.requesterUserId !== viewerId || waiting.has(confirmation.id)) continue;
    // Known before the last completed server read but absent from it: no longer waiting. The store
    // keeps a turn after its stream closes (for example after leaving the chat), so its copy can be stale.
    if (server.startedAt > 0 && confirmation.receivedAt < server.startedAt) continue;
    waiting.set(confirmation.id, { id: confirmation.id, sessionId: null, sessionTitle: null });
  }
  const count = waiting.size;

  useConfirmationQueuePoll({
    enabled: mountCheck || anyActiveStep || count > 0,
    load: async () => {
      const startedAt = Date.now();
      try {
        const items = await listToolConfirmations();
        readStartedAtRef.current = startedAt;
        return items;
      } catch (failure) {
        setMountCheck(false);
        throw failure;
      }
    },
    // Same update batch as the items, so `enabled` never flickers off while an item is arriving.
    onItems: (items) => {
      setServer({ items, startedAt: readStartedAtRef.current || Date.now() });
      setMountCheck(false);
    },
  });

  useEffect(() => {
    setTitleAttentionCount(count);
  }, [count]);
  useEffect(() => () => setTitleAttentionCount(0), []);

  const groups: NoticeGroup[] = [];
  for (const item of waiting.values()) {
    if (visibleIds.has(item.id)) continue;
    const group = groups.find((candidate) => candidate.sessionId === item.sessionId);
    if (group) {
      group.ids.push(item.id);
      group.sessionTitle ??= item.sessionTitle;
    } else {
      groups.push({ sessionId: item.sessionId, sessionTitle: item.sessionTitle, ids: [item.id] });
    }
  }

  const view = (group: NoticeGroup) => {
    const id = group.ids[0];
    if (!id) return;
    if (group.sessionId === null || group.sessionId === viewedSessionId()) {
      if (focusConfirmationCard(id)) return;
    }
    if (!group.sessionId) return;
    // The card mounts after the session loads; it scrolls into view and takes focus then.
    requestConfirmationFocus(id);
    router.push(`/?session=${encodeURIComponent(group.sessionId)}`);
  };

  return (
    <div className="tcf-notice" role="status" aria-label={t("noticeLabel")}>
      {groups.map((group) => (
        <div className="tcf-notice-row" key={group.sessionId ?? "current"}>
          <span className="tcf-notice-text">
            {group.sessionId === null
              ? t("noticeCurrent", { count: group.ids.length })
              : t("noticeSession", { title: group.sessionTitle?.trim() || t("untitled"), count: group.ids.length })}
          </span>
          <button type="button" className="tcf-notice-view" onClick={() => view(group)}>
            {t("view")}
          </button>
        </div>
      ))}
    </div>
  );
}
