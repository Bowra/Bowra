"use client";

/**
 * SummaryCard — admin credits overview card.
 *
 * Shows: team pool / last 30d usage / active users / perspective dropdown (three views).
 * Three states: loading shows skeleton / empty shows guidance / error shows retry button.
 */

import { useState } from "react";
import { useTranslations } from "next-intl";

import {
  AdminCreditsError,
  type CreditActivationAttentionItem,
  type CreditSummary,
} from "@/lib/admin-credits-api";
import { adminCreditsErrorKey } from "@/lib/admin-credits-error";
import { useAppRouter } from "@/lib/use-app-router";
import { toast } from "@/lib/use-toast";
import { DialogShell } from "@/components/ui/dialog";
import { CharIcon } from "@/components/char-icon";

// Compact number formatting: 1234567 → "1.2M", 1234 → "1.2k", without Intl.NumberFormat
// (locale consistency). Matches KPI style.
function fmt(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${Math.round(n / 1_000)}k`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return n.toLocaleString();
}

type ViewMode = "attention" | "top_users" | "top_workspaces";

interface Props {
  summary: CreditSummary | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  onAttentionStateChange?: (
    item: CreditActivationAttentionItem,
    state: "dismissed",
    reason?: string,
  ) => Promise<void> | void;
  onAttentionAction?: (
    item: CreditActivationAttentionItem,
    actionKey: string,
  ) => Promise<void> | void;
}

export function SummaryCard({
  summary,
  loading,
  error,
  onRetry,
  onAttentionStateChange,
  onAttentionAction,
}: Props) {
  const t = useTranslations("admin.credits");
  const [view, setView] = useState<ViewMode>("attention");

  if (loading) {
    return (
      <section className="legacy-credits-card">
        <SkeletonStripe />
        <SkeletonStripe />
      </section>
    );
  }

  if (error) {
    return (
      <section className="legacy-credits-card">
        <div className="legacy-credits-summary-error">
          {error}
        </div>
        <button
          type="button"
          onClick={onRetry}
          className="ui-btn legacy-credits-summary-retry"
        >
          {t("error_retry")}
        </button>
      </section>
    );
  }

  if (!summary) {
    return (
      <section className="legacy-credits-card">
        <div className="legacy-credits-summary-empty">
          {t("empty_summary")}
        </div>
      </section>
    );
  }

  const usedPct = summary.team.quota > 0
    ? Math.min(100, Math.round((1 - summary.team.balance / summary.team.quota) * 100))
    : 0;
  const poolTone: StatTone = usedPct >= 95
    ? "err"
    : usedPct >= 80
    ? "warn"
    : "accent";

  return (
    <section className="legacy-credits-card credits-summary">
      <div className="legacy-credits-stats">
        <Stat
          label={t("team_pool")}
          value={`${fmt(summary.team.balance)} / ${fmt(summary.team.quota)} ⚡`}
          subValue={`${usedPct}% used`}
          tone={poolTone}
          progress={usedPct}
        />
        <Stat
          label={t("last_30d_spend")}
          value={`${fmt(summary.last_30d.spend)} ⚡`}
          subValue={`${summary.last_30d.turns} turns · ${fmt(summary.last_30d.tokens_in + summary.last_30d.tokens_out)} tokens`}
        />
        <Stat
          label={t("active_users_7d")}
          value={`${summary.active_users_7d} / ${summary.total_users_active}`}
          subValue=""
        />
      </div>

      <div className="legacy-credits-views">
        <div className="legacy-credits-views-head">
          <span className="legacy-credits-label" id="credits-view-by-label">
            {t("attention_view_by")}
          </span>
          <select
            value={view}
            onChange={(e) => setView(e.target.value as ViewMode)}
            aria-labelledby="credits-view-by-label"
            className="ui-input legacy-credits-view-select"
          >
            <option value="attention">{t("view_attention")}</option>
            <option value="top_users">{t("view_top_users")}</option>
            <option value="top_workspaces">{t("view_top_workspaces")}</option>
          </select>
        </div>
        {view === "attention" && (
          (summary.activation_attention?.length ?? 0) > 0 ? (
            <ActivationAttentionList
              items={summary.activation_attention}
              onStateChange={onAttentionStateChange}
              onAction={onAttentionAction}
            />
          ) : (
            <AttentionList items={summary.view_attention} />
          )
        )}
        {view === "top_users" && <TopUserList items={summary.view_top_users} />}
        {view === "top_workspaces" && <TopWorkspaceList items={summary.view_top_workspaces} />}
      </div>
    </section>
  );
}

// Move original inline styles to legacy classes (styles/ui-controls-legacy.css “逐页验收 B”); replace usage-derived colors with tone modifiers.
type StatTone = "err" | "warn" | "accent";

function Stat({ label, value, subValue, tone, progress }: {
  label: string; value: string; subValue?: string;
  tone?: StatTone; progress?: number;
}) {
  return (
    <div className={`legacy-credits-stat${tone ? ` is-${tone}` : ""}`}>
      <div className="legacy-credits-label legacy-credits-stat-label">
        {label}
      </div>
      <div className="legacy-credits-stat-value">
        {value}
      </div>
      {subValue ? (
        <div className="legacy-credits-stat-sub">
          {subValue}
        </div>
      ) : null}
      {progress !== undefined ? (
        <div className="legacy-credits-stat-track">
          <div
            className="legacy-credits-stat-bar"
            style={{ width: `${Math.min(100, progress)}%` }}
          />
        </div>
      ) : null}
    </div>
  );
}

function SkeletonStripe() {
  return <div className="legacy-credits-skeleton-stripe" />;
}

function ActivationAttentionList({
  items,
  onStateChange,
  onAction,
}: {
  items: CreditActivationAttentionItem[];
  onStateChange?: Props["onAttentionStateChange"];
  onAction?: Props["onAttentionAction"];
}) {
  const t = useTranslations("admin.credits");
  const router = useAppRouter();
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [dismissTarget, setDismissTarget] = useState<CreditActivationAttentionItem | null>(null);

  if (items.length === 0) {
    return <div className="legacy-credits-dash">—</div>;
  }

  const runAction = async (item: CreditActivationAttentionItem, actionKey: string) => {
    if (!onAction) return;
    if (actionKey === "dismiss_attention_item") {
      setDismissTarget(item);
      return;
    }
    const key = `${item.attention_item_id}:action:${actionKey}`;
    setBusyKey(key);
    try {
      await onAction(item, actionKey);
    } catch (e: unknown) {
      toast({
        kind: "error",
        text: e instanceof AdminCreditsError
          ? t(adminCreditsErrorKey(e))
          : t("attention_action_unavailable"),
      });
    } finally {
      setBusyKey(null);
    }
  };

  const runDismiss = async (reason: string) => {
    if (!dismissTarget || !onStateChange) return;
    const item = dismissTarget;
    const key = `${item.attention_item_id}:dismissed`;
    setBusyKey(key);
    try {
      await onStateChange(item, "dismissed", reason);
      setDismissTarget(null);
    } catch (e: unknown) {
      toast({
        kind: "error",
        text: e instanceof AdminCreditsError
          ? t(adminCreditsErrorKey(e))
          : t("err_generic"),
      });
    } finally {
      setBusyKey(null);
    }
  };

  return (
    <>
      <ul className="legacy-credits-list">
      {items.map((item) => {
        const subject = attentionSubject(item);
        const recommended = item.recommended_action;
        const actionAllowed = recommended?.allowed ?? false;
        const actionLabel = attentionActionLabel(
          t,
          recommended?.key ?? item.recommended_action_key,
          recommended?.label ?? item.recommended_action_key,
        );
        const disabledLabel = attentionDisabledLabel(t, recommended?.disabled_reason);
        return (
          <li key={item.attention_item_id} className="legacy-credits-attn-item">
            <div className="legacy-credits-min0">
              <div className="legacy-credits-attn-subject-row">
                <SeverityBadge severity={item.severity} />
                <span className="legacy-credits-mono legacy-credits-ellipsis">
                  {subject}
                </span>
              </div>
              <div className="legacy-credits-attn-sub">
                {attentionPolicyLabel(t, item.policy_key)} · {formatEvidence(item, t)}
              </div>
            </div>
            <div className="legacy-credits-min0">
              <div className="legacy-credits-attn-reason legacy-credits-ellipsis">
                {item.attention_reason || item.action_intent?.reason || "—"}
              </div>
              <div className="legacy-credits-attn-sub">
                {t("attention_recommended")}: {actionLabel}
                {!actionAllowed && disabledLabel
                  ? ` · ${disabledLabel}`
                  : ""}
              </div>
            </div>
            <div className="legacy-credits-attn-actions">
              {recommended ? (
                <AttentionButton
                  disabled={
                    !actionAllowed ||
                    !onAction ||
                    busyKey === `${item.attention_item_id}:action:${recommended.key}`
                  }
                  variant="primary"
                  onClick={() => { void runAction(item, recommended.key); }}
                >
                  {busyKey === `${item.attention_item_id}:action:${recommended.key}`
                    ? "..."
                    : actionLabel}
                </AttentionButton>
              ) : null}
              {item.user_id ? (
                <AttentionButton
                  onClick={() => router.push(`/admin/credits/users?user_id=${encodeURIComponent(item.user_id || "")}`)}
                >
                  {t("attention_open_user")}
                </AttentionButton>
              ) : null}
              <AttentionButton
                disabled={!onStateChange || busyKey === `${item.attention_item_id}:dismissed`}
                onClick={() => setDismissTarget(item)}
              >
                {busyKey === `${item.attention_item_id}:dismissed` ? "..." : t("attention_dismiss")}
              </AttentionButton>
            </div>
          </li>
        );
      })}
      </ul>
      {dismissTarget ? (
        <DismissAttentionModal
          busy={busyKey === `${dismissTarget.attention_item_id}:dismissed`}
          onCancel={() => setDismissTarget(null)}
          onDismiss={(reason) => { void runDismiss(reason); }}
        />
      ) : null}
    </>
  );
    }

function AttentionList({ items }: { items: CreditSummary["view_attention"] }) {
  const t = useTranslations("admin.credits");
  if (items.length === 0) {
    return <div className="legacy-credits-dash">—</div>;
  }
  return (
    <ul className="legacy-credits-list">
      {items.map((u) => (
        <li key={u.user_id} className="legacy-credits-list-item legacy-credits-list-item--center">
          <span className="legacy-credits-mono">{u.email}</span>
          <span className="legacy-credits-list-meta">
            balance {Math.round(u.balance_pct * 100)}%
            {u.flags.includes("growth_spike") && ` · 7d ↑${Math.round(u.growth_7d * 100)}%`}
            {u.flags.includes("new_user") && " · new"}
          </span>
        </li>
      ))}
    </ul>
  );
}

function DismissAttentionModal({
  busy,
  onCancel,
  onDismiss,
}: {
  busy: boolean;
  onCancel: () => void;
  onDismiss: (reason: string) => void;
}) {
  const t = useTranslations("admin.credits");
  const [reason, setReason] = useState("watch_later");
  return (
    <DialogShell
      onClose={onCancel}
      busy={busy}
      label={t("attention_dismiss_title")}
      backdropStyle={{
        position: "fixed",
        inset: 0,
        zIndex: 60,
        background: "color-mix(in srgb, var(--c-ink) 28%, transparent)",
        display: "grid",
        placeItems: "center",
        padding: 16,
      }}
      panelStyle={{
        width: "min(420px, 100%)",
        background: "var(--c-surface)",
        color: "var(--c-ink-1)",
        border: "1px solid var(--c-line)",
        borderRadius: 8,
        boxShadow: "0 18px 42px rgba(0,0,0,0.22)",
        padding: 16,
      }}
    >
      <h3 className="legacy-credits-dismiss-title">
        {t("attention_dismiss_title")}
      </h3>
      <label className="legacy-credits-dismiss-field">
        {t("attention_dismiss_reason")}
        <select
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          className="ui-input legacy-credits-dismiss-select"
        >
          <option value="watch_later">{t("attention_dismiss_reason_watch_later")}</option>
          <option value="handled_offline">{t("attention_dismiss_reason_handled_offline")}</option>
          <option value="expected_demo">{t("attention_dismiss_reason_expected_demo")}</option>
          <option value="not_relevant">{t("attention_dismiss_reason_not_relevant")}</option>
        </select>
      </label>
      <div className="legacy-credits-dismiss-foot">
        <AttentionButton disabled={busy} onClick={onCancel}>
          {t("attention_dismiss_cancel")}
        </AttentionButton>
        <AttentionButton
          disabled={busy}
          variant="primary"
          onClick={() => onDismiss(reason)}
        >
          {busy ? "..." : t("attention_dismiss_confirm")}
        </AttentionButton>
      </div>
    </DialogShell>
  );
}

function SeverityBadge({ severity }: { severity: CreditActivationAttentionItem["severity"] }) {
  const t = useTranslations("admin.credits");
  const label = severity === "critical"
    ? t("attention_severity_critical")
    : severity === "warning"
    ? t("attention_severity_warning")
    : t("attention_severity_info");
  const tone = severity === "critical"
    ? "is-critical"
    : severity === "warning"
    ? "is-warning"
    : "is-info";
  return (
    <span className={`legacy-credits-severity ${tone}`}>
      {label}
    </span>
  );
}

function AttentionButton({
  disabled,
  variant = "secondary",
  onClick,
  children,
}: {
  disabled?: boolean;
  variant?: "primary" | "secondary";
  onClick: () => void;
  children: React.ReactNode;
}) {
  const primary = variant === "primary";
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={`ui-btn${primary ? " ui-btn--primary" : ""} legacy-credits-attn-btn${primary ? " is-primary" : ""}`}
    >
      {children}
    </button>
  );
}

function attentionSubject(item: CreditActivationAttentionItem): string {
  const evidence = item.evidence ?? {};
  const email = typeof evidence.email === "string" ? evidence.email : item.email;
  if (email) return email;
  if (item.user_id) return item.user_id;
  if (item.workspace_id) return item.workspace_id;
  return item.team_id;
}

function attentionActionLabel(
  t: (key: string) => string,
  actionKey: string | null | undefined,
  fallback: string,
): string {
  switch (actionKey) {
    case "grant_credit":
      return t("attention_action_grant_credit");
    case "edit_user_quota":
      return t("attention_action_edit_user_quota");
    case "edit_user_tier":
      return t("attention_action_edit_user_tier");
    case "period_reset":
      return t("attention_action_period_reset");
    case "increase_team_quota":
    case "increase_team_quota_or_renew":
      return t("attention_action_increase_team_quota");
    case "open_billing":
      return t("attention_action_open_billing");
    case "dismiss_attention_item":
      return t("attention_action_dismiss_attention_item");
    case "view_ledger":
      return t("attention_action_view_ledger");
    default:
      return fallback;
  }
}

function attentionDisabledLabel(
  t: (key: string) => string,
  reason: string | null | undefined,
): string {
  switch (reason) {
    case "insufficient_capability":
      return t("attention_disabled_capability");
    case "team_pool_critical":
      return t("attention_disabled_team_pool");
    case "unknown_action":
      return t("attention_disabled_unknown_action");
    default:
      return reason ?? "";
  }
}

function attentionPolicyLabel(t: (key: string) => string, policyKey: string): string {
  switch (policyKey) {
    case "low_balance_power_user":
      return t("attention_policy_low_balance_power_user");
    case "critical_balance_active_user":
      return t("attention_policy_critical_balance_active_user");
    case "blocked_valuable_user":
      return t("attention_policy_blocked_valuable_user");
    case "growth_spike":
      return t("attention_policy_growth_spike");
    case "new_user_activated":
      return t("attention_policy_new_user_activated");
    case "team_pool_critical":
      return t("attention_policy_team_pool_critical");
    case "team_pool_warning":
      return t("attention_policy_team_pool_warning");
    default:
      return policyKey;
  }
}

function formatEvidence(
  item: CreditActivationAttentionItem,
  t: (key: string, values?: Record<string, string | number>) => string,
): string {
  const evidence = item.evidence ?? {};
  const balancePct = typeof evidence.balance_pct === "number"
    ? t("attention_evidence_balance_pct", { pct: Math.round(evidence.balance_pct * 100) })
    : null;
  const blocks = typeof evidence.exhausted_blocks_7d === "number" && evidence.exhausted_blocks_7d > 0
    ? t("attention_evidence_blocks_7d", { count: evidence.exhausted_blocks_7d })
    : null;
  const spend = typeof evidence.spend_7d === "number" && evidence.spend_7d > 0
    ? t("attention_evidence_spend_7d", { spend: fmt(evidence.spend_7d) })
    : null;
  return [balancePct, blocks, spend].filter(Boolean).join(" · ") || item.state;
}


function TopUserList({ items }: { items: CreditSummary["view_top_users"] }) {
  if (items.length === 0) return <div className="legacy-credits-dash">—</div>;
  return (
    <ul className="legacy-credits-list">
      {items.map((u) => (
        <li key={u.user_id} className="legacy-credits-list-item">
          <span className="legacy-credits-mono">{u.email}</span>
          <span>{fmt(u.spend)} <CharIcon ch="⚡" /></span>
        </li>
      ))}
    </ul>
  );
}

function TopWorkspaceList({ items }: { items: CreditSummary["view_top_workspaces"] }) {
  if (items.length === 0) return <div className="legacy-credits-dash">—</div>;
  return (
    <ul className="legacy-credits-list">
      {items.map((w) => (
        <li key={w.workspace_id ?? "_no_ws_"} className="legacy-credits-list-item">
          <span className="legacy-credits-mono">
            {w.workspace_id ?? "(no workspace)"}
          </span>
          <span>{fmt(w.spend)} <CharIcon ch="⚡" /></span>
        </li>
      ))}
    </ul>
  );
}
