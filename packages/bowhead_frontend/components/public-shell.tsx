"use client";

/**
 * The single shell for signed-out public pages: sign-in, registration, activation, forgot / reset password, invitation acceptance, no team access,
 * and the post-invitation profile page. Layout: brand row + one column (solid card on desktop, no card on phones) + footer language button.
 *
 * It mounts no authenticated shell (no sidebar, top bar or session drawer) and calls no authenticated API; the brand comes only from
 * the gateway first-paint injection read by `PublicBrandProvider`, falling back to the product default when missing or unparsable.
 * Spec and decisions: the "公开页面" (public pages) section of docs/design/组件统一-技术设计.md.
 */

import {
  type ReactNode,
  type SVGProps,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { useLocale, useTranslations } from "next-intl";

import { BowheadLogo } from "@/components/bowhead-logo";
import { useBrand } from "@/components/brand-provider";
import { AlertCircle, Check, ChevronDown } from "@/components/icons";
import { MenuFloating, useMenu, useMenuItem } from "@/components/ui/menu";
import { appPath } from "@/lib/base-path";
import type { Locale } from "@/lib/locale";
import { LOCALE_NATIVE_NAME, LOCALE_ORDER, writeLocaleCookie } from "@/lib/locale-names";
import {
  isCanonicalBrandBundle,
  type BrandRenderable,
} from "@/lib/theme-config";

import "@/styles/public-pages.css";

export type PublicTone = "neutral" | "success" | "warning" | "danger";

export interface PublicShellProps {
  /** The page's only h1: names the action ("Sign in", "Set a new password") without repeating the brand. */
  title: string;
  subtitle?: ReactNode;
  /** One line below the card ("No account yet? Sign up"). */
  footer?: ReactNode;
  children?: ReactNode;
  /** 400 for form pages, 440 for invitation pages. */
  width?: "form" | "invite";
  /** Above the title: the invitation flow's "Step 1 of 3". */
  steps?: ReactNode;
  /** Between title and description: the inviter. */
  byline?: ReactNode;
  /** Result card: 44px icon circle. */
  status?: { tone: PublicTone; icon: ReactNode; delayed?: boolean };
  /** When a result card appears, focus and announce its title ("Sent", "Password updated"). */
  announce?: boolean;
}

export function PublicShell({
  title,
  subtitle,
  footer,
  children,
  width = "form",
  steps,
  byline,
  status,
  announce = false,
}: PublicShellProps) {
  const titleId = useId();
  const titleRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (announce) titleRef.current?.focus();
  }, [announce, title]);
  return (
    <div className="pub-page">
      <main
        className={
          width === "invite" ? "pub-column pub-column--wide" : "pub-column"
        }
      >
        <PublicBrandRow />
        <section className="pub-card" aria-labelledby={titleId}>
          {steps}
          <div
            className="pub-result-head"
            role={announce ? "status" : undefined}
          >
            {status ? (
              <span
                className={`pub-status-icon pub-status-icon--${status.tone}${status.delayed ? " pub-delayed" : ""}`}
                aria-hidden="true"
              >
                {status.icon}
              </span>
            ) : null}
            <h1
              id={titleId}
              ref={titleRef}
              className="pub-title"
              tabIndex={announce ? -1 : undefined}
            >
              {title}
            </h1>
            {byline ? <p className="pub-inviter">{byline}</p> : null}
            {subtitle ? <p className="pub-lead">{subtitle}</p> : null}
          </div>
          {children}
        </section>
        {footer ? <div className="pub-card-foot">{footer}</div> : null}
        <footer className="pub-page-foot">
          <PublicLocaleMenu />
        </footer>
      </main>
    </div>
  );
}

/**
 * Brand row: logo + application name, shown once per page. The product default and built-in themes show the Bowhead mark;
 * a custom brand is fully white-labelled (only the customer's logo and name); without a logo only the name shows, with no initial-letter tile.
 */
function PublicBrandRow() {
  const { brand, bundle } = useBrand();
  const bowhead =
    isCanonicalBrandBundle(bundle) ||
    ["bowhead", "bowra"].includes(brand.applicationName.trim().toLowerCase());
  const light = assetPath(brand.logoOnLight);
  const dark = assetPath(brand.logoOnDark);
  const [broken, setBroken] = useState(false);
  let logo: ReactNode = null;
  if ((light || dark) && !broken) {
    logo = (
      <span className="pub-brand__logo">
        <img
          src={appPath(light ?? dark ?? "")}
          alt=""
          className="brand-graphic__light"
          onError={() => setBroken(true)}
        />
        <img
          src={appPath(dark ?? light ?? "")}
          alt=""
          className="brand-graphic__dark"
          onError={() => setBroken(true)}
        />
      </span>
    );
  } else if (bowhead) {
    logo = (
      <span className="pub-brand__logo">
        <BowheadLogo size={56} aria-hidden="true" focusable="false" />
      </span>
    );
  }
  return (
    <div className="pub-brand" data-testid="public-brand">
      {logo}
      <span className="pub-brand__name" translate="no">
        {brand.applicationName}
      </span>
    </div>
  );
}

function assetPath(renderable: BrandRenderable): string | null {
  return renderable.kind === "asset" ? renderable.path : null;
}

/**
 * Translation function for public-page copy: same usage as `useTranslations(namespace)`, and it also passes the current brand's application name
 * as the `{applicationName}` argument to every message. The product name lives only in message templates (`{applicationName}`);
 * interpolated sentences are never rewritten, so team, space and inviter names containing "Bowhead" render verbatim,
 * and characters like `$&` in the application name are not treated as replacement patterns. An explicit `applicationName` from the caller wins.
 */
export function useBrandedTranslations<Namespace extends string>(
  namespace: Namespace,
): ReturnType<typeof useTranslations<Namespace>> {
  const t = useTranslations(namespace);
  const { brand } = useBrand();
  // Read a ref instead of closing over the name: the returned function's identity changes only with t, so page effects do not rerun (and refetch) when the brand switches after hydration;
  // the brand change itself rerenders consumers, which then read the current application name.
  const nameRef = useRef(brand.applicationName);
  nameRef.current = brand.applicationName;
  return useMemo(() => {
    type Values = Record<string, unknown> | undefined;
    const withName = (values: Values) => ({ applicationName: nameRef.current, ...(values ?? {}) });
    const call = t as unknown as (key: string, values?: Values, formats?: unknown) => string;
    const branded = (key: string, values?: Values, formats?: unknown) =>
      call(key, withName(values), formats);
    const rich = t.rich as unknown as (key: string, values?: Values, formats?: unknown) => unknown;
    const markup = t.markup as unknown as (key: string, values?: Values, formats?: unknown) => unknown;
    return Object.assign(branded, t, {
      rich: (key: string, values?: Values, formats?: unknown) => rich(key, withName(values), formats),
      markup: (key: string, values?: Values, formats?: unknown) => markup(key, withName(values), formats),
    }) as unknown as ReturnType<typeof useTranslations<Namespace>>;
  }, [t]);
}

/* ── Language ─────────────────────────────────────────────────────── */

// Same native-name table and order as the in-app language menu (Header, Language & Paging Spec D1); public pages
// offer no "Automatic": there is no account to save it to, and clearing the cookie already follows the browser.
const EMAIL_DRAFT_KEY = "bowhead.public.email_draft";

/**
 * Switch language on public pages: only write the `NEXT_LOCALE` cookie and reload, without calling any authenticated API.
 * A typed email is stashed in this tab and restored by `usePublicEmailDraft` after reload; passwords are not kept.
 */
function switchPublicLocale(next: Locale): void {
  try {
    const email = document.querySelector<HTMLInputElement>(
      '.pub-page input[type="email"]:not([readonly])',
    )?.value;
    if (email) window.sessionStorage.setItem(EMAIL_DRAFT_KEY, email);
  } catch {
    /* When storage is unavailable the email is simply not kept */
  }
  writeLocaleCookie(next);
  window.location.reload();
}

/** Restore the email after a language-switch reload (read once). */
export function usePublicEmailDraft(restore: (email: string) => void): void {
  const restoreRef = useRef(restore);
  restoreRef.current = restore;
  useEffect(() => {
    try {
      const draft = window.sessionStorage.getItem(EMAIL_DRAFT_KEY);
      if (draft) {
        window.sessionStorage.removeItem(EMAIL_DRAFT_KEY);
        restoreRef.current(draft);
      }
    } catch {
      /* Storage unavailable */
    }
  }, []);
}

function PublicLocaleMenu() {
  const locale = useLocale() as Locale;
  const t = useTranslations("topbar");
  const [open, setOpen] = useState(false);
  const menu = useMenu({
    open,
    onOpenChange: setOpen,
    placement: "top",
    selectedIndex: LOCALE_ORDER.indexOf(locale),
  });
  return (
    <>
      <button
        ref={menu.refs.setReference}
        type="button"
        className="pub-lang"
        aria-label={t("switch_language_current", {
          locale: LOCALE_NATIVE_NAME[locale] ?? locale,
        })}
        {...menu.getReferenceProps()}
      >
        <GlobeIcon aria-hidden="true" />
        <span>{LOCALE_NATIVE_NAME[locale] ?? locale}</span>
        <ChevronDown size={12} aria-hidden="true" />
      </button>
      <MenuFloating menu={menu}>
        <div
          ref={menu.refs.setFloating}
          {...menu.getFloatingProps({ className: "pub-menu" })}
        >
          {LOCALE_ORDER.map((option) => (
            <LocaleOption
              key={option}
              locale={option}
              checked={option === locale}
              onPick={(next) => {
                if (next !== locale) switchPublicLocale(next);
              }}
            />
          ))}
        </div>
      </MenuFloating>
    </>
  );
}

function LocaleOption({
  locale,
  checked,
  onPick,
}: {
  locale: Locale;
  checked: boolean;
  onPick: (next: Locale) => void;
}) {
  const itemProps = useMenuItem({
    role: "menuitemradio",
    checked,
    label: LOCALE_NATIVE_NAME[locale],
    onSelect: () => onPick(locale),
  });
  return (
    <button type="button" className="pub-menu-item" lang={locale} {...itemProps}>
      <span className="pub-menu-item__check" aria-hidden="true">
        {checked ? <Check size={14} /> : null}
      </span>
      <span>{LOCALE_NATIVE_NAME[locale]}</span>
    </button>
  );
}

/* ── Banners, buttons and icons ─────────────────────────────────────────── */

export type PublicBannerTone = "error" | "warning" | "info" | "success";

/**
 * Form-top banner: icon + text (no color bar). Errors and warnings use `role="alert"`, info and success `role="status"`.
 * When `focusKey` changes, focus moves to the banner (tabindex=-1) so screen readers start reading there.
 */
export function PublicBanner({
  tone = "error",
  title,
  children,
  icon,
  focusKey,
  testId,
}: {
  tone?: PublicBannerTone;
  title?: ReactNode;
  children?: ReactNode;
  icon?: ReactNode;
  focusKey?: unknown;
  testId?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (focusKey !== undefined && focusKey !== null && focusKey !== false) {
      ref.current?.focus();
    }
  }, [focusKey]);
  const role = tone === "error" || tone === "warning" ? "alert" : "status";
  return (
    <div
      ref={ref}
      role={role}
      tabIndex={-1}
      className={tone === "error" ? "pub-banner" : `pub-banner pub-banner--${tone}`}
      data-testid={testId}
    >
      <span className="pub-banner__icon" aria-hidden="true">
        {icon ?? <AlertCircle size={14} />}
      </span>
      <span className="pub-banner__body">
        {title ? <b className="pub-banner__title">{title}</b> : null}
        {children}
      </span>
    </div>
  );
}

/** Primary button while submitting: inline spinner + "...ing" label, same width, not clickable. */
export function BusyLabel({
  busy,
  idle,
  pending,
}: {
  busy: boolean;
  idle: ReactNode;
  pending: ReactNode;
}) {
  return busy ? (
    <>
      <span className="pub-spinner" aria-hidden="true" />
      <span>{pending}</span>
    </>
  ) : (
    <span>{idle}</span>
  );
}

/** The three invitation steps as one line ("Step 1 of 3 · Confirm invitation"), never truncated on phones. */
export function PublicSteps({
  current,
  total = 3,
  label,
}: {
  current: number;
  total?: number;
  label: string;
}) {
  const t = useTranslations("invite");
  return (
    <div className="pub-steps">
      {Array.from({ length: total }, (_, index) => {
        const step = index + 1;
        const className =
          step < current
            ? "pub-steps__dot pub-steps__dot--done"
            : step === current
              ? "pub-steps__dot pub-steps__dot--current"
              : "pub-steps__dot";
        return <span key={step} className={className} aria-hidden="true" />;
      })}
      <span className="pub-steps__text">
        {t("progress.position", { current, total })}
        {" · "}
        <b>
          {label}
        </b>
      </span>
    </div>
  );
}

/** Invitation details: grouped list (team / space / invited email / expiry). */
export function PublicSummary({
  label,
  rows,
}: {
  label: string;
  rows: Array<{ term: string; value: ReactNode; detail?: ReactNode }>;
}) {
  return (
    <dl className="pub-summary" aria-label={label}>
      {rows.map((row) => (
        <div key={row.term}>
          <dt>{row.term}</dt>
          <dd>
            {row.value}
            {row.detail ? <small>{row.detail}</small> : null}
          </dd>
        </div>
      ))}
    </dl>
  );
}

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function lucide(size: number, props: SVGProps<SVGSVGElement>) {
  return {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.5,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    ...props,
  };
}

function GlobeIcon({ size = 14, ...props }: IconProps) {
  return (
    <svg {...lucide(size, props)}>
      <circle cx="12" cy="12" r="10" />
      <path d="M2 12h20" />
      <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
    </svg>
  );
}

export function BuildingIcon({ size = 16, ...props }: IconProps) {
  return (
    <svg {...lucide(size, props)}>
      <rect x="4" y="2" width="16" height="20" rx="2" />
      <path d="M9 22v-4h6v4" />
      <path d="M8 6h.01M16 6h.01M12 6h.01M12 10h.01M12 14h.01M16 10h.01M16 14h.01M8 10h.01M8 14h.01" />
    </svg>
  );
}

export function LinkOffIcon({ size = 20, ...props }: IconProps) {
  return (
    <svg {...lucide(size, props)}>
      <path d="M9 17H7A5 5 0 0 1 7 7" />
      <path d="M15 7h2a5 5 0 0 1 4 8" />
      <path d="M8 12h4" />
      <path d="m2 2 20 20" />
    </svg>
  );
}

export function CircleCheckIcon({ size = 20, ...props }: IconProps) {
  return (
    <svg {...lucide(size, props)}>
      <circle cx="12" cy="12" r="10" />
      <path d="m9 12 2 2 4-4" />
    </svg>
  );
}


export function MailIcon({ size = 20, ...props }: IconProps) {
  return (
    <svg {...lucide(size, props)}>
      <rect x="2" y="4" width="20" height="16" rx="2" />
      <path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7" />
    </svg>
  );
}

export function UsersIcon({ size = 20, ...props }: IconProps) {
  return (
    <svg {...lucide(size, props)}>
      <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
      <circle cx="9" cy="7" r="4" />
      <path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" />
    </svg>
  );
}

export function UserIcon({ size = 14, ...props }: IconProps) {
  return (
    <svg {...lucide(size, props)}>
      <circle cx="12" cy="8" r="4" />
      <path d="M20 21a8 8 0 0 0-16 0" />
    </svg>
  );
}

export function LockIcon({ size = 14, ...props }: IconProps) {
  return (
    <svg {...lucide(size, props)}>
      <rect x="4" y="11" width="16" height="10" rx="2" />
      <path d="M8 11V7a4 4 0 0 1 8 0v4" />
    </svg>
  );
}

export function InfoIcon({ size = 14, ...props }: IconProps) {
  return (
    <svg {...lucide(size, props)}>
      <circle cx="12" cy="12" r="10" />
      <path d="M12 16v-4M12 8h.01" />
    </svg>
  );
}

export function FeishuMark() {
  return (
    <span className="pub-sso-mark" aria-hidden="true">
      <span />
      <span />
      <span />
      <span />
    </span>
  );
}
