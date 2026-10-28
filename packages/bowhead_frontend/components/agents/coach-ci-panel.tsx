"use client";

/**
 * Coach evaluation requests (Claude Design "Agent CI Spec" part 1, 3e; decisions D4–D7).
 *
 * Coach may only spend model requests on an evaluation after the Owner consents. The consent card
 * states what is evaluated, which versions are compared, the hard limits (model requests,
 * comparisons, run time) and the estimated usage (tokens, model requests, duration — never cost);
 * "Allow and start evaluation…" opens a confirmation that restates the limits and the deadline, and
 * only that confirmation authorizes. Stopping a running evaluation is a destructive confirmation.
 *
 * The panel is a collapsible card: right column from a 880 container, below the release check on
 * narrower containers; it opens by itself while a request awaits consent.
 */

import { forwardRef, useCallback, useEffect, useId, useRef, useState, type ForwardedRef } from "react";
import { useTranslations } from "next-intl";
import { Bot, ChevronDown, CircleCheck, CircleHelp, CirclePause, Clock, Gauge, History } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api-client";
import {
  authorizeCoachCI,
  cancelCoachCI,
  listCoachCI,
  startCoachCI,
  type CoachCIDelegation,
  type CoachCIPage,
} from "@/lib/coach-ci-api";
import type { AgentChangeProposal } from "@/lib/agents-api";
import { useSpaceSelection } from "@/lib/use-space";
import {
  CardStatus,
  useCiConfirm,
  CiCardHead,
  CiProgressBar,
  CiQuantities,
  CiSpinner,
  CopyDiagnosticsButton,
  useCiFormat,
  type CardStatusValue,
} from "./agent-ci-shared";

const ACTIVE = new Set(["queued", "running", "finalizing"]);
const COLLAPSE_KEY = "bowhead.agent_ci.coach_panel_collapsed";
const POLL_MS = 5000;

/** Which card a delegation shows (spec 3e, eight states). Pure for the state-machine tests. */
export type CoachRequestView =
  | "consent"
  | "start_authorized"
  | "running"
  | "completed"
  | "stopped"
  | "exhausted"
  | "unknown"
  | "expired";

export function coachRequestView(item: CoachCIDelegation): CoachRequestView {
  if (item.state === "unknown") return "unknown";
  if (item.state === "exhausted") return "exhausted";
  // Stopped by the Owner reads as stopped; expiry or a changed scope reads as no longer startable.
  if (item.state === "revoked") return !item.stop_reason || item.stop_reason === "coach_ci_owner_cancelled" ? "stopped" : "expired";
  if (item.state === "requested") return item.current_admission === "consent_required" ? "consent" : "expired";
  if (item.executions.some((execution) => ACTIVE.has(execution.status))) return "running";
  if (item.executions.length) return "completed";
  return item.current_admission === "current_db_authority" ? "start_authorized" : "expired";
}

function usedRequests(item: CoachCIDelegation): number {
  return Math.max(0, item.budget.max_provider_calls - item.remaining_provider_calls);
}

function usedTokens(item: CoachCIDelegation): number {
  return item.usage.reduce((total, row) => total + row.tokens_input + row.tokens_output, 0);
}

function readCollapsed(): boolean | null {
  try {
    const value = window.localStorage.getItem(COLLAPSE_KEY);
    return value === null ? null : value === "1";
  } catch {
    return null;
  }
}

function writeCollapsed(collapsed: boolean) {
  try {
    window.localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0");
  } catch {
    // Collapsing is a per-viewer convenience; nothing depends on it persisting.
  }
}

interface CoachPanelProps {
  agentId: string;
  liveSequence: number;
  /** Server-computed number the candidate would be released as (see candidateReleaseSequence). */
  candidateSequence?: number;
  proposals: AgentChangeProposal[];
  suites: Array<{ key: string; name: string; count: number }>;
  /** Controlled expansion; null = default (open while a request awaits consent). */
  expanded: boolean | null;
  onExpandedChange: (expanded: boolean) => void;
  onPendingChange: (count: number) => void;
  /** Whether the panel shows anything (requests exist); the page uses it to choose one or two columns. */
  onPresenceChange: (visible: boolean) => void;
  onViewResults?: () => void;
  onChanged: () => Promise<void>;
}

function CoachCIPanelInner({
  agentId,
  liveSequence,
  proposals,
  suites,
  expanded,
  onExpandedChange,
  onPendingChange,
  onPresenceChange,
  onViewResults,
  onChanged,
  candidateSequence = liveSequence + 1,
}: CoachPanelProps, ref: ForwardedRef<HTMLElement>) {
  const t = useTranslations("agents_marketplace");
  const format = useCiFormat();
  const { items: spaces } = useSpaceSelection();
  const { confirm, dialog } = useCiConfirm();
  const bodyId = useId();
  const [page, setPage] = useState<CoachCIPage | null>(null);
  const [offset, setOffset] = useState(0);
  const [busy, setBusy] = useState<string | null>(null);
  const [status, setStatus] = useState<{ id: string; value: CardStatusValue } | null>(null);
  const [disabled, setDisabled] = useState(false);
  const [showEarlier, setShowEarlier] = useState(false);
  const [storedCollapsed] = useState(readCollapsed);
  const generation = useRef(0);

  const load = useCallback(async () => {
    const current = ++generation.current;
    try {
      const next = await listCoachCI(agentId, offset);
      if (current !== generation.current) return;
      setPage(next);
      setDisabled(false);
    } catch (cause) {
      if (current !== generation.current) return;
      if (cause instanceof ApiError && [401, 403, 404].includes(cause.status)) {
        setPage(null);
        setDisabled(true);
      }
    }
  }, [agentId, offset]);

  useEffect(() => {
    void load();
    return () => {
      generation.current += 1;
    };
  }, [load]);

  const items = page?.items ?? [];
  const pending = items.filter((item) => coachRequestView(item) === "consent").length;
  const running = items.some((item) => coachRequestView(item) === "running");
  // The panel exists only while there are requests; with none, the release check keeps a single column.
  const visible = !disabled && page !== null && (items.length > 0 || offset > 0);
  useEffect(() => {
    onPendingChange(disabled ? 0 : pending);
  }, [disabled, onPendingChange, pending]);
  useEffect(() => {
    onPresenceChange(visible);
  }, [onPresenceChange, visible]);

  // A running evaluation refreshes its progress; the numbers come from committed rows (polling, no events).
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [load, running]);

  // Open by itself whenever a request needs the Owner (consent, a start to resume, a run, an outcome to check).
  const needsOwner = items.some((item) => ["consent", "start_authorized", "running", "unknown"].includes(coachRequestView(item)));
  const open = expanded ?? (needsOwner || storedCollapsed === false);
  const toggle = () => {
    writeCollapsed(open);
    onExpandedChange(!open);
  };

  if (!visible) return null;

  const act = async (item: CoachCIDelegation, mode: "start" | "stop") => {
    if (busy) return;
    setBusy(item.delegation_id);
    setStatus(null);
    try {
      if (mode === "stop") await cancelCoachCI(agentId, item.delegation_id);
      else {
        if (item.state === "requested") await authorizeCoachCI(agentId, item.delegation_id, item.row_version);
        await startCoachCI(agentId, item.delegation_id);
      }
      if (mode === "stop") setStatus({ id: item.delegation_id, value: { tone: "success", message: t("coach_ci_stopped_inline") } });
      await onChanged();
    } catch (cause) {
      const known = cause instanceof ApiError && cause.status < 500;
      setStatus({
        id: item.delegation_id,
        value: { tone: "error", message: t(known ? "coach_ci_blocked" : "coach_ci_recovery"), cause, onRetry: known ? undefined : () => void load() },
      });
    } finally {
      await load();
      setBusy(null);
    }
  };

  const estimateText = (item: CoachCIDelegation): string => {
    const per = item.estimate?.status === "available" ? item.estimate.per_comparison : null;
    if (!per || !(per.tokens || per.model_calls || per.duration_ms)) return t("coach_ci_no_estimate");
    return t("version_about", {
      value: [
        per.tokens ? t("version_usage_tokens", { value: format.tokenRange(per.tokens) }) : null,
        per.model_calls ? t("version_usage_requests", { value: format.countRange(per.model_calls) }) : null,
        per.duration_ms ? format.minuteRange(per.duration_ms) : null,
      ].filter(Boolean).join(t("version_list_separator")),
    });
  };

  const limitRows = (item: CoachCIDelegation, deadline?: Date) => [
    { key: "calls", label: t("coach_ci_limit_requests"), value: t("coach_ci_times", { value: format.count(item.budget.max_provider_calls) }) },
    ...(deadline ? [] : [{ key: "comparisons", label: t("coach_ci_limit_comparisons"), value: t("coach_ci_times", { value: format.count(item.budget.max_comparisons) }) }]),
    deadline
      ? { key: "duration", label: t("coach_ci_limit_longest"), value: t("coach_ci_until", { duration: format.minutes(item.budget.duration_seconds * 1000), time: format.time(deadline) }) }
      : { key: "duration", label: t("coach_ci_limit_duration"), value: format.minutes(item.budget.duration_seconds * 1000) },
    ...(item.estimate?.tokens_ceiling_at_cap ? [{ key: "tokens", label: t("coach_ci_limit_tokens"), value: t("version_about", { value: format.tokens(item.estimate.tokens_ceiling_at_cap) }) }] : []),
    { key: "estimate", label: t("coach_ci_estimate"), value: estimateText(item) },
  ];

  const allow = async (item: CoachCIDelegation) => {
    const deadline = new Date(Date.now() + item.budget.duration_seconds * 1000);
    if (!await confirm({
      title: t("coach_ci_confirm_title"),
      description: <>
        <p>{t("coach_ci_confirm_body", { candidate: `v${candidateSequence}`, live: `v${liveSequence}` })}</p>
        <CiQuantities rows={limitRows(item, deadline)} />
        <p>{t("coach_ci_confirm_note")}</p>
      </>,
      confirmLabel: t("coach_ci_confirm_allow"),
      tone: "primary",
    })) return;
    void act(item, "start");
  };

  const stop = async (item: CoachCIDelegation) => {
    const tokens = usedTokens(item);
    if (!await confirm({
      title: t("coach_ci_stop_title"),
      description: t("coach_ci_stop_body", {
        usage: [tokens ? t("version_usage_tokens", { value: format.tokens(tokens) }) : null, t("version_usage_requests", { value: format.count(usedRequests(item)) })].filter(Boolean).join(t("version_list_comma")),
      }),
      cancelLabel: t("coach_ci_stop_keep"),
      confirmLabel: t("coach_ci_stop_confirm"),
      tone: "danger",
    })) return;
    void act(item, "stop");
  };

  const changeText = (item: CoachCIDelegation): string => {
    const proposal = proposals.find((entry) => entry.proposal_id === item.proposal_id);
    const name = proposal?.rationale.split("\n")[0]?.trim().slice(0, 60);
    if (!proposal) return t("coach_ci_change_fallback");
    return name
      ? t("coach_ci_change_named", { name, count: proposal.operations.length })
      : t("coach_ci_change_unnamed", { count: proposal.operations.length });
  };

  const renderCard = (item: CoachCIDelegation) => {
    const view = coachRequestView(item);
    const itemBusy = busy === item.delegation_id;
    const cardStatus = status?.id === item.delegation_id ? status.value : null;
    const titleId = `${bodyId}-${item.delegation_id}`;
    const spaceName = spaces.find((space) => space.id === item.workspace_id)?.name;
    const usage = [
      t("coach_ci_used_requests", { used: format.count(usedRequests(item)), limit: format.count(item.budget.max_provider_calls) }),
      usedTokens(item) ? t("version_usage_tokens", { value: format.tokens(usedTokens(item)) }) : null,
    ].filter(Boolean).join(t("version_list_comma"));
    const diagnostics = () => ({
      delegation_id: item.delegation_id,
      proposal_id: item.proposal_id,
      workspace_id: item.workspace_id,
      state: item.state,
      current_admission: item.current_admission,
      stop_reason: item.stop_reason,
      executions: item.executions.map((execution) => `${execution.id}:${execution.status}`),
    });
    let body;
    switch (view) {
      case "consent":
      case "start_authorized": {
        const recovering = view === "start_authorized";
        body = <>
          <CiCardHead
            titleId={titleId}
            title={t("coach_ci_consent_title")}
            badge={<Badge tone={recovering ? "accent" : "warning"}>{t(recovering ? "coach_ci_badge_authorized" : "coach_ci_badge_awaiting")}</Badge>}
          />
          <dl className="am-ci-kv">
            <dt>{t("coach_ci_field_change")}</dt><dd>{changeText(item)}</dd>
            <dt>{t("coach_ci_field_compare")}</dt><dd>{t("coach_ci_compare_value", { candidate: `v${candidateSequence}`, live: `v${liveSequence}` })}</dd>
            {suites.length ? <><dt>{t("coach_ci_field_suites")}</dt><dd>{suites.map((suite) => t("coach_ci_suite_value", { name: suite.name, count: suite.count })).join(t("version_list_semicolon"))}</dd></> : null}
            {spaceName ? <><dt>{t("coach_ci_field_space")}</dt><dd>{spaceName}</dd></> : null}
          </dl>
          <CiQuantities
            label={t("coach_ci_limits_label")}
            rows={[
              ...limitRows(item),
              ...(recovering && item.expires_at ? [{ key: "expires", label: t("coach_ci_consent_expires"), value: format.time(item.expires_at) }] : []),
            ]}
          />
          {item.estimate?.may_exhaust_call_cap ? <p className="am-ci-note" data-tone="warning">{t("coach_ci_may_exhaust")}</p> : null}
          <p className="am-ci-note">{t("coach_ci_isolated_note")}</p>
          <div className="am-ci-acts" data-dock="primary">
            {recovering ? (
              <Button variant="secondary" className="settings-button--destructive-text" disabled={busy !== null} onClick={() => void stop(item)}>{t("coach_ci_stop")}</Button>
            ) : (
              <Button variant="secondary" disabled={busy !== null} onClick={() => { writeCollapsed(true); onExpandedChange(false); }}>{t("coach_ci_later")}</Button>
            )}
            <span className="am-ci-sp" />
            <Button variant="primary" disabled={busy !== null} loading={itemBusy} onClick={() => (recovering ? void act(item, "start") : void allow(item))}>
              {t(recovering ? "coach_ci_start_authorized" : "coach_ci_allow")}
            </Button>
          </div>
        </>;
        break;
      }
      case "running": {
        const execution = item.executions.find((entry) => ACTIVE.has(entry.status));
        const progress = execution?.progress;
        const calls = progress?.model_calls.delegation_used ?? usedRequests(item);
        const tokens = progress?.usage.tokens ?? usedTokens(item);
        const remaining = item.expires_at ? new Date(item.expires_at).valueOf() - Date.now() : null;
        body = <>
          <CiCardHead
            titleId={titleId}
            icon={<CiSpinner />}
            tone="accent"
            title={t("coach_ci_running_title")}
            body={progress ? t("coach_ci_running_body", { completed: Math.min(progress.cases.total, progress.cases.completed + 1), total: progress.cases.total }) : undefined}
            badge={<Badge tone="accent">{t("coach_ci_badge_running")}</Badge>}
          />
          <CiQuantities
            label={t("coach_ci_progress_label")}
            rows={[
              {
                key: "calls",
                label: t("coach_ci_requests"),
                value: t("version_fraction", { done: format.count(calls), total: format.count(item.budget.max_provider_calls) }),
                bar: <CiProgressBar value={calls} max={item.budget.max_provider_calls} label={t("coach_ci_requests_label", { used: calls, limit: item.budget.max_provider_calls })} />,
              },
              ...(tokens ? [{
                key: "tokens",
                label: t("coach_ci_tokens"),
                value: item.estimate?.tokens_ceiling_at_cap
                  ? t("version_fraction", { done: format.tokens(tokens), total: format.tokens(item.estimate.tokens_ceiling_at_cap) })
                  : format.tokens(tokens),
              }] : []),
              { key: "comparisons", label: t("coach_ci_comparisons"), value: t("version_fraction", { done: format.count(item.executions.length), total: format.count(item.budget.max_comparisons) }) },
              ...(remaining !== null && remaining > 0 ? [{ key: "remaining", label: t("coach_ci_time_left"), value: format.elapsed(remaining) }] : []),
            ]}
          />
          <div className="am-ci-acts">
            <Button variant="secondary" className="settings-button--destructive-text" disabled={busy !== null} loading={itemBusy} onClick={() => void stop(item)}>{t("coach_ci_stop")}</Button>
          </div>
        </>;
        break;
      }
      case "completed":
        body = <>
          <CiCardHead titleId={titleId} icon={<CircleCheck size={16} />} tone="positive" title={t("coach_ci_completed_title")} body={t("coach_ci_completed_body", { usage })} badge={<Badge tone="positive">{t("coach_ci_badge_completed")}</Badge>} />
          {onViewResults ? <div className="am-ci-acts"><Button variant="secondary" onClick={onViewResults}>{t("coach_ci_view_results")}</Button></div> : null}
        </>;
        break;
      case "stopped":
        body = <CiCardHead titleId={titleId} icon={<CirclePause size={16} />} title={t("coach_ci_stopped_title")} body={t("coach_ci_stopped_body", { usage })} badge={<Badge tone="neutral">{t("coach_ci_badge_stopped")}</Badge>} />;
        break;
      case "exhausted":
        body = <>
          <CiCardHead titleId={titleId} icon={<Gauge size={16} />} tone="warning" title={t("coach_ci_exhausted_title")} body={t("coach_ci_exhausted_body", { limit: format.count(item.budget.max_provider_calls) })} badge={<Badge tone="warning">{t("coach_ci_badge_exhausted")}</Badge>} />
          <CiQuantities rows={[{
            key: "calls",
            label: t("coach_ci_requests"),
            value: t("version_fraction", { done: format.count(usedRequests(item)), total: format.count(item.budget.max_provider_calls) }),
            bar: <CiProgressBar tone="warning" value={usedRequests(item)} max={item.budget.max_provider_calls} label={t("coach_ci_requests_label", { used: usedRequests(item), limit: item.budget.max_provider_calls })} />,
          }]} />
        </>;
        break;
      case "unknown":
        body = <>
          <CiCardHead titleId={titleId} icon={<CircleHelp size={16} />} tone="warning" title={t("coach_ci_unknown_title")} body={t("coach_ci_unknown_body")} badge={<Badge tone="warning">{t("coach_ci_badge_unknown")}</Badge>} />
          <div className="am-ci-acts">
            <Button variant="secondary" disabled={busy !== null} onClick={() => void load()}>{t("coach_ci_refresh_status")}</Button>
            <CopyDiagnosticsButton record={diagnostics} />
          </div>
        </>;
        break;
      case "expired":
        body = <CiCardHead titleId={titleId} icon={<Clock size={16} />} title={t("coach_ci_expired_title")} body={t("coach_ci_expired_body")} badge={<Badge tone="neutral">{t("coach_ci_badge_expired")}</Badge>} />;
        break;
    }
    return (
      <section key={item.delegation_id} className="am-ci-card" data-coach-view={view} data-attention={view === "consent" || undefined} role="group" aria-labelledby={titleId} aria-busy={itemBusy || undefined}>
        {body}
        <CardStatus status={cardStatus} />
      </section>
    );
  };

  const current = items.filter((item) => ["consent", "start_authorized", "running", "unknown"].includes(coachRequestView(item)));
  const shown = current.length ? current : items.slice(0, 1);
  const earlier = items.filter((item) => !shown.includes(item));

  return (
    <section ref={ref} tabIndex={-1} className="am-ci-coll" aria-labelledby={`${bodyId}-title`}>
      {dialog}
      <button type="button" className="am-ci-collh" aria-expanded={open} aria-controls={bodyId} onClick={toggle}>
        <Bot size={16} aria-hidden className="am-ci-c-mut" />
        <span id={`${bodyId}-title`}>{t("coach_ci_title")}</span>
        {pending ? <Badge tone="warning">{t("coach_ci_pending_count", { count: pending })}</Badge>
          : running ? <Badge tone="accent">{t("coach_ci_badge_running")}</Badge>
            : items.length ? <span className="am-ci-cnt">{format.count(items.length)}</span> : null}
        <span className="am-ci-sp" />
        <ChevronDown size={16} aria-hidden className="am-ci-chev" data-open={open || undefined} />
      </button>
      <div id={bodyId} className="am-ci-collb" hidden={!open}>
        {shown.map(renderCard)}
        {earlier.length ? (
          <div className="am-ci-irow">
            <History size={14} aria-hidden />
            <span>{t("coach_ci_earlier", { count: earlier.length })}</span>
            <span className="am-ci-sp" />
            <Button size="sm" variant="ghost" aria-expanded={showEarlier} onClick={() => setShowEarlier((value) => !value)}>{t(showEarlier ? "coach_ci_hide_earlier" : "coach_ci_view_earlier")}</Button>
          </div>
        ) : null}
        {showEarlier ? earlier.map(renderCard) : null}
        {offset > 0 || (page?.next_offset ?? null) !== null ? (
          <div className="am-ci-acts">
            {offset > 0 ? <Button size="sm" variant="ghost" disabled={busy !== null} onClick={() => setOffset(Math.max(0, offset - 10))}>{t("coach_ci_previous")}</Button> : null}
            {page?.next_offset !== null && page?.next_offset !== undefined ? <Button size="sm" variant="ghost" disabled={busy !== null} onClick={() => setOffset(page.next_offset!)}>{t("coach_ci_next")}</Button> : null}
          </div>
        ) : null}
      </div>
    </section>
  );
}

export const CoachCIPanel = forwardRef(CoachCIPanelInner);

