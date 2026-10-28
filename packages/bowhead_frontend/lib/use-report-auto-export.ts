"use client";

import { useEffect, useRef } from "react";

import {
  consumeReportExportParam,
  reportAutoExportReadiness,
  type AutoExportFmt,
} from "./report-share";
import { toast } from "./use-toast";
import { withoutNextRouterHistoryMarkers } from "./url-session-nav";

type TriggerAutoExport = (
  format: AutoExportFmt,
  options?: { retryTransient?: boolean },
) => Promise<boolean>;

// false means only “surface not ready yet, retryable”; success, a submitted fallback,
// or an explicit user-facing rejection return true, preventing duplicate auto-export jobs / Toasts.

export function useReportAutoExport({
  enabled = true,
  canvasId,
  sessionId,
  emptyMessage,
  triggerExport,
}: {
  enabled?: boolean;
  canvasId: string | null;
  sessionId: string | null;
  emptyMessage: string;
  triggerExport: TriggerAutoExport;
}): void {
  const doneRef = useRef(false);

  useEffect(() => {
    if (!enabled || doneRef.current || typeof window === "undefined") return;
    const url = new URL(window.location.href);
    const readiness = reportAutoExportReadiness(url, canvasId, sessionId);
    if (readiness === "none" || readiness === "waiting") return;

    // Consuming and removing the parameter must retain atomic ordering; refresh / back must not repeat export.
    const format = consumeReportExportParam(url);
    window.history.replaceState(
      withoutNextRouterHistoryMarkers(window.history.state),
      "",
      `${url.pathname}${url.search}${url.hash}`,
    );
    doneRef.current = true;
    if (!format) return;
    if (readiness === "empty") {
      toast({ kind: "error", text: emptyMessage });
      return;
    }

    // On cold load, report DOM may not yet have V2 surface receipts. Retry transient failures only;
    // after six unsuccessful readiness attempts, hand back to triggerExport's existing fallback/warning logic.
    void (async () => {
      for (let attempt = 0; attempt < 6; attempt += 1) {
        if (await triggerExport(format, { retryTransient: true })) return;
        await new Promise((resolve) => window.setTimeout(resolve, 1_500));
      }
      void triggerExport(format);
    })();
  }, [canvasId, emptyMessage, enabled, sessionId, triggerExport]);
}
