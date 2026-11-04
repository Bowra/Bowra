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
import { HttpStatusError } from "@/lib/api-client";
import { normalizeAppActionUrl } from "@/lib/legacy-app-links";
import { localizeNotification, notificationTypeLabel } from "@/lib/notification-copy";
import { useAppRouter } from "@/lib/use-app-router";

const TABS: { id: NotificationTab; labelKey: string }[] = [
  { id: "action", labelKey: "tab_action" },
  { id: "updates", labelKey: "tab_updates" },
  { id: "all", labelKey: "tab_all" },
];

function hasStatus(error: unknown, status: number): boolean {
  return error instanceof HttpStatusError && error.status === status;
}

export function NotificationCenter() {
  const t = useTranslations("notifications");
  const router = useAppRouter();
  const [disabled, setDisabled] = useState(
    process.env.NEXT_PUBLIC_BOWHEAD_NOTIFICATION_CENTER === "0",
  );
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<NotificationTab>("action");
  const [counts, setCounts] = useState<NotificationCountDTO | null>(null);
  const [items, setItems] = useState<NotificationItemDTO[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<ShownError | null>(null);
  const [countErr, setCountErr] = useState<ShownError | null>(null);
  const [readingAll, setReadingAll] = useState(false);
  const mounted = useRef(true);
  const view = useRef({ open: false, tab: "action" as NotificationTab });
  const listGeneration = useRef(0);
  const countGeneration = useRef(0);
  const pageInFlight = useRef<string | null>(null);
  const readAllInFlight = useRef(false);
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

  const close = useCallback(() => {
    view.current.open = false;
    listGeneration.current += 1;
    pageInFlight.current = null;
    setOpen(false);
  }, []);

  const disableCenter = useCallback(() => {
    close();
    countGeneration.current += 1;
    setDisabled(true);
  }, [close]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      listGeneration.current += 1;
      countGeneration.current += 1;
    };
  }, []);

  const refreshCount = useCallback(async () => {
    if (disabled) return;
    const generation = ++countGeneration.current;
    const current = () => mounted.current && generation === countGeneration.current;
    try {
      const body = await fetchNotificationCount();
      if (!current()) return;
      setCounts(body);
      setCountErr(null);
    } catch (e: unknown) {
      if (!current()) return;
      if (hasStatus(e, 404)) disableCenter();
      else setCountErr(shownError(t("count_failed_notice"), e));
    }
  }, [disabled, disableCenter, t]);

  const load = useCallback(async (activeTab: NotificationTab, cursor?: string | null, preserveError = false) => {
    if (disabled || !view.current.open || view.current.tab !== activeTab) return;
    if (cursor && pageInFlight.current !== null) return;
    const generation = cursor ? listGeneration.current : ++listGeneration.current;
    const pageKey = `${generation}:${cursor ?? ""}`;
    pageInFlight.current = cursor ? pageKey : null;
    const current = () => mounted.current && view.current.open && view.current.tab === activeTab && generation === listGeneration.current;
    setLoading(true);
    if (!preserveError) setErr(null);
    try {
      const body = await fetchNotifications(activeTab, cursor);
      if (!current()) return;
      setItems((previous) => cursor ? Array.from(new Map([...previous, ...body.items].map((item) => [item.id, item])).values()) : body.items);
      setNextCursor(body.nextCursor);
    } catch (e: unknown) {
      if (!current()) return;
      if (hasStatus(e, 404)) disableCenter();
      else setErr(shownError(t("load_failed_notice"), e));
    } finally {
      if (current()) setLoading(false);
      if (pageInFlight.current === pageKey) pageInFlight.current = null;
    }
  }, [disabled, disableCenter, t]);

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
      if (document.querySelector('[data-notification-center="true"]')?.contains(target)) return;
      close();
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") { close(); triggerRef.current?.focus(); }
    };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open, close]);

  const handleOpen = () => {
    if (view.current.open) close();
    else { view.current.open = true; setOpen(true); }
  };

  const changeTab = (next: NotificationTab) => {
    if (view.current.tab === next) return;
    view.current.tab = next;
    listGeneration.current += 1;
    pageInFlight.current = null;
    setItems([]);
    setNextCursor(null);
    setErr(null);
    setLoading(true);
    setTab(next);
  };

  const markBusy = (id: string) => {
    inFlightRef.current.add(id);
    setBusyIds((s) => new Set(s).add(id));
  };
  const clearBusy = (id: string) => {
    inFlightRef.current.delete(id);
    if (!mounted.current) return;
    setBusyIds((s) => {
      const n = new Set(s);
      n.delete(id);
      return n;
    });
  };

  const ownsView = (generation: number) => mounted.current && view.current.open && listGeneration.current === generation;

  const handleAction = async (item: NotificationItemDTO) => {
    if (inFlightRef.current.has(item.id)) return;
    const generation = listGeneration.current;
    markBusy(item.id);
    try {
      if (!item.readAt) await markNotificationRead(item.id);
      void refreshCount();
      if (!ownsView(generation)) {
        if (mounted.current && view.current.open) void load(view.current.tab);
        return;
      }
      if (item.actionUrl) {
        close();
        router.push(normalizeAppActionUrl(item.actionUrl));
      } else {
        void load(view.current.tab);
      }
    } catch (e: unknown) {
      if (!ownsView(generation)) return;
      setErr(shownError(t(hasStatus(e, 404) ? "item_unavailable_notice" : "mark_read_failed_notice"), e));
      if (hasStatus(e, 404)) {
        setItems((current) => current.filter((x) => x.id !== item.id));
        void refreshCount();
        void load(view.current.tab, undefined, true);
      }
    } finally {
      clearBusy(item.id);
    }
  };

  const handleArchive = async (id: string) => {
    if (inFlightRef.current.has(id)) return;
    const generation = listGeneration.current;
    markBusy(id);
    try {
      await archiveNotification(id);
      void refreshCount();
      if (ownsView(generation)) setItems((current) => current.filter((x) => x.id !== id));
      if (mounted.current && view.current.open) void load(view.current.tab);
    } catch (e: unknown) {
      if (!ownsView(generation)) return;
      setErr(shownError(t(hasStatus(e, 404) ? "item_unavailable_notice" : "archive_failed_notice"), e));
      if (hasStatus(e, 404)) {
        setItems((current) => current.filter((x) => x.id !== id));
        void refreshCount();
        void load(view.current.tab, undefined, true);
      }
    } finally {
      clearBusy(id);
    }
  };

  const handleReadAll = async () => {
    if (readAllInFlight.current) return;
    readAllInFlight.current = true;
    setReadingAll(true);
    const generation = listGeneration.current;
    const activeTab = view.current.tab;
    const ids = new Set(items.map((item) => item.id));
    try {
      await markAllNotificationsRead(activeTab);
      void refreshCount();
      if (ownsView(generation)) setItems((current) => current.map((x) => ids.has(x.id) ? { ...x, readAt: x.readAt ?? new Date().toISOString() } : x));
      if (mounted.current && view.current.open) void load(view.current.tab);
    } catch (e: unknown) {
      if (ownsView(generation)) setErr(shownError(t("read_all_failed_notice"), e));
    } finally {
      readAllInFlight.current = false;
      if (mounted.current) setReadingAll(false);
    }
  };

  const actionCount = counts?.actionRequired ?? 0;
  const pendingUpdates = counts?.updatesUnread ?? 0;
  const bellLabel = countErr && !counts ? t("count_unavailable") :
    actionCount > 0 && pendingUpdates > 0
      ? t("bell_aria_pending_updates", { action: actionCount, updates: pendingUpdates })
      : actionCount > 0
        ? t("bell_aria", { badge: actionCount })
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
        {actionCount > 0 ? (
          <span className="notification-bell__badge">
            {actionCount > 99 ? "99+" : actionCount}
          </span>
        ) : pendingUpdates > 0 ? (
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
                  {counts ? t("panel_meta", { action: actionCount, updates: pendingUpdates }) : t(countErr ? "count_unavailable" : "loading")}
                </div>
              </div>
              {tab !== "action" && items.some((x) => !x.readAt) ? (
                <button type="button" className="notification-ghost-btn" disabled={readingAll} onClick={() => void handleReadAll()}>
                  <Check size={12} />
                  {t("mark_all_read")}
                </button>
              ) : null}
            </div>

            <Tabs value={tab} onValueChange={changeTab}>
              <TabList appearance="segmented" className="notification-tabs" aria-label={t("tabs_aria")}>
                {TABS.map((it) => (
                  <Tab key={it.id} value={it.id} className={clsx("notification-tab", { active: tab === it.id })}>
                    {t(it.labelKey)}
                    {it.id === "action" && actionCount > 0 ? (
                      <span>{actionCount}</span>
                    ) : null}
                    {it.id === "updates" && pendingUpdates > 0 ? (
                      <span>{pendingUpdates}</span>
                    ) : null}
                  </Tab>
                ))}
              </TabList>
            </Tabs>

            <div className="notification-list">
              {countErr ? (
                <div className="notification-empty danger" role="status">
                  {countErr.message}
                  <ErrorDiagnosticsInline error={countErr.error} surface="global" />
                  <button type="button" className="notification-ghost-btn" onClick={() => void refreshCount()}>{t("retry")}</button>
                </div>
              ) : null}
              {err ? (
                <div className="notification-empty danger">
                  {err.message}
                  <ErrorDiagnosticsInline error={err.error} surface="global" />
                  <button type="button" className="notification-ghost-btn" onClick={() => void load(tab)}>{t("retry")}</button>
                </div>
              ) : null}
              {!err && loading && items.length === 0 ? <div className="notification-empty">{t("loading")}</div> : null}
              {!err && !loading && items.length === 0 ? <NotificationEmpty tab={tab} /> : null}
              {items.map((item) => (
                  <NotificationRow
                    key={item.id}
                    item={item}
                    busy={busyIds.has(item.id)}
                    onOpen={() => void handleAction(item)}
                    onArchive={() => void handleArchive(item.id)}
                  />
                ))}
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
  const actionRequired = item.actionRequired;
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

