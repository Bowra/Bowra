"use client";

/**
 * Licensing FE slice A (§13.2) — entitlement status banner.
 *
 * Full-width strip pinned below the top nav (mounted in main.v2-main after
 * v2-main-head, app/page.tsx), pushing the workspace down — never an overlay.
 * `active` renders nothing (healthy is silent). State comes from the
 * entitlement-store (X-Entitlement-Health on a 200, or the 402/403 deny code).
 * Verbatim translation of design/entitlement-banner/entitlement-banner.html.
 *
 * CN copy is locked here (mirrors error-messages.ts — no LLM/i18n leakage; the
 * product is zh-CN first). Action links/CTAs route to LICENSE_ACTION_HREF — the
 * in-app admin licensing home by default (no dead `#` links); an external
 * sales/support portal can be supplied via NEXT_PUBLIC_BOWHEAD_LICENSE_CONTACT_URL.
 * An online activation/renewal flow is Phase 3 (out of scope).
 */

import { useTranslations } from "next-intl";

import { useEntitlement } from "@/lib/entitlement-store";
import { appPath } from "@/lib/base-path";
import {
  BadgeCheck as LucideBadgeCheck,
  CircleX as LucideCircleX,
  Lock as LucideLock,
  RotateCw as LucideRotateCw,
  TriangleAlert as LucideTriangleAlert,
  Upload as LucideUpload,
  X as LucideX,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";
import { CharIconText } from "@/components/char-icon";

/** Where Renew / Upload License / Contact Support / View Reason point. Admin licensing home by default.
 *  `appPath` is a no-op on the external-URL override, so this stays safe either way. */
const LICENSE_ACTION_HREF = appPath(
  process.env.NEXT_PUBLIC_BOWHEAD_LICENSE_CONTACT_URL || "/team/settings/license/",
);

const IconWarn = () => (
  <LucideGlyph icon={LucideTriangleAlert} size={13} />
);
const IconStop = () => (
  <LucideGlyph icon={LucideCircleX} size={13} />
);
const IconServer = () => (
  <LucideGlyph icon={LucideBadgeCheck} size={13} />
);
const IconCrit = () => (
  <LucideGlyph icon={LucideTriangleAlert} size={13} />
);
const IconLock = () => (
  <LucideGlyph icon={LucideLock} size={12} />
);
const IconUpload = () => (
  <LucideGlyph icon={LucideUpload} size={13} />
);
const IconDegraded = () => (
  <LucideGlyph icon={LucideRotateCw} size={11} />
);
const IconClose = () => (
  <LucideGlyph icon={LucideX} size={13} />
);

/** Read-only badge (lock + read-only mode) shared by the banner strip and the composer chip. */
function RoBadge() {
  const t = useTranslations("entitlement");
  return (
    <span className="ent-banner__ro">
      <IconLock />
      {t("read_only")}
    </span>
  );
}

/** The persistent read-only chip rendered near the composer input (over_grace / suspended). */
export function EntitlementReadOnlyChip() {
  const t = useTranslations("entitlement");
  return (
    <span className="ent-ro-chip" data-testid="ent-ro-chip">
      <IconLock />
      {t("read_only")} <span className="dim">{t("read_only_restore")}</span>
    </span>
  );
}

function DegradedTag() {
  const t = useTranslations("entitlement");
  return (
    <span className="ent-banner__degraded">
      <IconDegraded />
      {t("degraded_tag")}
    </span>
  );
}

/** YYYY-MM-DD from an ISO grace-until (handles both date-only and full timestamps). */
function fmtGraceDate(raw: string | null): string {
  if (!raw) return "";
  return /^\d{4}-\d{2}-\d{2}/.test(raw) ? raw.slice(0, 10) : raw;
}

export function EntitlementBanner() {
  const t = useTranslations("entitlement");
  const ent = useEntitlement();
  if (!ent.visible) return null;

  const { health, graceUntil, degraded, dismissible, dismiss } = ent;
  const dismissBtn = dismissible ? (
    <button className="ent-banner__x" type="button" aria-label={t("close")} onClick={dismiss}>
      <IconClose />
    </button>
  ) : null;

  // ── grace — amber warning, dismissible (re-appears next session) ──
  if (health === "grace") {
    return (
      <div role="status" aria-live="polite" className="ent-banner ent-banner--status" data-testid="ent-banner" data-state="grace">
        <span className="ent-banner__icon" aria-hidden><IconWarn /></span>
        <span className="ent-banner__msg">
          {t("grace_msg_before")}<span className="date">{fmtGraceDate(graceUntil)}</span>{t("grace_msg_after")}
        </span>
        {degraded ? <DegradedTag /> : null}
        <span className="ent-banner__spacer" />
        <a className="ent-banner__link" href={LICENSE_ACTION_HREF}><CharIconText ch="↗" at="end">{t("contact_sales_renew")}</CharIconText></a>
        {dismissBtn}
      </div>
    );
  }

  // ── over_grace — read-only, persistent ──
  if (health === "over_grace") {
    return (
      <div role="alert" className="ent-banner ent-banner--error" data-testid="ent-banner" data-state="over_grace">
        <RoBadge />
        <span className="ent-banner__msg">
          {t("over_grace_msg_before")}<b>{t("read_only")}</b>{t("over_grace_msg_after")}
        </span>
        {degraded ? <DegradedTag /> : null}
        <span className="ent-banner__spacer" />
        <a className="ent-banner__link" href={LICENSE_ACTION_HREF}><CharIconText ch="↗" at="end">{t("contact_sales_renew")}</CharIconText></a>
        <a className="ent-banner__cta" href={LICENSE_ACTION_HREF}>{t("renew_now")}</a>
      </div>
    );
  }

  // ── suspended — admin-directed, no self-serve CTA ──
  if (health === "suspended") {
    return (
      <div role="alert" className="ent-banner ent-banner--error" data-testid="ent-banner" data-state="suspended">
        <span className="ent-banner__icon" aria-hidden><IconStop /></span>
        <span className="ent-banner__msg">
          <b>{t("suspended_team")}</b>{t("suspended_msg_after")}
        </span>
        <span className="ent-banner__ro" style={{ marginLeft: 2 }}>{t("read_only")}</span>
        <span className="ent-banner__spacer" />
        <a className="ent-banner__link" href={LICENSE_ACTION_HREF}><CharIconText ch="↗" at="end">{t("view_suspend_reason")}</CharIconText></a>
      </div>
    );
  }

  // ── needs_license — on-prem not activated, neutral-urgent ──
  if (health === "needs_license") {
    return (
      <div role="alert" className="ent-banner ent-banner--neutral" data-testid="ent-banner" data-state="needs_license">
        <span className="ent-banner__icon" aria-hidden><IconServer /></span>
        <span className="ent-banner__msg">
          {t("needs_license_before")}<b>{t("needs_license_term")}</b>{t("needs_license_after")}
        </span>
        <span className="ent-banner__spacer" />
        <a className="ent-banner__link" href={LICENSE_ACTION_HREF}>{t("how_to_get_license")}</a>
        <a className="ent-banner__cta" href={LICENSE_ACTION_HREF}><IconUpload />{t("upload_license")}</a>
      </div>
    );
  }

  // ── tamper — strongest, app hard-blocked ──
  if (health === "tamper") {
    return (
      <div role="alert" className="ent-banner ent-banner--crit" data-testid="ent-banner" data-state="tamper">
        <span className="ent-banner__icon" aria-hidden><IconCrit /></span>
        <span className="ent-banner__msg">
          <b>{t("tamper_label")}</b>{t("tamper_msg_after")}
        </span>
        <span className="ent-banner__spacer" />
        <span className="ent-banner__code">err · E-LIC-403</span>
        <a className="ent-banner__cta" href={LICENSE_ACTION_HREF}>{t("contact_support")}</a>
      </div>
    );
  }

  // ── active + degraded — quietest standalone strip (informational, never red) ──
  if (health === "active" && degraded) {
    return (
      <div role="status" aria-live="polite" className="ent-banner ent-banner--muted" data-testid="ent-banner" data-state="degraded">
        <span className="ent-banner__icon" aria-hidden><IconDegraded /></span>
        <span className="ent-banner__msg">{t("active_degraded_msg")}</span>
        <span className="ent-banner__spacer" />
        {dismissBtn}
      </div>
    );
  }

  return null;
}
