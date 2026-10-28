"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";

import { copyTextWithFallback } from "@/lib/clipboard";
import { toast, type ToastAction } from "@/lib/use-toast";
import {
  buildDiagnosticCopyBundle,
  errorDiagnosticBuildVersion,
  errorDiagnosticsFrom,
  normalizeErrorDiagnostics,
} from "@/lib/error-presentation";
import type {
  ErrorDiagnostics,
  ErrorPresentation,
  ErrorSurface,
} from "@/lib/error-presentation";
import { useErrorDiagnosticsCapability } from "@/lib/use-error-diagnostics-capability";

import styles from "./error-notice.module.css";
import {
  Copy as LucideCopy,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";

export interface ErrorDiagnosticsLabels {
  surfaceNames?: Partial<Record<ErrorSurface, string>>;
  summary: string;
  surface: string;
  version: string;
  observedAt: string;
  httpStatus: string;
  errorCode: string;
  diagnosticCode: string;
  requestId: string;
  serverRequestIdUnavailable: string;
  occurrenceId: string;
  digest: string;
  traceId: string;
  turnId: string;
  operationId: string;
  safeDetail: string;
  retryable: string;
  retryableYes: string;
  retryableNo: string;
  copy: string;
  copySuccess: string;
  copyFailed: string;
  copyFallbackPrompt: string;
}

export function ErrorNotice({
  message,
  action,
  diagnostics,
  surface,
  onAction,
  liveRole = "alert",
  diagnosticsEnabled,
}: {
  message: string;
  action?: ErrorPresentation["action"];
  diagnostics?: ErrorDiagnostics;
  surface: ErrorSurface;
  onAction?: () => void;
  liveRole?: "alert" | "status";
  /** A parent capability adapter may pass the already-resolved runtime flag. */
  diagnosticsEnabled?: boolean;
}) {
  const showDiagnostics = useErrorDiagnosticsCapability(
    diagnosticsEnabled,
  );
  const safeDiagnostics = normalizeErrorDiagnostics(diagnostics);

  return (
    <section className={styles.notice} data-testid="error-notice">
      <div className={styles.primary}>
        <span className={styles.marker} aria-hidden="true">
          !
        </span>
        <div className={styles.message} role={liveRole}>
          {message}
        </div>
      </div>
      {action ? (
        action.href ? (
          <a className={styles.action} href={action.href}>
            {action.label}
          </a>
        ) : (
          <button className={styles.action} type="button" onClick={onAction}>
            {action.label}
          </button>
        )
      ) : null}
      {showDiagnostics && safeDiagnostics ? (
        <ErrorDiagnosticsDisclosure
          diagnostics={safeDiagnostics}
          surface={surface}
        />
      ) : null}
    </section>
  );
}

/** A displayed failure: one user-visible sentence plus the original failure object for diagnostics. */
export interface ShownError {
  message: string;
  error?: unknown;
}

export function shownError(message: string, error?: unknown): ShownError {
  return { message, error };
}

/**
 * Diagnostics entry beside error copy. Render **outside** `role=alert` / `role=status` announcement containers,
 * or screen readers repeat diagnostics along with the explanation.
 *
 * - Diagnostics enabled: collapsed Diagnostics section (including Copy).
 * - Diagnostics disabled: only a small Copy Diagnostics button, without directly showing status codes;
 *   copied content includes status code, error code and request ID.
 * - Render nothing when the failure object has no recognizable diagnostics.
 */
export function ErrorDiagnosticsInline({
  error,
  surface,
  diagnosticsEnabled,
  quickCopy = true,
}: {
  error: unknown;
  surface: ErrorSurface;
  diagnosticsEnabled?: boolean;
  /** False when the surrounding card already offers Copy diagnostics as its primary action. */
  quickCopy?: boolean;
}) {
  const showDisclosure = useErrorDiagnosticsCapability(diagnosticsEnabled);
  const diagnostics = errorDiagnosticsFrom(error);
  if (!diagnostics) return null;
  if (showDisclosure) {
    return <ErrorDiagnosticsDisclosure diagnostics={diagnostics} surface={surface} />;
  }
  if (!quickCopy) return null;
  return <CopyDiagnosticsQuickButton diagnostics={diagnostics} surface={surface} />;
}

/**
 * Copy diagnostics as a card's primary action button (chat turn feedback D5). Same bundle and
 * clipboard fallback as the quick button; the caller supplies the class and label.
 */
export function CopyDiagnosticsActionButton({
  error,
  surface,
  label,
  className,
}: {
  error: unknown;
  surface: ErrorSurface;
  label: string;
  className?: string;
}) {
  const t = useTranslations("errors.diagnostics");
  const [copyState, setCopyState] = useState<"idle" | "success" | "failed">("idle");
  const diagnostics = errorDiagnosticsFrom(error);
  if (!diagnostics) return null;
  const copy = async () => {
    setCopyState((await copyDiagnostics(diagnostics, surface, t("copyFallbackPrompt"))) ? "success" : "failed");
  };
  return (
    <>
      <button type="button" className={className} onClick={() => void copy()} data-testid="final-fail-copy-diagnostics">
        {label}
      </button>
      <span className={styles.quickCopyStatus} role="status">
        {copyState === "success" ? t("copied") : copyState === "failed" ? t("copyShortFailed") : ""}
      </span>
    </>
  );
}

function CopyDiagnosticsQuickButton({
  diagnostics,
  surface,
}: {
  diagnostics: ErrorDiagnostics;
  surface: ErrorSurface;
}) {
  const t = useTranslations("errors.diagnostics");
  const [copyState, setCopyState] = useState<"idle" | "success" | "failed">("idle");
  const copy = async () => {
    setCopyState((await copyDiagnostics(diagnostics, surface, t("copyFallbackPrompt"))) ? "success" : "failed");
  };
  return (
    <span className={styles.quickCopy} data-testid="error-copy-diagnostics">
      <button className={styles.quickCopyButton} type="button" onClick={() => void copy()}>
        {t("copyShort")}
      </button>
      <span className={styles.quickCopyStatus} role="status">
        {copyState === "success" ? t("copied") : copyState === "failed" ? t("copyShortFailed") : ""}
      </span>
    </span>
  );
}

/** Copies the diagnostic bundle (clipboard, with the prompt fallback). True only for a real clipboard copy. */
export async function copyDiagnostics(
  diagnostics: ErrorDiagnostics,
  surface: ErrorSurface,
  fallbackPrompt: string,
): Promise<boolean> {
  const bundle = buildDiagnosticCopyBundle({
    diagnostics,
    surface,
    version: errorDiagnosticBuildVersion(),
  });
  return bundle ? copyTextWithFallback(bundle, fallbackPrompt) : false;
}

export interface CopyDiagnosticsToastLabels {
  copyShort: string;
  copied: string;
  copyShortFailed: string;
  copyFallbackPrompt: string;
}

/**
 * For Toasts outside the React tree (debounced callbacks, global retry queue): callers provide localized copy.
 * Return undefined if the failure object has no recognizable diagnostics (add no Toast action).
 */
export function copyDiagnosticsToastAction(
  error: unknown,
  surface: ErrorSurface,
  labels: CopyDiagnosticsToastLabels,
): ToastAction | undefined {
  const diagnostics = errorDiagnosticsFrom(error);
  if (!diagnostics) return undefined;
  return {
    label: labels.copyShort,
    onAction: async () => {
      const copied = await copyDiagnostics(diagnostics, surface, labels.copyFallbackPrompt);
      toast({ kind: copied ? "success" : "error", text: copied ? labels.copied : labels.copyShortFailed });
    },
  };
}

/**
 * When a Toast cannot fit a button: return a Copy Diagnostics action, then show a separate Copied or Copy Failed Toast.
 * Return undefined if the failure object has no recognizable diagnostics (add no Toast action).
 */
export function useCopyDiagnosticsToastAction(): (
  error: unknown,
  surface: ErrorSurface,
) => ToastAction | undefined {
  const t = useTranslations("errors.diagnostics");
  return (error, surface) =>
    copyDiagnosticsToastAction(error, surface, {
      copyShort: t("copyShort"),
      copied: t("copied"),
      copyShortFailed: t("copyShortFailed"),
      copyFallbackPrompt: t("copyFallbackPrompt"),
    });
}

export function ErrorDiagnosticsDisclosure({
  diagnostics,
  surface,
}: {
  diagnostics: ErrorDiagnostics;
  surface: ErrorSurface;
}) {
  const t = useTranslations("errors");
  const labels: ErrorDiagnosticsLabels = {
    surfaceNames: { [surface]: t(`surfaces.${surface}`) },
    summary: t("diagnostics.summary"),
    surface: t("diagnostics.surface"),
    version: t("diagnostics.version"),
    observedAt: t("diagnostics.observedAt"),
    httpStatus: t("diagnostics.httpStatus"),
    errorCode: t("diagnostics.errorCode"),
    diagnosticCode: t("diagnostics.diagnosticCode"),
    requestId: t("diagnostics.requestId"),
    serverRequestIdUnavailable: t(
      "diagnostics.serverRequestIdUnavailable",
    ),
    occurrenceId: t("diagnostics.occurrenceId"),
    digest: t("diagnostics.digest"),
    traceId: t("diagnostics.traceId"),
    turnId: t("diagnostics.turnId"),
    operationId: t("diagnostics.operationId"),
    safeDetail: t("diagnostics.safeDetail"),
    retryable: t("diagnostics.retryable"),
    retryableYes: t("diagnostics.retryableYes"),
    retryableNo: t("diagnostics.retryableNo"),
    copy: t("diagnostics.copy"),
    copySuccess: t("diagnostics.copySuccess"),
    copyFailed: t("diagnostics.copyFailed"),
    copyFallbackPrompt: t("diagnostics.copyFallbackPrompt"),
  };
  return (
    <ErrorDiagnosticsDisclosureView
      diagnostics={diagnostics}
      surface={surface}
      labels={labels}
    />
  );
}

export function ErrorDiagnosticsDisclosureView({
  diagnostics,
  surface,
  labels,
}: {
  diagnostics: ErrorDiagnostics;
  surface: ErrorSurface;
  labels: ErrorDiagnosticsLabels;
}) {
  const safe = normalizeErrorDiagnostics(diagnostics);
  const version = errorDiagnosticBuildVersion();
  if (!safe) return null;

  return (
    <details className={styles.disclosure} data-testid="error-diagnostics">
      <summary className={styles.summary}>{labels.summary}</summary>
      <div className={styles.instrumentPanel}>
        <dl className={styles.facts}>
          <DiagnosticRow
            label={labels.surface}
            value={labels.surfaceNames?.[surface] ?? surface}
          />
          <DiagnosticRow
            label={labels.version}
            value={version}
          />
          {safe.observedAt ? (
            <DiagnosticRow
              label={labels.observedAt}
              value={safe.observedAt}
            />
          ) : null}
          {safe.status !== undefined ? (
            <DiagnosticRow
              label={labels.httpStatus}
              value={String(safe.status)}
            />
          ) : null}
          {safe.code ? (
            <DiagnosticRow
              label={labels.errorCode}
              value={safe.code}
            />
          ) : null}
          {safe.diagnosticCode ? (
            <DiagnosticRow
              label={labels.diagnosticCode}
              value={safe.diagnosticCode}
            />
          ) : null}
          {safe.requestId ? (
            <DiagnosticRow
              label={labels.requestId}
              value={safe.requestId}
            />
          ) : null}
          {safe.serverRequestIdUnavailable ? (
            <DiagnosticRow
              label={labels.requestId}
              value={labels.serverRequestIdUnavailable}
            />
          ) : null}
          {safe.occurrenceId ? (
            <DiagnosticRow
              label={labels.occurrenceId}
              value={safe.occurrenceId}
            />
          ) : null}
          {safe.digest ? (
            <DiagnosticRow
              label={labels.digest}
              value={safe.digest}
            />
          ) : null}
          {safe.traceId ? (
            <DiagnosticRow
              label={labels.traceId}
              value={safe.traceId}
            />
          ) : null}
          {safe.turnId ? (
            <DiagnosticRow
              label={labels.turnId}
              value={safe.turnId}
            />
          ) : null}
          {safe.operationId ? (
            <DiagnosticRow
              label={labels.operationId}
              value={safe.operationId}
            />
          ) : null}
          {safe.safeDetail ? (
            <DiagnosticRow
              label={labels.safeDetail}
              value={safe.safeDetail}
            />
          ) : null}
          {safe.retryable !== undefined ? (
            <DiagnosticRow
              label={labels.retryable}
              value={
                safe.retryable
                  ? labels.retryableYes
                  : labels.retryableNo
              }
              machineValue={String(safe.retryable)}
            />
          ) : null}
        </dl>
        <CopyDiagnosticButton
          diagnostics={safe}
          surface={surface}
          labels={labels}
        />
      </div>
    </details>
  );
}

function DiagnosticRow({
  label,
  value,
  machineValue,
}: {
  label: string;
  value: string;
  machineValue?: string;
}) {
  return (
    <>
      <dt>{label}</dt>
      <dd>
        <code translate="no" data-machine-value={machineValue}>
          {value}
        </code>
      </dd>
    </>
  );
}

function CopyDiagnosticButton({
  diagnostics,
  surface,
  labels,
}: {
  diagnostics: ErrorDiagnostics;
  surface: ErrorSurface;
  labels: ErrorDiagnosticsLabels;
}) {
  const [copyState, setCopyState] = useState<
    "idle" | "success" | "failed"
  >("idle");
  const version = errorDiagnosticBuildVersion();

  const copy = async () => {
    const bundle = buildDiagnosticCopyBundle({
      diagnostics,
      surface,
      version,
    });
    if (!bundle) {
      setCopyState("failed");
      return;
    }
    const copied = await copyTextWithFallback(
      bundle,
      labels.copyFallbackPrompt,
    );
    setCopyState(copied ? "success" : "failed");
  };

  return (
    <div className={styles.copyRow}>
      <button
        className={styles.copyButton}
        type="button"
        onClick={() => void copy()}
        aria-label={labels.copy}
      >
        <CopyIcon />
        {labels.copy}
      </button>
      <span className={styles.copyStatus} role="status">
        {copyState === "success"
          ? labels.copySuccess
          : copyState === "failed"
            ? labels.copyFailed
            : ""}
      </span>
    </div>
  );
}

function CopyIcon() {
  return (
    <LucideGlyph icon={LucideCopy} size={14} aria-hidden="true" />
  );
}
