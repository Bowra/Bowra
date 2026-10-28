"use client";

/**
 * Completion popup (agent run limits, phase 3; mockup "Agent Run Limits Spec Part 2" D3).
 *
 * Polls the turns of this user's sessions that ended since the last check. A turn that ends
 * while the user is not looking at its session shows an in-app toast with an Open action;
 * when the tab is in the background, the context is secure and the user granted permission,
 * a browser notification is shown as well. Permission is never requested on page load: the
 * first completion toast is followed by one offer, whose answer is remembered per browser.
 */

import { useEffect } from "react";
import { useTranslations } from "next-intl";

import { apiFetch } from "@/lib/api-client";
import { unprefixAppPath } from "@/lib/base-path";
import { toast, type ToastKind } from "@/lib/use-toast";
import { useAppRouter } from "@/lib/use-app-router";

export interface TurnCompletion {
  turn_id: string;
  session_id: string;
  title: string;
  outcome: "completed" | "partial" | "failed" | "cancelled";
  ended_at: string;
}

const URL = "/api/v1/sessions/turn-completions";
const VISIBLE_POLL_MS = 15_000;
const HIDDEN_POLL_MS = 60_000;
const OFFER_KEY = "bowhead.completion-notifications.offer";
const CHANNEL = "bowhead-turn-completions";

/** The session the user is looking at, when the chat page is in the foreground. */
export function viewedSessionId(): string | null {
  if (typeof document === "undefined" || document.visibilityState !== "visible") return null;
  if (unprefixAppPath(window.location.pathname) !== "/") return null;
  return new URLSearchParams(window.location.search).get("session");
}

function readOffer(): string | null {
  try {
    return window.localStorage.getItem(OFFER_KEY);
  } catch {
    return null;
  }
}

function writeOffer(value: "granted" | "dismissed"): void {
  try {
    window.localStorage.setItem(OFFER_KEY, value);
  } catch {
    // Private windows may block storage; the offer can then show again later.
  }
}

function notificationsAvailable(): boolean {
  return typeof window !== "undefined" && window.isSecureContext && "Notification" in window;
}

export function TurnCompletionNotifier() {
  const t = useTranslations("completion");
  const router = useAppRouter();

  useEffect(() => {
    let cursor: string | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let stopped = false;
    let offered = false;
    const shown = new Set<string>();
    let channel: BroadcastChannel | null = null;
    try {
      channel = typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel(CHANNEL);
      channel?.addEventListener("message", (event: MessageEvent) => {
        if (typeof event.data === "string") shown.add(event.data);
      });
    } catch {
      channel = null;
    }

    const open = (sessionId: string) => router.push(`/?session=${encodeURIComponent(sessionId)}`);

    const announce = (item: TurnCompletion) => {
      if (shown.has(item.turn_id)) return;
      shown.add(item.turn_id);
      channel?.postMessage(item.turn_id);
      if (item.session_id === viewedSessionId()) return;
      const title = item.title || t("untitled");
      const kind: ToastKind = item.outcome === "completed" ? "success" : item.outcome === "partial" ? "warning" : "error";
      const text = t(`${item.outcome}`, { title });
      toast({ kind, text, action: { label: t("open"), onAction: () => open(item.session_id) } });
      if (notificationsAvailable() && document.visibilityState === "hidden" && Notification.permission === "granted") {
        try {
          const note = new Notification(text, { body: t("notification_body"), tag: item.turn_id });
          note.onclick = () => {
            window.focus();
            open(item.session_id);
            note.close();
          };
        } catch {
          // Some browsers only allow notifications from a service worker.
        }
      }
      if (!offered && notificationsAvailable() && Notification.permission === "default" && readOffer() === null) {
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
      }
    };

    const poll = async () => {
      try {
        const query = cursor ? `?after=${encodeURIComponent(cursor)}` : "";
        const response = await apiFetch(`${URL}${query}`, { cache: "no-store" });
        if (response.ok) {
          const body = (await response.json()) as { items?: TurnCompletion[]; cursor?: string };
          if (cursor !== null) for (const item of body.items ?? []) announce(item);
          if (typeof body.cursor === "string") cursor = body.cursor;
        }
      } catch {
        // A missed poll is retried on the next tick.
      }
      if (!stopped) {
        timer = setTimeout(() => void poll(), document.visibilityState === "hidden" ? HIDDEN_POLL_MS : VISIBLE_POLL_MS);
      }
    };

    void poll();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      channel?.close();
    };
  }, [router, t]);

  return null;
}
