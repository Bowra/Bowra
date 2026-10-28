"use client";

/**
 * ChatCard — the one card shell for everything the conversation stream shows besides prose
 * (Claude Design "Chat Cards Spec" parts 1 and 2, decisions D1–D12, approved 2026-10-05).
 *
 * One shell: content surface, hairline structural edge, radius --r-4, no shadow, no tint, no accent bar.
 * Only `intent="input"` (the user must act) swaps the edge for a 1px warning edge. The intent colours the
 * leading icon and the eyebrow only; body text always stays ink / ink-2.
 *
 * Two sizes: `regular` (eyebrow, title, body, footer) for things to read or decide, and `row` (40 high,
 * icon + one sentence + trailing actions) for things that already happened. Settled states collapse to
 * rows in place, so live and replayed turns show the same line.
 *
 * Accessibility: the card is a `role="group"` named by its eyebrow and title (the status word is part of
 * the name, e.g. "Needs your answer: …"). `live` decides announcements: input cards are silent (their
 * name already says what is needed), receipts and progress are polite (progress meta such as elapsed time
 * is excluded), failures announce their title assertively once when they appear. Callers pass
 * `live="off"` for replayed history so a refresh does not re-announce it.
 *
 * Whole-card activation (`onActivate`): a row card becomes a single button (step summary); a regular card
 * keeps a group root with a full-size activation button and the `trailing` control as a sibling
 * (file card: open in Results + download), never nesting one interactive element in another.
 *
 * Styles: styles/ui-refresh-cards.css (`.ui-card*`, `--card-*` tokens).
 */

import clsx from "clsx";
import { useTranslations } from "next-intl";
import {
  CircleAlert,
  CircleCheck,
  Check,
  ChevronDown,
  ChevronRight,
  Clock,
  Copy,
  Info,
  MessageCircleQuestion,
  type LucideIcon,
} from "lucide-react";
import {
  useId,
  useState,
  type AnchorHTMLAttributes,
  type ButtonHTMLAttributes,
  type CSSProperties,
  type ReactNode,
} from "react";

import { LucideGlyph } from "@/components/icon-refresh";
import { Button } from "@/components/ui/button";
import { copyDiagnostics } from "@/components/error-notice";
import {
  errorDiagnosticBuildVersion,
  normalizeErrorDiagnostics,
  type ErrorDiagnostics,
  type ErrorSurface,
} from "@/lib/error-presentation";

export type ChatCardIntent = "input" | "info" | "success" | "failure" | "progress";

export interface ChatCardAction {
  label: string;
  onSelect: () => void | Promise<void>;
  icon?: LucideIcon;
  /** Spinner in place of the icon and this label while `busy`; the other card controls are disabled. */
  busyLabel?: string;
  /** Rendered under the footer and linked with aria-describedby; the action is unavailable while set. */
  disabledReason?: string;
  /** The reason is temporary and resolves by itself (spinner instead of the info icon). */
  disabledReasonPending?: boolean;
  /** In-flight state for this action (submitting, undoing). */
  busy?: boolean;
  /** Unavailable without a visible reason (e.g. nothing selected yet, while another action runs). */
  disabled?: boolean;
  /** Secondary actions: `plain` text button (default) or filled `secondary` button. The footer-right action
   *  may also be `secondary` when the card's only way forward is not a commitment (Review in Canvas). */
  variant?: "plain" | "secondary";
  /** Test hook on the rendered button. */
  testId?: string;
  /** Accessible name when the visible label leaves out a count or object (e.g. "The other 3 are right"). */
  ariaLabel?: string;
}

export interface ChatCardDetails {
  /** Copied in full by "Copy diagnostic information"; only human-readable facts are shown. */
  diagnostics?: ErrorDiagnostics;
  /** Where the failure happened, in words (e.g. "Chat · Generate presentation"). */
  location?: ReactNode;
  content?: ReactNode;
  surface?: ErrorSurface;
}

export interface ChatCardProps {
  intent: ChatCardIntent;
  /** `row` = 40px settled/receipt line. */
  size?: "regular" | "row";
  /** Default per intent; "spinner" shows the progress spinner. */
  icon?: LucideIcon | "spinner";
  /** 36px tile (file card); replaces the icon column. */
  media?: ReactNode;
  /** 12/600 status word in the intent colour. */
  eyebrow?: ReactNode;
  /** Right of the eyebrow (row: after the sentence), 12 ink-3 tabular. */
  meta?: ReactNode;
  /** 14/600; row size: the sentence itself (status word in <b>). */
  title?: ReactNode;
  /** Body 13/1.55 and content (options, inputs). */
  children?: ReactNode;
  /** At most one, footer right. */
  primary?: ChatCardAction;
  /** Footer left (plain / secondary buttons). */
  secondary?: ChatCardAction[];
  /** Footer left before the secondary actions (e.g. the "Remember my choice" checkbox). */
  footerStart?: ReactNode;
  /** Row size: Undo, Open in Canvas, chevron. Regular size: the control beside the activation area. */
  trailing?: ReactNode;
  /** Collapsed "Technical details" with "Copy diagnostic information". */
  details?: ChatCardDetails;
  /** Inline error above the footer; keeps what the user entered. */
  error?: { message: ReactNode; onRetry?: () => void };
  /** 0–1: 2px bar on the bottom edge; "indeterminate" sweeps it (static under reduced motion). */
  progress?: number | "indeterminate";
  /** The whole card is one button (row) or carries one activation area beside `trailing` (regular). */
  onActivate?: () => void;
  /** Accessible name for the activation button (defaults to the card's text). */
  activateLabel?: string;
  live?: "polite" | "assertive" | "off";
  /** Leading icon colour override for the info intent (step summary that affected the answer). */
  iconTone?: "warning" | "muted";
  className?: string;
  /** Test hook passthrough. */
  "data-testid"?: string;
  /** Extra state attribute for styling hooks and tests. */
  "data-state"?: string;
}

const DEFAULT_ICON: Record<Exclude<ChatCardIntent, "progress">, { regular: LucideIcon; row: LucideIcon }> = {
  input: { regular: MessageCircleQuestion, row: Clock },
  info: { regular: Info, row: Info },
  success: { regular: CircleCheck, row: Check },
  failure: { regular: CircleAlert, row: CircleAlert },
};

const DEFAULT_LIVE: Record<ChatCardIntent, "polite" | "assertive" | "off"> = {
  input: "off",
  info: "polite",
  success: "polite",
  failure: "assertive",
  progress: "polite",
};

/** Progress spinner (14 in regular cards, 12 in rows); a static accent dot under reduced motion. */
export function ChatCardSpinner({ small = false }: { small?: boolean }) {
  return <span className={clsx("ui-card__spinner", small && "ui-card__spinner--sm")} aria-hidden="true" />;
}

type ChatCardButtonBase = {
  variant?: "primary" | "secondary" | "plain" | "ghost";
  size?: "sm" | "md";
  icon?: LucideIcon;
  loading?: boolean;
  children: ReactNode;
};

/**
 * Button for card footers and row trailing slots: shared `settings-button` control with the card's
 * plain (ink-2 text) and ghost (accent text) treatments. Pass `href` for a link (e.g. "View in Memory").
 */
export function ChatCardButton(
  props:
    | (ChatCardButtonBase & ButtonHTMLAttributes<HTMLButtonElement> & { href?: undefined })
    | (ChatCardButtonBase & AnchorHTMLAttributes<HTMLAnchorElement> & { href: string }),
) {
  const { variant = "plain", size = "sm", icon, loading = false, children, className, ...rest } = props;
  const classes = clsx(
    "settings-button",
    variant === "primary" ? "settings-button--primary" : variant === "secondary" ? "settings-button--secondary" : "settings-button--ghost",
    size === "sm" ? "settings-button--sm" : "settings-button--md",
    variant === "plain" && "ui-card__btn--plain",
    className,
  );
  const content = (
    <>
      {icon && !loading ? <LucideGlyph icon={icon} size={14} aria-hidden="true" /> : null}
      {children}
    </>
  );
  if (typeof rest.href === "string") {
    return (
      <a {...(rest as AnchorHTMLAttributes<HTMLAnchorElement>)} className={classes}>
        {content}
      </a>
    );
  }
  const buttonProps = rest as ButtonHTMLAttributes<HTMLButtonElement>;
  return (
    <button type="button" {...buttonProps} aria-busy={loading || undefined} className={classes}>
      {content}
    </button>
  );
}

/** One-line hint inside a card (why an action is unavailable, what a skipped question means). */
export function ChatCardHint({
  id,
  icon = Info,
  pending = false,
  tone = "muted",
  children,
}: {
  id?: string;
  icon?: LucideIcon;
  /** Spinner instead of the icon (e.g. "Saving progress…"). */
  pending?: boolean;
  tone?: "muted" | "error";
  children: ReactNode;
}) {
  return (
    <span id={id} className={clsx("ui-card__hint", tone === "error" && "ui-card__hint--error")}>
      {pending ? <ChatCardSpinner small /> : <LucideGlyph icon={icon} size={14} aria-hidden="true" />}
      <span>{children}</span>
    </span>
  );
}

export function ChatCard({
  intent,
  size = "regular",
  icon,
  media,
  eyebrow,
  meta,
  title,
  children,
  primary,
  secondary,
  footerStart,
  trailing,
  details,
  error,
  progress,
  onActivate,
  activateLabel,
  live,
  iconTone,
  className,
  "data-testid": testId,
  "data-state": dataState,
}: ChatCardProps) {
  const baseId = useId().replaceAll(":", "");
  const eyebrowId = `${baseId}-eyebrow`;
  const titleId = `${baseId}-title`;
  const whyId = `${baseId}-why`;
  const row = size === "row";
  const liveMode = live ?? DEFAULT_LIVE[intent];
  const busy = Boolean(primary?.busy || secondary?.some((action) => action.busy));

  const iconNode = media ? (
    <span className="ui-card__media" aria-hidden="true">{media}</span>
  ) : (
    <span className="ui-card__icon" aria-hidden="true">
      {icon === "spinner" || (icon === undefined && intent === "progress") ? (
        <ChatCardSpinner small={row} />
      ) : (
        <LucideGlyph
          icon={(icon as LucideIcon | undefined) ?? DEFAULT_ICON[intent as Exclude<ChatCardIntent, "progress">][row ? "row" : "regular"]}
          size={row ? 14 : 16}
          aria-hidden="true"
        />
      )}
    </span>
  );

  const labelledBy = [eyebrow ? eyebrowId : null, title ? titleId : null].filter(Boolean).join(" ") || undefined;
  const progressStyle =
    typeof progress === "number"
      ? ({ "--card-progress": `${Math.round(Math.max(0, Math.min(1, progress)) * 100)}%` } as CSSProperties)
      : undefined;
  const progressBar = typeof progress === "number"
    ? <span className="ui-card__progress" aria-hidden="true" />
    : progress === "indeterminate"
      ? <span className="ui-card__progress ui-card__progress--indeterminate" aria-hidden="true" />
      : null;

  const rootClass = clsx(
    "ui-card",
    `ui-card--${intent}`,
    row ? "ui-card--row" : "ui-card--regular",
    media ? "ui-card--media" : null,
    onActivate ? "ui-card--activatable" : null,
    iconTone ? `ui-card--icon-${iconTone}` : null,
    className,
  );

  // Row card with whole-card activation: the card itself is the button (step summary).
  if (row && onActivate) {
    return (
      <button
        type="button"
        className={rootClass}
        style={progressStyle}
        onClick={onActivate}
        aria-label={activateLabel}
        data-testid={testId}
        data-state={dataState}
        data-intent={intent}
      >
        {iconNode}
        <span className="ui-card__line">
          {title}
          {meta ? <span className="ui-card__row-meta">{meta}</span> : null}
        </span>
        {trailing ? <span className="ui-card__trailing">{trailing}</span> : null}
        {progressBar}
      </button>
    );
  }

  const liveProps =
    liveMode === "polite" ? { "aria-live": "polite" as const } : {};

  if (row) {
    return (
      <div
        className={rootClass}
        role="group"
        aria-labelledby={titleId}
        style={progressStyle}
        data-testid={testId}
        data-state={dataState}
        data-intent={intent}
        {...liveProps}
      >
        {iconNode}
        <span className="ui-card__line" id={titleId} role={liveMode === "assertive" ? "alert" : undefined}>
          {title}
          {meta ? (
            <span className="ui-card__row-meta" aria-live={intent === "progress" ? "off" : undefined}>
              {meta}
            </span>
          ) : null}
        </span>
        {trailing ? <span className="ui-card__trailing">{trailing}</span> : null}
        {progressBar}
      </div>
    );
  }

  const textBlock = (
    <>
      {eyebrow || meta ? (
        <span className="ui-card__eyebrow">
          {eyebrow ? <span id={eyebrowId}>{eyebrow}</span> : null}
          {meta ? (
            <span className="ui-card__meta" aria-live={intent === "progress" ? "off" : undefined}>
              {meta}
            </span>
          ) : null}
        </span>
      ) : null}
      {title ? (
        <span className="ui-card__title" id={titleId} role={liveMode === "assertive" ? "alert" : undefined}>
          {title}
        </span>
      ) : null}
    </>
  );

  const reason = primary?.disabledReason;
  const hasFooter = Boolean(primary || secondary?.length || footerStart);
  // Details, error, footer and the disabled reason: below the content, or after the activation button so their
  // own controls never nest inside it.
  const afterContent = details || error || hasFooter || reason ? (
    <>
      {details ? <ChatCardDetailsBlock details={details} /> : null}
      {error ? (
        <span className="ui-card__error" role="alert">
          <LucideGlyph icon={CircleAlert} size={14} aria-hidden="true" />
          <span>{error.message}</span>
        </span>
      ) : null}
      {hasFooter ? (
        <div className="ui-card__footer">
          {footerStart}
          {secondary?.map((action) => (
            <ChatCardButton
              key={action.label}
              variant={action.variant ?? "plain"}
              size="md"
              icon={action.icon}
              loading={action.busy}
              disabled={action.disabled || (busy && !action.busy)}
              onClick={() => void action.onSelect()}
              data-testid={action.testId}
            >
              {action.busy && action.busyLabel ? action.busyLabel : action.label}
            </ChatCardButton>
          ))}
          <span className="ui-card__footer-space" />
          {primary ? (
            <Button
              variant={primary.variant === "secondary" ? "secondary" : "primary"}
              size="md"
              loading={primary.busy}
              disabled={primary.disabled || (busy && !primary.busy)}
              aria-disabled={reason ? true : undefined}
              aria-describedby={reason ? whyId : undefined}
              aria-label={primary.ariaLabel}
              data-testid={primary.testId}
              onClick={() => {
                if (reason || primary.busy) return;
                void primary.onSelect();
              }}
            >
              {primary.icon && !primary.busy ? <LucideGlyph icon={primary.icon} size={14} aria-hidden="true" /> : null}
              {primary.busy && primary.busyLabel ? primary.busyLabel : primary.label}
            </Button>
          ) : null}
        </div>
      ) : null}
      {reason ? (
        <span className="ui-card__why">
          <ChatCardHint id={whyId} pending={primary?.disabledReasonPending}>{reason}</ChatCardHint>
        </span>
      ) : null}
    </>
  ) : null;

  return (
    <div
      className={rootClass}
      role="group"
      aria-labelledby={labelledBy}
      aria-busy={busy || undefined}
      style={progressStyle}
      data-testid={testId}
      data-state={dataState}
      data-intent={intent}
      {...liveProps}
    >
      {onActivate ? (
        <>
          <button type="button" className="ui-card__activate" onClick={onActivate} aria-label={activateLabel}>
            {iconNode}
            <span className="ui-card__body">{textBlock}{children}</span>
          </button>
          {afterContent ? <div className="ui-card__body ui-card__after">{afterContent}</div> : null}
        </>
      ) : (
        <>
          {iconNode}
          <div className="ui-card__body">
            {textBlock}
            {children ? <div className="ui-card__content">{children}</div> : null}
            {afterContent}
          </div>
        </>
      )}
      {trailing ? <span className="ui-card__trailing">{trailing}</span> : null}
      {progressBar}
    </div>
  );
}

function formatObservedAt(value: string | undefined): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/**
 * Collapsed technical details. Shows only facts a person can read (where, when, which build); reason codes,
 * tool names and session / turn identifiers go only into the copied diagnostic text.
 */
function ChatCardDetailsBlock({ details }: { details: ChatCardDetails }) {
  const t = useTranslations("chat_card");
  const [open, setOpen] = useState(false);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const panelId = `${useId().replaceAll(":", "")}-details`;
  const diagnostics = normalizeErrorDiagnostics(details.diagnostics);
  const observedAt = formatObservedAt(diagnostics?.observedAt);
  const copy = async () => {
    if (!diagnostics) return;
    const ok = await copyDiagnostics(diagnostics, details.surface ?? "chat", t("copy_fallback_prompt"));
    setCopyState(ok ? "copied" : "failed");
  };
  return (
    <>
      <button
        type="button"
        className="ui-card__details-toggle"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((value) => !value)}
      >
        <LucideGlyph icon={open ? ChevronDown : ChevronRight} size={14} aria-hidden="true" />
        {t("details")}
      </button>
      {open ? (
        <div className="ui-card__details" id={panelId}>
          <dl>
            {details.location ? (
              <>
                <dt>{t("details_location")}</dt>
                <dd>{details.location}</dd>
              </>
            ) : null}
            {observedAt ? (
              <>
                <dt>{t("details_time")}</dt>
                <dd>{observedAt}</dd>
              </>
            ) : null}
            <dt>{t("details_version")}</dt>
            <dd>{errorDiagnosticBuildVersion()}</dd>
          </dl>
          {details.content}
          {diagnostics ? (
            <div className="ui-card__details-actions">
              <ChatCardButton variant="secondary" size="sm" icon={Copy} onClick={() => void copy()}>
                {t("copy_diagnostics")}
              </ChatCardButton>
              <span className="ui-card__details-note" role="status">
                {copyState === "copied" ? t("copied") : copyState === "failed" ? t("copy_failed") : t("copy_diagnostics_note")}
              </span>
            </div>
          ) : null}
        </div>
      ) : null}
    </>
  );
}
