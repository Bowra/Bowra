"use client";

import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useLocale, useTranslations } from "next-intl";

import { useBrand } from "@/components/brand-provider";
import { AlertTriangle, Arrow, Bell, Bolt, Chat, Check, Doc, Folder, Info, RefreshCw, Spark } from "@/components/icons";
import {
  fetchHomeProposals,
  fetchHomeSnapshot,
  type HomeActiveItem,
  type HomeAttentionItem,
  type HomeOutputItem,
  type HomeProposalItem,
  type HomeProposalKind,
  type HomeSnapshot,
} from "@/lib/home-api";
import { ApiError } from "@/lib/api-client";
import { fetchMe, requestSpaceAssignment } from "@/lib/auth-api";
import {
  TEAM_SETTINGS_ROLES,
  type SettingsTeamRole,
} from "@/lib/settings-navigation";
// impression is a counter (record every exposure of the same proposal), via non-deduplicating `track`;
// start is a click, via default-deduplicating `trackInteraction`. Previously differentiated by one function's options;
// two named entries now express the difference at each call site.
import { track, trackInteraction } from "@/lib/metrics";
import { normalizeAppActionUrl } from "@/lib/legacy-app-links";
import { reportDisplayTitle } from "@/lib/report-title";
import { localizeNotification } from "@/lib/notification-copy";
import { useAppRouter } from "@/lib/use-app-router";

interface HomeWorkSurfaceProps {
  workspaceId: string | null;
  workspaceName: string;
  workspaceLoading: boolean;
  workspaceError?: string | null;
  readOnly: boolean;
  onStartPrompt: (prompt: string) => Promise<boolean> | boolean;
  onWorkspaceInvalidated?: () => Promise<void> | void;
  /**
   * Single-column home: page supplies composer after title/before proposals (DOM order = focus/screen-reader order).
   */
  composerSlot?: ReactNode;
}

interface LocalWorkProposal {
  id: "shape" | "create" | "automate";
  kind: HomeProposalKind;
  promptKey: "shapePrompt" | "createPrompt" | "automatePrompt";
  titleKey: "shapeTitle" | "createTitle" | "automateTitle";
  detailKey: "shapeDetail" | "createDetail" | "automateDetail";
}

interface ProposalState {
  workspaceId: string;
  locale: string;
  generationId: string;
  strategy: "llm" | "deterministic";
  items: HomeProposalItem[];
}

interface DisplayProposal {
  id: string;
  kind: HomeProposalKind;
  title: string;
  detail: string;
  prompt: string;
}

interface SnapshotClockAnchor {
  workspaceId: string;
  serverTimeMs: number;
  receivedAtMs: number;
}

const WORK_PROPOSALS: LocalWorkProposal[] = [
  {
    id: "shape",
    kind: "analyze",
    titleKey: "shapeTitle",
    detailKey: "shapeDetail",
    promptKey: "shapePrompt",
  },
  {
    id: "create",
    kind: "create",
    titleKey: "createTitle",
    detailKey: "createDetail",
    promptKey: "createPrompt",
  },
  {
    id: "automate",
    kind: "automate",
    titleKey: "automateTitle",
    detailKey: "automateDetail",
    promptKey: "automatePrompt",
  },
];

function formatRelativeTime(value: string | null | undefined, locale: string, referenceTimeMs = Date.now()): string | null {
  if (!value) return null;
  const time = Date.parse(value);
  if (!Number.isFinite(time)) return null;
  const deltaSeconds = Math.round((time - referenceTimeMs) / 1000);
  const formatter = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  if (Math.abs(deltaSeconds) < 60) return formatter.format(deltaSeconds, "second");
  const deltaMinutes = Math.round(deltaSeconds / 60);
  if (Math.abs(deltaMinutes) < 60) return formatter.format(deltaMinutes, "minute");
  const deltaHours = Math.round(deltaMinutes / 60);
  if (Math.abs(deltaHours) < 24) return formatter.format(deltaHours, "hour");
  return formatter.format(Math.round(deltaHours / 24), "day");
}

function activeTimestamp(item: HomeActiveItem): string | null {
  return item.updatedAt ?? item.startedAt;
}

function activeStatus(item: HomeActiveItem): "running" | "paused" | "cancelling" {
  if (item.engine === "matrix" && (item.nativeStatus === "paused" || item.nativeStatus === "cancelling")) {
    return item.nativeStatus;
  }
  return "running";
}

function isSnapshotDisabled(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404 && (error.code === "home_snapshot_disabled" || error.message === "home snapshot disabled");
}

function disabledSnapshot(workspaceId: string, workspaceName: string): HomeSnapshot {
  return {
    version: "home.snapshot.v1",
    generatedAt: new Date().toISOString(),
    scope: { workspaceId, workspaceName },
    attentionScope: "user",
    attention: [],
    active: [],
    outputs: [],
    counts: { attention: 0, active: 0, outputs: 0 },
    partial: true,
    issues: [{ source: "snapshot", code: "feature_disabled" }],
  };
}

/**
 * The `home.*` message key of the home page H1. The chat page passes the same text as its top-bar title so
 * the large-title pattern shows the title once (bar hidden while this H1 is on screen).
 */
export function homePageTitleKey({
  workspaceId,
  workspaceLoading,
  workspaceError = null,
}: {
  workspaceId: string | null;
  workspaceLoading: boolean;
  workspaceError?: unknown;
}): "title" | "noSpacePageTitle" {
  if (workspaceLoading) return "title";
  return workspaceError || !workspaceId ? "noSpacePageTitle" : "title";
}

function HomeSkeleton() {
  return (
    <div className="home-work-skeleton" aria-hidden="true">
      <div className="home-work-skeleton-line home-work-skeleton-line-short" />
      <div className="home-work-skeleton-line home-work-skeleton-line-title" />
      <div className="home-work-skeleton-grid">
        <div className="home-work-skeleton-panel" />
        <div className="home-work-skeleton-panel" />
      </div>
    </div>
  );
}

function AttentionRow({ item, referenceTime, onOpen }: { item: HomeAttentionItem; referenceTime: number; onOpen: (url: string) => void }) {
  const locale = useLocale();
  const t = useTranslations("home");
  const tn = useTranslations("notifications");
  const relative = formatRelativeTime(item.createdAt, locale, referenceTime);
  const copy = localizeNotification({ ...item, type: item.nativeType }, tn, locale);
  return (
    <button className="home-work-row home-work-row-attention" type="button" onClick={() => onOpen(item.actionUrl)}>
      <span className="home-work-row-icon" aria-hidden="true">
        <Bell />
      </span>
      <span className="home-work-row-copy">
        <span className="home-work-row-title">{copy.title}</span>
        {copy.body ? <span className="home-work-row-detail">{copy.body}</span> : null}
      </span>
      <span className="home-work-row-meta">{relative ?? t("open")}</span>
      <Arrow aria-hidden="true" />
    </button>
  );
}

function ActiveRow({ item, referenceTime, onOpen }: { item: HomeActiveItem; referenceTime: number; onOpen: (url: string) => void }) {
  const locale = useLocale();
  const t = useTranslations("home");
  const relative = formatRelativeTime(activeTimestamp(item), locale, referenceTime);
  const progress = item.engine === "matrix" ? item.progress : null;
  const percentage = progress && progress.total > 0 ? Math.min(100, Math.round((progress.completed / progress.total) * 100)) : 0;
  const Icon = item.engine === "routine" ? Bolt : item.engine === "matrix" ? Folder : Chat;
  const status = activeStatus(item);
  return (
    <button className="home-work-row home-work-row-active" type="button" onClick={() => onOpen(item.actionUrl)}>
      <span className={`home-work-row-icon home-work-engine-${item.engine}`} aria-hidden="true">
        <Icon />
      </span>
      <span className="home-work-row-copy">
        <span className="home-work-row-title">{item.title}</span>
        <span className="home-work-row-detail">
          {t(`engine.${item.engine}`)}
          {relative ? ` · ${relative}` : ""}
        </span>
        {progress ? (
          <span className="home-work-progress">
            <span className="home-work-progress-track" aria-hidden="true">
              <span className="home-work-progress-fill" style={{ width: `${percentage}%` }} />
            </span>
            <span>
              {progress.failed > 0
                ? t("matrixProgressWithFailures", {
                    completed: progress.completed,
                    total: progress.total,
                    failed: progress.failed,
                  })
                : t("matrixProgress", { completed: progress.completed, total: progress.total })}
            </span>
          </span>
        ) : null}
      </span>
      <span className={`home-work-live home-work-live-${status}`}>
        <span aria-hidden="true" />
        {t(`status.${status}`)}
      </span>
      <Arrow aria-hidden="true" />
    </button>
  );
}

function OutputRow({ item, referenceTime, onOpen }: { item: HomeOutputItem; referenceTime: number; onOpen: (url: string) => void }) {
  const locale = useLocale();
  const t = useTranslations("home");
  const relative = formatRelativeTime(item.updatedAt, locale, referenceTime);
  // An untitled report arrives with an empty title; use the report list's localized "<date> Report".
  const title = item.kind === "report" ? reportDisplayTitle({ title: item.title, first_section_title: null, created_at: item.createdAt, id: "" }) : item.title;
  return (
    <button className="home-work-row home-work-row-output" type="button" onClick={() => onOpen(item.actionUrl)}>
      <span className="home-work-row-icon" aria-hidden="true">
        <Doc />
      </span>
      <span className="home-work-row-copy">
        <span className="home-work-row-title">{title}</span>
        <span className="home-work-row-detail">
          {t(`output.${item.kind}`)}
          {item.format ? ` · ${item.format.toUpperCase()}` : ""}
          {relative ? ` · ${relative}` : ""}
        </span>
      </span>
      <Arrow aria-hidden="true" />
    </button>
  );
}

export function HomeWorkSurface({
  workspaceId,
  workspaceName,
  workspaceLoading,
  workspaceError = null,
  readOnly,
  onStartPrompt,
  onWorkspaceInvalidated,
  composerSlot,
}: HomeWorkSurfaceProps) {
  const locale = useLocale();
  const router = useAppRouter();
  const t = useTranslations("home");
  const { brand } = useBrand();
  const [snapshot, setSnapshot] = useState<HomeSnapshot | null>(null);
  const [clockAnchor, setClockAnchor] = useState<SnapshotClockAnchor | null>(null);
  const [clockNowMs, setClockNowMs] = useState(() => Date.now());
  const [proposalState, setProposalState] = useState<ProposalState | null>(null);
  const [proposalSettledScope, setProposalSettledScope] = useState<string | null>(null);
  const [unavailableScope, setUnavailableScope] = useState<string | null>(null);
  const requestRef = useRef<AbortController | null>(null);
  const proposalRequestRef = useRef<AbortController | null>(null);
  const proposalImpressionRef = useRef(new Set<string>());
  const invalidationRef = useRef(onWorkspaceInvalidated);
  invalidationRef.current = onWorkspaceInvalidated;
  // The space *name* resolves one render after its id (the picker shows
  // "（加载中）" first). It only labels the disabled-snapshot placeholder, so it
  // must not be a fetch dependency: as a dep it re-ran the whole load and a
  // returning browser — which already knows its space id — asked the gateway for
  // the same snapshot twice on every cold start.
  const workspaceNameRef = useRef(workspaceName);
  workspaceNameRef.current = workspaceName;
  const workspaceUnavailable = !workspaceLoading && Boolean(workspaceError);
  const noWorkspace = !workspaceLoading && !workspaceUnavailable && !workspaceId;
  // Only members without a space ever see the empty state, and only team
  // owners/admins can act on it — so the role lookup is scoped to that case
  // instead of running on every home render.
  const [canManageSpaces, setCanManageSpaces] = useState<boolean | null>(null);
  const [canRequestSpace, setCanRequestSpace] = useState(false);
  const [spaceRoleResolved, setSpaceRoleResolved] = useState(false);
  const [assignmentRequestState, setAssignmentRequestState] = useState<
    "idle" | "submitting" | "sent" | "error"
  >("idle");
  useEffect(() => {
    if (!noWorkspace) return;
    let cancelled = false;
    setSpaceRoleResolved(false);
    void fetchMe()
      .then((me) => {
        if (cancelled) return;
        const role = me.team_role || (me.is_team_admin ? "team_admin" : "member");
        setCanManageSpaces(TEAM_SETTINGS_ROLES.includes(role as SettingsTeamRole));
        setCanRequestSpace(me.capabilities?.space_admin_v2 === true);
        setSpaceRoleResolved(true);
      })
      // Unknown role keeps the CTA hidden: sending a member to a page that
      // rejects them is worse than asking them to contact an admin.
      .catch(() => {
        if (!cancelled) {
          setCanManageSpaces(false);
          setCanRequestSpace(false);
          setSpaceRoleResolved(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [noWorkspace]);

  const requestAssignment = async () => {
    if (assignmentRequestState === "submitting" || assignmentRequestState === "sent") return;
    setAssignmentRequestState("submitting");
    try {
      await requestSpaceAssignment();
      setAssignmentRequestState("sent");
    } catch {
      setAssignmentRequestState("error");
    }
  };
  const currentSnapshot = snapshot?.scope.workspaceId === workspaceId ? snapshot : null;
  const currentClockAnchor = clockAnchor?.workspaceId === workspaceId ? clockAnchor : null;
  const referenceTimeMs = currentClockAnchor
    ? currentClockAnchor.serverTimeMs + (clockNowMs - currentClockAnchor.receivedAtMs)
    : clockNowMs;
  const unavailable = unavailableScope === workspaceId;

  const loadSnapshot = useCallback(
    async (quiet = false) => {
      requestRef.current?.abort();
      if (!workspaceId) {
        setSnapshot(null);
        setClockAnchor(null);
        setUnavailableScope(null);
        return;
      }
      const controller = new AbortController();
      requestRef.current = controller;
      if (!quiet) {
        setSnapshot((current) => (current?.scope.workspaceId === workspaceId ? current : null));
        setUnavailableScope(null);
      }
      try {
        const result = await fetchHomeSnapshot(workspaceId, controller.signal);
        if (requestRef.current !== controller) return;
        if (!result?.scope || result.scope.workspaceId !== workspaceId) {
          throw new Error("home snapshot scope mismatch");
        }
        const receivedAtMs = Date.now();
        const serverTimeMs = Date.parse(result.generatedAt);
        setSnapshot(result);
        setClockAnchor(Number.isFinite(serverTimeMs)
          ? { workspaceId, serverTimeMs, receivedAtMs }
          : null);
        setClockNowMs(receivedAtMs);
        setUnavailableScope(null);
      } catch (error) {
        if (controller.signal.aborted || requestRef.current !== controller) return;
        if (isSnapshotDisabled(error)) {
          const result = disabledSnapshot(workspaceId, workspaceNameRef.current);
          const receivedAtMs = Date.now();
          setSnapshot(result);
          setClockAnchor({
            workspaceId,
            serverTimeMs: Date.parse(result.generatedAt),
            receivedAtMs,
          });
          setClockNowMs(receivedAtMs);
          setUnavailableScope(null);
          return;
        }
        if (error instanceof ApiError && error.status === 404 && invalidationRef.current) {
          setSnapshot((current) => (current?.scope.workspaceId === workspaceId ? null : current));
          setUnavailableScope(null);
          try {
            await invalidationRef.current();
            if (requestRef.current === controller) {
              setUnavailableScope(workspaceId);
            }
          } catch {
            setUnavailableScope(workspaceId);
          }
          return;
        }
        setUnavailableScope(workspaceId);
      }
    },
    [workspaceId],
  );

  useEffect(() => {
    const timer = window.setInterval(() => setClockNowMs(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    void loadSnapshot();
    const refresh = () => {
      if (document.visibilityState === "visible") void loadSnapshot(true);
    };
    const timer = window.setInterval(refresh, 45_000);
    window.addEventListener("focus", refresh);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", refresh);
      requestRef.current?.abort();
    };
  }, [loadSnapshot]);

  useEffect(() => {
    proposalRequestRef.current?.abort();
    if (!workspaceId) {
      setProposalState(null);
      setProposalSettledScope(null);
      return;
    }
    const controller = new AbortController();
    const scopeKey = `${workspaceId}:${locale}`;
    proposalRequestRef.current = controller;
    setProposalSettledScope(null);
    setProposalState((current) => (current?.workspaceId === workspaceId && current.locale === locale ? current : null));
    void fetchHomeProposals(workspaceId, locale, controller.signal)
      .then((result) => {
        if (controller.signal.aborted || proposalRequestRef.current !== controller) return;
        setProposalState({
          workspaceId,
          locale,
          generationId: result.generationId,
          strategy: result.strategy,
          items: result.items,
        });
      })
      .catch(() => {
        // Proposal is additive. Snapshot and the local fallback remain usable.
      })
      .finally(() => {
        if (!controller.signal.aborted && proposalRequestRef.current === controller) {
          setProposalSettledScope(scopeKey);
        }
      });
    return () => controller.abort();
  }, [locale, workspaceId]);

  const fallbackProposals = useMemo<DisplayProposal[]>(
    () =>
      WORK_PROPOSALS.map((proposal) => ({
        id: proposal.id,
        kind: proposal.kind,
        title: t(`proposal.${proposal.titleKey}`),
        detail: t(`proposal.${proposal.detailKey}`),
        prompt: t(`proposal.${proposal.promptKey}`),
      })),
    [t],
  );
  const currentProposalState = proposalState?.workspaceId === workspaceId && proposalState.locale === locale ? proposalState : null;
  const currentProposalScope = workspaceId ? `${workspaceId}:${locale}` : null;
  const displayProposals = useMemo<DisplayProposal[]>(
    () =>
      currentProposalState?.items.length
        ? currentProposalState.items.map((proposal) => ({
            id: proposal.id,
            kind: proposal.kind,
            title: proposal.title,
            detail: proposal.reason,
            prompt: proposal.prompt,
          }))
        : fallbackProposals,
    [currentProposalState, fallbackProposals],
  );
  const displayProposalStrategy = currentProposalState?.items.length ? currentProposalState.strategy : "local";
  const displayProposalGeneration = currentProposalState?.items.length ? currentProposalState.generationId : "local";

  const summary = useMemo(() => {
    if (workspaceUnavailable) return t("spaceListUnavailableSummary");
    if (noWorkspace) return t("noSpaceSummary");
    if (unavailable && !currentSnapshot) return t("unavailableSummary");
    if (!currentSnapshot) return t("summaryEmpty");
    const { attention, active, outputs } = currentSnapshot.counts;
    if (attention > 0) {
      return t.rich("summaryAttention", {
        count: attention,
        strong: (chunks) => <strong>{chunks}</strong>,
      });
    }
    if (active > 0) {
      return t.rich("summaryActive", {
        count: active,
        strong: (chunks) => <strong>{chunks}</strong>,
      });
    }
    if (outputs > 0) {
      return t.rich("summaryOutputs", {
        count: outputs,
        strong: (chunks) => <strong>{chunks}</strong>,
      });
    }
    if (currentSnapshot.partial) return t("partialSummary");
    return t("summaryEmpty");
  }, [currentSnapshot, noWorkspace, t, unavailable, workspaceUnavailable]);
  const summaryLabel = useMemo(() => {
    if (!currentSnapshot) return null;
    if (currentSnapshot.counts.attention > 0) return t("attention");
    if (currentSnapshot.counts.active > 0) return t("active");
    if (currentSnapshot.counts.outputs > 0) return t("outputs");
    return null;
  }, [currentSnapshot, t]);

  const openItem = (url: string) => {
    router.push(normalizeAppActionUrl(url));
  };
  const displayWorkspaceName = noWorkspace || workspaceUnavailable
    ? brand.applicationName
    : currentSnapshot?.scope.workspaceName || workspaceName || t("workspaceFallback");
  const pageTitle = t(homePageTitleKey({ workspaceId, workspaceLoading, workspaceError }));
  const showInitialLoading = workspaceLoading || Boolean(workspaceId && !currentSnapshot && !unavailable);
  const showInitialError = Boolean(workspaceId && unavailable && !currentSnapshot);
  const showUpdatedTime = Boolean(currentSnapshot && !showInitialLoading && !noWorkspace && !showInitialError);

  useEffect(() => {
    if (!workspaceId || proposalSettledScope !== currentProposalScope || showInitialLoading || noWorkspace) return;
    for (const proposal of displayProposals) {
      const key = `${workspaceId}:${displayProposalGeneration}:${proposal.id}`;
      if (proposalImpressionRef.current.has(key)) continue;
      proposalImpressionRef.current.add(key);
      track("bowhead_home_proposal_ui_total", {
        action: "impression",
        strategy: displayProposalStrategy,
        kind: proposal.kind,
        accepted: "na",
      });
    }
  }, [
    currentProposalScope,
    displayProposalGeneration,
    displayProposals,
    displayProposalStrategy,
    noWorkspace,
    proposalSettledScope,
    showInitialLoading,
    workspaceId,
  ]);

  const startProposal = async (proposal: DisplayProposal) => {
    let accepted = false;
    try {
      accepted = await onStartPrompt(proposal.prompt);
    } catch {
      accepted = false;
    } finally {
      trackInteraction("bowhead_home_proposal_ui_total", {
        action: "start",
        strategy: displayProposalStrategy,
        kind: proposal.kind,
        accepted: accepted ? "true" : "false",
      });
    }
  };

  // Single-column home: proposals precede work groups (Workbench Spec §1).
  // Each section has a key; reordering moves its own DOM rather than reusing nodes by position.
  const briefSection = (
    <Fragment key="brief">
      {showInitialError ? (
        <section className="home-work-state home-work-state-in-layout" aria-labelledby="home-unavailable-title">
          <span className="home-work-state-icon home-work-state-icon-error" aria-hidden="true">
            <AlertTriangle />
          </span>
          <div>
            <h2 id="home-unavailable-title">{t("unavailableTitle")}</h2>
            <p>{t("unavailableDetail")}</p>
            <button type="button" onClick={() => void loadSnapshot()}>
              {t("retry")}
              <RefreshCw aria-hidden="true" />
            </button>
          </div>
        </section>
      ) : (
        <section className="home-work-brief" aria-labelledby="home-work-brief-title">
          <div className="home-work-section-heading">
            <div>
              <h2 id="home-work-brief-title">{t("briefTitle")}</h2>
              <p>{t("briefDetail")}</p>
            </div>
          </div>

          {currentSnapshot?.attention.length ? (
            <div className="home-work-group home-work-group-attention">
              <div className="home-work-group-label">
                <span>{t("attention")}</span>
                <strong>{currentSnapshot.counts.attention}</strong>
              </div>
              <div className="home-work-rows">
                {currentSnapshot.attention.map((item) => (
                  <AttentionRow key={item.id} item={item} referenceTime={referenceTimeMs} onOpen={openItem} />
                ))}
              </div>
            </div>
          ) : null}

          {currentSnapshot?.active.length ? (
            <div className="home-work-group home-work-group-active">
              <div className="home-work-group-label">
                <span>{t("active")}</span>
                <strong>{currentSnapshot.counts.active}</strong>
              </div>
              <div className="home-work-rows">
                {currentSnapshot.active.map((item) => (
                  <ActiveRow key={item.id} item={item} referenceTime={referenceTimeMs} onOpen={openItem} />
                ))}
              </div>
            </div>
          ) : null}

          {currentSnapshot?.outputs.length ? (
            <div className="home-work-group home-work-group-output">
              <div className="home-work-group-label">
                <span>{t("outputs")}</span>
                <strong>{currentSnapshot.counts.outputs}</strong>
              </div>
              <div className="home-work-rows">
                {currentSnapshot.outputs.map((item) => (
                  <OutputRow key={item.id} item={item} referenceTime={referenceTimeMs} onOpen={openItem} />
                ))}
              </div>
            </div>
          ) : null}

          {currentSnapshot &&
          !currentSnapshot.partial &&
          currentSnapshot.counts.attention === 0 &&
          currentSnapshot.counts.active === 0 &&
          currentSnapshot.counts.outputs === 0 ? (
            <div className="home-work-clear">
              <Check aria-hidden="true" />
              <div>
                <strong>{t("clearTitle")}</strong>
                <p>{t("clearDetail")}</p>
              </div>
            </div>
          ) : null}

          {currentSnapshot?.partial || unavailable ? (
            <p className="home-work-partial" role="status">
              {t("partial")}
            </p>
          ) : null}
        </section>
      )}
    </Fragment>
  );
  const proposalsSection = (
    <section key="proposals" className="home-work-proposals" aria-labelledby="home-work-proposals-title">
      <div className="home-work-section-heading">
        <div>
          <h2 id="home-work-proposals-title">{t("proposalsTitle")}</h2>
          <p>{t("proposalsDetail")}</p>
        </div>
      </div>
      <div className="home-work-proposal-list">
        {displayProposals.map((proposal) => {
          const ProposalIcon = proposal.kind === "analyze" ? Spark : proposal.kind === "create" ? Doc : Bolt;
          return (
            <button
              className="home-work-proposal"
              type="button"
              key={proposal.id}
              disabled={readOnly || !workspaceId}
              onClick={() => void startProposal(proposal)}
              aria-label={t("startProposal", { title: proposal.title })}
            >
              <span className={`home-work-proposal-icon home-work-proposal-icon-${proposal.kind}`} aria-hidden="true">
                <ProposalIcon />
              </span>
              <span className="home-work-proposal-copy">
                <span className="home-work-proposal-title">{proposal.title}</span>
                <span className="home-work-proposal-detail">{proposal.detail}</span>
              </span>
              <span className="home-work-proposal-action" aria-hidden="true">
                <Arrow aria-hidden="true" />
              </span>
            </button>
          );
        })}
      </div>
      {readOnly && workspaceId ? (
        <p className="home-work-proposal-blocked" data-testid="home-proposals-blocked">
          {t("proposalsReadOnly")}
        </p>
      ) : null}
      <p className="home-work-proposal-note">
        <Info size={17} aria-hidden="true" focusable="false" />
        <span>{t("proposalNote")}</span>
      </p>
    </section>
  );

  const updatedTime = showUpdatedTime ? (
    <time className="home-work-updated" dateTime={currentSnapshot?.generatedAt}>
      <span aria-hidden="true" />
      {t("updated", {
        time: formatRelativeTime(currentSnapshot?.generatedAt, locale, referenceTimeMs) ?? t("justNow"),
      })}
    </time>
  ) : null;

  return (
    <div
      className={`home-work-surface${showInitialLoading ? " home-work-surface-loading" : ""}`}
      aria-label={t("ariaLabel")}
      aria-busy={showInitialLoading}
    >
      <div className="home-work-inner">
        {/* P2 batch 6 (brand appears once): the sidebar header is the only brand mark, so the home page has
            no watermark or logo + wordmark row and keeps only the updated time. */}
        {updatedTime ? <div className="home-work-brand home-work-brand--time-only">{updatedTime}</div> : null}

        <header className="home-work-header">
          <div>
            {noWorkspace || workspaceUnavailable ? null : (
              <p className="home-work-kicker">{displayWorkspaceName}</p>
            )}
            <h1>{pageTitle}</h1>
          </div>
          <div className="home-work-summary" role="status">
            {summaryLabel ? <span className="home-work-summary-label">{summaryLabel}</span> : null}
            <span className="home-work-summary-text">{summary}</span>
          </div>
        </header>

        {composerSlot ? <div className="home-work-composer-slot">{composerSlot}</div> : null}

        {showInitialLoading ? (
          <HomeSkeleton />
        ) : workspaceUnavailable ? (
          <section className="home-work-state" aria-labelledby="home-space-list-unavailable-title">
            <span className="home-work-state-icon home-work-state-icon-error" aria-hidden="true">
              <AlertTriangle />
            </span>
            <div>
              <h2 id="home-space-list-unavailable-title">{t("spaceListUnavailableTitle")}</h2>
              <p>{t("spaceListUnavailableDetail")}</p>
              <button type="button" onClick={() => void onWorkspaceInvalidated?.()}>
                {t("retry")}
                <RefreshCw aria-hidden="true" />
              </button>
            </div>
          </section>
        ) : noWorkspace ? (
          <section className="home-work-state" aria-labelledby="home-no-space-title">
            <span className="home-work-state-icon" aria-hidden="true">
              <Folder />
            </span>
            <div>
              <h2 id="home-no-space-title">{t("noSpaceTitle")}</h2>
              <p>{canManageSpaces === false ? t("noSpaceAskAdminDetail") : t("noSpaceDetail")}</p>
              {canManageSpaces ? (
                <button type="button" onClick={() => openItem("/team/settings/spaces/?from=no-space")}>
                  {t("manageSpaces")}
                  <Arrow aria-hidden="true" />
                </button>
              ) : canManageSpaces === false && spaceRoleResolved && canRequestSpace ? (
                <>
                  <button
                    type="button"
                    disabled={assignmentRequestState === "submitting" || assignmentRequestState === "sent"}
                    onClick={() => void requestAssignment()}
                  >
                    {assignmentRequestState === "submitting"
                      ? t("requestSpaceBusy")
                      : assignmentRequestState === "sent"
                        ? t("requestSpaceSent")
                        : t("requestSpace")}
                    <Bell aria-hidden="true" />
                  </button>
                  {assignmentRequestState === "error" ? (
                    <p role="alert">{t("requestSpaceFailed")}</p>
                  ) : null}
                </>
              ) : null}
            </div>
          </section>
        ) : (
          <div className="home-work-layout">
            {[proposalsSection, briefSection]}
          </div>
        )}

        {!showInitialLoading && !noWorkspace && !showInitialError ? (
          <footer className="home-work-footer">
            <span>{t("footerScope")}</span>
          </footer>
        ) : null}
      </div>
    </div>
  );
}
