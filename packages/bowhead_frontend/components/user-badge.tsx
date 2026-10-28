"use client";

/**
 * UserBadge—Topbar shows current user tier + credit balance.
 *
 * P4.5b #18: users had no visible credit after login → top-right "ULTRA · 30000 ⚡".
 * Fetch /api/v1/auth/me, lightweight refresh every minute.
 */

import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
} from "react";
import {
  FloatingFocusManager,
  FloatingNode,
  FloatingPortal,
  autoUpdate,
  flip,
  offset,
  shift,
  useDismiss,
  useFloating,
  useFloatingNodeId,
  useInteractions,
  useRole,
} from "@floating-ui/react";
import { Gauge } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { AuthError, fetchMe, type MeUser } from "@/lib/auth-api";
import { appPath } from "@/lib/base-path";
import { creditUsage, formatCreditResetDate } from "@/lib/credit-period";
import { balancePct, isCriticalBalance, isLowBalance } from "@/lib/credit-thresholds";
import { liftedZIndex } from "@/lib/layer";
import { useAppRouter } from "@/lib/use-app-router";
import { renderRefreshSvg } from "@/components/icon-refresh";
import { DialogShell } from "@/components/ui/dialog";
import { useNarrowViewport } from "@/components/ui/sheet";

export function UserBadge() {
  const [me, setMe] = useState<MeUser | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setInterval> | null = null;
    const load = async () => {
      try {
        const u = await fetchMe();
        if (!cancelled) setMe(u);
      } catch (e) {
        // 401 = unauthenticated, stop polling; retain polling for other errors (network fluctuations).
        if (e instanceof AuthError && e.status === 401 && timer) {
          clearInterval(timer);
          timer = null;
        }
      }
    };
    void load();
    timer = setInterval(() => void load(), 60_000);
    return () => { cancelled = true; if (timer) clearInterval(timer); };
  }, []);

  if (!me) return null;

  return <CreditBadgeButton me={me} />;
}

/** Own credit ledger: the only credit detail page today, and only for roles that can read team credits. */
export function creditDetailPath(me: Pick<MeUser, "id" | "credit_capabilities">): string | null {
  if (me.credit_capabilities?.credits_read !== true || !me.id) return null;
  return `/admin/credits/users?user_id=${encodeURIComponent(me.id)}`;
}

/**
 * P2 batch 3 (Sidebar & Topbar spec, topbar credits) plus the spec's credits popover: the badge is a button
 * showing the gauge and the balance (only a low balance changes colour and adds words); activating it opens
 * a material popover (narrow screens: the shared bottom sheet) with the plan, a usage bar, the reset date,
 * the low-balance message and, when the role can read it, a link to the user's own credit ledger.
 */
function CreditBadgeButton({ me }: { me: MeUser }) {
  const t = useTranslations("sidebar");
  const locale = useLocale();
  const router = useAppRouter();
  const [open, setOpen] = useState(false);
  const narrow = useNarrowViewport(true);
  const nodeId = useFloatingNodeId();
  const titleId = useId();
  const panelId = useId();
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);

  const floating = useFloating({
    nodeId,
    open: open && !narrow,
    onOpenChange: setOpen,
    placement: "bottom-end",
    strategy: "fixed",
    whileElementsMounted: autoUpdate,
    middleware: [offset(6), flip({ padding: 8 }), shift({ padding: 8 })],
  });
  const dismiss = useDismiss(floating.context);
  const role = useRole(floating.context, { role: "dialog" });
  const { getReferenceProps, getFloatingProps } = useInteractions([dismiss, role]);

  // Opened from inside the narrow drawer (a modal layer), the body-mounted popover lifts above it (section 图层).
  const [liftZ, setLiftZ] = useState<number | null>(null);
  const floatingElement = floating.elements.floating;
  useLayoutEffect(() => {
    if (!open || !floatingElement) {
      setLiftZ(null);
      return;
    }
    setLiftZ((prev) => prev ?? liftedZIndex(floatingElement, [triggerRef.current]));
  }, [open, floatingElement]);

  const balance = me.credit_balance;
  const quota = me.credit_quota;
  const balanceText = balance.toLocaleString();
  const pct = balancePct(balance, quota);
  const critical = isCriticalBalance(balance, quota);
  const low = isLowBalance(balance, quota);
  const level = critical ? "critical" : low ? "low" : undefined;
  const value = critical
    ? t("credits_critical", { balance: balanceText })
    : low && pct !== null
      ? t("credits_low", { pct: Math.round(pct * 100), balance: balanceText })
      : balanceText;
  // Plan names are product names (Pro / Ultra), not translated
  const plan = me.tier.charAt(0).toUpperCase() + me.tier.slice(1);
  const usage = creditUsage(balance, quota);
  const resetDate = formatCreditResetDate(me.credit_period_resets_at, locale);
  const detailPath = creditDetailPath(me);
  const stateMessage = !level
    ? null
    : usage === null
      ? t("credits_state_empty")
      : t(level === "critical" ? "credits_state_critical" : "credits_state_low", { pct: usage.leftPct });

  const close = () => setOpen(false);
  const openDetail = (event: ReactMouseEvent<HTMLAnchorElement>) => {
    if (!detailPath || event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    close();
    router.push(detailPath);
  };
  // The narrow drawer cycles Tab inside its own panel; keep Tab inside the popover to the popover itself.
  const keepTab = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.key === "Tab") event.stopPropagation();
  };

  // Built only while open: the closed badge renders no popover copy.
  const renderBody = () => (
    <>
      <h2 id={titleId} className="ui-credit-pop__title">
        {t("credits_plan", { plan })}
      </h2>
      <p className="ui-credit-pop__balance">
        {t.rich("credits_left", {
          balance: balanceText,
          num: (chunks) => <span className="ui-credit-pop__num">{chunks}</span>,
        })}
      </p>
      {usage ? (
        <>
          <div
            role="progressbar"
            className="ui-credit-pop__bar"
            data-level={level}
            aria-label={t("credits_usage_label")}
            aria-valuemin={0}
            aria-valuemax={usage.quota}
            aria-valuenow={usage.used}
            aria-valuetext={t("credits_usage_value", {
              used: usage.used.toLocaleString(),
              quota: usage.quota.toLocaleString(),
              pct: usage.usedPct,
            })}
            style={{ "--ui-credit-used": `${usage.usedPct}%` } as CSSProperties}
          >
            <span className="ui-credit-pop__fill" />
          </div>
          <p className="ui-credit-pop__meta" aria-hidden="true">
            {t("credits_used_of", { used: usage.used.toLocaleString(), quota: usage.quota.toLocaleString() })}
          </p>
        </>
      ) : (
        <p className="ui-credit-pop__meta">{t("credits_allowance_unknown")}</p>
      )}
      {resetDate ? <p className="ui-credit-pop__meta">{t("credits_resets_on", { date: resetDate })}</p> : null}
      {stateMessage ? (
        <p className="ui-credit-pop__state" data-level={level}>
          {stateMessage}
        </p>
      ) : null}
      {detailPath ? (
        <a className="ui-credit-pop__link" href={appPath(detailPath)} onClick={openDetail}>
          {t("credits_view_details")}
        </a>
      ) : null}
    </>
  );

  return (
    <>
      <button
        type="button"
        ref={(node) => {
          triggerRef.current = node;
          floating.refs.setReference(node);
        }}
        data-testid="user-credit-badge"
        className="ui-credit-badge"
        data-level={level}
        {...getReferenceProps({ onClick: () => setOpen((current) => !current) })}
        aria-label={t("credits_button_aria", { value })}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
      >
        {renderRefreshSvg(Gauge, 14, 14, { "aria-hidden": true })}
        <span className="ui-credit-badge__value">{value}</span>
      </button>
      <FloatingNode id={nodeId}>
        {open && !narrow ? (
          <FloatingPortal>
            <FloatingFocusManager context={floating.context} modal={false} initialFocus={panelRef}>
              <div
                ref={(node) => {
                  panelRef.current = node;
                  floating.refs.setFloating(node);
                }}
                className="ui-credit-pop"
                tabIndex={-1}
                style={{ ...floating.floatingStyles, zIndex: liftZ ?? "var(--z-menu)" }}
                {...getFloatingProps({ onKeyDown: keepTab })}
                id={panelId}
                aria-labelledby={titleId}
              >
                {renderBody()}
              </div>
            </FloatingFocusManager>
          </FloatingPortal>
        ) : null}
      </FloatingNode>
      {open && narrow ? (
        <DialogShell
          onClose={close}
          presentation="sheet"
          labelledBy={titleId}
          returnFocusRef={triggerRef}
          backdropClassName="settings-dialog-backdrop"
          panelClassName="ui-credit-pop ui-credit-pop--sheet"
          panelProps={{ id: panelId, onKeyDown: keepTab }}
        >
          {renderBody()}
        </DialogShell>
      ) : null}
    </>
  );
}
