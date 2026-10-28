"use client";

/**
 * V3 Admin Credits — main page.
 *
 * Team admins see team overview + tier defaults + user list + individual-user drill-down.
 *
 * Subcomponents:
 *   - SummaryCard       (overview card + perspective dropdown)
 *   - TierDefaults      (edit PRO/ULTRA default quotas)
 *   - UsersTable        (filter/pagination/[⋯] menu)
 *   - PeriodResetModal  (preview + cooldown)
 *   - CsvDownloadButton (four-scope dropdown)
 *
 * Design: docs/V3-admin-credits-page-design.md (PRD v2)
 *       + docs/V3-admin-credits-tech-design.md §2.3
 */

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { AdminShell } from "@/components/admin-shell";
import { AdminRouteState } from "@/components/shell/AdminRouteState";
import { AccessWhoCanHelpWhenEnabled } from "@/components/access/access-request-panel";
import { CsvDownloadButton } from "@/components/admin/credits/csv-download-button";
import { EditUserModal } from "@/components/admin/credits/edit-user-modal";
import { GrantCreditModal } from "@/components/admin/credits/grant-credit-modal";
import { PeriodResetModal } from "@/components/admin/credits/period-reset-modal";
import { SummaryCard } from "@/components/admin/credits/summary-card";
import { TierDefaults } from "@/components/admin/credits/tier-defaults";
import { TeamQuotaModal } from "@/components/admin/credits/team-quota-modal";
import { UsersTable } from "@/components/admin/credits/users-table";
import {
  fetchCreditSummary,
  fetchCreditUserInfo,
  trackCreditAttentionActionClicked,
  updateCreditAttentionState,
  type CreditActivationAttentionItem,
  type CreditSummary,
  type CreditUserRow,
} from "@/lib/admin-credits-api";
import { adminCreditsErrorKey } from "@/lib/admin-credits-error";
import type { CreditCapabilities, MeUser } from "@/lib/auth-api";
import { useAuthRedirect } from "@/lib/auth/use-auth-redirect";
import { useAppRouter } from "@/lib/use-app-router";
import { CharIconText } from "@/components/char-icon";

export default function CreditsAdminPage() {
  const t = useTranslations("admin.credits");
  const router = useAppRouter();
  const auth = useAuthRedirect({ requiredCapabilities: ["credits_read"] });

  const [summary, setSummary] = useState<CreditSummary | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [summaryError, setSummaryError] = useState<string | null>(null);

  // Bump to force all children to refetch (after user changes / grant / reset).
  const [refreshTick, setRefreshTick] = useState(0);

  const [resetMode, setResetMode] = useState<"balance_only" | "balance_and_quota" | null>(null);
  const [adjustingTeamQuota, setAdjustingTeamQuota] = useState(false);
  const [editingUser, setEditingUser] = useState<CreditUserRow | null>(null);
  const [grantingUser, setGrantingUser] = useState<CreditUserRow | null>(null);
  const creditCaps = creditCapabilities(auth.me);

  // ─── summary load (refresh on bump) ─────────
  useEffect(() => {
    if (auth.status !== "authed") return;
    let cancelled = false;
    setSummaryLoading(true);
    setSummaryError(null);
    fetchCreditSummary()
      .then((s) => { if (!cancelled) setSummary(s); })
      .catch((e: unknown) => {
        if (!cancelled) setSummaryError(t(adminCreditsErrorKey(e)));
      })
      .finally(() => { if (!cancelled) setSummaryLoading(false); });
    return () => { cancelled = true; };
  }, [refreshTick, auth.status, t]);

  const bumpRefresh = () => setRefreshTick((k) => k + 1);

  const handleAttentionStateChange = async (
    item: CreditActivationAttentionItem,
    state: "dismissed",
    reason?: string,
  ) => {
    await updateCreditAttentionState(item.attention_item_id, {
      state,
      reason,
    });
    bumpRefresh();
  };

  const openAttentionUserAction = async (
    item: CreditActivationAttentionItem,
    actionKey: string,
    mode: "grant" | "edit",
  ) => {
    if (!item.user_id) {
      throw new Error(t("attention_user_required"));
    }
    const user = await fetchCreditUserInfo(item.user_id);
    if (mode === "grant") {
      setGrantingUser(user);
    } else {
      setEditingUser(user);
    }
  };

  const trackAttentionActionClick = (
    item: CreditActivationAttentionItem,
    actionKey: string,
  ) => {
    const action = item.available_actions?.find((candidate) => candidate.key === actionKey)
      ?? (item.recommended_action?.key === actionKey ? item.recommended_action : undefined);
    void trackCreditAttentionActionClicked(item.attention_item_id, {
      action_key: actionKey,
      source_surface: "summary",
      allowed: action?.allowed ?? true,
      disabled_reason: action?.disabled_reason ?? null,
    }).catch(() => undefined);
  };

  const handleAttentionAction = async (
    item: CreditActivationAttentionItem,
    actionKey: string,
  ) => {
    trackAttentionActionClick(item, actionKey);
    if (actionKey === "grant_credit") {
      if (!creditCaps?.credit_grant) throw new Error(t("attention_action_unavailable"));
      await openAttentionUserAction(item, actionKey, "grant");
      return;
    }
    if (actionKey === "edit_user_quota") {
      if (!creditCaps?.credit_user_quota_write) throw new Error(t("attention_action_unavailable"));
      await openAttentionUserAction(item, actionKey, "edit");
      return;
    }
    if (actionKey === "edit_user_tier") {
      if (!creditCaps?.credit_user_tier_write) throw new Error(t("attention_action_unavailable"));
      await openAttentionUserAction(item, actionKey, "edit");
      return;
    }
    if (actionKey === "view_ledger") {
      if (!item.user_id) throw new Error(t("attention_user_required"));
      router.push(`/admin/credits/users?user_id=${encodeURIComponent(item.user_id)}`);
      return;
    }
    if (actionKey === "period_reset") {
      if (!creditCaps?.credit_period_reset) throw new Error(t("attention_action_unavailable"));
      setResetMode("balance_only");
      return;
    }
    if (
      actionKey === "increase_team_quota" ||
      actionKey === "increase_team_quota_or_renew" ||
      actionKey === "open_billing"
    ) {
      if (!creditCaps?.credit_team_quota_write || !summary) {
        throw new Error(t("attention_action_unavailable"));
      }
      setAdjustingTeamQuota(true);
      return;
    }
    if (actionKey === "dismiss_attention_item") {
      await handleAttentionStateChange(item, "dismissed", "watch_later");
      return;
    }
    throw new Error(t("attention_action_unavailable"));
  };

  const finishActiveAttentionAction = () => {
    bumpRefresh();
  };

  const clearAttentionModal = () => {
    setEditingUser(null);
    setGrantingUser(null);
  };

  // ─── Rendering ───────────────────────────────────
  // 401 → useAuthRedirect already used window.location.replace to /login; show a placeholder while loading/redirecting.
  if (auth.status === "loading" || auth.status === "redirecting") {
    return (
      <AdminShell current="credits" mobileTitle={t("page_title")}>
        <AdminRouteState title={t("page_title")}>{t("loading")}</AdminRouteState>
      </AdminShell>
    );
  }
  if (auth.status === "not_admin") {
    return (
      <AdminShell current="credits" mobileTitle={t("page_title")}>
        <AdminRouteState title={t("page_title")} tone="forbidden" aside={<AccessWhoCanHelpWhenEnabled scope="team" />}>
          {t("forbidden_prefix")}{t("credits_access_required")}
        </AdminRouteState>
      </AdminShell>
    );
  }
  if (auth.status === "error") {
    return (
      <AdminShell current="credits" mobileTitle={t("page_title")}>
        <AdminRouteState title={t("page_title")} tone="error">
          {t("err_generic")}
        </AdminRouteState>
      </AdminShell>
    );
  }

  return (
    <AdminShell current="credits" mobileTitle={t("page_title")}>
      <div className="admin-config-page admin-credits-page">
        <header className="admin-config-page__header">
          <div>
            <h1 className="ui-page-title legacy-admin-credits-title">
              {t("page_title")}
            </h1>
            <p className="ui-page-desc legacy-admin-credits-desc">
              {t("page_subtitle")}
            </p>
          </div>
          <div className="legacy-credits-head-actions">
            <CsvDownloadButton />
            {creditCaps?.credit_team_quota_write ? (
              <button
                type="button"
                onClick={() => setAdjustingTeamQuota(true)}
                className="ui-btn legacy-credits-head-btn"
              >
                {t("team_quota_adjust_button")}
              </button>
            ) : null}
            <button
              type="button"
              onClick={() => {
                if (creditCaps?.credit_period_reset) setResetMode("balance_only");
              }}
              disabled={!creditCaps?.credit_period_reset}
              className="ui-btn legacy-credits-head-btn legacy-credits-head-btn--dim-disabled"
            >
              <CharIconText ch="▾" at="end">{t("period_reset_button")}</CharIconText>
            </button>
          </div>
        </header>

        <SummaryCard
          summary={summary}
          loading={summaryLoading}
          error={summaryError}
          onRetry={bumpRefresh}
          onAttentionStateChange={handleAttentionStateChange}
          onAttentionAction={handleAttentionAction}
        />
        {creditCaps?.credit_user_tier_write ? (
          <TierDefaults
            teamId={auth.me?.team_id ?? null}
            onChange={bumpRefresh}
            refreshTick={refreshTick}
          />
        ) : null}
        <UsersTable
          refreshTick={refreshTick}
          onAfterMutate={bumpRefresh}
          capabilities={creditCaps}
          onAdjustTeamQuota={() => setAdjustingTeamQuota(true)}
        />

        {adjustingTeamQuota && summary ? (
          <TeamQuotaModal
            currentQuota={summary.team.quota}
            onClose={() => setAdjustingTeamQuota(false)}
            onSuccess={() => {
              setAdjustingTeamQuota(false);
              bumpRefresh();
            }}
          />
        ) : null}

        {resetMode && auth.me?.team_id && (
          <PeriodResetModal
            mode={resetMode}
            onClose={() => setResetMode(null)}
            onSuccess={() => {
              setResetMode(null);
              finishActiveAttentionAction();
            }}
            onSwitchMode={(m) => setResetMode(m)}
          />
        )}
        {editingUser ? (
          <EditUserModal
            user={editingUser}
            capabilities={creditCaps}
            onClose={clearAttentionModal}
            onSaved={() => {
              setEditingUser(null);
              finishActiveAttentionAction();
            }}
          />
        ) : null}
        {grantingUser ? (
          <GrantCreditModal
            user={grantingUser}
            onClose={clearAttentionModal}
            onSuccess={() => {
              setGrantingUser(null);
              finishActiveAttentionAction();
            }}
          />
        ) : null}
      </div>
    </AdminShell>
  );
}

function creditCapabilities(user: MeUser | null): CreditCapabilities | null {
  return user?.credit_capabilities ?? user?.capabilities?.credit ?? null;
}
