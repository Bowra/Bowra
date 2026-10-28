"use client";

import { useEffect, useMemo } from "react";

import {
  ErrorDiagnosticsDisclosureView,
} from "@/components/error-notice";
import { withUiHooks } from "@/components/settings/ui-hook-classes";
import {
  boundaryErrorCopy,
} from "@/lib/boundary-error-copy";
import type {
  BoundaryErrorLocale,
} from "@/lib/boundary-error-copy";
import { appPath } from "@/lib/base-path";
import { projectClientBoundaryFailure } from "@/lib/error-presentation";
import { useErrorDiagnosticsCapability } from "@/lib/use-error-diagnostics-capability";

import moduleStyles from "./boundary-error.module.css";

// UI refresh (page-by-page acceptance cleanup): append stable global ui-boundary-* hooks to module classes; new appearance lives in
// styles/ui-refresh-pages-main.css, “tail:states”.
// Root boundary (global-error) supplies its own <html>, lacks the flag attribute and authenticated-layout stylesheets, and is unaffected.
const styles = withUiHooks(moduleStyles, "ui-boundary");

export function BoundaryErrorPage({
  error,
  reset,
  surface,
  locale,
}: {
  error: unknown;
  reset: () => void;
  surface: "route" | "global";
  locale: BoundaryErrorLocale;
}) {
  const copy = boundaryErrorCopy(locale);
  const presentation = useMemo(
    () =>
      projectClientBoundaryFailure(
        error,
        surface,
        copy.failureMessages,
      ),
    [copy.failureMessages, error, surface],
  );
  const diagnosticsEnabled = useErrorDiagnosticsCapability();
  const diagnostics = presentation.diagnostics;
  const chunkLoad = diagnostics?.code === "client_chunk_load_failed";
  const title = chunkLoad
    ? copy.chunkTitle
    : surface === "global"
      ? copy.globalTitle
      : copy.routeTitle;

  useEffect(() => {
    if (!diagnostics) return;
    console.error(`[${surface}-error]`, {
      error_code: diagnostics.code,
      diagnostic_code: diagnostics.diagnosticCode,
      occurrence_id: diagnostics.occurrenceId,
      digest: diagnostics.digest,
    });
  }, [diagnostics, surface]);

  const runPrimaryAction = () => {
    if (chunkLoad) {
      window.location.reload();
      return;
    }
    reset();
  };

  return (
    <main
      className={styles.shell}
      data-surface={surface}
      data-testid="boundary-error"
    >
      <section className={styles.card}>
        <div className={styles.rail} aria-hidden="true" />
        <div className={styles.primary} role="alert">
          <div className={styles.recoveryLabel}>{copy.recoveryLabel}</div>
          <h1 className={styles.title}>{title}</h1>
          <p className={styles.message}>{presentation.message}</p>
        </div>
        <div className={styles.actions}>
          <button
            className={styles.primaryAction}
            type="button"
            onClick={runPrimaryAction}
          >
            {presentation.action?.label ?? copy.retry}
          </button>
          {!chunkLoad ? (
            <button
              className={styles.secondaryAction}
              type="button"
              onClick={() => window.location.reload()}
            >
              {copy.reload}
            </button>
          ) : null}
          {surface === "route" ? (
            <a
              className={styles.secondaryAction}
              href={appPath("/")}
            >
              {copy.home}
            </a>
          ) : null}
        </div>
        {diagnosticsEnabled && diagnostics ? (
          <div className={styles.diagnostics}>
            <ErrorDiagnosticsDisclosureView
              diagnostics={diagnostics}
              surface={surface}
              labels={copy.diagnostics}
            />
          </div>
        ) : null}
      </section>
    </main>
  );
}
