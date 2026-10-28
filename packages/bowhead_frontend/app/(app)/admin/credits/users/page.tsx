"use client";

/**
 * V3 admin credits drill-down — individual user usage details.
 *
 * Header: email · tier · balance/quota · 30d consumption + actions (CSV / Edit / Grant)
 * Body: LedgerTable
 * Privacy boundary: turn_id does not link to /admin/turns/{id}.
 */

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";

import { AdminShell } from "@/components/admin-shell";
import { AdminRouteState } from "@/components/shell/AdminRouteState";
import { AccessWhoCanHelpWhenEnabled } from "@/components/access/access-request-panel";
import { EditUserModal } from "@/components/admin/credits/edit-user-modal";
import { GrantCreditModal } from "@/components/admin/credits/grant-credit-modal";
import { LedgerTable } from "@/components/admin/credits/ledger-table";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import {
  AdminCreditsError,
  csvDownloadUrl,
  fetchCreditUserInfo,
  type CreditUserRow,
} from "@/lib/admin-credits-api";
import { adminCreditsErrorKey } from "@/lib/admin-credits-error";
import type { CreditCapabilities, MeUser } from "@/lib/auth-api";
import { useAuthRedirect } from "@/lib/auth/use-auth-redirect";
import { useAppRouter } from "@/lib/use-app-router";
import { CharIcon, CharIconText } from "@/components/char-icon";

/**
 * Static-export friendly: replace dynamic route /admin/credits/users/[user_id] with
 * /admin/credits/users?user_id=xxx (query string), avoiding generateStaticParams
 * failure when user_id is unknown at build time (legacy V3 admin credits routing issue).
 *
 * Next.js 15 + output:export requires useSearchParams() inside <Suspense> for
 * prerendering (avoiding CSR bailout). Top-level page default export wraps Suspense; inner uses the hook.
 */
export default function CreditUserDrilldownPage() {
  const t = useTranslations("admin.credits");
  return (
    <Suspense fallback={<div className="legacy-credits-user-fallback">{t("loading")}</div>}>
      <CreditUserDrilldownInner />
    </Suspense>
  );
}

function CreditUserDrilldownInner() {
  const t = useTranslations("admin.credits");
  const router = useAppRouter();
  const searchParams = useSearchParams();
  const user_id = searchParams.get("user_id") ?? "";

  const auth = useAuthRedirect({ requiredCapabilities: ["credits_read"] });
  const creditCaps = creditCapabilities(auth.me);
  // 403 from drill-down API (cross-team) still surfaces as "not_admin" in this page state.
  const [forcedNotAdmin, setForcedNotAdmin] = useState(false);
  const [user, setUser] = useState<CreditUserRow | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);

  const [editing, setEditing] = useState(false);
  const [granting, setGranting] = useState(false);

  // Fetch user info via dedicated API (P0 fix: avoid list size=200, which misses users in teams larger than 200)
  useEffect(() => {
    if (auth.status !== "authed") return;
    if (forcedNotAdmin) return;
    if (!user_id) {
      setError(t("missing_user_id"));
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchCreditUserInfo(user_id)
      .then((u) => { if (!cancelled) setUser(u); })
      .catch((e: unknown) => {
        if (e instanceof AdminCreditsError && e.status === 403) {
          if (!cancelled) setForcedNotAdmin(true);
          return;
        }
        if (e instanceof AdminCreditsError && e.status === 404) {
          if (!cancelled) setError(t("user_not_found"));
          return;
        }
        if (!cancelled) setError(t(adminCreditsErrorKey(e)));
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [user_id, refreshTick, auth.status, forcedNotAdmin, t]);

  if (auth.status === "loading" || auth.status === "redirecting") {
    return (
      <AdminShell current="credits" mobileTitle={t("page_title")}>
        <AdminRouteState title={t("page_title")}>{t("loading")}</AdminRouteState>
      </AdminShell>
    );
  }
  if (auth.status === "not_admin" || forcedNotAdmin) {
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
      <div className="admin-config-page admin-credits-user-page">
        <button
          type="button"
          onClick={() => router.push("/admin/credits")}
          className="legacy-credits-back"
        ><CharIcon ch="←" /> {t("page_title")}</button>

        {loading ? (
          <LoadingSkeleton rows={4} label={t("loading")} />
        ) : error ? (
          <div className="legacy-credits-user-error">{error}</div>
        ) : user ? (
          <>
            <UserHeader
              user={user}
              capabilities={creditCaps}
              onEdit={() => setEditing(true)}
              onGrant={() => setGranting(true)}
            />
            <LedgerTable userId={user.user_id} refreshTick={refreshTick} />

            {editing ? (
              <EditUserModal
                user={user}
                capabilities={creditCaps}
                onClose={() => setEditing(false)}
                onSaved={() => { setEditing(false); setRefreshTick((k) => k + 1); }}
              />
            ) : null}
            {granting ? (
              <GrantCreditModal
                user={user}
                onClose={() => setGranting(false)}
                onSuccess={() => { setGranting(false); setRefreshTick((k) => k + 1); }}
              />
            ) : null}
          </>
        ) : null}
      </div>
    </AdminShell>
  );
}

function UserHeader({ user, capabilities, onEdit, onGrant }: {
  user: CreditUserRow;
  capabilities: CreditCapabilities | null;
  onEdit: () => void;
  onGrant: () => void;
}) {
  const t = useTranslations("admin.credits");
  const canEdit = !!(
    capabilities?.credit_user_quota_write || capabilities?.credit_user_tier_write
  );
  return (
    <header className="legacy-credits-user-head">
      <div>
        <h1 className="ui-page-title legacy-credits-user-title">{user.email}</h1>
        <div className="legacy-credits-user-meta">
          {user.display_name ? (
            <span className="legacy-credits-user-name">{user.display_name}</span>
          ) : null}
          <span className={`legacy-credits-tier${user.tier === "ultra" ? " is-ultra" : ""}`}>{user.tier.toUpperCase()}</span>
          {user.status === "removed" ? (
            <span className="legacy-credits-tag legacy-credits-tag--removed legacy-credits-tag--head">{t("removed_user_badge")}</span>
          ) : null}
        </div>
        <div className="legacy-credits-user-balance">
          {t("balance_label")} {user.credit_balance.toLocaleString()} / {user.credit_quota.toLocaleString()} <CharIcon ch="⚡" />
          {" · "}
          <CharIconText ch="⚡" at="end">{t("spend_30d_label", { spend: user.spend_30d.toLocaleString() })}</CharIconText>
        </div>
      </div>
      <div className="legacy-credits-user-actions">
        <a
          href={csvDownloadUrl("user_ledger", { user_id: user.user_id })}
          download
          className="ui-btn legacy-credits-head-btn"
        >
          ⬇ {t("csv_ledger_download")}
        </a>
        <button type="button" onClick={onEdit} disabled={user.status === "removed" || !canEdit} className="ui-btn legacy-credits-head-btn">
          {t("row_action_edit")}
        </button>
        <button type="button" onClick={onGrant} disabled={user.status === "removed" || !capabilities?.credit_grant} className="ui-btn ui-btn--primary legacy-credits-head-btn">
          {t("row_action_grant")}
        </button>
      </div>
    </header>
  );
}


function creditCapabilities(user: MeUser | null): CreditCapabilities | null {
  return user?.credit_capabilities ?? user?.capabilities?.credit ?? null;
}
