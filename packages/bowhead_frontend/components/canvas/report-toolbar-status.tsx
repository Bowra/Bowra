/**
 * Report toolbar status group (appearance refresh batch 4b, Report Canvas Chrome Spec §1, §9).
 *
 * With feature enabled and results toolbar present, remove sticky full-width `StreamingBanner`; place the same current-activity
 * wording in 44-high toolbar status group: spinner (static under reduced motion) + "正在更新报告 · 第 3 / 6 节"
 * + "停止" when caller provides stop callback. Copy shares `useStreamingActivity` with banner, reading stores without new events/timing.
 * Outside writing, status group displays caller-provided content (status badge).
 *
 * Screen readers: permanent visually hidden polite region exists before writing, ensuring first progress is announced. Visible text changes
 * with progress/rotating copy; direct live region would announce every 2s, so update progress at most every 10s here (design keyboard/accessibility);
 * writing completion bypasses throttling to announce terminal state immediately. Recognize only this turn's outcome (runner `terminalStatus`: completed / aborted /
 * cancelled / failed), not inferred canvas data — `turn_aborted` / `turn_failed` do not write canvas errors, and normal completion may mark
 * unclosed sections is_partial (batch 4b review N1). If canvas leaves writing before turn terminal state, wait briefly; if absent or unknown,
 * announce neutral "写作已结束". Announce only completion observed on this page; no F5 replay (refresh skips writing → ended transition).
 */

"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";

import { useStreamingActivity } from "./streaming-banner";

/** Minimum interval between writing-progress screen-reader announcements. */
export const STREAMING_ANNOUNCE_INTERVAL_MS = 10_000;

/** Maximum wait when canvas leaves writing before turn terminal arrives; on timeout announce neutral "写作已结束". */
export const TERMINAL_WAIT_MS = 1_500;

export type StreamingOutcome = "done" | "stopped" | "failed" | "ended";

/**
 * Connection state during writing (batch 4b remainder, Report Canvas Chrome Spec §9 pause/interruption). Use frontend-verifiable facts only:
 * runner transport reconnecting (`streamingState === "error"` while turn still runs) is `reconnecting`; abandoned reconnect (no longer running) is
 * `lost`. Gateway `streaming.state: paused` means several seconds without new output (model/tools may still work), not a displayed pause.
 */
export type StreamConnection = "reconnecting" | "lost";

/** Most recent writing outcome for this report (current-turn terminal and this turn wrote report): stopped by user or failed. */
export type WritingInterruption = "stopped" | "failed";

/** Positive integer section count can appear in description (omit numbers for 0/missing). exported for tests. */
export function savedCount(n: number | null | undefined): n is number {
  return typeof n === "number" && Number.isInteger(n) && n > 0;
}

/** Current-turn terminal → persistent writing-interruption toolbar note; only stopped/failed count as interruptions. exported for tests. */
export function writingInterruption(terminalStatus: string | null | undefined): WritingInterruption | null {
  const outcome = streamingOutcome(terminalStatus);
  return outcome === "stopped" || outcome === "failed" ? outcome : null;
}

/** Current-turn terminal (runner terminalStatus) → announcement category; null if unknown/absent. exported for tests. */
export function streamingOutcome(terminalStatus: string | null | undefined): Exclude<StreamingOutcome, "ended"> | null {
  if (terminalStatus === "completed") return "done";
  if (terminalStatus === "aborted" || terminalStatus === "cancelled") return "stopped";
  if (terminalStatus === "failed" || terminalStatus === "error") return "failed";
  return null;
}

/**
 * Status-group announcement: writing progress at most every `intervalMs` (first immediately per writing run); completion announces terminal state immediately
 * when known, otherwise waits at most `waitMs`, then says "写作已结束" if absent. `label` maps categories to sentences.
 */
export function useToolbarAnnouncement(
  isStreaming: boolean,
  progress: string | null,
  terminalStatus: string | null | undefined,
  label: (outcome: StreamingOutcome) => string,
  intervalMs = STREAMING_ANNOUNCE_INTERVAL_MS,
  waitMs = TERMINAL_WAIT_MS,
  /** Connection-interruption note (batch 4b remainder): announce immediately bypassing throttle and replacing subsequent "写作已结束". */
  interrupt: string | null = null,
): string {
  const [announced, setAnnounced] = useState("");
  const interruptRef = useRef(interrupt);
  interruptRef.current = interrupt;
  const lastRef = useRef<number | null>(null);
  const timerRef = useRef<number | null>(null);
  const waitRef = useRef<number | null>(null);
  const pendingEndRef = useRef(false);
  const wasStreamingRef = useRef(isStreaming);
  const labelRef = useRef(label);
  labelRef.current = label;
  const outcome = streamingOutcome(terminalStatus);

  const clearTimer = () => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  };
  const clearWait = () => {
    if (waitRef.current !== null) {
      window.clearTimeout(waitRef.current);
      waitRef.current = null;
    }
  };

  // Writing ended: cancel pending progress; announce known terminal immediately or await it (up to waitMs)
  useEffect(() => {
    const was = wasStreamingRef.current;
    wasStreamingRef.current = isStreaming;
    if (was && !isStreaming) {
      clearTimer();
      lastRef.current = null;
      if (outcome) {
        setAnnounced(labelRef.current(outcome));
        return;
      }
      pendingEndRef.current = true;
      clearWait();
      waitRef.current = window.setTimeout(() => {
        waitRef.current = null;
        if (!pendingEndRef.current) return;
        pendingEndRef.current = false;
        if (interruptRef.current) return;
        setAnnounced(labelRef.current("ended"));
      }, waitMs);
    }
    if (!was && isStreaming) {
      lastRef.current = null;
      pendingEndRef.current = false;
      clearWait();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isStreaming]);

  // Terminal arrives while waiting: announce immediately
  useEffect(() => {
    if (!pendingEndRef.current || !outcome) return;
    pendingEndRef.current = false;
    clearWait();
    setAnnounced(labelRef.current(outcome));
  }, [outcome]);

  // Connection interrupted: announce immediately, invalidate pending progress and "写作已结束"
  useEffect(() => {
    if (!interrupt) return;
    clearTimer();
    clearWait();
    pendingEndRef.current = false;
    setAnnounced(interrupt);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [interrupt]);

  // Writing progress: first announcement immediate, subsequent throttled
  useEffect(() => {
    if (!isStreaming || !progress || interruptRef.current) return;
    const now = Date.now();
    const wait = lastRef.current === null ? 0 : lastRef.current + intervalMs - now;
    clearTimer();
    if (wait <= 0) {
      lastRef.current = now;
      setAnnounced(progress);
      return;
    }
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      lastRef.current = Date.now();
      setAnnounced(progress);
    }, wait);
  }, [isStreaming, progress, intervalMs]);

  useEffect(
    () => () => {
      clearTimer();
      clearWait();
    },
    [],
  );
  return announced;
}

export function StreamingToolbarStatus({
  turnId,
  onCancel,
  onProgress,
  paused = false,
}: {
  turnId: string | null;
  onCancel?: () => void;
  /** Report visible-text changes to status group (throttled screen-reader announcements). */
  onProgress?: (text: string) => void;
  /** Batch 4b remainder: disconnected and reconnecting (frontend transport fact). Replace spinner with static pause marker and "已暂停，正在重新连接". */
  paused?: boolean;
}) {
  const t = useTranslations("streaming");
  const { title, detail } = useStreamingActivity(turnId);
  const text = paused ? t("paused_reconnecting") : detail ? `${title} · ${detail}` : title;
  useEffect(() => {
    onProgress?.(text);
  }, [onProgress, text]);
  return (
    <div
      className={paused ? "rc-toolbar-stream is-paused" : "rc-toolbar-stream"}
      data-testid="report-toolbar-streaming"
      {...(paused ? { "data-state": "paused" } : null)}
    >
      {paused ? (
        <span className="rc-toolbar-stream-paused" aria-hidden="true" />
      ) : (
        <span className="rc-spinner rc-toolbar-stream-spinner" aria-hidden="true" />
      )}
      <span className="rc-toolbar-stream-text" aria-hidden="true">
        {text}
      </span>
      {onCancel ? (
        <button type="button" className="rc-toolbar-stream-stop" onClick={onCancel}>
          {t("cancel")}
        </button>
      ) : null}
    </div>
  );
}

/** Toolbar status group: writing progress while streaming, otherwise `idle` (status badge); screen-reader region permanent. */
export function ReportToolbarStatusGroup({
  isStreaming,
  turnId,
  onCancel,
  idle,
  turnTerminalStatus = null,
  connection = null,
  interruption = null,
  onRefresh,
  sectionsSaved = null,
}: {
  isStreaming: boolean;
  turnId: string | null;
  onCancel?: () => void;
  idle: ReactNode;
  /** This turn's terminal (runner terminalStatus), supplied by workbench; without it writing end only announces "写作已结束". */
  turnTerminalStatus?: string | null;
  /** Batch 4b remainder: connection state during writing (see StreamConnection). */
  connection?: StreamConnection | null;
  /** Batch 4b remainder: latest writing on this report stopped/failed (pass only if this turn wrote report). Hidden during writing. */
  interruption?: WritingInterruption | null;
  /** "刷新报告" on connection interruption: refetch this report from server. */
  onRefresh?: () => void;
  /**
   * Report sections persisted this turn (terminal done report_sections_saved). Positive integer says "已保存 N 节"; missing/0
   * (no new sections beyond structural/code blocks, or revision only changed prior sections) says number-free "已写的部分已保存".
   */
  sectionsSaved?: number | null;
}) {
  const t = useTranslations("streaming");
  const [progress, setProgress] = useState<string | null>(null);
  useEffect(() => {
    if (!isStreaming) setProgress(null);
  }, [isStreaming]);
  const lost = connection === "lost";
  const announced = useToolbarAnnouncement(
    isStreaming,
    isStreaming ? progress : null,
    turnTerminalStatus,
    (outcome) =>
      t(
        outcome === "failed"
          ? "announce_failed"
          : outcome === "stopped"
            ? "announce_stopped"
            : outcome === "done"
              ? "announce_done"
              : "announce_ended",
      ),
    STREAMING_ANNOUNCE_INTERVAL_MS,
    TERMINAL_WAIT_MS,
    lost ? t("interrupted_lost") : null,
  );
  // Persistent note: connection interruption first (writing state no longer trustworthy); otherwise outside writing describe previous writing stopped/failed.
  let note: ReactNode = null;
  if (lost) {
    note = (
      <span className="rc-toolbar-outcome is-error" data-testid="report-toolbar-outcome" data-outcome="lost">
        <span className="rc-toolbar-outcome-text">{t("interrupted_lost")}</span>
        {onRefresh ? (
          <button type="button" className="rc-toolbar-outcome-action" onClick={onRefresh}>
            {t("refresh_report")}
          </button>
        ) : null}
      </span>
    );
  } else if (interruption) {
    // Cannot reach here while writing and connected: render progress below; notes follow badge only outside writing
    note = (
      <span
        className={interruption === "failed" ? "rc-toolbar-outcome is-error" : "rc-toolbar-outcome"}
        data-testid="report-toolbar-outcome"
        data-outcome={interruption}
      >
        <span className="rc-toolbar-outcome-text">
          {savedCount(sectionsSaved)
            ? t(interruption === "failed" ? "interrupted_failed_count" : "interrupted_stopped_count", { count: sectionsSaved })
            : t(interruption === "failed" ? "interrupted_failed" : "interrupted_stopped")}
        </span>
      </span>
    );
  }
  return (
    <>
      {isStreaming && !lost ? (
        <StreamingToolbarStatus
          turnId={turnId}
          onCancel={onCancel}
          onProgress={setProgress}
          paused={connection === "reconnecting"}
        />
      ) : (
        <>
          {idle}
          {note}
        </>
      )}
      <span className="rc-toolbar-sr-only" role="status" aria-live="polite" data-testid="report-toolbar-announcer">
        {announced}
      </span>
    </>
  );
}
