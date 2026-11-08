"use client";

/**
 * One attention loop (FEATURE_NOTIFICATION_REACH): a single timer for everything that may need the
 * user while they are elsewhere.
 *
 * Each tick polls the turns that ended since the last check (the same announcements as
 * `TurnCompletionNotifier`), then the notification count, which it publishes to the bell. When
 * the count reports a newer urgent item, it fetches those items and announces each once: an
 * in-app toast with Open, plus a browser notification while the tab is in the background and
 * permission is granted. Nothing that arrived before the page loaded is announced; the badge
 * already shows it. Tabs share what they announced, so one event is announced once.
 *
 * Permission is never requested on page load: the first announcement of either kind is followed
 * by one offer, whose answer is remembered per browser.
 */

import { useEffect } from "react";
import { useLocale, useTranslations } from "next-intl";

import { fetchBrowserCandidates, fetchNotificationCount, type NotificationItemDTO } from "@/lib/api";
import { apiFetch, HttpStatusError } from "@/lib/api-client";
import {
  browserNotificationAllowed,
  HIDDEN_POLL_MS,
  NOTIFICATION_CHANNEL,
  openBroadcastChannel,
  permissionOfferDue,
  publishNotificationCount,
  registerAttentionRefresh,
  TURN_CHANNEL,
  VISIBLE_POLL_MS,
  viewedSessionId,
  writeOffer,
} from "@/lib/attention-loop";
import { localizeNotification } from "@/lib/notification-copy";
import { openAnnouncedNotification } from "@/lib/notification-open";
import { toast, type ToastKind } from "@/lib/use-toast";
import { useAppRouter } from "@/lib/use-app-router";
import { TURN_COMPLETIONS_URL, type TurnCompletion } from "@/components/turn-completion-notifier";

const centerDisabled = () => process.env.NEXT_PUBLIC_BOWHEAD_NOTIFICATION_CENTER === "0";

export function AttentionLoop() {
  const t = useTranslations("completion");
  const tn = useTranslations("notifications");
  const locale = useLocale();
  const router = useAppRouter();

  useEffect(() => {
    let cursor: string | null = null;
    let candidatesSince: string | null | undefined;
    let countOff = centerDisabled();
    let timer: ReturnType<typeof setTimeout> | null = null;
    let running = false;
    let stopped = false;
    let offered = false;
    const shownTurns = new Set<string>();
    const shownItems = new Set<string>();
    const turnChannel = openBroadcastChannel(TURN_CHANNEL, (id) => shownTurns.add(id));
    const itemChannel = openBroadcastChannel(NOTIFICATION_CHANNEL, (id) => shownItems.add(id));

    const offerPermission = () => {
      if (offered || !permissionOfferDue()) return;
      offered = true;
      toast({
        kind: "info",
        text: t("offer"),
        durationMs: 15_000,
        action: {
          label: t("enable"),
          onAction: async () => {
            const result = await Notification.requestPermission();
            writeOffer(result === "granted" ? "granted" : "dismissed");
          },
        },
        onDismiss: () => writeOffer("dismissed"),
      });
    };

    const showBrowserNotification = (title: string, body: string, tag: string, onOpen: () => void) => {
      if (!browserNotificationAllowed()) return;
      try {
        const note = new Notification(title, { body, tag });
        note.onclick = () => {
          window.focus();
          onOpen();
          note.close();
        };
      } catch {
        // Some browsers only allow notifications from a service worker.
      }
    };

    const openSession = (sessionId: string) => router.push(`/?session=${encodeURIComponent(sessionId)}`);

    const announceTurn = (item: TurnCompletion) => {
      if (shownTurns.has(item.turn_id)) return;
      shownTurns.add(item.turn_id);
      turnChannel?.postMessage(item.turn_id);
      if (item.session_id === viewedSessionId()) return;
      const title = item.title || t("untitled");
      const kind: ToastKind = item.outcome === "completed" ? "success" : item.outcome === "partial" ? "warning" : "error";
      const text = t(`${item.outcome}`, { title });
      toast({ kind, text, action: { label: t("open"), onAction: () => openSession(item.session_id) } });
      showBrowserNotification(text, t("notification_body"), item.turn_id, () => openSession(item.session_id));
      offerPermission();
    };

    const announceItem = (item: NotificationItemDTO) => {
      if (shownItems.has(item.id)) return;
      shownItems.add(item.id);
      itemChannel?.postMessage(item.id);
      const copy = localizeNotification(item, tn, locale);
      const open = () => void openAnnouncedNotification(item, router);
      toast({ kind: "warning", text: copy.title, action: { label: t("open"), onAction: open } });
      showBrowserNotification(copy.title, copy.body ?? tn("browser_notification_body"), `ntf:${item.id}`, open);
      offerPermission();
    };

    const pollTurns = async () => {
      const query = cursor ? `?after=${encodeURIComponent(cursor)}` : "";
      const response = await apiFetch(`${TURN_COMPLETIONS_URL}${query}`, { cache: "no-store" });
      if (!response.ok) return;
      const body = (await response.json()) as { items?: TurnCompletion[]; cursor?: string };
      if (cursor !== null) for (const item of body.items ?? []) announceTurn(item);
      if (typeof body.cursor === "string") cursor = body.cursor;
    };

    const pollNotifications = async () => {
      if (countOff) return;
      let counts;
      try {
        counts = await fetchNotificationCount();
      } catch (error: unknown) {
        // The center is switched off on the server: stop asking until the page reloads.
        if (error instanceof HttpStatusError && error.status === 404) countOff = true;
        publishNotificationCount({ ok: false, error });
        return;
      }
      publishNotificationCount({ ok: true, counts });
      const latest = counts.latestBrowserCandidateAt ?? null;
      // The first count only sets the starting point: earlier items are on the badge already.
      if (candidatesSince === undefined) {
        candidatesSince = latest;
        return;
      }
      if (!latest || (candidatesSince !== null && latest <= candidatesSince)) return;
      const items = await fetchBrowserCandidates(candidatesSince);
      candidatesSince = latest;
      for (const item of [...items].reverse()) announceItem(item);
    };

    const tick = async () => {
      if (running || stopped) return;
      running = true;
      if (timer) clearTimeout(timer);
      timer = null;
      try {
        await pollTurns();
      } catch {
        // A missed poll is retried on the next tick.
      }
      try {
        await pollNotifications();
      } catch {
        // Announcing urgent items is best effort; the badge still shows them.
      }
      running = false;
      if (!stopped) {
        timer = setTimeout(() => void tick(), document.visibilityState === "hidden" ? HIDDEN_POLL_MS : VISIBLE_POLL_MS);
      }
    };

    const onFocus = () => void tick();
    window.addEventListener("focus", onFocus);
    registerAttentionRefresh(() => void tick());
    void tick();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      window.removeEventListener("focus", onFocus);
      registerAttentionRefresh(null);
      turnChannel?.close();
      itemChannel?.close();
    };
  }, [locale, router, t, tn]);

  return null;
}
