"use client";

/**
 * UsersTable — admin credits user list.
 *
 * Columns: Email / Tier / Balance/Quota / 30d consumption / Activity / [⋯] menu
 * Filters: email / Tier / Space / "显示已停用用户"
 * Sort: Attention needed (default) / consumption↓ / balance%↑ / Email A-Z / recent activity
 * [⋯]: edit quota/tier/balance · Grant credit · usage details · reset this user's balance
 */

import { useEffect, useId, useState } from "react";
import { MenuFloating, MenuItemReason, useMenu, useMenuItem, withConfirmEllipsis } from "@/components/ui/menu";
import { useTranslations } from "next-intl";

import {
  fetchCreditUsers,
  patchCreditUser,
  trackCreditAttentionActionClicked,
  type CreditUserRow,
} from "@/lib/admin-credits-api";
import { adminCreditsErrorKey } from "@/lib/admin-credits-error";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ListRow, ListRows, useListRowsLayout } from "@/components/ui/list-rows";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import type { CreditCapabilities } from "@/lib/auth-api";
import { useAppRouter } from "@/lib/use-app-router";

import { EditUserModal } from "./edit-user-modal";
import { GrantCreditModal } from "./grant-credit-modal";
import { CharIcon } from "@/components/char-icon";

interface Props {
  refreshTick: number;
  onAfterMutate: () => void;
  capabilities: CreditCapabilities | null;
  onAdjustTeamQuota: () => void;
}

type SortKey = "attention" | "spend" | "balance_pct" | "email" | "last_activity";

const PAGE_SIZE = 20;

export function UsersTable({ refreshTick, onAfterMutate, capabilities, onAdjustTeamQuota }: Props) {
  const t = useTranslations("admin.credits");
  const router = useAppRouter();

  const [items, setItems] = useState<CreditUserRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [q, setQ] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  const [tier, setTier] = useState<"" | "pro" | "ultra">("");
  const [workspace, setWorkspace] = useState("");
  const [includeRemoved, setIncludeRemoved] = useState(false);
  const [sort, setSort] = useState<SortKey>("attention");

  const [editingUser, setEditingUser] = useState<CreditUserRow | null>(null);
  const [grantingUser, setGrantingUser] = useState<CreditUserRow | null>(null);
  const [resetUser, setResetUser] = useState<CreditUserRow | null>(null);
  const [resettingUserId, setResettingUserId] = useState<string | null>(null);
  const [resetError, setResetError] = useState<string | null>(null);
  const [openMenuId, setOpenMenuId] = useState<string | null>(null);
  // Mobile (<640): the table becomes list rows (decision 12).
  const listLayout = useListRowsLayout();

  // debounce search input
  useEffect(() => {
    const h = setTimeout(() => setDebouncedQ(q), 300);
    return () => clearTimeout(h);
  }, [q]);

  // load users
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchCreditUsers({
      page, size: PAGE_SIZE, sort,
      q: debouncedQ || undefined,
      tier: tier || undefined,
      workspace: workspace || undefined,
      include_removed: includeRemoved,
    })
      .then((d) => {
        if (cancelled) return;
        setItems(d.items);
        setTotal(d.total);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(t(adminCreditsErrorKey(e)));
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [
    page,
    sort,
    debouncedQ,
    tier,
    workspace,
    includeRemoved,
    refreshTick,
    t,
  ]);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const handleResetBalance = async (u: CreditUserRow) => {
    if (!capabilities?.credit_user_quota_write) return;
    setResettingUserId(u.user_id);
    setResetError(null);
    try {
      await patchCreditUser(u.user_id, { credit_balance: u.credit_quota });
      setResetUser(null);
      onAfterMutate();
    } catch (e: unknown) {
      setResetError(t(adminCreditsErrorKey(e)));
    } finally {
      setResettingUserId(null);
    }
  };

  const rowProps = (u: CreditUserRow): UserRowProps => ({
    user: u,
    menuOpen: openMenuId === u.user_id,
    onMenuOpenChange: (open) =>
      setOpenMenuId((current) => (open ? u.user_id : current === u.user_id ? null : current)),
    onEdit: () => { setEditingUser(u); setOpenMenuId(null); },
    onGrant: () => { setGrantingUser(u); setOpenMenuId(null); },
    onDrilldown: () => {
      router.push(`/admin/credits/users?user_id=${encodeURIComponent(u.user_id)}`);
      setOpenMenuId(null);
    },
    onAdjustTeamQuota: () => {
      onAdjustTeamQuota();
      setOpenMenuId(null);
    },
    onResetBalance: () => { setResetUser(u); setOpenMenuId(null); },
    capabilities,
    t,
  });

  return (
    <section className="legacy-credits-card credits-users">
      <h2 className="legacy-credits-section-title">{t("users_table_title")}</h2>

      {/* Filter toolbar. */}
      <div className="legacy-credits-toolbar">
        <input
          type="text"
          placeholder={t("search_placeholder")}
          value={q}
          onChange={(e) => { setQ(e.target.value); setPage(1); }}
          className="ui-input legacy-input-credits-table legacy-credits-search"
        />
        <SelectFilter
          value={tier}
          onChange={(v) => { setTier(v as "" | "pro" | "ultra"); setPage(1); }}
          options={[
            { value: "", label: t("filter_tier") },
            { value: "pro", label: t("tier_pro") },
            { value: "ultra", label: t("tier_ultra") },
          ]}
        />
        <SelectFilter
          value={sort}
          onChange={(v) => { setSort(v as SortKey); setPage(1); }}
          options={[
            { value: "attention", label: `${t("sort_label")}: ${t("sort_attention")}` },
            { value: "spend", label: t("sort_spend") },
            { value: "balance_pct", label: t("sort_balance_pct") },
            { value: "email", label: t("sort_email") },
            { value: "last_activity", label: t("sort_last_activity") },
          ]}
        />
        <label className="legacy-credits-removed-toggle">
          <input
            type="checkbox"
            checked={includeRemoved}
            onChange={(e) => { setIncludeRemoved(e.target.checked); setPage(1); }}
          />
          {t("filter_show_removed")}
        </label>
      </div>

      {/* table */}
      {resetError ? (
        <div role="alert" className="legacy-credits-error-banner">
          {resetError}
        </div>
      ) : null}
      {listLayout ? (
        <div className="ui-table-frame legacy-credits-users-frame">
          {loading ? (
            <LoadingSkeleton rows={4} label={t("loading")} />
          ) : error ? (
            <div role="alert" className="legacy-credits-state legacy-credits-state--error">{error}</div>
          ) : items.length === 0 ? (
            <div className="legacy-credits-state legacy-credits-state--empty">{t("empty_users")}</div>
          ) : (
            <ListRows label={t("users_table_title")} className="credits-user-rows">
              {items.map((u) => <UserListRow key={u.user_id} {...rowProps(u)} />)}
            </ListRows>
          )}
        </div>
      ) : (
      <div className="ui-table-frame legacy-credits-users-frame">
        <table className="ui-table legacy-credits-users-table">
          <thead>
                <tr>
                  <Th>{t("col_email")}</Th>
                  <Th>{t("col_attention")}</Th>
                  <Th>{t("col_tier")}</Th>
                  <Th align="right" num>{t("col_balance_quota")}</Th>
                  <Th align="right" num>{t("col_spend_30d")}</Th>
                  <Th>{t("col_last_activity")}</Th>
              <Th align="right">{t("col_actions")}</Th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
                  <tr><td colSpan={7} className="legacy-credits-state legacy-credits-state--loading">
                    <LoadingSkeleton rows={4} label={t("loading")} />
                  </td></tr>
                ) : error ? (
                  <tr><td colSpan={7} className="legacy-credits-state legacy-credits-state--error">
                    {error}
                  </td></tr>
                ) : items.length === 0 ? (
                  <tr><td colSpan={7} className="legacy-credits-state legacy-credits-state--empty">
                    {t("empty_users")}
                  </td></tr>
            ) : (
              items.map((u) => <UserRow key={u.user_id} {...rowProps(u)} />)
            )}
          </tbody>
        </table>
      </div>
      )}

      {/* pagination */}
      {totalPages > 1 ? (
        <div className="ui-pager legacy-credits-pager">
          <PageBtn disabled={page <= 1} onClick={() => setPage(page - 1)}><CharIcon ch="‹" /></PageBtn>
          <span className="ui-pager-summary legacy-credits-pager-summary">
            {page} / {totalPages}
          </span>
          <PageBtn disabled={page >= totalPages} onClick={() => setPage(page + 1)}><CharIcon ch="›" /></PageBtn>
        </div>
      ) : null}

      {editingUser ? (
        <EditUserModal
          user={editingUser}
          capabilities={capabilities}
          onClose={() => setEditingUser(null)}
          onSaved={() => { setEditingUser(null); onAfterMutate(); }}
        />
      ) : null}
      {grantingUser ? (
        <GrantCreditModal
          user={grantingUser}
          onClose={() => setGrantingUser(null)}
          onSuccess={() => { setGrantingUser(null); onAfterMutate(); }}
        />
      ) : null}
      {resetUser ? (
        <ConfirmDialog
          title={t("row_action_reset_user")}
          description={t("reset_confirm_desc", { email: resetUser.email, quota: resetUser.credit_quota.toLocaleString() })}
          confirmLabel={t("row_action_reset_user")}
          cancelLabel={t("cancel_button")}
          tone="danger"
          busy={resettingUserId === resetUser.user_id}
          onCancel={() => setResetUser(null)}
          onConfirm={() => void handleResetBalance(resetUser)}
        />
      ) : null}
    </section>
  );
}

interface UserRowProps {
  user: CreditUserRow;
  menuOpen: boolean;
  onMenuOpenChange: (open: boolean) => void;
  onEdit: () => void;
  onGrant: () => void;
  onDrilldown: () => void;
  onAdjustTeamQuota: () => void;
  onResetBalance: () => void;
  capabilities: CreditCapabilities | null;
  t: (key: string) => string;
}

/** Email, removed/admin markers, and display name (first table column). */
function UserIdentity({ user, t }: { user: CreditUserRow; t: (key: string) => string }) {
  const isRemoved = user.status === "removed";
  return (
    <>
      <div className="legacy-credits-identity">
        <span className="legacy-credits-email">{user.email}</span>
        {isRemoved ? (
          <span className="legacy-credits-tag legacy-credits-tag--removed">{t("removed_user_badge")}</span>
        ) : null}
        {user.is_team_admin ? (
          <span className="legacy-credits-tag legacy-credits-tag--admin">{t("admin_badge")}</span>
        ) : null}
      </div>
      {user.display_name ? (
        <div className="legacy-credits-display-name">
          {user.display_name}
        </div>
      ) : null}
    </>
  );
}

function TierBadge({ tier }: { tier: CreditUserRow["tier"] }) {
  return (
    <span className={`legacy-credits-tier${tier === "ultra" ? " is-ultra" : ""}`}>{tier.toUpperCase()}</span>
  );
}

function UserBalance({ user }: { user: CreditUserRow }) {
  const balancePct = user.credit_quota > 0 ? user.credit_balance / user.credit_quota : 0;
  const lowBalance = isLowBalance(user);
  return (
    <>
      <div className="legacy-credits-balance">
        <span className={lowBalance ? "legacy-credits-balance-low" : undefined}>
          {user.credit_balance.toLocaleString()}
        </span>
        <span className="legacy-credits-balance-quota"> / {user.credit_quota.toLocaleString()}</span>
      </div>
      <div className="legacy-credits-balance-track">
        <div
          className={`legacy-credits-balance-bar${lowBalance ? " is-low" : ""}`}
          style={{ width: `${Math.max(2, balancePct * 100)}%` }}
        />
      </div>
    </>
  );
}

/** Balance below 20% of quota is red (same table/mobile-row rule). */
function isLowBalance(user: CreditUserRow): boolean {
  const balancePct = user.credit_quota > 0 ? user.credit_balance / user.credit_quota : 0;
  return balancePct < 0.20;
}

/**
 * Removed team users without any activity have no usage details: entire table-row “⋯” unavailable,
 * mobile whole-row click never navigates; show reason.
 */
function hasNoUsageToView(user: CreditUserRow): boolean {
  return user.status === "removed" && !user.last_activity_at;
}

/**
 * Mobile list row (<640). Retain information even at 320px:
 * - Title: email (ellipsis; full value in whole-row button accessible name/hover hint);
 * - Second: nonshrinking plan badge/disabled/admin markers + ellipsized name; plan stays off trailing edge for text width;
 * - Third (at most two lines): balance/quota (red <20%, table rule) · 30d consumption · recent activity;
 * - Trailing: original “⋯” menu;
 * - Whole-row click opens usage details (same “看用量明细” menu action); removed inactive users cannot click, explanation below.
 * Attention items/suggested actions sit below row.
 */
function UserListRow(props: UserRowProps) {
  const { user, t } = props;
  const isRemoved = user.status === "removed";
  const noUsage = hasNoUsageToView(user);
  return (
    <ListRow
      className="credits-user-listrow"
      muted={isRemoved}
      title={<span title={user.email}>{user.email}</span>}
      subtitle={
        <span className="credits-user-listrow-sub">
          <TierBadge tier={user.tier} />
          {isRemoved ? (
            <span className="legacy-credits-tag legacy-credits-tag--removed">{t("removed_user_badge")}</span>
          ) : null}
          {user.is_team_admin ? (
            <span className="legacy-credits-tag legacy-credits-tag--admin">{t("admin_badge")}</span>
          ) : null}
          {user.display_name ? <span className="credits-user-listrow-name">{user.display_name}</span> : null}
        </span>
      }
      openLabel={noUsage ? undefined : user.email}
      onOpen={noUsage ? undefined : props.onDrilldown}
      meta={
        <span className="ui-tabular">
          <span className={isLowBalance(user) ? "legacy-credits-balance-low" : undefined}>
            {user.credit_balance.toLocaleString()}
          </span>
          {" / "}{user.credit_quota.toLocaleString()} <CharIcon ch="⚡" />
          {" · "}
          {user.spend_30d > 0 ? user.spend_30d.toLocaleString() : "—"}
          {" · "}
          {user.last_activity_at ? formatRelative(user.last_activity_at) : "—"}
        </span>
      }
      actions={<UserRowMenu {...props} />}
      detail={noUsage || user.activation_attention ? (
        <>
          {noUsage ? <p className="credits-user-listrow-reason">{t("row_reason_no_usage")}</p> : null}
          {user.activation_attention ? (
            <AttentionCell
              user={user}
              onGrant={props.onGrant}
              onEdit={props.onEdit}
              onDrilldown={props.onDrilldown}
              onAdjustTeamQuota={props.onAdjustTeamQuota}
              t={t}
            />
          ) : null}
        </>
      ) : undefined}
    />
  );
}

function UserRow(props: UserRowProps) {
  const {
    user,
    onEdit,
    onGrant,
    onDrilldown,
    onAdjustTeamQuota,
    t,
  } = props;
  const isRemoved = user.status === "removed";
  return (
    <tr className={`legacy-credits-user-row${isRemoved ? " is-removed" : ""}`}>
      <td className="legacy-credits-td">
        <UserIdentity user={user} t={t} />
      </td>
      <td className="legacy-credits-td">
        <AttentionCell
          user={user}
          onGrant={onGrant}
          onEdit={onEdit}
          onDrilldown={onDrilldown}
          onAdjustTeamQuota={onAdjustTeamQuota}
          t={t}
        />
      </td>
      <td className="legacy-credits-td">
        <TierBadge tier={user.tier} />
      </td>
      <td className="legacy-credits-td legacy-credits-td--right ui-num">
        <UserBalance user={user} />
      </td>
      <td className="legacy-credits-td legacy-credits-td--right legacy-credits-td--mono ui-num">
        {user.spend_30d > 0 ? user.spend_30d.toLocaleString() : "—"}
      </td>
      <td className="legacy-credits-td legacy-credits-td--meta ui-tabular">
        {user.last_activity_at ? formatRelative(user.last_activity_at) : "—"}
      </td>
      <td className="legacy-credits-td legacy-credits-td--right legacy-credits-td--rel ui-table-actions">
        <UserRowMenu {...props} />
      </td>
    </tr>
  );
}

function UserRowMenu({
  user,
  menuOpen,
  onMenuOpenChange,
  onEdit,
  onGrant,
  onDrilldown,
  onResetBalance,
  capabilities,
  t,
}: UserRowProps) {
  const isRemoved = user.status === "removed";
  // Row “⋯” portals to body (design “图层”); old absolute overlay inside cell was clipped by table
  // overflow-x:auto also making overflow-y auto; last-row menus fully clipped (real-browser 0% visible).
  // No closeOnScroll (edge trigger may scroll container for focus); close only once whole row leaves view,
  // avoiding menu clamped to viewport edge without identifiable row.
  const menu = useMenu({
    open: menuOpen,
    onOpenChange: onMenuOpenChange,
    placement: "bottom-end",
    offset: 2,
    closeWhenAnchorHidden: true,
  });
  const canEdit = !!(
    capabilities?.credit_user_quota_write || capabilities?.credit_user_tier_write
  );

  return (
    <>
        <button
          type="button"
          ref={menu.refs.setReference}
          aria-label={t("more_aria")}
          disabled={hasNoUsageToView(user)}
          {...menu.getReferenceProps({ onClick: (e) => e.stopPropagation() })}
          className="legacy-credits-more"
        ><CharIcon ch="⋯" /></button>
        <MenuFloating menu={menu}>
          <div
            ref={menu.refs.setFloating}
            {...menu.getFloatingProps({
              "aria-label": t("more_aria"),
              onClick: (e) => e.stopPropagation(),
              className: "legacy-menu-credits",
            })}
          >
            {/* Disabled reasons (MenuItem shows them): removed from team first, then missing permission. */}
            <MenuItem
              onClick={onEdit}
              disabled={isRemoved || !canEdit}
              reason={isRemoved ? t("row_reason_removed") : t("row_reason_no_edit")}
            >
              {t("row_action_edit")}
            </MenuItem>
            <MenuItem
              onClick={onGrant}
              disabled={isRemoved || !capabilities?.credit_grant}
              reason={isRemoved ? t("row_reason_removed") : t("row_reason_no_grant")}
            >
              {t("row_action_grant")}
            </MenuItem>
            <MenuItem onClick={onDrilldown}>{t("row_action_drilldown")}</MenuItem>
            {/* Behavior batch A: balance reset clears data, confirms first; red danger item with “…”. */}
            <MenuItem
              onClick={onResetBalance}
              danger
              disabled={isRemoved || !capabilities?.credit_user_quota_write}
              reason={isRemoved ? t("row_reason_removed") : t("row_reason_no_reset")}
            >
              {t("row_action_reset_user_menu")}
            </MenuItem>
          </div>
        </MenuFloating>
    </>
  );
}

function AttentionCell({
  user,
  onGrant,
  onEdit,
  onDrilldown,
  onAdjustTeamQuota,
  t,
}: {
  user: CreditUserRow;
  onGrant: () => void;
  onEdit: () => void;
  onDrilldown: () => void;
  onAdjustTeamQuota: () => void;
  t: (key: string) => string;
}) {
  const item = user.activation_attention;
  if (!item) {
    return <span className="legacy-credits-dash">—</span>;
  }
  const recommended = user.recommended_action ?? item.recommended_action;
  const actionKey = recommended?.key ?? user.recommended_action_key ?? item.recommended_action_key;
  const actionLabel = attentionActionLabel(t, actionKey, recommended?.label ?? actionKey ?? "—");
  const disabled = recommended ? !recommended.allowed : true;
  const flags = (user.segment_flags?.length ? user.segment_flags : item.segment_flags).slice(0, 2);
  const runAction = () => {
    if (disabled) return;
    void trackCreditAttentionActionClicked(item.attention_item_id, {
      action_key: actionKey ?? "unknown",
      source_surface: "users",
      allowed: recommended?.allowed ?? true,
      disabled_reason: recommended?.disabled_reason ?? null,
    }).catch(() => undefined);
    if (actionKey === "grant_credit") {
      onGrant();
      return;
    }
    if (actionKey === "edit_user_quota" || actionKey === "edit_user_tier") {
      onEdit();
      return;
    }
    if (
      actionKey === "increase_team_quota" ||
      actionKey === "increase_team_quota_or_renew" ||
      actionKey === "open_billing"
    ) {
      onAdjustTeamQuota();
      return;
    }
    onDrilldown();
  };

  return (
    <div className="legacy-credits-attn-cell">
      <div className="legacy-credits-attn-cell-head">
        <span className={`legacy-credits-severity legacy-credits-severity--cell ${attentionSeverityClass(item.severity)}`}>
          {attentionSeverityLabel(t, item.severity)}
        </span>
        <span className="legacy-credits-attn-policy">
          {attentionPolicyLabel(t, user.primary_segment ?? item.primary_segment ?? item.policy_key)}
        </span>
      </div>
      {flags.length ? (
        <div className="legacy-credits-attn-flags">
          {flags.map((flag) => (
            <span key={flag} className="legacy-credits-attn-flag">
              {attentionPolicyLabel(t, flag)}
            </span>
          ))}
        </div>
      ) : null}
      <button
        type="button"
        disabled={disabled}
        onClick={runAction}
        className="ui-btn ui-btn--primary legacy-credits-attn-go"
      >
        {actionLabel}
      </button>
    </div>
  );
}

function attentionSeverityClass(
  severity: NonNullable<CreditUserRow["activation_attention"]>["severity"],
): string {
  return severity === "critical"
    ? "is-critical"
    : severity === "warning"
    ? "is-warning"
    : "is-info";
}

function attentionSeverityLabel(
  t: (key: string) => string,
  severity: NonNullable<CreditUserRow["activation_attention"]>["severity"],
): string {
  return severity === "critical"
    ? t("attention_severity_critical")
    : severity === "warning"
    ? t("attention_severity_warning")
    : t("attention_severity_info");
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
    case "view_ledger":
      return t("attention_action_view_ledger");
    default:
      return fallback;
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

function Th({ children, align = "left", num = false }: { children: React.ReactNode; align?: "left" | "right" | "center"; num?: boolean }) {
  // Inline styles moved to styles/ui-controls-legacy.css (appearance batch 4; page acceptance B alignment modifiers).
  return (
    <th className={`legacy-credits-th legacy-credits-th--${align}${num ? " ui-num" : ""}`}>{children}</th>
  );
}

function MenuItem({ onClick, disabled, reason, danger = false, children }: {
  onClick: () => void; disabled?: boolean;
  /** Confirmation-first danger: red with “…”. */
  danger?: boolean;
  /** Disabled reason: persistent second line/aria-describedby (behavior batch A). */
  reason?: string;
  children: React.ReactNode;
}) {
  const itemProps = useMenuItem({ onSelect: onClick, disabled });
  const reasonId = useId();
  const shownReason = disabled && reason ? reason : null;
  return (
    <>
      {/* Inline styles and hover background rules moved to styles/ui-controls-legacy.css (appearance batch 3). */}
      <button
        type="button"
        className="legacy-menu-credits-item"
        aria-describedby={shownReason ? reasonId : undefined}
        data-danger={danger ? "true" : undefined}
        {...itemProps}
      >
        {danger && typeof children === "string" ? withConfirmEllipsis(children) : children}
      </button>
      {shownReason ? <MenuItemReason id={reasonId}>{shownReason}</MenuItemReason> : null}
    </>
  );
}

function SelectFilter({ value, onChange, options }: {
  value: string; onChange: (v: string) => void;
  options: { value: string; label: string }[];
}) {
  return (
    <select
      value={value} onChange={(e) => onChange(e.target.value)}
      className="ui-input legacy-input-credits-table"
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>{o.label}</option>
      ))}
    </select>
  );
}

function PageBtn({ disabled, onClick, children }: {
  disabled: boolean; onClick: () => void; children: React.ReactNode;
}) {
  return (
    // Inline styles moved to styles/ui-controls-legacy.css (appearance batch 4).
    <button
      type="button" disabled={disabled} onClick={onClick}
      className="ui-pager-btn legacy-credits-pagebtn"
    >{children}</button>
  );
}

function formatRelative(iso: string): string {
  try {
    const t = new Date(iso).getTime();
    const sec = Math.max(0, (Date.now() - t) / 1000);
    if (sec < 60) return `${Math.round(sec)}s`;
    if (sec < 3600) return `${Math.round(sec / 60)}m`;
    if (sec < 86400) return `${Math.round(sec / 3600)}h`;
    return `${Math.round(sec / 86400)}d`;
  } catch {
    return iso.slice(0, 10);
  }
}


