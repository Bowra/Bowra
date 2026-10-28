"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import {
  autoUpdate,
  flip,
  FloatingPortal,
  offset,
  shift,
  useFloating,
} from "@floating-ui/react";
import clsx from "clsx";

import { ErrorDiagnosticsInline, type ShownError, shownError } from "@/components/error-notice";
import { ArchiveBox, Arrow, Bell, Check, Inbox } from "@/components/icons";
import { ListTime } from "@/components/ui/list-time";
import { Tab, TabList, Tabs } from "@/components/ui/tabs";
import {
  archiveNotification,
  fetchNotificationCount,
  fetchNotifications,
  markAllNotificationsRead,
  markNotificationRead,
  type NotificationCountDTO,
  type NotificationItemDTO,
  type NotificationTab,
} from "@/lib/api";
import { normalizeAppActionUrl } from "@/lib/legacy-app-links";
import { localizeNotification, notificationTypeLabel } from "@/lib/notification-copy";
import { useAppRouter } from "@/lib/use-app-router";

const TABS: { id: NotificationTab; labelKey: string }[] = [
  { id: "action", labelKey: "tab_action" },
  { id: "updates", labelKey: "tab_updates" },
  { id: "all", labelKey: "tab_all" },
];

function isDisabledError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return message.includes(": 404") || message.includes(": 503");
}

export function NotificationCenter() {
  const t = useTranslations("notifications");
  const router = useAppRouter();
  const [disabled, setDisabled] = useState(
    process.env.NEXT_PUBLIC_BOWHEAD_NOTIFICATION_CENTER === "0",
  );
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<NotificationTab>("action");
  const [counts, setCounts] = useState<NotificationCountDTO>({
    actionRequired: 0,
    updatesUnread: 0,
    totalBadge: 0,
  });
  const [items, setItems] = useState<NotificationItemDTO[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<ShownError | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  // A3 (notification double-click guard): a slow markRead/archive used to let the
  // user click the same row repeatedly (re-firing read + router.push). inFlightRef
  // is the synchronous guard (no setState staleness on same-tick double-clicks);
  // busyIds drives the disabled state for rendering.
  const inFlightRef = useRef<Set<string>>(new Set());
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set());

  const { refs, floatingStyles } = useFloating({
    open,
    placement: "bottom-end",
    whileElementsMounted: autoUpdate,
    middleware: [offset(8), flip(), shift({ padding: 10 })],
  });

  const refreshCount = useCallback(async () => {
    if (disabled) return;
    try {
      setCounts(await fetchNotificationCount());
    } catch (e: unknown) {
      if (isDisabledError(e)) {
        setDisabled(true);
        setOpen(false);
      }
      // Topbar should not become noisy when the notification endpoint is down.
    }
  }, [disabled]);

  const load = useCallback(async (activeTab: NotificationTab, cursor?: string | null) => {
    if (disabled) return;
    setLoading(true);
    setErr(null);
    try {
      const body = await fetchNotifications(activeTab, cursor);
      setItems((current) => (cursor ? [...current, ...body.items] : body.items));
      setNextCursor(body.nextCursor);
    } catch (e: unknown) {
      if (isDisabledError(e)) {
        setDisabled(true);
        setOpen(false);
      } else {
        setErr(shownError(t("load_failed_notice"), e));
      }
    } finally {
      setLoading(false);
    }
  }, [disabled, t]);

  useEffect(() => {
    void refreshCount();
    const id = window.setInterval(() => void refreshCount(), 45000);
    const onFocus = () => void refreshCount();
    window.addEventListener("focus", onFocus);
    return () => {
      window.clearInterval(id);
      window.removeEventListener("focus", onFocus);
    };
  }, [refreshCount]);

  useEffect(() => {
    if (!open) return;
    void load(tab);
    void refreshCount();
  }, [load, open, refreshCount, tab]);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      const target = e.target as Node;
      if (triggerRef.current?.contains(target)) return;
      const panel = document.querySelector('[data-notification-center="true"]');
      if (panel?.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [open]);

  const handleOpen = () => setOpen((value) => !value);

  const markBusy = (id: string) => {
    inFlightRef.current.add(id);
    setBusyIds((s) => new Set(s).add(id));
  };
  const clearBusy = (id: string) => {
    inFlightRef.current.delete(id);
    setBusyIds((s) => {
      const n = new Set(s);
      n.delete(id);
      return n;
    });
  };

  const handleAction = async (item: NotificationItemDTO) => {
    if (inFlightRef.current.has(item.id)) return;  // in-flight → ignore re-click
    markBusy(item.id);
    try {
      if (!item.readAt) {
        try {
          await markNotificationRead(item.id);
          setItems((current) => current.map((x) => (x.id === item.id ? { ...x, readAt: new Date().toISOString() } : x)));
          void refreshCount();
        } catch (e: unknown) {
          // A3: don't swallow. A failed markRead previously errored silently, and
          // the unread badge "came back" on the next 45s refetch — surface it.
          if (isDisabledError(e)) {
            setDisabled(true);
            setOpen(false);
            return;
          }
          setErr(shownError(t("mark_read_failed_notice"), e));
        }
      }
      if (item.actionUrl) {
        setOpen(false);
        router.push(normalizeAppActionUrl(item.actionUrl));
      }
    } finally {
      clearBusy(item.id);
    }
  };

  const handleArchive = async (id: string) => {
    if (inFlightRef.current.has(id)) return;  // in-flight → ignore re-click
    markBusy(id);
    try {
      await archiveNotification(id);
      setItems((current) => current.filter((x) => x.id !== id));
      void refreshCount();
    } catch (e: unknown) {
      if (isDisabledError(e)) {
        setDisabled(true);
        setOpen(false);
      } else {
        setErr(shownError(t("archive_failed_notice"), e));
      }
    } finally {
      clearBusy(id);
    }
  };

  const handleReadAll = async () => {
    await markAllNotificationsRead(tab);
    setItems((current) => current.map((x) => ({ ...x, readAt: x.readAt ?? new Date().toISOString() })));
    void refreshCount();
  };

  const badge = counts.totalBadge;
  // P2 batch 3: the bell shows only the action-required count (a dot for plain updates), so
  // its accessible name says the same split: "N pending", plus "M unread updates" when there are any.
  const pendingUpdates = Math.max(counts.updatesUnread, badge - counts.actionRequired, 0);
  const bellLabel =
    counts.actionRequired > 0 && pendingUpdates > 0
      ? t("bell_aria_pending_updates", { action: counts.actionRequired, updates: pendingUpdates })
      : counts.actionRequired > 0
        ? t("bell_aria", { badge: counts.actionRequired })
        : pendingUpdates > 0
          ? t("bell_aria_updates", { updates: pendingUpdates })
          : t("bell_title");

  if (disabled) return null;

  return (
    <>
      <button
        type="button"
        ref={(node) => {
          triggerRef.current = node;
          refs.setReference(node);
        }}
        className={clsx("v2-icon-btn", "notification-bell", { active: open })}
        onClick={handleOpen}
        title={t("bell_title")}
        aria-label={bellLabel}
        aria-expanded={open}
      >
        <Bell size={17} />
        {/* P2 batch 3 (topbar notifications): a number only for things that need you; ordinary updates
            alone show a dot. The accessible name still carries the full unread count. */}
        {counts.actionRequired > 0 ? (
          <span className="notification-bell__badge">
            {counts.actionRequired > 99 ? "99+" : counts.actionRequired}
          </span>
        ) : badge > 0 ? (
          <span className="notification-bell__dot" aria-hidden="true" />
        ) : null}
      </button>

      {open ? (
        <FloatingPortal>
          <section
            ref={refs.setFloating}
            style={floatingStyles}
            className="notification-panel"
            data-notification-center="true"
            aria-label={t("panel_aria")}
          >
            <div className="notification-panel__head">
              <div>
                <div className="notification-panel__title">{t("panel_title")}</div>
                <div className="notification-panel__meta">
                  {t("panel_meta", { action: counts.actionRequired, updates: counts.updatesUnread })}
                </div>
              </div>
              {tab !== "action" && items.some((x) => !x.readAt) ? (
                <button type="button" className="notification-ghost-btn" onClick={() => void handleReadAll()}>
                  <Check size={12} />
                  {t("mark_all_read")}
                </button>
              ) : null}
            </div>

            <Tabs value={tab} onValueChange={setTab}>
              <TabList appearance="segmented" className="notification-tabs" aria-label={t("tabs_aria")}>
                {TABS.map((it) => (
                  <Tab key={it.id} value={it.id} className={clsx("notification-tab", { active: tab === it.id })}>
                    {t(it.labelKey)}
                    {it.id === "action" && counts.actionRequired > 0 ? (
                      <span>{counts.actionRequired}</span>
                    ) : null}
                    {it.id === "updates" && counts.updatesUnread > 0 ? (
                      <span>{counts.updatesUnread}</span>
                    ) : null}
                  </Tab>
                ))}
              </TabList>
            </Tabs>

            <div className="notification-list">
              {err ? (
                <div className="notification-empty danger">
                  {err.message}
                  <ErrorDiagnosticsInline error={err.error} surface="global" />
                </div>
              ) : null}
              {!err && loading && items.length === 0 ? <div className="notification-empty">{t("loading")}</div> : null}
              {!err && !loading && items.length === 0 ? <NotificationEmpty tab={tab} /> : null}
              {!err ? (
                items.map((item) => (
                  <NotificationRow
                    key={item.id}
                    item={item}
                    busy={busyIds.has(item.id)}
                    onOpen={() => void handleAction(item)}
                    onArchive={() => void handleArchive(item.id)}
                  />
                ))
              ) : null}
            </div>

            {nextCursor ? (
              <button
                type="button"
                className="notification-load-more"
                disabled={loading}
                onClick={() => void load(tab, nextCursor)}
              >
                {loading ? t("loading") : t("load_more")}
              </button>
            ) : null}
          </section>
        </FloatingPortal>
      ) : null}
    </>
  );
}

function NotificationRow({
  item,
  busy = false,
  onOpen,
  onArchive,
}: {
  item: NotificationItemDTO;
  busy?: boolean;
  onOpen: () => void;
  onArchive: () => void;
}) {
  const t = useTranslations("notifications");
  const locale = useLocale();
  const copy = localizeNotification(item, t, locale);
  const unread = !item.readAt;
  const actionRequired = (item.type === "approval.required" || item.type === "access_requested" || item.type === "memory.candidate")
    && item.state === "open";
  return (
    <article
      className={clsx("notification-row", { unread, action: actionRequired, "is-busy": busy })}
      aria-busy={busy || undefined}
    >
      <button
        type="button"
        className="notification-row__main"
        onClick={onOpen}
        disabled={busy}
      >
        <span className="notification-row__dot" aria-hidden="true" />
        <span className="notification-row__body">
          <span className="notification-row__top">
            <span className="notification-row__title">{copy.title}</span>
            <span className={clsx("notification-row__chip", priorityClass(item.priority))}>
              {notificationTypeLabel(item, t)}
            </span>
          </span>
          {copy.body ? <span className="notification-row__text">{copy.body}</span> : null}
          <ListTime className="notification-row__time" value={item.createdAt} empty="" />
        </span>
        {item.actionUrl ? <Arrow size={12} /> : null}
      </button>
      <button
        type="button"
        className="notification-row__archive"
        onClick={onArchive}
        disabled={busy}
        title={t("archive_title")}
        aria-label={t("archive_aria")}
      >
        <ArchiveBox size={14} />
      </button>
    </article>
  );
}

function NotificationEmpty({ tab }: { tab: NotificationTab }) {
  const t = useTranslations("notifications");
  const text = tab === "action" ? t("empty_action") : tab === "updates" ? t("empty_updates") : t("empty_all");
  return (
    <div className="notification-empty">
      <Inbox size={20} />
      {text}
    </div>
  );
}

function priorityClass(priority: string): string {
  if (priority === "p1") return "urgent";
  if (priority === "p2") return "normal";
  return "quiet";
}

