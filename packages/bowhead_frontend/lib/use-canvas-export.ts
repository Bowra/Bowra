/**
 * useCanvasExport: unified export lifecycle hook (WP1, design D5).
 *
 * Consolidate independent start+poll implementations in workspace.tsx and reports/page.tsx.
 * Canvas-independent: start() takes canvasId per call; one instance serves arbitrary list rows.
 *
 * Timeout contract: after ~60s polling, no silent disappearance; enter timedOut and guide users to export
 * history drawer (job persisted in SQLite, downloadable from drawer when complete).
 */

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchExportJob,
  fetchExportJobV2,
  startExportJob,
  startExportJobV2,
  type ExportJobDTO,
  type StartExportJobV2Input,
  type CanvasExportFmt,
} from "@/lib/api";
import { randomUuid } from "./random-id";

export type ExportFmt = CanvasExportFmt;

export interface ExportState {
  job: ExportJobDTO | null;
  /** Poll timeout (job may still run in background): guide users to recover it in drawer. */
  timedOut: boolean;
  /** Start failure (HTTP error, no job): raw failure object; UI explains status in localized copy, with status code only in "复制诊断". */
  startError: unknown;
  /** Arguments of latest start (Retry / retry with default theme after failure). */
  lastArgs: {
    canvasId: string;
    fmt: ExportFmt;
    opts?: Parameters<typeof startExportJob>[2];
    v2Request?: StartExportJobV2Input;
    idempotencyKey?: string;
  } | null;
}

const POLL_INTERVAL_MS = 800;
const POLL_MAX = 75; // ~60s

export function useCanvasExport() {
  const [state, setState] = useState<ExportState>({
    job: null,
    timedOut: false,
    startError: null,
    lastArgs: null,
  });
  const pollToken = useRef(0);

  // Cancel in-flight polling on unmount
  useEffect(
    () => () => {
      pollToken.current += 1;
    },
    [],
  );

  const start = useCallback(
    async (
      canvasId: string,
      fmt: ExportFmt,
      opts?: Parameters<typeof startExportJob>[2],
      v2Request?: StartExportJobV2Input,
      suppliedIdempotencyKey?: string,
    ) => {
      const token = ++pollToken.current;
      const idempotencyKey = v2Request
        ? suppliedIdempotencyKey ?? newExportIdempotencyKey()
        : undefined;
      setState({
        job: null,
        timedOut: false,
        startError: null,
        lastArgs: {
          canvasId,
          fmt,
          opts,
          v2Request,
          idempotencyKey,
        },
      });
      let job: ExportJobDTO;
      try {
        job = v2Request
          ? await startExportJobV2(
              canvasId,
              v2Request,
              idempotencyKey as string,
            )
          : await startExportJob(canvasId, fmt, opts);
      } catch (e) {
        if (pollToken.current !== token) return;
        setState((s) => ({ ...s, startError: e ?? new Error("start export failed") }));
        return;
      }
      if (pollToken.current !== token) return;
      setState((s) => ({ ...s, job }));
      if (
        job.status === "done"
        || job.status === "failed"
        || job.status === "canceled"
      ) return;

      for (let i = 0; i < POLL_MAX; i++) {
        await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
        if (pollToken.current !== token) return;
        try {
          job =
            job.contractVersion === 2
              ? await fetchExportJobV2(canvasId, job.jobId)
              : await fetchExportJob(canvasId, job.jobId);
        } catch {
          continue; // Tolerate one poll failure (network jitter); retry next tick
        }
        if (pollToken.current !== token) return;
        setState((s) => ({ ...s, job }));
        if (
          job.status === "done" ||
          job.status === "failed" ||
          job.status === "canceled"
        )
          return;
      }
      if (pollToken.current === token) {
        setState((s) => ({ ...s, timedOut: true }));
      }
    },
    [],
  );

  const retry = useCallback(() => {
    const a = state.lastArgs;
    if (a) {
      void start(
        a.canvasId,
        a.fmt,
        a.opts,
        a.v2Request,
        state.startError ? a.idempotencyKey : undefined,
      );
    }
  }, [state.lastArgs, state.startError, start]);

  /** Recover from PPTX template-render failure: re-export same canvas with default theme. */
  const retryWithDefaultTheme = useCallback(() => {
    const a = state.lastArgs;
    if (a) void start(a.canvasId, "pptx", { mode: "default" });
  }, [state.lastArgs, start]);

  const dismiss = useCallback(() => {
    pollToken.current += 1; // Stop in-flight polling
    setState({ job: null, timedOut: false, startError: null, lastArgs: null });
  }, []);

  const busy =
    !state.timedOut &&
    (state.job?.status === "queued" || state.job?.status === "running");

  return { state, busy, start, retry, retryWithDefaultTheme, dismiss };
}

function newExportIdempotencyKey(): string {
  return `canvas-export-${randomUuid()}`;
}
