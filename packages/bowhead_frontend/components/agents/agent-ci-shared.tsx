"use client";

/**
 * Shared pieces of the Agent CI surfaces (version workbench, Coach evaluation requests, automation):
 * usage formatting, inline card status and "Copy diagnostics" for identifiers that never appear in the
 * UI (Claude Design "Agent CI Spec" parts 1 and 2).
 *
 * Usage is shown as tokens, model requests and duration only. Nothing here formats cost, credits or
 * currency (D6/D7).
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useLocale, useTranslations } from "next-intl";
import { CircleAlert, CircleCheck, Info } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useConfirmDialog, type ConfirmDialogOptions } from "@/components/ui/confirm-dialog";
import { ErrorDiagnosticsInline } from "@/components/error-notice";
import { copyTextWithFallback } from "@/lib/clipboard";
import type { AgentEvalUsageRange } from "@/lib/agents-api";

export interface CiFormat {
  /** Integer with grouping (9,800). */
  count: (value: number) => string;
  /** A 0–1 share as a whole percentage (67%). */
  percent: (share: number) => string;
  /** Token figure; compact from ten thousand up (2.4万 / 24K). */
  tokens: (value: number) => string;
  tokenRange: (range: AgentEvalUsageRange) => string;
  countRange: (range: AgentEvalUsageRange) => string;
  /** Minutes from milliseconds, rounded up (at least one). */
  minutes: (ms: number) => string;
  minuteRange: (range: AgentEvalUsageRange) => string;
  /** Elapsed time as minutes and seconds (4 min 12 sec). */
  elapsed: (ms: number) => string;
  time: (value: string | number | Date) => string;
  date: (value: string | number | Date) => string;
  dateTime: (value: string | number | Date) => string;
}

function toDate(value: string | number | Date): Date {
  return value instanceof Date ? value : new Date(value);
}

export function makeCiFormat(locale: string): CiFormat {
  const integer = new Intl.NumberFormat(locale, { maximumFractionDigits: 0 });
  const percent = new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 0 });
  const compact = new Intl.NumberFormat(locale, { notation: "compact", maximumFractionDigits: 1 });
  const minuteUnit = new Intl.NumberFormat(locale, { style: "unit", unit: "minute", unitDisplay: "short", maximumFractionDigits: 0 });
  const secondUnit = new Intl.NumberFormat(locale, { style: "unit", unit: "second", unitDisplay: "short", maximumFractionDigits: 0 });
  const time = new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit", hour12: false });
  const date = new Intl.DateTimeFormat(locale, { month: "short", day: "numeric" });
  const dateTime = new Intl.DateTimeFormat(locale, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
  const tokens = (value: number) => (Math.abs(value) >= 10_000 ? compact.format(value) : integer.format(value));
  const wholeMinutes = (ms: number, round: (value: number) => number) => Math.max(1, round(ms / 60_000));
  const safe = (format: Intl.DateTimeFormat) => (value: string | number | Date) => {
    const parsed = toDate(value);
    return Number.isNaN(parsed.valueOf()) ? "" : format.format(parsed);
  };
  return {
    count: (value) => integer.format(value),
    percent: (share) => percent.format(share),
    tokens,
    tokenRange: (range) => `${tokens(range.low)}–${tokens(range.high)}`,
    countRange: (range) => `${integer.format(range.low)}–${integer.format(range.high)}`,
    minutes: (ms) => minuteUnit.format(wholeMinutes(ms, Math.ceil)),
    minuteRange: (range) => {
      const low = wholeMinutes(range.low, Math.floor);
      const high = Math.max(low, wholeMinutes(range.high, Math.ceil));
      return low === high ? minuteUnit.format(high) : `${integer.format(low)}–${minuteUnit.format(high)}`;
    },
    elapsed: (ms) => {
      const total = Math.max(0, Math.floor(ms / 1000));
      const minutes = Math.floor(total / 60);
      const seconds = total % 60;
      if (minutes === 0) return secondUnit.format(seconds);
      return seconds === 0 ? minuteUnit.format(minutes) : `${minuteUnit.format(minutes)} ${secondUnit.format(seconds)}`;
    },
    time: safe(time),
    date: safe(date),
    dateTime: safe(dateTime),
  };
}

export function useCiFormat(): CiFormat {
  const locale = useLocale();
  return useMemo(() => makeCiFormat(locale), [locale]);
}

/** One status line inside the card an action belongs to. */
export interface CardStatusValue {
  tone: "success" | "error" | "info";
  message: string;
  /** Raw failure, only for "Copy diagnostics". */
  cause?: unknown;
  onRetry?: () => void;
}

/** Success lines fade back to the card's ordinary state after three seconds; errors stay. */
export const CARD_STATUS_FADE_MS = 3000;

export function useCardStatus(): [CardStatusValue | null, (value: CardStatusValue | null) => void] {
  const [status, setStatus] = useState<CardStatusValue | null>(null);
  const timer = useRef<number | null>(null);
  const update = useCallback((value: CardStatusValue | null) => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = null;
    setStatus(value);
    if (value?.tone === "success") {
      timer.current = window.setTimeout(() => {
        timer.current = null;
        setStatus((current) => (current === value ? null : current));
      }, CARD_STATUS_FADE_MS);
    }
  }, []);
  useEffect(() => () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
  }, []);
  return [status, update];
}

/**
 * Inline status next to the affected card (not only an sr-only region): success is polite, an error is
 * an assertive alert with Retry and Copy diagnostics beside it (outside the alert so they are not
 * re-announced).
 */
export function CardStatus({ status }: { status: CardStatusValue | null }) {
  const t = useTranslations("agents_marketplace");
  const Icon = status?.tone === "success" ? CircleCheck : status?.tone === "error" ? CircleAlert : Info;
  // The live regions stay mounted so a message written into them is announced; the visible line repeats the
  // text for sighted users (hidden from assistive technology to avoid reading it twice).
  return (
    <div className="am-ci-status-slot">
      <span className="sr-only" role="status" aria-live="polite">{status && status.tone !== "error" ? status.message : ""}</span>
      <span className="sr-only" role="alert" aria-live="assertive">{status?.tone === "error" ? status.message : ""}</span>
      {status ? (
        <div className="am-ci-ist" data-tone={status.tone}>
          <Icon size={14} aria-hidden className="am-ci-ist-icon" />
          <span aria-hidden className="am-ci-ist-text">{status.message}</span>
          {status.onRetry || status.cause !== undefined ? (
            <span className="am-ci-ist-actions">
              {status.onRetry ? <Button size="sm" variant="ghost" onClick={status.onRetry}>{t("version_retry")}</Button> : null}
              {status.cause !== undefined ? <ErrorDiagnosticsInline error={status.cause} surface="settings" /> : null}
            </span>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/**
 * Agent CI confirmations: a bottom action sheet below 640 with the confirm action above Cancel (spec part
 * 2, section 6). The body wrapper marks these sheets for the stacked footer.
 */
export function useCiConfirm(): { confirm: (options: ConfirmDialogOptions) => Promise<boolean>; dialog: ReactNode } {
  const { confirm, dialog } = useConfirmDialog();
  const ciConfirm = useCallback((options: ConfirmDialogOptions) => confirm({
    ...options,
    presentation: "sheet",
    description: <div className="am-ci-dialog-body">{typeof options.description === "string" ? <p>{options.description}</p> : options.description}</div>,
  }), [confirm]);
  return { confirm: ciConfirm, dialog };
}

/** Copies identifiers and codes that stay out of the interface (revision, delivery and execution ids). */
export async function copyDiagnosticRecord(record: Record<string, unknown>, fallbackPrompt: string): Promise<boolean> {
  const lines = Object.entries(record)
    .filter(([, value]) => value !== undefined && value !== null && value !== "")
    .map(([key, value]) => `${key}: ${Array.isArray(value) ? value.join(", ") : String(value)}`);
  const text = lines.join("\n");
  return copyTextWithFallback(text, fallbackPrompt, lines.join(" · "));
}

/** Ghost "Copy diagnostics" button; the result replaces the label for 1.5 seconds. */
export function CopyDiagnosticsButton({ record, size = "sm" }: { record: () => Record<string, unknown>; size?: "sm" | "md" }) {
  const t = useTranslations("agents_marketplace");
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  useEffect(() => {
    if (state === "idle") return;
    const timer = window.setTimeout(() => setState("idle"), 1500);
    return () => window.clearTimeout(timer);
  }, [state]);
  return (
    <Button
      size={size}
      variant="ghost"
      onClick={() => void copyDiagnosticRecord(record(), t("version_copy_diagnostics_prompt")).then((ok) => setState(ok ? "copied" : "failed"))}
    >
      {state === "copied" ? t("version_copied") : state === "failed" ? t("version_copy_failed") : t("version_copy_diagnostics")}
    </Button>
  );
}

/** Determinate 6px progress bar; the label is read as "Model requests 63 / 120". */
export function CiProgressBar({ value, max, label, tone = "accent" }: { value: number; max: number; label: string; tone?: "accent" | "warning" }) {
  const safeMax = Math.max(1, max);
  const clamped = Math.min(safeMax, Math.max(0, value));
  return (
    <div
      className="am-ci-prog"
      data-tone={tone}
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={safeMax}
      aria-valuenow={clamped}
    >
      <i style={{ width: `${(clamped / safeMax) * 100}%` }} />
    </div>
  );
}

/** Label/value rows on a fill surface (limits, progress, usage). */
export function CiQuantities({ rows, label }: { rows: Array<{ key: string; label: ReactNode; value: ReactNode; bar?: ReactNode }>; label?: string }) {
  return (
    <dl className="am-ci-qbox" aria-label={label}>
      {rows.map((row) => (
        <div key={row.key} className="am-ci-qrow">
          <dt>{row.label}</dt>
          <dd>{row.value}</dd>
          {row.bar ?? null}
        </div>
      ))}
    </dl>
  );
}

/** Card heading: 32px circular icon, 15/650 title, 13 body, optional trailing badge. */
export function CiCardHead({ icon, tone = "neutral", title, titleId, body, badge }: {
  icon?: ReactNode;
  tone?: "neutral" | "accent" | "positive" | "warning" | "danger";
  title: ReactNode;
  titleId?: string;
  body?: ReactNode;
  badge?: ReactNode;
}) {
  return (
    <div className="am-ci-gh">
      {icon ? <span className="am-ci-gi" data-tone={tone} aria-hidden>{icon}</span> : null}
      <div className="am-ci-gt">
        <h3 id={titleId} tabIndex={titleId ? -1 : undefined}>{title}</h3>
        {body ? <p>{body}</p> : null}
      </div>
      {badge ?? null}
    </div>
  );
}

/** Inline spinner for card icons and slots. */
export function CiSpinner() {
  return <span className="am-ci-spin" aria-hidden />;
}
