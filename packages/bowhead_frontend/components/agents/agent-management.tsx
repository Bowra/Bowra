"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type MouseEvent,
} from "react";
import { usePathname } from "next/navigation";
import { AGENT_MANAGEMENT_VIEWS, agentManagementHref, agentManagementViewForPath, withAgentReturnView } from "@/lib/agent-navigation";
import { CapabilityLink } from "@/components/capabilities/capability-link";
import { useLocale, useTranslations } from "next-intl";

import { DialogShell } from "@/components/ui/dialog";
import { useConfirmDialog } from "@/components/ui/confirm-dialog";
import { useListDate } from "@/components/ui/list-time";
import { PageTabs } from "@/components/shell/page-frame";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import {
  getAgentUnhireImpact,
  getAgentReleaseInboxCount,
  hireAgent,
  listManagedAgents,
  previewAgentTeamDefault,
  removeAgentTeamDefault,
  setAgentTeamDefault,
  setAgentPersonalStatus,
  unhireAgent,
  type AgentCatalogItem,
  type AgentAssignmentMutation,
  type AgentManagementItem,
  type AgentManagementView,
  type AgentTeamDefaultPreview,
  type AgentUnhireImpact,
} from "@/lib/agents-api";
import { appPath } from "@/lib/base-path";
import { shouldHandleAppLinkClick } from "@/lib/navigation-click";
import { useAppRouter } from "@/lib/use-app-router";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { useSpaceSelection } from "@/lib/use-space";
import { handoffAgentToNewChat } from "@/lib/composer-handoff";
import {
  discardSubagentDraft,
  getManualSubagentDraft,
  getSubagentDraft,
  listManualSubagentDrafts,
  listSubagentDrafts,
  SubagentDraftApiError,
  type SubagentDraftListItem,
} from "@/lib/subagent-drafts-api";
import {
  archivedCreatedCount,
  CREATED_SEARCH_ROW_CAP,
  createdAgentRows,
  createdQueryActive,
  DEFAULT_CREATED_QUERY,
  splitHiredRows,
  type CreatedListQuery,
} from "@/lib/agent-management-list";
import { useAgentReleaseBarCapability } from "@/lib/use-agent-release-bar-capability";
import { agentArtworkStyle } from "./agent-artwork";
import { agentMark } from "./agent-mark";
import { AgentReleaseInbox } from "./agent-release-inbox";
import { CreatedDraftRows, CreatedListControls, ManagedAgentRow, UnavailableHiredRow, type ManagedRowHost } from "./agent-management-rows";
import { AgentRolePreferencesPanel } from "./agent-role-preferences";
import { AgentTeamTagGovernance } from "./agent-team-tag-governance";
import { AgentPageHeader } from "./agent-page-frame";
import { useAgentListingText } from "./agent-listing-text";
import { LimitedStatus, ReasonRetry, ReasonStatus } from "./availability-reason";
import { useCapabilityLabel } from "./capability-label";
import { withHttpFailureReason } from "@/lib/user-facing-error";

const VIEWS = AGENT_MANAGEMENT_VIEWS;

const LISTING_SAFETY_REASON_KEYS: Record<string, string> = {
  credential_or_secret: "listing_safety_reason_credential_or_secret",
  pii_email: "listing_safety_reason_pii",
  pii_cn_national_id: "listing_safety_reason_pii",
  pii_cn_mobile: "listing_safety_reason_pii",
  forbidden_control_character: "listing_safety_reason_control_character",
  unsafe_url: "listing_safety_reason_unsafe_url",
  listing_content_injection: "listing_safety_reason_content_injection",
  description_routing_hijack: "listing_safety_reason_content_injection",
  listing_safety_check_failed: "listing_safety_reason_check_failed",
};

type ActiveDialog =
  | { kind: "unhire"; item: AgentCatalogItem; value: AgentUnhireImpact }
  | { kind: "team_default"; item: AgentCatalogItem; value: AgentTeamDefaultPreview }
  | null;

function applyAssignmentMutation(
  item: AgentCatalogItem,
  mutation: AgentAssignmentMutation,
): AgentCatalogItem {
  const status = mutation.assignment.status;
  const listingBlocked = item.listing_safety?.effective_status === "blocked";
  return {
    ...item,
    assignment: { ...item.assignment, ...mutation.assignment },
    availability: {
      ...item.availability,
      runnable: mutation.runnable,
      public_reason: mutation.public_reason,
    },
    actions: {
      ...item.actions,
      hire: !listingBlocked && (status === "not_hired" || status === "removed"),
      enable: !listingBlocked && status === "personal_disabled",
      disable: status === "active",
      unhire: status === "active" || status === "personal_disabled" || status === "access_revoked",
    },
  };
}

function isCatalogItem(item: AgentManagementItem): item is AgentCatalogItem {
  return item.kind !== "agent_archived_management_item";
}

function assignmentStatusKey(item: AgentCatalogItem): string {
  if (item.assignment.status === "active" && !item.availability.runnable) {
    return "assignment_active_unavailable";
  }
  return `assignment_${item.assignment.status}`;
}

export function AgentManagement() {
  const pathname = usePathname();
  const requestedView = agentManagementViewForPath(pathname ?? (typeof window === "undefined" ? "" : window.location.pathname));
  const router = useAppRouter();
  const t = useTranslations("agents_marketplace");
  const tList = useTranslations("agent_list_v2");
  const locale = useLocale();
  // FEATURE_AGENT_RELEASE_BAR: the redesigned rows, groups and 团队审批 label (slices 2-5 tech design §5).
  const listV2 = useAgentReleaseBarCapability();
  const { confirm, dialog: confirmDialog } = useConfirmDialog();
  const listDate = useListDate();
  const canDirectUse = useOptionalAuthSession()?.me?.capabilities?.composer?.agent_start_use === true;
  const canResumeDrafts = useOptionalAuthSession()?.me?.capabilities?.subagent_authoring?.authoring === true;
  const canResumeManualDrafts = useOptionalAuthSession()?.me?.capabilities?.subagent_authoring?.unified_draft === true;
  const teamId = useOptionalAuthSession()?.me?.team_id ?? "";
  // Manual drafts are saved in a Space: wait for the resolved Space instead of asking without one.
  const spaces = useSpaceSelection();
  const manualDraftSpaceId = canResumeManualDrafts ? spaces.activeId : null;
  const manualDraftSpacePending = canResumeManualDrafts && !manualDraftSpaceId && spaces.loading;
  // A failed Space list is not "no Space": report the drafts as unreadable rather than as none.
  const manualDraftSpaceFailed = canResumeManualDrafts && !manualDraftSpaceId && Boolean(spaces.error);
  const capabilityLabel = useCapabilityLabel();
  const { displayName, summary } = useAgentListingText();
  const [view, setView] = useState<AgentManagementView>(requestedView);
  const [items, setItems] = useState<AgentManagementItem[]>([]);
  const [visibleViews, setVisibleViews] = useState<Record<AgentManagementView, boolean>>({ hired: true, created: true, shared: true, archived: true, team: false });
  const [reviewCount, setReviewCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [validating, setValidating] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeDialog, setActiveDialog] = useState<ActiveDialog>(null);
  const [canCreate, setCanCreate] = useState(false);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [totalCount, setTotalCount] = useState(0);
  const [statusMessage, setStatusMessage] = useState("");
  const [drafts, setDrafts] = useState<SubagentDraftListItem[]>([]);
  const [draftsError, setDraftsError] = useState<"failed" | "unavailable" | null>(null);
  const [draftsFailure, setDraftsFailure] = useState<unknown>(null);
  const [draftsLoading, setDraftsLoading] = useState(false);
  const [rosterVersion, setRosterVersion] = useState(0);
  const [manualDraftIds, setManualDraftIds] = useState<ReadonlySet<string>>(new Set());
  const [draftBusyId, setDraftBusyId] = useState<string | null>(null);
  const [createdQuery, setCreatedQuery] = useState<CreatedListQuery>(DEFAULT_CREATED_QUERY);
  const [searchLoading, setSearchLoading] = useState(false);
  const searchLoadRef = useRef(false);
  const searchLoadFailedRef = useRef(false);
  const refreshSequence = useRef(0);
  const previewSequence = useRef(0);
  const dialogTriggerRef = useRef<HTMLElement | null>(null);
  const viewRef = useRef<AgentManagementView>(view);
  const loadedAdditionalPageRef = useRef(false);
  const listHeadingRef = useRef<HTMLHeadingElement | null>(null);
  viewRef.current = view;

  const refresh = useCallback(async (
    nextView: AgentManagementView,
    options: {
      append?: boolean;
      cursor?: string | null;
      preserve?: boolean;
      mutationSucceeded?: boolean;
    } = {},
  ) => {
    const sequence = refreshSequence.current + 1;
    refreshSequence.current = sequence;
    if (options.append) setLoadingMore(true);
    else if (options.preserve) setValidating(true);
    else {
      setLoading(true);
      setLoaded(false);
      setItems([]);
      setNextCursor(null);
      setTotalCount(0);
    }
    try {
      const result = await listManagedAgents(nextView, {
        cursor: options.cursor ?? undefined,
        limit: 48,
      });
      if (
        refreshSequence.current !== sequence
        || viewRef.current !== nextView
      ) return false;
      setItems((current) => options.append
        ? [...new Map([...current, ...result.items].map((item) => [item.agent_id, item])).values()]
        : result.items);
      // A replaced first page drops the pages a search loaded; the search loads them again.
      if (!options.append) searchLoadFailedRef.current = false;
      setTotalCount((current) => result.total_count ?? (
        options.append ? current + result.items.length : result.items.length
      ));
      loadedAdditionalPageRef.current = Boolean(options.append);
      setNextCursor(result.next_cursor ?? null);
      setVisibleViews(result.views);
      setCanCreate(result.actions.create);
      if (result.view !== nextView) {
        router.replace(agentManagementHref(result.view));
        viewRef.current = result.view;
        setView(result.view);
      }
      setLoaded(true);
      setError(null);
      return true;
    } catch (cause) {
      if (refreshSequence.current !== sequence) return false;
      setError(withHttpFailureReason(t(options.mutationSucceeded ? "saved_refresh_failed" : "load_failed"), cause, { read: true }));
      return false;
    } finally {
      if (refreshSequence.current === sequence) {
        setLoading(false);
        setLoadingMore(false);
        setValidating(false);
      }
    }
  }, [router, t]);

  const announceFirstPageReset = useCallback(() => {
    setStatusMessage(t("manage_saved_first_page"));
    window.setTimeout(() => {
      listHeadingRef.current?.scrollIntoView?.({ block: "start" });
      listHeadingRef.current?.focus();
    }, 0);
  }, [t]);

  useEffect(() => {
    setView(requestedView);
    setActiveDialog(null);
    setCreatedQuery(DEFAULT_CREATED_QUERY);
    previewSequence.current += 1;
  }, [requestedView]);
  useEffect(() => { void refresh(view); }, [refresh, view]);
  useEffect(() => {
    if (view !== "created" || !canResumeDrafts || manualDraftSpacePending) return;
    let cancelled = false;
    setDraftsError(null);
    setDraftsLoading(true);
    void Promise.all([
      listSubagentDrafts(),
      // Without any Space there is no Space-bound manual draft to continue.
      manualDraftSpaceFailed
        ? Promise.reject(new Error("space_list_unavailable"))
        : manualDraftSpaceId && teamId
          ? listManualSubagentDrafts({ teamId, workspaceId: manualDraftSpaceId })
          : Promise.resolve([]),
    ]).then(([conversationDrafts, manualDrafts]) => {
      const items = [...conversationDrafts, ...manualDrafts]
        .sort((left, right) => right.updated_at.localeCompare(left.updated_at));
      if (!cancelled) {
        setDrafts(items);
        setManualDraftIds(new Set(manualDrafts.map((draft) => draft.draft_id)));
        setDraftsError(null);
      }
    }).catch((cause: unknown) => {
      if (cancelled) return;
      // During an upgrade the draft store answers 503; say so calmly instead of alerting.
      const unavailable = cause instanceof SubagentDraftApiError && cause.code === "subagent_draft_store_unavailable";
      setDraftsError(unavailable ? "unavailable" : "failed");
      setDraftsFailure(cause);
    }).finally(() => {
      if (!cancelled) setDraftsLoading(false);
    });
    return () => { cancelled = true; };
  }, [view, canResumeDrafts, manualDraftSpaceId, manualDraftSpacePending, manualDraftSpaceFailed, teamId]);
  useEffect(() => {
    if (!visibleViews.team) return;
    let cancelled = false;
    void getAgentReleaseInboxCount().then((result) => {
      if (!cancelled) setReviewCount(result.count);
    }).catch(() => {
      if (!cancelled) setReviewCount(0);
    });
    return () => { cancelled = true; };
  }, [visibleViews.team]);

  // Decision SD2: search, filter and sort first load the remaining pages through the cursor, at most 500 rows.
  const searchActive = listV2 && view === "created" && createdQueryActive(createdQuery);
  useEffect(() => {
    if (!searchActive || !nextCursor || loading || validating || loadingMore) return;
    if (searchLoadRef.current || searchLoadFailedRef.current || items.length >= CREATED_SEARCH_ROW_CAP) return;
    searchLoadRef.current = true;
    const sequence = refreshSequence.current;
    const requestView = viewRef.current;
    setSearchLoading(true);
    void (async () => {
      let cursor: string | null = nextCursor;
      let count = items.length;
      try {
        while (cursor && count < CREATED_SEARCH_ROW_CAP) {
          const result = await listManagedAgents(requestView, { cursor, limit: 100 });
          if (refreshSequence.current !== sequence || viewRef.current !== requestView) return;
          setItems((current) => [...new Map([...current, ...result.items].map((item) => [item.agent_id, item])).values()]);
          loadedAdditionalPageRef.current = true;
          count += result.items.length;
          cursor = result.next_cursor ?? null;
          setNextCursor(cursor);
        }
      } catch (cause) {
        if (refreshSequence.current !== sequence) return;
        searchLoadFailedRef.current = true;
        setError(withHttpFailureReason(t("load_failed"), cause, { read: true }));
      } finally {
        searchLoadRef.current = false;
        setSearchLoading(false);
      }
    })();
  }, [searchActive, nextCursor, loading, validating, loadingMore, items.length, t]);

  const deleteDraft = async (draft: SubagentDraftListItem, trigger: HTMLElement) => {
    const name = draft.title || t("drafts_untitled");
    const confirmed = await confirm({
      title: tList("draft_delete_title", { name }),
      description: tList("draft_delete_body"),
      confirmLabel: tList("draft_delete_confirm"),
      returnFocusRef: { current: trigger },
    });
    if (!confirmed) return;
    setStatusMessage("");
    setDraftBusyId(draft.draft_id);
    try {
      // A manual draft lives in a Space: its read and delete name that Space (the list read it there).
      const context = manualDraftIds.has(draft.draft_id) && teamId && manualDraftSpaceId
        ? { teamId, workspaceId: manualDraftSpaceId } : null;
      if (manualDraftIds.has(draft.draft_id) && !context) {
        throw new SubagentDraftApiError(400, "subagent_draft_context_required", "The draft's Space is not resolved");
      }
      const etag = context
        ? (await getManualSubagentDraft(draft.draft_id, context)).etag
        : (await getSubagentDraft(draft.draft_id)).etag;
      await discardSubagentDraft(draft.draft_id, etag, context ?? undefined);
      setDrafts((current) => current.filter((entry) => entry.draft_id !== draft.draft_id));
      setStatusMessage(tList("draft_deleted", { name }));
      window.setTimeout(() => listHeadingRef.current?.focus(), 0);
    } catch (cause) {
      const conflict = cause instanceof SubagentDraftApiError && cause.status === 409;
      setError(withHttpFailureReason(tList(conflict ? "draft_delete_conflict" : "draft_delete_failed", { name }), cause));
    } finally {
      setDraftBusyId(null);
    }
  };

  const changeView = (next: AgentManagementView) => {
    if (pendingId !== null || next === viewRef.current) return;
    router.push(agentManagementHref(next));
  };

  const navigate = useCallback((event: MouseEvent<HTMLAnchorElement>, path: string) => {
    if (!shouldHandleAppLinkClick(event)) return;
    event.preventDefault();
    router.push(path);
  }, [router]);

  const closeDialog = useCallback(() => {
    if (pendingId !== null) return;
    previewSequence.current += 1;
    setActiveDialog(null);
  }, [pendingId]);


  useEffect(() => {
    if (activeDialog === null) return;
    const surface = document.querySelector<HTMLElement>(".ui-page-frame-scroll");
    if (!surface) return;
    const previousOverflow = surface.style.overflow;
    surface.style.overflow = "hidden";
    return () => {
      surface.style.overflow = previousOverflow;
    };
  }, [activeDialog]);

  const setStatus = async (item: AgentCatalogItem, status: "active" | "disabled") => {
    if (!item.assignment.row_version) return;
    const actionView = viewRef.current;
    const resetsPagination = loadedAdditionalPageRef.current;
    setStatusMessage("");
    setPendingId(item.agent_id);
    try {
      const mutation = await setAgentPersonalStatus(item.agent_id, status, item.assignment.row_version);
      if (viewRef.current === actionView) {
        setItems((current) => current.map((entry) => (
          entry.agent_id === item.agent_id && isCatalogItem(entry) ? applyAssignmentMutation(entry, mutation) : entry
        )));
      }
      const refreshed = await refresh(viewRef.current, { preserve: true, mutationSucceeded: true });
      setRosterVersion((current) => current + 1);
      if (refreshed && resetsPagination) announceFirstPageReset();
      else setStatusMessage(tList(status === "disabled" ? "status_paused" : "status_enabled", { name: displayName(item) }));
      // The row moves between 在用 and 现在用不了; keep keyboard focus on it instead of dropping it to the page.
      window.setTimeout(() => focusMovedRow(item.agent_id), 0);
    } catch (cause) {
      setError(withHttpFailureReason(t("action_failed"), cause));
    } finally {
      setPendingId(null);
    }
  };

  const hire = async (item: AgentCatalogItem) => {
    const actionView = viewRef.current;
    const resetsPagination = loadedAdditionalPageRef.current;
    setStatusMessage("");
    setPendingId(item.agent_id);
    try {
      const mutation = await hireAgent(item.agent_id);
      if (viewRef.current === actionView) {
        setItems((current) => current.map((entry) => (
          entry.agent_id === item.agent_id && isCatalogItem(entry) ? applyAssignmentMutation(entry, mutation) : entry
        )));
      }
      const refreshed = await refresh(viewRef.current, { preserve: true, mutationSucceeded: true });
      setRosterVersion((current) => current + 1);
      if (refreshed && resetsPagination) announceFirstPageReset();
    } catch (cause) {
      setError(withHttpFailureReason(t("action_failed"), cause));
    } finally {
      setPendingId(null);
    }
  };

  const previewUnhire = async (item: AgentCatalogItem, trigger: HTMLElement) => {
    const previewView = viewRef.current;
    const sequence = previewSequence.current + 1;
    previewSequence.current = sequence;
    dialogTriggerRef.current = trigger;
    setActiveDialog(null);
    setPendingId(item.agent_id);
    try {
      const value = await getAgentUnhireImpact(item.agent_id);
      if (previewSequence.current === sequence && viewRef.current === previewView) {
        setActiveDialog({ kind: "unhire", item, value });
      }
    } catch (cause) {
      if (previewSequence.current === sequence) setError(withHttpFailureReason(t("action_failed"), cause, { read: true }));
    } finally {
      if (previewSequence.current === sequence) setPendingId(null);
    }
  };

  const confirmUnhire = async (scheduleAction?: "pause" | "expire") => {
    if (activeDialog?.kind !== "unhire") return;
    const { item, value } = activeDialog;
    const actionView = viewRef.current;
    const resetsPagination = loadedAdditionalPageRef.current;
    setStatusMessage("");
    setPendingId(item.agent_id);
    try {
      const mutation = await unhireAgent(item.agent_id, value, scheduleAction);
      setActiveDialog(null);
      if (viewRef.current === actionView) {
        setItems((current) => actionView === "hired"
          ? current.filter((entry) => entry.agent_id !== item.agent_id)
          : current.map((entry) => (
              entry.agent_id === item.agent_id && isCatalogItem(entry) ? applyAssignmentMutation(entry, mutation) : entry
            )));
      }
      const refreshed = await refresh(viewRef.current, { preserve: true, mutationSucceeded: true });
      setRosterVersion((current) => current + 1);
      if (refreshed && resetsPagination) announceFirstPageReset();
    } catch (cause) {
      setError(withHttpFailureReason(t("action_failed"), cause));
    } finally {
      setPendingId(null);
    }
  };

  const previewDefault = async (item: AgentCatalogItem, trigger: HTMLElement) => {
    const previewView = viewRef.current;
    const sequence = previewSequence.current + 1;
    previewSequence.current = sequence;
    dialogTriggerRef.current = trigger;
    setActiveDialog(null);
    setPendingId(item.agent_id);
    try {
      const action = item.team_default.enabled ? "remove" : "set";
      const value = await previewAgentTeamDefault(item.agent_id, action);
      if (previewSequence.current === sequence && viewRef.current === previewView) {
        setActiveDialog({ kind: "team_default", item, value });
      }
    } catch (cause) {
      if (previewSequence.current === sequence) setError(withHttpFailureReason(t("action_failed"), cause, { read: true }));
    } finally {
      if (previewSequence.current === sequence) setPendingId(null);
    }
  };

  const confirmDefault = async () => {
    if (activeDialog?.kind !== "team_default") return;
    const { item, value } = activeDialog;
    const actionView = viewRef.current;
    const resetsPagination = loadedAdditionalPageRef.current;
    setStatusMessage("");
    setPendingId(item.agent_id);
    try {
      if (value.action === "set") {
        await setAgentTeamDefault(item.agent_id);
      } else {
        await removeAgentTeamDefault(item.agent_id);
      }
      setActiveDialog(null);
      if (viewRef.current === actionView) {
        setItems((current) => current.map((entry) => entry.agent_id === item.agent_id && isCatalogItem(entry)
          ? { ...entry, team_default: { ...entry.team_default, enabled: value.action === "set" } }
          : entry));
      }
      const refreshed = await refresh(viewRef.current, { preserve: true, mutationSucceeded: true });
      if (refreshed && resetsPagination) announceFirstPageReset();
    } catch (cause) {
      setError(withHttpFailureReason(t("action_failed"), cause));
    } finally {
      setPendingId(null);
    }
  };

  const emptyState = (
    <div className="am-empty"><h3>{t(`empty_${view}_title`)}</h3><p>{t(`empty_${view}_body`)}</p>{view === "hired" || view === "shared" ? <a className="settings-button settings-button--primary settings-button--md" href={appPath("/agents/marketplace/")} onClick={(event) => navigate(event, "/agents/marketplace/")}>{t("find_agents")}</a> : null}</div>
  );
  const rowHost: ManagedRowHost = {
    view,
    pending: pendingId !== null,
    canDirectUse,
    onNavigate: navigate,
    onStartUse: (item) => handoffAgentToNewChat({ agentId: item.agent_id, label: displayName(item) }),
    onHire: (item) => void hire(item),
    onSetStatus: (item, status) => void setStatus(item, status),
    onUnhire: (item, trigger) => void previewUnhire(item, trigger),
    onRetry: () => void refresh(view, { preserve: true }),
  };
  const skeleton = [0, 1, 2].map((value) => <div key={value} className="am-management-row am-skeleton" />);
  // The announced count matches the visible heading: 我创建的 leaves archived Agents out and follows search and filter.
  const v2ListCount = listV2 && view === "created"
    ? (!nextCursor || searchActive ? createdAgentRows(items, createdQuery, { name: displayName, summary, locale }).length : Math.max(0, totalCount - archivedCreatedCount(items)))
    : totalCount;

  const renderListV2 = () => {
    const busy = loading || validating || loadingMore || searchLoading;
    if (view === "created") {
      const rows = createdAgentRows(items, createdQuery, { name: displayName, summary, locale });
      const archived = archivedCreatedCount(items);
      const complete = !nextCursor;
      const capped = searchActive && Boolean(nextCursor) && items.length >= CREATED_SEARCH_ROW_CAP;
      return <>
        {canResumeDrafts && !draftsLoading && !draftsError && drafts.length > 0
          ? <CreatedDraftRows drafts={drafts} busyDraftId={draftBusyId} onNavigate={navigate} onDelete={(draft, trigger) => void deleteDraft(draft, trigger)} />
          : null}
        <CreatedListControls value={createdQuery} onChange={setCreatedQuery} />
        <p className="am-list-v2-note" role="status">
          {searchActive && searchLoading ? tList("search_loading") : capped ? tList("search_capped", { count: CREATED_SEARCH_ROW_CAP }) : ""}
        </p>
        <section className="am-management-list am-list-v2-group" aria-labelledby="am-list-v2-created" aria-busy={busy}>
          <h2 id="am-list-v2-created" ref={listHeadingRef} tabIndex={-1}>
            {tList("created_heading")}{complete || searchActive ? <span> · {rows.length}</span> : null}
          </h2>
          {loading ? skeleton : rows.length ? rows.map((item) => <ManagedAgentRow key={item.agent_id} item={item} host={rowHost} />)
            : loaded && !error ? (searchActive ? <p className="am-list-v2-empty">{tList("no_match")}</p>
              : archived ? <p className="am-list-v2-empty">{tList("created_none_active")}</p> : emptyState) : null}
          {archived ? (
            <p className="am-list-v2-pointer">
              {tList.rich(complete ? "archived_pointer" : "archived_pointer_some", {
                count: archived,
                link: (chunks) => <a href={appPath(agentManagementHref("archived"))} onClick={(event) => navigate(event, agentManagementHref("archived"))}>{chunks}</a>,
              })}
            </p>
          ) : null}
        </section>
      </>;
    }
    if (view === "hired") {
      const groups = splitHiredRows(items);
      return <>
        <section className="am-management-list am-list-v2-group" aria-labelledby="am-list-v2-active" aria-busy={busy}>
          <h2 id="am-list-v2-active" ref={listHeadingRef} tabIndex={-1}>{tList("hired_active_heading")}{loading ? null : <span> · {groups.active.length}</span>}</h2>
          {loading ? skeleton : groups.active.length ? groups.active.map((item) => <ManagedAgentRow key={item.agent_id} item={item} host={rowHost} />)
            : loaded && !error ? (groups.unavailable.length ? <p className="am-list-v2-empty">{tList("hired_active_empty")}</p> : emptyState) : null}
        </section>
        {!loading && groups.unavailable.length ? (
          <details className="am-list-v2-folded">
            <summary>{tList("hired_unavailable_heading")} <span>· {groups.unavailable.length}</span></summary>
            <div className="am-management-list">
              {groups.unavailable.map((item) => <UnavailableHiredRow key={item.agent_id} item={item} host={rowHost} />)}
            </div>
          </details>
        ) : null}
      </>;
    }
    const rows = items.filter(isCatalogItem);
    return (
      <section className="am-management-list am-list-v2-group" aria-labelledby="am-list-v2-shared" aria-busy={busy}>
        <h2 id="am-list-v2-shared" ref={listHeadingRef} tabIndex={-1}>{t("view_shared")}{loading ? null : <span> · {totalCount}</span>}</h2>
        {loading ? skeleton : rows.length ? rows.map((item) => <ManagedAgentRow key={item.agent_id} item={item} host={rowHost} />) : loaded && !error ? emptyState : null}
      </section>
    );
  };

  return (
    <div className="am-page am-manage-page">
      <div className="am-page-content" inert={activeDialog !== null ? true : undefined}>
      <AgentPageHeader page="manage" canCreate={canCreate} description={t("manage_subtitle")} />
      <div inert={pendingId !== null ? true : undefined} aria-disabled={pendingId !== null || undefined}>
        <PageTabs
          label={t("manage_views")}
          items={VIEWS.filter((value) => visibleViews[value]).map((value) => ({
            key: value,
            label: listV2 && value === "team" ? tList("view_team") : t(`view_${value}`),
            href: agentManagementHref(value),
            current: value === view,
            count: value === "team" && reviewCount ? reviewCount : undefined,
          }))}
          onNavigate={(item) => changeView(item.key as AgentManagementView)}
        />
      </div>
      {view === "created" && canResumeDrafts && draftsError === "failed" ? <p role="alert">{withHttpFailureReason(t("drafts_load_failed"), draftsFailure, { read: true })}</p> : null}
      {view === "created" && canResumeDrafts && draftsError === "unavailable" ? <p role="status">{t("drafts_unavailable")}</p> : null}
      {!listV2 && view === "created" && canResumeDrafts && !draftsLoading && !draftsError && drafts.length > 0 ? (
        <section className="am-management-context" aria-labelledby="am-resume-drafts-title">
          <div>
            <span>{t("drafts_eyebrow")}</span>
            <h2 id="am-resume-drafts-title">{t("drafts_title")}</h2>
            <p>{t("drafts_body")}</p>
            <ul className="am-resumable-drafts">
              {drafts.map((draft) => {
                const path = `/?agent_draft=${encodeURIComponent(draft.draft_id)}`;
                return <li key={draft.draft_id}>
                  <a href={appPath(path)} onClick={(event) => navigate(event, path)}>
                    {draft.title || t("drafts_untitled")} · {t("drafts_continue")}
                  </a>
                  <small className="legacy-am-muted-block">
                    {t(draft.source_mode === "manual" ? "drafts_source_manual" : "drafts_source_ai")}
                    {" · "}{t(`drafts_status_${draft.draft_status}` as never)}
                    {" · "}{t("drafts_updated", { date: listDate(draft.updated_at)?.label ?? "—" })}
                  </small>
                </li>;
              })}
            </ul>
          </div>
        </section>
      ) : null}
      {error ? <div className="am-notice" role="alert"><span>{error}</span><button onClick={() => void refresh(view)}>{t("retry")}</button></div> : null}
      <p className="sr-only" role="status" aria-live="polite">{statusMessage}</p>
      {view === "team" ? <AgentReleaseInbox totalCount={reviewCount} /> : null}
      {view === "team" ? <AgentTeamTagGovernance /> : null}
      {listV2 ? (
        <p className="sr-only" aria-live="polite">
          {loading ? t("view_loading") : validating ? t("view_result_count_updating", { count: v2ListCount })
            : searchActive ? tList("search_result_count", { count: v2ListCount }) : t("view_result_count", { count: v2ListCount })}
        </p>
      ) : <section className="am-management-context" aria-labelledby="am-management-context-title">
        <div>
          <span>{t(`view_${view}_eyebrow` as never)}</span>
          <h2 id="am-management-context-title">{t(`view_${view}_title` as never)}</h2>
          <p>{t(`view_${view}_body` as never)}</p>
        </div>
        <strong aria-live="polite">
          {loading
            ? t("view_loading")
            : validating
              ? t("view_result_count_updating", { count: totalCount })
              : t("view_result_count", { count: totalCount })}
        </strong>
      </section>}
      {listV2 && (view === "created" || view === "hired" || view === "shared") ? renderListV2() : <section className="am-management-list" aria-busy={loading || validating || loadingMore}>
        <h2 ref={listHeadingRef} className="sr-only" tabIndex={-1}>{listV2 && view === "team" ? tList("view_team") : t(`view_${view}`)}</h2>
        {loading ? [0, 1, 2].map((value) => <div key={value} className="am-management-row am-skeleton" />) : items.length ? items.map((item) => !isCatalogItem(item) ? (
          <article className="am-management-row" key={item.agent_id}>
            <CapabilityLink className="am-management-identity" href={withAgentReturnView(`/agents/manage/?agent_id=${encodeURIComponent(item.agent_id)}`, view)}>
              <span className="am-management-mark" style={agentArtworkStyle(`${item.agent_id}:${item.display_name}`)}>{agentMark(item.display_name)}</span>
              <div><h2>{item.display_name}</h2><p>{item.summary}</p><small>{t("release_version_label", { version: item.release_sequence })}</small></div>
            </CapabilityLink>
            <div className="am-management-status"><span data-status="relationship-archived">{t("relationship_archived")}</span><small>{t("release_archived_at", { date: listDate(item.archived_at)?.label ?? "—" })}</small></div>
            <div className="am-management-actions"><CapabilityLink href={withAgentReturnView(`/agents/manage/?agent_id=${encodeURIComponent(item.agent_id)}`, view)}>{t("release_review_and_restore")}</CapabilityLink></div>
          </article>
        ) : (
          <article className="am-management-row" key={item.agent_id} data-agent-id={item.agent_id}>
            {item.actions.view_detail ? (
              <a className="am-management-identity" href={appPath(withAgentReturnView(`/agents/detail/?id=${encodeURIComponent(item.agent_id)}`, view))} onClick={(event) => navigate(event, withAgentReturnView(`/agents/detail/?id=${encodeURIComponent(item.agent_id)}`, view))}>
                <span className="am-management-mark" style={agentArtworkStyle(`${item.agent_id}:${displayName(item)}`)}>{agentMark(displayName(item))}</span>
                <div><h2>{displayName(item)}</h2><p>{summary(item)}</p><small>{item.capabilities.slice(0, 3).map(capabilityLabel).join(" · ")}</small></div>
              </a>
            ) : (
              <div className="am-management-identity">
                <span className="am-management-mark" style={agentArtworkStyle(`${item.agent_id}:${displayName(item)}`)}>{agentMark(displayName(item))}</span>
                <div><h2>{displayName(item)}</h2><p>{summary(item)}</p></div>
              </div>
            )}
            <div className="am-management-status">
              {item.listing_safety?.effective_status === "blocked" ? (
                <>
                  <span data-status="listing-blocked">{t("listing_safety_blocked")}</span>
                  <small className="am-listing-safety-reason">
                    {t((LISTING_SAFETY_REASON_KEYS[item.listing_safety.reasons[0] ?? ""] ?? "listing_safety_reason_unknown") as never)}
                  </small>
                </>
              ) : null}
              <ReasonStatus item={item} enabled={view === "hired" || (view === "created" && item.assignment.status === "removed")}>
                <span data-status={view === "hired" && assignmentStatusKey(item) === "assignment_active_unavailable"
                  ? "assignment-unavailable"
                  : `relationship-${view}`}>
                  {t((view === "created"
                    ? "relationship_owner"
                    : view === "shared"
                      ? "relationship_shared"
                      : view === "team" && item.team_default.enabled
                        ? "relationship_team_default"
                        : view === "team"
                          ? "relationship_team_catalog"
                          : assignmentStatusKey(item)) as never)}
                </span>
              </ReasonStatus>
              {view === "created" && item.lifecycle_state === "archived" ? <span data-status="relationship-archived">{t("relationship_archived")}</span> : null}
              <LimitedStatus item={item} enabled={view === "hired"} />
              {view === "shared" ? <small>{t(assignmentStatusKey(item) as never)}</small> : null}
              <small>
                {item.source.owner_label ? t("by_creator", { name: item.source.owner_label }) : t("by_bowhead")}
                {item.source.kind === "team" ? ` · ${t("creator_listing_original")}` : ""}
                {item.assignment.source ? ` · ${t((item.relationship === "owned" && item.assignment.source === "user_hired" ? "source_owner_added" : item.assignment.source === "system" ? "source_assignment_system" : `source_${item.assignment.source}`) as never)}` : ""}
              </small>
            </div>
            <div className="am-management-actions">
              {canDirectUse && item.availability.runnable && item.assignment.status === "active" ? <button className="am-action-primary" onClick={() => handoffAgentToNewChat({ agentId: item.agent_id, label: displayName(item) })}>{t("start_use")}</button> : null}
              {view === "hired" ? <ReasonRetry item={item} disabled={pendingId !== null} onRetry={() => void refresh(view, { preserve: true })} /> : null}
              {(view === "shared" || view === "created") && item.actions.hire ? <button className="am-action-primary" disabled={pendingId !== null} onClick={() => void hire(item)}>{t("hire")}</button> : null}
              {(view === "hired" || view === "shared") && item.actions.disable ? <button disabled={pendingId !== null} onClick={() => void setStatus(item, "disabled")}>{t("pause")}</button> : null}
              {(view === "hired" || view === "shared") && item.actions.enable ? <button disabled={pendingId !== null} onClick={() => void setStatus(item, "active")}>{t("enable")}</button> : null}
              {(view === "created" || view === "team") && item.actions.manage && item.manager_url ? <CapabilityLink href={withAgentReturnView(item.manager_url, view)}>{t("configure")}</CapabilityLink> : null}
              {view === "team" && item.actions.manage_team_default ? (
                <button disabled={pendingId !== null} onClick={(event) => void previewDefault(item, event.currentTarget)}>
                  {item.team_default.enabled ? t("remove_team_default") : t("set_team_default")}
                </button>
              ) : null}
              {(view === "hired" || view === "shared") && item.actions.unhire ? (
                <RowActionsMenu
                  label={t("more_actions_for", { name: displayName(item) })}
                  pending={pendingId !== null}
                  actions={[{
                    id: "unhire",
                    label: t("unhire"),
                    danger: true,
                    disabled: pendingId !== null,
                    // Closing confirmation returns focus to this row's “⋯”
                    onSelect: () => void previewUnhire(item, rowMoreButton(item.agent_id)),
                  }]}
                />
              ) : null}
            </div>
          </article>
        )) : loaded && !error ? emptyState : null}
      </section>}
      {nextCursor && !loading && !searchActive ? <div className="am-load-more"><button type="button" disabled={loadingMore || pendingId !== null} onClick={() => void refresh(viewRef.current, { append: true, cursor: nextCursor })}>{loadingMore ? t("loading_more") : t("load_more")}</button></div> : null}
      {/* Below the Agent list, and only once a role has two verified candidates (design §7.5). */}
      {view === "hired" && loaded ? <AgentRolePreferencesPanel rosterVersion={rosterVersion} /> : null}
      </div>

      {activeDialog?.kind === "unhire" ? (
        <DialogShell
          onClose={closeDialog}
          busy={pendingId !== null}
          mount="inline"
          returnFocusRef={dialogTriggerRef}
          labelledBy="am-unhire-title"
          backdropClassName="am-dialog-backdrop"
          panelClassName="am-dialog"
          panelProps={{ "aria-describedby": "am-unhire-description" }}
        >
          <span>{t("unhire_review")}</span>
          <h2 id="am-unhire-title">{t("unhire_title", { name: displayName(activeDialog.item) })}</h2>
          <p id="am-unhire-description">{activeDialog.value.running_count ? t("unhire_running_blocked") : activeDialog.value.active_count ? t("unhire_schedule_impact", { count: activeDialog.value.active_count }) : t("unhire_no_schedule_impact")}</p>
          {activeDialog.value.jobs.length ? <ul>{activeDialog.value.jobs.map((job) => <li key={job.id}><span>{job.name}</span><small>{job.has_in_flight_run ? t("running") : t(`schedule_${job.status}` as never)}</small></li>)}</ul> : null}
          <footer>
            <button disabled={pendingId !== null} onClick={closeDialog}>{t("cancel")}</button>
            {!activeDialog.value.running_count ? <button className="am-danger-confirm" disabled={pendingId !== null} onClick={() => void confirmUnhire(activeDialog.value.active_count ? "pause" : undefined)}>{activeDialog.value.active_count ? t("pause_and_unhire") : t("confirm_unhire")}</button> : null}
          </footer>
        </DialogShell>
      ) : null}
      {activeDialog?.kind === "team_default" ? (
        <DialogShell
          onClose={closeDialog}
          busy={pendingId !== null}
          mount="inline"
          returnFocusRef={dialogTriggerRef}
          labelledBy="am-default-title"
          backdropClassName="am-dialog-backdrop"
          panelClassName="am-dialog"
          panelProps={{ "aria-describedby": "am-default-description" }}
        >
          <span>{t("team_default_review")}</span>
          <h2 id="am-default-title">
            {activeDialog.value.action === "set"
              ? t("team_default_set_title", { name: displayName(activeDialog.item) })
              : t("team_default_remove_title", { name: displayName(activeDialog.item) })}
          </h2>
          <p id="am-default-description">
            {activeDialog.value.action === "set"
              ? t("team_default_set_impact", {
                affected: activeDialog.value.affected_count,
                optedOut: activeDialog.value.opted_out_count,
              })
              : t("team_default_remove_impact", { affected: activeDialog.value.affected_count })}
          </p>
          <footer>
            <button disabled={pendingId !== null} onClick={closeDialog}>{t("cancel")}</button>
            <button className="am-action-primary" disabled={pendingId !== null} onClick={() => void confirmDefault()}>
              {activeDialog.value.action === "set" ? t("confirm_team_default") : t("confirm_remove_team_default")}
            </button>
          </footer>
        </DialogShell>
      ) : null}
      {confirmDialog}
    </div>
  );
}

/** After 停用 / 重新启用 the row re-renders in another group: open a folded group and focus the row's first action. */
function focusMovedRow(agentId: string): void {
  const row = Array.from(document.querySelectorAll<HTMLElement>(".am-management-row[data-agent-id]"))
    .find((element) => element.dataset.agentId === agentId);
  if (!row) return;
  const folded = row.closest("details");
  if (folded && !folded.open) folded.open = true;
  row.querySelector<HTMLElement>(".am-management-actions button:not([disabled])")?.focus();
}

/** Management-row “⋯” trigger; fall back to page main if not found. */
function rowMoreButton(agentId: string): HTMLElement {
  const row = Array.from(document.querySelectorAll<HTMLElement>(".am-management-row[data-agent-id]"))
    .find((element) => element.dataset.agentId === agentId);
  return row?.querySelector<HTMLElement>(".ui-row-more") ?? document.body;
}
