"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type MouseEvent,
  type ReactNode,
  type RefObject,
} from "react";
import clsx from "clsx";
import { useLocale, useTimeZone, useTranslations } from "next-intl";

import type { SessionGroup, SessionItem } from "@/lib/types";
import {
  ArchiveBox,
  Edit,
  Trash,
  Chat,
  Database,
  Doc,
  FileText,
  History,
  Pin,
  Search,
} from "@/components/icons";
import {
  fetchSessions,
  SessionArchiveError,
} from "@/lib/api";
import { adaptSessionGroups } from "@/lib/session-groups";
import { formatCalendarDay, formatListDate, type ListDate } from "@/lib/locale";
import { contentExcerpt, splitMatches } from "@/lib/search-match";
import { notifySessionListChanged } from "@/lib/session-list-events";
import { toast } from "@/lib/use-toast";
import { trackInteraction as track } from "@/lib/metrics";
import {
  searchResultCount as countSearchResults,
  unifiedWorkResultAppHref,
  type UnifiedWorkKind,
  type UnifiedWorkSearchGroup,
  type UnifiedWorkSearchItem,
  useUnifiedWorkSearch,
} from "@/lib/unified-work-search";
import {
  renameInitialTitle,
  sessionRenameErrorKey,
  submitSessionRename,
  reconcileSessionTitles,
  useSessionTitleOverrides,
} from "@/lib/session-rename";
import { SessionActionMenu, type SessionMenuAction } from "./session-action-menu";
import { SessionRenameDialog } from "./session-rename-dialog";
import {
  RecoveryGroups,
  type RecoveryFocusRequest,
  type RecoveryRenderGroup,
} from "./recovery-groups";
import { CharIcon } from "@/components/char-icon";
import { LucideGlyph } from "@/components/icon-refresh";
import { ChevronDown } from "lucide-react";
import { OlderSessionsRow, type OlderSessionsPaging } from "./older-sessions-row";

export type SessionMutationResult = "success" | "cancelled";

interface WorkRecoveryProps {
  groups: SessionGroup[];
  currentSessionId: string | null;
  workspaceId?: string;
  query: string;
  onQueryChange: (query: string) => void;
  onPick: (sessionId: string) => void;
  onTogglePin?: (
    sessionId: string,
    currentlyPinned: boolean,
  ) => void | Promise<void>;
  archiveEnabled?: boolean;
  archivedCount?: number;
  onArchive?: (
    sessionId: string,
  ) => SessionMutationResult | Promise<SessionMutationResult>;
  onRestore?: (sessionId: string) => void | Promise<void>;
  /** Soft-delete a session (feedback-triage-2026-08-24: customer asks “对话要可以删掉”).
   *  Same shape as archive: confirmation can return "cancelled"; backend 409 for an active turn. */
  onDelete?: (
    sessionId: string,
  ) => SessionMutationResult | Promise<SessionMutationResult>;
  /** Undo previous soft deletion (success Toast Undo action). */
  onUndelete?: (sessionId: string) => void | Promise<void>;
  /** Persist an owner-chosen title. The row shows the new title at once and rolls back on failure. */
  onRename?: (sessionId: string, title: string) => Promise<unknown>;
  onNavigateSearchResult?: (
    event: MouseEvent<HTMLAnchorElement>,
    result: UnifiedWorkSearchItem,
  ) => void;
  searchInputRef?: RefObject<HTMLInputElement | null>;
  surface?: "expanded" | "overlay" | "flyout" | "drawer";
  showSearch?: boolean;
  navigationSlot?: ReactNode;
  listStatusSlot?: ReactNode;
  /** Older-session paging (spec 4a): renders the "Show older" row at the end of the list when present. */
  olderSessions?: OlderSessionsPaging;
}

export function WorkRecovery({
  groups,
  currentSessionId,
  workspaceId,
  query,
  onQueryChange,
  onPick,
  onTogglePin,
  archiveEnabled = false,
  archivedCount = 0,
  onArchive,
  onRestore,
  onDelete,
  onUndelete,
  onRename,
  onNavigateSearchResult,
  searchInputRef,
  surface = "expanded",
  showSearch = true,
  navigationSlot,
  listStatusSlot,
  olderSessions,
}: WorkRecoveryProps) {
  const t = useTranslations("session");
  const lifecycleErrorText = useSessionLifecycleErrorText();
  const locale = useLocale();
  // Behavior batch A: disabled items keep their label with the reason on a second line.
  // Session deletion needs no confirmation or “…”; allow 8-second undo (2026-08-24 triage, 2026-09-27 user confirmation: undoable deletes need no confirmation).
  const [mode, setMode] = useState<"active" | "archived">("active");
  const search = useUnifiedWorkSearch(query, mode === "active");
  const [pendingPinIds, setPendingPinIds] = useState<Set<string>>(
    () => new Set(),
  );
  const pendingPinIdsRef = useRef<Set<string>>(new Set());
  const [pinError, setPinError] = useState<string | null>(null);
  const [pendingLifecycleIds, setPendingLifecycleIds] = useState<
    Set<string>
  >(() => new Set());
  const pendingLifecycleIdsRef = useRef<Set<string>>(new Set());
  const [renameTarget, setRenameTarget] = useState<SessionItem | null>(null);
  const titleOverrides = useSessionTitleOverrides();
  const listRef = useRef<HTMLDivElement | null>(null);
  // Pending focus moves (spec 4a/4b): set on click, resolved when the request settles.
  const [olderFocusRequest, setOlderFocusRequest] = useState<RecoveryFocusRequest | null>(null);
  const olderPendingRef = useRef<{ pagesBefore: number } | null>(null);
  const searchMorePendingRef = useRef(false);
  const [announcement, setAnnouncement] = useState("");
  const [hiddenActiveIds, setHiddenActiveIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [hiddenArchivedIds, setHiddenArchivedIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [archivedGroups, setArchivedGroups] = useState<SessionGroup[]>([]);
  const [archivedStatus, setArchivedStatus] = useState<
    "idle" | "loading" | "ready" | "error"
  >("idle");
  const [archivedRetryKey, setArchivedRetryKey] = useState(0);
  const [archiveCountDelta, setArchiveCountDelta] = useState(0);
  const previousArchivedCountRef = useRef(archivedCount);
  const filter = query.trim().toLocaleLowerCase();
  const globalSearchActive = mode === "active" && filter.length > 0;
  const loadedGroups =
    mode === "archived" ? archivedGroups : groups;
  // A settled rename leaves the optimistic store once the loaded list reports the same title.
  useEffect(() => {
    reconcileSessionTitles(loadedGroups.flatMap((group) => group.items));
  }, [loadedGroups]);
  // A rename shows at once on every surface; the loaded list catches up on refresh.
  const displayGroups = titleOverrides.size
    ? loadedGroups.map((group) => ({
        ...group,
        items: group.items.map((item) => {
          const title = titleOverrides.get(item.id);
          return title === undefined || title === item.title
            ? item
            : { ...item, title };
        }),
      }))
    : loadedGroups;
  const allItems = displayGroups
    .flatMap((group) => group.items)
    .filter((item) =>
      mode === "archived"
        ? !hiddenArchivedIds.has(item.id)
        : !hiddenActiveIds.has(item.id),
    );
  const visibleItems = allItems.filter((item) => matchesFilter(item, filter));
  const pinned =
    mode === "active"
      ? visibleItems.filter((item) => item.pinned)
      : [];
  const total = allItems.length;
  const visibleTotal = visibleItems.length;
  const displayedArchivedCount = Math.max(
    0,
    archivedCount + archiveCountDelta,
  );
  const searchResponse =
    search.status === "ready" ? search.response : null;
  const searchResultCount =
    searchResponse?.groups.reduce(
      (count, group) => count + group.items.length,
      0,
    ) ?? 0;
  // The announced count agrees with the group headings (their totals, the session one growing with "Show more").
  const searchFound = searchResponse
    ? countSearchResults(searchResponse.groups, search.moreSessions.total)
    : { count: 0, atLeast: false };

  useEffect(() => {
    if (globalSearchActive) setPinError(null);
  }, [globalSearchActive]);

  useEffect(() => {
    if (previousArchivedCountRef.current === archivedCount) return;
    previousArchivedCountRef.current = archivedCount;
    setArchiveCountDelta(0);
  }, [archivedCount]);

  useEffect(() => {
    const presentIds = new Set(
      groups.flatMap((group) => group.items.map((item) => item.id)),
    );
    setHiddenActiveIds((current) => {
      const next = new Set(
        [...current].filter((id) => presentIds.has(id)),
      );
      return next.size === current.size ? current : next;
    });
  }, [groups]);

  useEffect(() => {
    if (mode !== "archived" || !archiveEnabled) return;
    const controller = new AbortController();
    const timer = window.setTimeout(
      () => {
        setArchivedStatus("loading");
        void fetchSessions({
          view: "archived",
          query,
          limit: 100,
          workspaceId,
          signal: controller.signal,
        })
          .then((response) => {
            if (controller.signal.aborted) return;
            setArchivedGroups(adaptSessionGroups(response.groups));
            setArchivedStatus("ready");
          })
          .catch((error: unknown) => {
            if (
              controller.signal.aborted ||
              (error instanceof DOMException &&
                error.name === "AbortError")
            ) {
              return;
            }
            setArchivedStatus("error");
          });
      },
      query.trim() ? 250 : 0,
    );
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [archiveEnabled, archivedRetryKey, mode, query, workspaceId]);

  useEffect(() => {
    if (!globalSearchActive || search.status === "error") {
      if (globalSearchActive && search.status === "error") {
        track("bowhead_sidebar_search_total", {
          action: "error",
          kind: "all",
          surface,
        });
      }
      return;
    }
    if (!searchResponse) return;
    track("bowhead_sidebar_search_total", {
      action: searchResponse.partial
        ? "partial"
        : searchResultCount > 0
          ? "results"
          : "no_results",
      kind: "all",
      surface,
    });
  }, [
    globalSearchActive,
    search.status,
    searchResponse,
    searchResultCount,
    surface,
  ]);

  const togglePin = async (item: SessionItem) => {
    if (!onTogglePin || pendingPinIdsRef.current.has(item.id)) return;
    pendingPinIdsRef.current.add(item.id);
    setPinError(null);
    setPendingPinIds((current) => new Set(current).add(item.id));
    try {
      await onTogglePin(item.id, Boolean(item.pinned));
      track("bowhead_sidebar_session_action_total", {
        action: item.pinned ? "unpin" : "pin",
        outcome: "success",
        surface,
      });
    } catch {
      track("bowhead_sidebar_session_action_total", {
        action: item.pinned ? "unpin" : "pin",
        outcome: "error",
        surface,
      });
      setPinError(t("rail_pin_failed"));
    } finally {
      pendingPinIdsRef.current.delete(item.id);
      setPendingPinIds((current) => {
        const next = new Set(current);
        next.delete(item.id);
        return next;
      });
    }
  };

  const archiveItem = async (item: SessionItem) => {
    if (
      !onArchive ||
      item.state === "active" ||
      pendingLifecycleIdsRef.current.has(item.id)
    ) {
      return;
    }
    markLifecyclePending(item.id, true);
    try {
      const result = await onArchive(item.id);
      if (result === "cancelled") {
        track("bowhead_sidebar_session_action_total", {
          action: "archive",
          outcome: "blocked",
          surface,
        });
        return;
      }
      setHiddenActiveIds((current) => new Set(current).add(item.id));
      setArchiveCountDelta((count) => count + 1);
      track("bowhead_sidebar_session_action_total", {
        action: "archive",
        outcome: "success",
        surface,
      });
      toast({
        kind: "success",
        undo: true,
        text: t("rail_archive_done", {
          title: truncateSessionTitle(item.title),
        }),
        durationMs: 8_000,
        action: onRestore
          ? {
              label: t("rail_archive_undo"),
              onAction: async () => {
                try {
                  await onRestore(item.id);
                  setHiddenActiveIds((current) => {
                    const next = new Set(current);
                    next.delete(item.id);
                    return next;
                  });
                  setArchiveCountDelta((count) => count - 1);
                  track("bowhead_sidebar_session_action_total", {
                    action: "undo",
                    outcome: "success",
                    surface,
                  });
                } catch (error) {
                  notifySessionListChanged();
                  track("bowhead_sidebar_session_action_total", {
                    action: "undo",
                    outcome: "error",
                    surface,
                  });
                  toast({
                    kind: "error",
                    text: t("rail_restore_failed"),
                  });
                  throw error;
                }
              },
            }
          : undefined,
      });
    } catch (error) {
      notifySessionListChanged();
      const isRunning =
        error instanceof SessionArchiveError &&
        error.code === "session_archive_active_turn";
      track("bowhead_sidebar_session_action_total", {
        action: "archive",
        outcome: isRunning ? "blocked" : "error",
        surface,
      });
      toast({
        kind: "error",
        text: t(
          isRunning
            ? "rail_archive_running_error"
            : "rail_archive_failed",
        ),
      });
    } finally {
      markLifecyclePending(item.id, false);
    }
  };

  /**
   * Soft-delete a session (feedback-triage-2026-08-24).
   *
   * Same structure as archiveItem: optimistic hiding + success Toast with Undo. Two differences:
   * ① Leave archive count unchanged (deleted sessions never enter archive view);
   * ② Backend 409 code is session_delete_active_turn: running sessions cannot be deleted,
   *    or that turn keeps consuming credits with nowhere to view it.
   */
  const deleteItem = async (item: SessionItem) => {
    if (
      !onDelete ||
      item.state === "active" ||
      pendingLifecycleIdsRef.current.has(item.id)
    ) {
      return;
    }
    markLifecyclePending(item.id, true);
    try {
      const result = await onDelete(item.id);
      if (result === "cancelled") {
        track("bowhead_sidebar_session_action_total", {
          action: "delete",
          outcome: "blocked",
          surface,
        });
        return;
      }
      setHiddenActiveIds((current) => new Set(current).add(item.id));
      track("bowhead_sidebar_session_action_total", {
        action: "delete",
        outcome: "success",
        surface,
      });
      toast({
        kind: "success",
        undo: true,
        text: t("rail_delete_done", {
          title: truncateSessionTitle(item.title),
        }),
        durationMs: 8_000,
        action: onUndelete
          ? {
              label: t("rail_delete_undo"),
              onAction: async () => {
                try {
                  await onUndelete(item.id);
                  setHiddenActiveIds((current) => {
                    const next = new Set(current);
                    next.delete(item.id);
                    return next;
                  });
                  track("bowhead_sidebar_session_action_total", {
                    action: "undo",
                    outcome: "success",
                    surface,
                  });
                } catch (error) {
                  notifySessionListChanged();
                  track("bowhead_sidebar_session_action_total", {
                    action: "undo",
                    outcome: "error",
                    surface,
                  });
                  toast({ kind: "error", text: lifecycleErrorText(error, t("rail_delete_undo_failed")) });
                  throw error;
                }
              },
            }
          : undefined,
      });
    } catch (error) {
      notifySessionListChanged();
      const isRunning =
        error instanceof SessionArchiveError &&
        error.code === "session_delete_active_turn";
      track("bowhead_sidebar_session_action_total", {
        action: "delete",
        outcome: isRunning ? "blocked" : "error",
        surface,
      });
      toast({
        kind: "error",
        text: lifecycleErrorText(error, t(isRunning ? "rail_delete_running_error" : "rail_delete_failed")),
      });
    } finally {
      markLifecyclePending(item.id, false);
    }
  };

  const renameItem = async (item: SessionItem, title: string) => {
    setRenameTarget(null);
    if (!onRename || title === item.title) return;
    try {
      await submitSessionRename(item.id, title, onRename);
      track("bowhead_sidebar_session_action_total", {
        action: "rename",
        outcome: "success",
        surface,
      });
    } catch (error) {
      track("bowhead_sidebar_session_action_total", {
        action: "rename",
        outcome: "error",
        surface,
      });
      toast({ kind: "error", text: t(sessionRenameErrorKey(error)) });
    }
  };

  const restoreItem = async (item: SessionItem) => {
    if (
      !onRestore ||
      pendingLifecycleIdsRef.current.has(item.id)
    ) {
      return;
    }
    markLifecyclePending(item.id, true);
    try {
      await onRestore(item.id);
      setHiddenArchivedIds((current) => new Set(current).add(item.id));
      setArchiveCountDelta((count) => count - 1);
      track("bowhead_sidebar_session_action_total", {
        action: "restore",
        outcome: "success",
        surface,
      });
      toast({ kind: "success", text: t("rail_restore_done") });
    } catch {
      notifySessionListChanged();
      track("bowhead_sidebar_session_action_total", {
        action: "restore",
        outcome: "error",
        surface,
      });
      toast({ kind: "error", text: t("rail_restore_failed") });
    } finally {
      markLifecyclePending(item.id, false);
    }
  };

  const markLifecyclePending = (id: string, pending: boolean) => {
    if (pending) pendingLifecycleIdsRef.current.add(id);
    else pendingLifecycleIdsRef.current.delete(id);
    setPendingLifecycleIds((current) => {
      const next = new Set(current);
      if (pending) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const enterArchivedMode = () => {
    setMode("archived");
    setHiddenArchivedIds(new Set());
    setArchivedStatus("idle");
    setPinError(null);
    onQueryChange("");
  };

  const leaveArchivedMode = () => {
    setMode("active");
    setPinError(null);
    onQueryChange("");
  };

  const isItemUnread = (item: SessionItem) =>
    Boolean(item.unread && currentSessionId !== item.id);

  // feedback-triage-2026-08-24: menu was gated entirely on archiveEnabled; disabling archive
  // also hid deletion. Render when actions exist; otherwise omit “⋯” to avoid an empty trigger.
  // “⋯” is the row's only action entry, so Pin alone is enough to render it.
  const sessionMenuActions = (item: SessionItem): SessionMenuAction[] =>
    archiveEnabled || onDelete || onTogglePin || onRename
      ? mode === "archived"
        ? onRestore
          ? [
              {
                id: "restore",
                label: t("rail_restore_title"),
                icon: <History aria-hidden />,
                disabled: pendingLifecycleIds.has(item.id),
                title: pendingLifecycleIds.has(item.id)
                  ? t("rail_action_pending_reason")
                  : undefined,
                onSelect: () => void restoreItem(item),
              },
            ]
          : []
        : [
            // Menu contract: Rename sits at the top, destructive Delete stays last.
            ...(onRename
              ? [
                  {
                    id: "rename",
                    label: t("rail_rename_title"),
                    icon: <Edit aria-hidden />,
                    opensDialog: true,
                    onSelect: () => setRenameTarget(item),
                  },
                ]
              : []),
            ...(onTogglePin
              ? [
                  {
                    id: "pin",
                    label: item.pinned
                      ? t("rail_unpin_title")
                      : t("rail_pin_title"),
                    icon: (
                      <Pin
                        aria-hidden
                        fill={
                          item.pinned ? "currentColor" : "none"
                        }
                      />
                    ),
                    disabled: pendingPinIds.has(item.id),
                    title: pendingPinIds.has(item.id)
                      ? t("rail_action_pending_reason")
                      : undefined,
                    onSelect: () => void togglePin(item),
                  },
                ]
              : []),
            ...(onArchive
              ? [
                  {
                    id: "archive",
                    label: t("rail_archive_title"),
                    icon: <ArchiveBox aria-hidden />,
                    disabled:
                      item.state === "active" ||
                      pendingLifecycleIds.has(item.id),
                    title:
                      item.state === "active"
                        ? t("rail_archive_running_error")
                        : pendingLifecycleIds.has(item.id)
                          ? t("rail_action_pending_reason")
                          : undefined,
                    onSelect: () => void archiveItem(item),
                  },
                ]
              : []),
            // feedback-triage-2026-08-24: customer asks “对话要可以删掉”.
            // Place after archive: destructive actions conventionally come last in menus.
            ...(onDelete
              ? [
                  {
                    id: "delete",
                    label: t("rail_delete_title"),
                    icon: <Trash aria-hidden />,
                    danger: true,
                    disabled:
                      item.state === "active" ||
                      pendingLifecycleIds.has(item.id),
                    title:
                      item.state === "active"
                        ? t("rail_delete_running_error")
                        : pendingLifecycleIds.has(item.id)
                          ? t("rail_action_pending_reason")
                          : undefined,
                    onSelect: () => void deleteItem(item),
                  },
                ]
              : []),
          ]
      : [];

  const renderItem = (
    item: SessionItem,
    {
      onActionMenuOpenChange,
    }: {
      onActionMenuOpenChange?: (open: boolean) => void;
    } = {},
  ) => (
    <div
      className={clsx("unified-work-item", {
        "is-active": currentSessionId === item.id,
        "is-unread": isItemUnread(item),
        "is-archived": item.archived,
        // No inline buttons; “⋯” (has-more) is the only row action.
        "has-more": sessionMenuActions(item).length > 0,
      })}
      key={item.id}
      data-session-id={item.id}
    >
      <button
        type="button"
        className="unified-work-item-main"
        onClick={() => {
          track("bowhead_sidebar_recent_opened_total", {
            source: filter ? "search" : item.pinned ? "pinned" : "recent",
            surface,
          });
          onPick(item.id);
        }}
        aria-current={currentSessionId === item.id ? "page" : undefined}
        title={item.title}
      >
        {/* Unread is the bold title alone; the state is spoken through hidden text. */}
        <span className="unified-work-copy">
          <span className="unified-work-title">{item.title}</span>
          {isItemUnread(item) ? (
            <span className="unified-work-sr-only">
              {t("rail_unread_aria")}
            </span>
          ) : null}
          {item.archived ? (
            <span className="unified-work-sr-only">
              {t("rail_state_archived")}
            </span>
          ) : null}
          {item.state !== "done" || item.skillApplied || item.time || item.updatedAt ? (
            <span className="unified-work-meta">
              {item.state !== "done" || item.skillApplied ? (
                <span className="unified-work-meta-signals">
                  {item.state !== "done" ? (
                    <span
                      className={clsx(
                        "unified-work-lifecycle-state",
                        item.state,
                      )}
                    >
                      {t(`rail_state_${item.state}`)}
                    </span>
                  ) : null}
                  {item.skillApplied ? (
                    <>
                      <span aria-hidden="true"><CharIcon ch="✦" aria-hidden /></span>
                      <span className="unified-work-sr-only">
                        {item.skillApplied}
                      </span>
                    </>
                  ) : null}
                </span>
              ) : null}
              <SessionRowTime item={item} />
            </span>
          ) : null}
        </span>
      </button>
      {sessionMenuActions(item).length ? (
        <SessionActionMenu
          label={t("rail_more_actions_for", {
            title: truncateSessionTitle(item.title),
          })}
          onOpenChange={onActionMenuOpenChange}
          actions={sessionMenuActions(item)}
        />
      ) : null}
    </div>
  );

  const searchQuery = query.trim();
  const renderMatches = (text: string) =>
    splitMatches(text, searchQuery).map((part, index) =>
      part.match ? <mark key={index}>{part.text}</mark> : part.text,
    );
  const renderSearchResult = (item: UnifiedWorkSearchItem) => {
    const Icon = SEARCH_RESULT_ICONS[item.kind];
    const contentMatch =
      item.kind === "session" && item.match === "content" && item.snippet;

    return (
      <a
        key={`${item.kind}:${item.id}`}
        data-result-id={`${item.kind}:${item.id}`}
        className="unified-search-result"
        href={unifiedWorkResultAppHref(item)}
        onClick={(event) => {
          track("bowhead_sidebar_search_total", {
            action: "open",
            kind: item.kind,
            surface,
          });
          onNavigateSearchResult?.(event, item);
        }}
        title={item.title}
      >
        <Icon aria-hidden />
        <span className="unified-search-result-copy">
          <span className="unified-search-result-title">
            {renderMatches(item.title)}
          </span>
          {contentMatch ? (
            // The hit is in the user's own message, not the title: say so and quote around the term.
            <span className="unified-search-result-snippet is-content">
              <span className="unified-search-source">
                {t("rail_search_match_content")}
              </span>
              {renderMatches(contentExcerpt(item.snippet ?? "", searchQuery))}
            </span>
          ) : item.snippet ? (
            <span className="unified-search-result-snippet">
              {item.snippet}
            </span>
          ) : item.updated_at ? (
            <SearchResultDate iso={item.updated_at} />
          ) : null}
        </span>
      </a>
    );
  };

  const recoveryGroups: RecoveryRenderGroup[] = [];
  if (mode === "active" && !globalSearchActive && pinned.length) {
    recoveryGroups.push({
      id: "pinned",
      label: t("rail_pinned_label"),
      items: pinned,
    });
  }
  if (
    !globalSearchActive &&
    (mode === "active" || archivedStatus === "ready")
  ) {
    for (const group of displayGroups) {
      const items = group.items.filter((item) => {
        if (
          mode === "active" &&
          (item.pinned || hiddenActiveIds.has(item.id))
        ) {
          return false;
        }
        if (
          mode === "archived" &&
          hiddenArchivedIds.has(item.id)
        ) {
          return false;
        }
        return matchesFilter(item, filter);
      });
      if (!items.length) continue;
      recoveryGroups.push({
        id: group.id,
        label: localizedGroupLabel(t, group.id, group.label),
        items,
      });
    }
  }

  const olderRowVisible =
    Boolean(olderSessions) &&
    mode === "active" &&
    !globalSearchActive &&
    !listStatusSlot &&
    total > 0;
  const focusInList = (selector: string, matches?: (element: HTMLElement) => boolean) => {
    const candidates = Array.from(
      listRef.current?.querySelectorAll<HTMLElement>(selector) ?? [],
    );
    const target = matches ? candidates.find(matches) : candidates.at(-1);
    target?.focus({ preventScroll: true });
    return Boolean(target);
  };
  const loadOlder = () => {
    if (!olderSessions || olderSessions.loadingMore) return;
    olderPendingRef.current = { pagesBefore: olderSessions.olderPagesLoaded };
    void olderSessions.onLoadMore();
  };
  const olderLoading = olderSessions?.loadingMore ?? false;
  const olderError = olderSessions?.loadMoreError ?? false;
  const olderPages = olderSessions?.olderPagesLoaded ?? 0;
  const olderPageIds = olderSessions?.latestOlderPageIds;
  // Spec 4a: when the batch lands, focus its first session (a poll can add rows at the top, so this follows the
  // page's own ids, not "whatever is new"); when it fails, focus Retry in the error row that replaced the button.
  useEffect(() => {
    const pending = olderPendingRef.current;
    if (!pending || olderLoading) return;
    if (olderError) {
      olderPendingRef.current = null;
      focusInList(".older-sessions-error button");
      return;
    }
    if (olderPages <= pending.pagesBefore) return;
    olderPendingRef.current = null;
    const ids = olderPageIds ?? [];
    setAnnouncement(t("rail_older_loaded_announce", { count: ids.length }));
    const firstId = ids[0];
    if (!firstId) return;
    // A long list is virtualized: the new row may not be in the DOM yet. The list scrolls it into its window and
    // focuses it (116 dogfood 1.5.25: focus fell to <body>).
    setOlderFocusRequest((current) => ({ sessionId: firstId, token: (current?.token ?? 0) + 1 }));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- runs when the request state changes
  }, [olderLoading, olderError, olderPages, olderPageIds]);

  const sessionGroupMore = search.moreSessions;
  const clearOlderFocusRequest = useCallback(() => setOlderFocusRequest(null), []);
  // A search or a mode switch replaces the list: a pending "focus the new page" request no longer applies.
  useEffect(() => {
    setOlderFocusRequest(null);
    olderPendingRef.current = null;
  }, [globalSearchActive, mode, query]);
  const showMoreSessions = () => {
    // aria-disabled while loading: the button keeps focus, repeated presses are ignored.
    if (sessionGroupMore.loading) return;
    searchMorePendingRef.current = true;
    void search.loadMoreSessions();
  };
  const firstSearchSessionIds =
    searchResponse?.groups.find((group) => group.kind === "session")?.items.map((item) => item.id) ?? [];
  // Spec 4b: a landed page moves focus to its first result; a failure moves it to Retry; when the button is gone
  // with nothing new to show, focus the last result instead of dropping it on the page.
  useEffect(() => {
    if (!searchMorePendingRef.current || sessionGroupMore.loading) return;
    searchMorePendingRef.current = false;
    if (sessionGroupMore.error) {
      focusInList(".unified-search-more-error button");
      return;
    }
    const shown = new Set(firstSearchSessionIds);
    const appended = sessionGroupMore.lastPageIds.filter((id) => !shown.has(id));
    const total = sessionGroupMore.total;
    if (total !== null) {
      setAnnouncement(
        t("rail_search_more_announce", {
          count: appended.length,
          shown: firstSearchSessionIds.length + sessionGroupMore.items.filter((item) => !shown.has(item.id)).length,
          total,
        }),
      );
    }
    const firstId = appended[0];
    if (
      !firstId ||
      !focusInList("[data-result-id]", (element) => element.dataset.resultId === `session:${firstId}`)
    ) {
      focusInList('[data-result-id^="session:"]');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- runs when the page request settles
  }, [sessionGroupMore]);
  const searchGroupItems = (group: UnifiedWorkSearchGroup) => {
    if (group.kind !== "session" || !sessionGroupMore.items.length) {
      return group.items;
    }
    const seen = new Set(group.items.map((item) => item.id));
    return [
      ...group.items,
      ...sessionGroupMore.items.filter((item) => !seen.has(item.id)),
    ];
  };

  return (
    <section
      className="unified-work-recovery"
      aria-label={t(
        mode === "archived"
          ? "rail_archived_heading"
          : "rail_search_and_recent",
      )}
    >
      {mode === "archived" ? (
        <div className="unified-work-archive-head">
          <button type="button" onClick={leaveArchivedMode}>
            <span aria-hidden="true"><CharIcon ch="←" aria-hidden /></span>
            {t("rail_back_to_recent")}
          </button>
          <h2>{t("rail_archived_heading")}</h2>
        </div>
      ) : null}
      {showSearch ? (
        <div className="unified-work-search">
          <Search aria-hidden />
          <input
            ref={searchInputRef}
            type="search"
            value={query}
            onChange={(event) => onQueryChange(event.target.value)}
            placeholder={t(
              mode === "archived"
                ? "rail_search_archived_placeholder"
                : "rail_search_all_placeholder",
            )}
            aria-label={t(
              mode === "archived"
                ? "rail_search_archived_placeholder"
                : "rail_search_all_placeholder",
            )}
            aria-keyshortcuts="Control+K Meta+K"
            name="work-search"
            autoComplete="off"
          />
          {query ? null : <kbd aria-hidden="true">⌘K</kbd>}
        </div>
      ) : null}
      {mode === "active" ? navigationSlot : null}
      <div ref={listRef} className="unified-work-list">
        {mode === "active" ? listStatusSlot : null}
        {pinError ? (
          <div
            className="unified-work-error"
            role="status"
            aria-live="polite"
            aria-label={pinError}
          >
            {pinError}
          </div>
        ) : null}
        {globalSearchActive ? (
          <div
            className="unified-search-results"
            role="region"
            aria-label={t("rail_search_results")}
            aria-busy={search.status === "loading"}
          >
            {search.status === "typing" ? (
              <div className="unified-work-empty" role="status">
                {t("rail_search_keep_typing", {
                  count: search.remaining,
                })}
              </div>
            ) : null}
            {search.status === "loading" ? (
              <div
                className="unified-work-empty"
                role="status"
                aria-live="polite"
              >
                {t("rail_search_loading")}
              </div>
            ) : null}
            {search.status === "error" ? (
              <div className="unified-search-failure">
                <span role="status">{t("rail_search_failed")}</span>
                <button type="button" onClick={search.retry}>
                  {t("rail_search_retry")}
                </button>
              </div>
            ) : null}
            {search.status === "ready" ? (
              <>
                {searchResultCount > 0 ? (
                  <div
                    className="unified-work-sr-only"
                    role="status"
                    aria-live="polite"
                  >
                    {t(
                      searchFound.atLeast
                        ? "rail_search_result_count_at_least"
                        : "rail_search_result_count",
                      { count: searchFound.count },
                    )}
                  </div>
                ) : null}
                {search.response.groups.every(
                  (group) => group.items.length === 0,
                ) ? (
                  <div
                    className="unified-work-empty unified-search-empty"
                    role="status"
                  >
                    <strong>
                      {t("rail_search_no_results", { query: searchQuery })}
                    </strong>
                    <span>{t("rail_search_scope_hint")}</span>
                  </div>
                ) : null}
                {search.response.groups.map((group) => {
                  if (group.unavailable && !group.timedOut) {
                    // Spec 4b: a failed source says so in its own place; the other groups are unaffected.
                    return (
                      <p
                        className="unified-search-note"
                        role="status"
                        key={group.kind}
                      >
                        {t("rail_search_group_unavailable", {
                          group: t(`rail_search_group_${group.kind}`),
                        })}
                      </p>
                    );
                  }
                  const items = searchGroupItems(group);
                  if (!items.length && !group.timedOut) return null;
                  const total =
                    group.kind === "session"
                      ? (sessionGroupMore.total ?? group.total)
                      : group.total;
                  const remaining =
                    total !== null ? Math.max(0, total - items.length) : 0;
                  const showMore =
                    group.kind === "session" &&
                    Boolean(sessionGroupMore.nextCursor) &&
                    remaining > 0;
                  return (
                    <div className="unified-work-group" key={group.kind}>
                      <h3 className="unified-search-group-head">
                        <span>{t(`rail_search_group_${group.kind}`)}</span>
                        {total !== null && !group.timedOut ? (
                          <span className="unified-search-count">
                            {t("rail_search_group_count", { count: total })}
                          </span>
                        ) : null}
                      </h3>
                      {group.timedOut ? (
                        // A timed-out source is "not searched in time", never "no matches".
                        <div className="unified-search-partial" role="status">
                          {t("rail_search_group_timed_out")}
                        </div>
                      ) : null}
                      {items.map(renderSearchResult)}
                      {group.kind === "session" && sessionGroupMore.error ? (
                        <div className="older-sessions-error unified-search-more-error">
                          <span role="status">
                            {t("rail_search_more_failed")}
                          </span>
                          <button type="button" onClick={showMoreSessions}>
                            {t("rail_search_retry")}
                          </button>
                        </div>
                      ) : showMore ? (
                        <button
                          type="button"
                          className={clsx("older-sessions-more", {
                            "is-touch": surface === "drawer",
                          })}
                          aria-busy={sessionGroupMore.loading || undefined}
                          aria-disabled={sessionGroupMore.loading || undefined}
                          onClick={showMoreSessions}
                        >
                          <LucideGlyph
                            icon={ChevronDown}
                            size={14}
                            aria-hidden="true"
                          />
                          <span>
                            {t("rail_search_show_more", { count: remaining })}
                          </span>
                        </button>
                      ) : null}
                    </div>
                  );
                })}
              </>
            ) : null}
          </div>
        ) : null}
        {mode === "archived" && archivedStatus === "loading" ? (
          <div className="unified-work-empty" role="status">
            {t("rail_archived_loading")}
          </div>
        ) : null}
        {mode === "archived" && archivedStatus === "error" ? (
          <div className="unified-search-failure">
            <span role="status">{t("rail_archived_load_failed")}</span>
            <button
              type="button"
              onClick={() => setArchivedRetryKey((key) => key + 1)}
            >
              {t("rail_search_retry")}
            </button>
          </div>
        ) : null}
        {mode === "archived" &&
        archivedStatus === "ready" &&
        total === 0 ? (
          <div className="unified-work-empty" role="status">
            {filter ? t("rail_no_results") : t("rail_archived_empty")}
          </div>
        ) : null}
        {mode === "active" &&
        !listStatusSlot &&
        !globalSearchActive &&
        total === 0 ? (
          // Reached only after the first load settled (loading and errors render listStatusSlot instead).
          <div className="unified-work-empty unified-work-empty--first" role="status">
            <strong>{t("rail_empty")}</strong>
            <span>{t("rail_empty_hint")}</span>
          </div>
        ) : null}
        {!globalSearchActive && total > 0 && filter && visibleTotal === 0 ? (
          <div className="unified-work-empty" role="status">
            {t("rail_no_results")}
          </div>
        ) : null}
        {recoveryGroups.length ? (
          <RecoveryGroups
            groups={recoveryGroups}
            scrollRef={listRef}
            renderItem={renderItem}
            focusRequest={olderFocusRequest}
            onFocusRequestSettled={clearOlderFocusRequest}
          />
        ) : null}
        {olderRowVisible && olderSessions ? (
          // Spec 4a: the last row of the "Older" group (always the last group), never shown while searching.
          <OlderSessionsRow
            paging={{ ...olderSessions, onLoadMore: loadOlder }}
            surface={surface}
          />
        ) : null}
      </div>
      {mode === "active" &&
      archiveEnabled &&
      displayedArchivedCount > 0 ? (
        <button
          type="button"
          className="unified-work-archive-entry"
          onClick={enterArchivedMode}
          aria-label={t("rail_archived_count", {
            count: displayedArchivedCount,
          })}
        >
          <ArchiveBox aria-hidden />
          <span aria-hidden="true">{t("rail_archived_heading")}</span>
          <span aria-hidden="true">{displayedArchivedCount}</span>
        </button>
      ) : null}
      {/* Loaded batches and search pages are announced here (spec 4: "计数写清楚"). */}
      <div
        className="unified-work-sr-only unified-work-announcer"
        role="status"
        aria-live="polite"
      >
        {announcement}
      </div>
      {renameTarget ? (
        <SessionRenameDialog
          initialTitle={renameInitialTitle(renameTarget.id, renameTarget.title)}
          onCancel={() => setRenameTarget(null)}
          onConfirm={(title) => void renameItem(renameTarget, title)}
        />
      ) : null}
    </section>
  );
}

/**
 * The shared list date rule with the rail's copy: relative within the same calendar day, "Yesterday" for the previous
 * day, then days and month/day — in the viewer's time zone (next-intl `useTimeZone()`).
 */
function useRailListDate(): (value: string) => ListDate | null {
  const t = useTranslations("session");
  const locale = useLocale();
  const timeZone = useTimeZone();
  return (value) =>
    formatListDate(
      value,
      locale,
      {
        justNow: () => t("rail_time_just_now"),
        minutesAgo: (count) => t("rail_time_minutes_ago", { count }),
        hoursAgo: (count) => t("rail_time_hours_ago", { count }),
        yesterday: () => t("rail_group_yest"),
        daysAgo: (count) => t("rail_time_days_ago", { count }),
      },
      new Date(),
      timeZone,
    );
}

/**
 * A session row's date. Rows with the Gateway's ISO `updatedAt` follow the same calendar-day rule as search results
 * and other lists ("Yesterday", not "1d ago"); rows without it (older Gateways) fall back to the legacy `time` text.
 */
export function SessionRowTime({ item }: { item: Pick<SessionItem, "time" | "updatedAt"> }) {
  const t = useTranslations("session");
  const locale = useLocale();
  const format = useRailListDate();
  const date = item.updatedAt ? format(item.updatedAt) : null;
  if (date) {
    return (
      <time dateTime={date.dateTime} title={date.title}>
        {date.label}
      </time>
    );
  }
  return item.time ? <span>{formatSessionTime(t, item.time, locale)}</span> : null;
}

/** Second line of a title hit: the list date rule (relative within a week, then month and day). */
function SearchResultDate({ iso }: { iso: string }) {
  const date = useRailListDate()(iso);
  if (!date) return null;
  return (
    <time
      className="unified-search-result-date"
      dateTime={date.dateTime}
      title={date.title}
    >
      {date.label}
    </time>
  );
}

const SEARCH_RESULT_ICONS: Record<UnifiedWorkKind, typeof Chat> = {
  session: Chat,
  report: Doc,
  file: FileText,
  knowledge: Database,
};

function formatSessionTime(
  t: ReturnType<typeof useTranslations>,
  value: string | undefined,
  locale: string,
): string {
  const normalized = value?.trim();
  if (!normalized) return "";
  if (normalized === "just now") return t("rail_time_just_now");
  // Older sessions arrive as a server calendar day ("2026-10-01"); show the list-date absolute form instead.
  const calendarDay = formatCalendarDay(normalized, locale);
  if (calendarDay) return calendarDay;
  const match = /^(\d+)([mhd])(?: ago)?$/.exec(normalized);
  if (!match) return normalized;
  const count = Number(match[1]);
  const key =
    match[2] === "m"
      ? "rail_time_minutes_ago"
      : match[2] === "h"
        ? "rail_time_hours_ago"
        : "rail_time_days_ago";
  return t(key, { count });
}

function matchesFilter(item: SessionItem, filter: string): boolean {
  return !filter || item.title.toLocaleLowerCase().includes(filter);
}

/**
 * Error text for a refused session archive/delete/undo: the specific lifecycle reason when the Gateway names one,
 * otherwise the caller's fallback. Shared by the session row menu and the title-bar menu.
 */
export function useSessionLifecycleErrorText(): (error: unknown, fallback: string) => string {
  const tLifecycleErrors = useTranslations("chat_send_errors.reasons");
  return (error, fallback) => {
    if (error instanceof SessionArchiveError) {
      switch (error.code) {
        case "session_transitioning":
        case "terminal_dependency_recovering":
        case "terminal_protocol_future_version":
        case "terminal_protocol_malformed":
        case "terminal_protocol_writer_not_ready":
          return tLifecycleErrors(error.code);
      }
    }
    return fallback;
  };
}

export function truncateSessionTitle(title: string): string {
  const characters = Array.from(title.trim());
  if (characters.length <= 30) return characters.join("");
  return `${characters.slice(0, 29).join("")}…`;
}

function localizedGroupLabel(
  t: ReturnType<typeof useTranslations>,
  id: string,
  fallback: string,
): string {
  const key = `rail_group_${id}`;
  // A missing key does not return the bare key: the configured fallback renders "session.rail_group_<id>", which
  // reached the sidebar as a group heading. Ask the catalog first and keep the server label otherwise.
  if (typeof t.has === "function") return t.has(key as never) ? t(key as never) : fallback;
  // Translators without `has` (simplified test doubles): treat an echoed key as missing.
  try {
    const translated = t(key as never);
    return translated && !translated.endsWith(key) ? translated : fallback;
  } catch {
    return fallback;
  }
}
