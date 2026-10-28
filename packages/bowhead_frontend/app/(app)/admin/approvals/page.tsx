"use client";

import { Suspense, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import clsx from "clsx";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";

import { AdminShell } from "@/components/admin-shell";
import { AccessWhoCanHelpWhenEnabled } from "@/components/access/access-request-panel";
import { AdminRouteState } from "@/components/shell/AdminRouteState";
import { useAuthRedirect } from "@/lib/auth/use-auth-redirect";
import { TEAM_SETTINGS_ROLES } from "@/lib/settings-navigation";
import { notifySettingsNavCountsChanged } from "@/lib/settings-nav-signal";
import { ErrorDiagnosticsInline, type ShownError, shownError } from "@/components/error-notice";
import { Pause } from "@/components/icons";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { Segment, SegmentedControl } from "@/components/ui/segmented-control";
import {
  approveApproval,
  fetchApprovals,
  rejectApproval,
  type ApprovalDTO,
} from "@/lib/api";

type Scope = "pending" | "all";

type TFn = (key: string, values?: Record<string, string | number>) => string;

export default function ApprovalsPage() {
  return (
    <Suspense fallback={<ApprovalsFallback />}>
      <ApprovalsAccessGate>
        <ApprovalsInner />
      </ApprovalsAccessGate>
    </Suspense>
  );
}

/**
 * Same route guard as the other administration pages (P2 batch 4 lists approvals in the sidebar's
 * administration group): only a team owner or team admin opens the page. The backend already returns
 * only approvals of teams the caller administers; this keeps other roles from a page that would always
 * look empty.
 */
function ApprovalsAccessGate({ children }: { children: ReactNode }) {
  const t = useTranslations("admin_approvals");
  const auth = useAuthRedirect({ allowedTeamRoles: [...TEAM_SETTINGS_ROLES] });
  if (auth.status === "authed") return <>{children}</>;
  const tone = auth.status === "not_admin" ? "forbidden" : auth.status === "error" ? "error" : "loading";
  return (
    <AdminShell current="approvals" mobileTitle={t("page_title")}>
      <AdminRouteState
        title={t("page_title")}
        tone={tone}
        aside={tone === "forbidden" ? <AccessWhoCanHelpWhenEnabled scope="team" /> : undefined}
      >
        {tone === "forbidden"
          ? t("not_authorized")
          : tone === "error"
            ? t("access_check_failed")
            : t("suspense_loading")}
      </AdminRouteState>
    </AdminShell>
  );
}

function ApprovalsFallback() {
  const t = useTranslations("admin_approvals");
  return <div className="legacy-approvals-fallback">{t("suspense_loading")}</div>;
}

function ApprovalsInner() {
  const t = useTranslations("admin_approvals");
  const searchParams = useSearchParams();
  const focusedApprovalId = searchParams.get("approvalId");
  const [scope, setScope] = useState<Scope>("pending");
  // Store list with its scope: switching hides old-scope entries, even if new-scope fetch fails (never masquerade as new results).
  const [list, setList] = useState<{ scope: Scope; items: ApprovalDTO[] } | null>(null);
  // Whether current scope has received a response (success/failure); until then show Loading rather than No Pending Approvals.
  const [loadedScope, setLoadedScope] = useState<Scope | null>(null);
  const [err, setErr] = useState<ShownError | null>(null);
  const [busy, setBusy] = useState<Set<string>>(new Set());

  // Accept only the latest response: scope switches (including Pending→All→Pending back to the same scope), polling and manual refresh
  // all send new requests; discard older late responses so they never overwrite the current list.
  const requestSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = requestSeq.current + 1;
    requestSeq.current = seq;
    try {
      const next = await fetchApprovals(scope);
      if (seq !== requestSeq.current) return;
      setErr(null);
      setList({ scope, items: next });
    } catch (e: unknown) {
      if (seq !== requestSeq.current) return;
      setErr(shownError(t("load_failed_notice"), e));
    }
    setLoadedScope(scope);
  }, [scope, t]);
  // Clear previous-scope errors on switching; show Loading until new-scope response.
  useEffect(() => {
    setErr(null);
  }, [scope]);
  // Show only current-scope items; empty until current scope has received a successful response.
  const items = list?.scope === scope ? list.items : [];

  useEffect(() => {
    let cancelled = false;
    load();
    const id = window.setInterval(() => {
      if (!cancelled) load();
    }, 4000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [load]);

  const decide = async (
    id: string,
    action: "approve" | "reject",
  ) => {
    setBusy((s) => new Set(s).add(id));
    try {
      if (action === "approve") await approveApproval(id);
      else await rejectApproval(id);
      // The sidebar's pending-approvals pill reads a shared, TTL-cached count; refresh it now.
      notifySettingsNavCountsChanged();
      await load();
    } catch (e: unknown) {
      setErr(shownError(t("decide_failed_notice"), e));
    } finally {
      setBusy((s) => {
        const next = new Set(s);
        next.delete(id);
        return next;
      });
    }
  };

  const pendingCount = items.filter((a) => a.status === "pending").length;

  return (
    <AdminShell current="approvals" mobileTitle={t("page_title")}>
      <div className="admin-config-page">
        <div className="admin-config-page__content">
          <h1 className="ui-page-title">
            {t("page_title")}{" "}
            {scope === "pending" && pendingCount > 0 ? (
              <span className="legacy-approvals-count">
                · {pendingCount}
              </span>
            ) : null}
          </h1>
          <p className="admin-config-page__lead ui-page-desc">
            {t("lead")}
          </p>

          <div className="legacy-approvals-toolbar">
            <SegmentedControl<Scope> value={scope} onValueChange={setScope} aria-label={t("scope_aria")}>
              {(["pending", "all"] as Scope[]).map((s) => (
                <Segment key={s} value={s}>
                  {s === "pending" ? t("scope_pending") : t("scope_all")}
                </Segment>
              ))}
            </SegmentedControl>
            <button type="button" className="btn-secondary" onClick={load}>
              {t("refresh")}
            </button>
          </div>

          {err ? (
            <div className="approval-card legacy-approvals-error">
              <strong>{t("error_label")}</strong> {err.message}
              <ErrorDiagnosticsInline error={err.error} surface="route" />
            </div>
          ) : null}

          {loadedScope !== scope && !err ? (
            <LoadingSkeleton rows={3} label={t("suspense_loading")} />
          ) : null}

          {loadedScope === scope && list?.scope === scope && items.length === 0 && !err ? (
            <div className="ui-empty legacy-approvals-empty">
              {scope === "pending"
                ? t("empty_pending")
                : t("empty_history")}
            </div>
          ) : null}

          {items.map((a) => (
            <ApprovalCard
              key={a.id}
              a={a}
              focused={a.id === focusedApprovalId}
              busy={busy.has(a.id)}
              onDecide={(act) => decide(a.id, act)}
              t={t}
            />
          ))}
        </div>
      </div>
    </AdminShell>
  );
}

function ApprovalCard({
  a,
  focused,
  busy,
  onDecide,
  t,
}: {
  a: ApprovalDTO;
  focused: boolean;
  busy: boolean;
  onDecide: (action: "approve" | "reject") => void;
  t: TFn;
}) {
  // C11: arguments preview is the core decision input — default expanded
  const [expanded, setExpanded] = useState(true);
  const pending = a.status === "pending";

  return (
    <div
      className={clsx(
        "approval-card",
        pending
          ? "legacy-approval-card--pending"
          : a.status === "rejected"
          ? "legacy-approval-card--rejected"
          : "legacy-approval-card--done",
        { "approval-card--focused": focused },
      )}
    >
      <div className="ap-head">
        <div className="ap-icon">
          <Pause />
        </div>
        <div>
          <div className="ap-title">{a.toolName}</div>
          <div className="ap-meta">
            {t("meta")} <code>{a.turnId}</code> · {t("meta_workspace")} {a.workspaceId} · {t("meta_risk")}{" "}
            <RiskBadge level={a.riskLevel} /> · {relTime(a.createdAt, t)}
          </div>
        </div>
        <div className="legacy-approval-status">
          <span className={clsx("intent-badge", statusClass(a.status))}>
            <span className="ib-dot" />
            {a.status}
          </span>
        </div>
      </div>

      {a.reason ? (
        <>
          <div className="ap-section-label">{t("reason")}</div>
          <p className="legacy-approval-reason">{a.reason}</p>
        </>
      ) : null}

      <div className="ap-section-label">{t("arguments")}</div>
      <pre className={clsx("diff-block legacy-approval-args", { "is-collapsed": !expanded })}>
        {JSON.stringify(a.arguments, null, 2)}
      </pre>

      {a.executionOk !== null ? (
        <>
          <div className="ap-section-label">
            {a.executionOk ? t("execution_ok") : t("execution_failed")}{" "}
            {a.executedAt ? `· ${relTime(a.executedAt, t)}` : ""}
          </div>
          <pre className="diff-block legacy-approval-result">
            {a.executionOk
              ? JSON.stringify(a.executionResult, null, 2)
              : a.executionError || t("no_error_details")}
          </pre>
        </>
      ) : null}

      <div className="ap-actions">
        <button
          type="button"
          className="btn-secondary"
          onClick={() => setExpanded((x) => !x)}
        >
          {expanded ? t("collapse") : t("expand")}
        </button>
        {pending ? (
          <>
            <button
              type="button"
              className="btn-danger"
              disabled={busy}
              onClick={() => onDecide("reject")}
            >
              {t("reject")}
            </button>
            <button
              type="button"
              className="btn-primary"
              disabled={busy}
              onClick={() => onDecide("approve")}
            >
              {t("approve")}
            </button>
          </>
        ) : (
          <span className="legacy-approval-decided">
            {t("decided_by", { who: a.decidedBy || "—" })} {a.decidedAt ? `· ${relTime(a.decidedAt, t)}` : ""}
          </span>
        )}
      </div>
    </div>
  );
}

// C12: colored risk badge — high/medium/low get visually distinct chips so admins
// can scan the queue without reading every label.
function RiskBadge({ level }: { level: string }) {
  const lc = (level || "").toLowerCase();
  const tone = lc === "high" || lc === "medium" ? lc : "low";
  return <span className={`legacy-risk-badge is-${tone}`}>{level}</span>;
}

function statusClass(s: ApprovalDTO["status"]): string {
  switch (s) {
    case "pending":
      return "modeling";
    case "approved":
    case "executed":
      return "compute";
    case "rejected":
    case "execution_failed":
      return "causal";
    case "expired":
    default:
      return "compute";
  }
}

function relTime(iso: string, t: TFn): string {
  try {
    const d = new Date(iso);
    const sec = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
    if (sec < 60) return t("rel_seconds_ago", { n: sec });
    if (sec < 3600) return t("rel_minutes_ago", { n: Math.floor(sec / 60) });
    if (sec < 86400) return t("rel_hours_ago", { n: Math.floor(sec / 3600) });
    return d.toLocaleDateString();
  } catch {
    return iso;
  }
}
