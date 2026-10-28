/**
 * StreamingBanner — top sticky banner throughout a streaming turn.
 *
 * Display condition: canvas-store status === "streaming"
 * Design: report-refactor D6 — pale accent-tint bar + terracotta spinner + Stop ghost pill
 * (replaces old blue/purple gradient bar, 2026-06-12).
 * Width: followup-context-deck #4 (b2877690) — .canvas-streaming-banner aligns banner
 * with report column (globals.css: normal follows .v2-body, canvas-maxed max-width 2200
 * + margin auto), no longer wider than cards as full-bleed. Visual and width classes coexist complementarily.
 *
 * 0.7.10 fix: remove elapsed-seconds counter — production measurements showed constant 0s (suspected frequent
 * StreamingBanner unmount/remount resets interval). Prefer removal over meaningless noise;
 * if duration is needed later, use done event summary.total.
 *
 * loading-UX (origin/main): activity-strip visuals + orb + rotating activity copy (structure/writing/verification/polish)
 * + clickable turn-copy + stop.
 * WP3 (design D6): current-turn progress "Section N of M" from outline_ready/section_added
 * (canvas-store selectStreamProgress, useSyncExternalStore primitive-key subscription avoids render loops).
 * Combined: prefer true section progress "Section N of M", otherwise rotating activity copy.
 *
 * report-streaming D3 (2026-08-07): **banner was making false claims** — always showed
 * "正在更新报告 — 已生成 N 个部分", while 116 production report turns have p50 62s
 * of peers still querying data with no report words yet, and N counts `filtered.length`
 * (all sections, including charts), reporting chart counts as report-part counts.
 * Now describe actual phases:
 *   - Before compose (monotonic latch `selectComposeStarted`) → title "正在分析" + currently running
 *     step's backend Chinese headline (`selectRunningStepLabel`, e.g. "查询数据 · sales.xlsx");
 *     without running step, fall back to "已产出 N 张图".
 *   - After compose → title "正在更新报告" + "正在撰写报告".
 * ⚠ Phase uses canvas report-header **monotonic latch**, not existence of a running step —
 * LLM thinking gaps between tools would cause analyzing ↔ writing flicker.
 * Ceiling: writer's 26s has no step events; only static "正在撰写报告" can appear.
 * Authority: docs/sprints/report-streaming/discovery.md §2.6 / §6.5.
 */

"use client";

import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { useTranslations } from "next-intl";
import {
  subscribeCanvas,
  getCanvasSnapshot,
  selectStreamProgress,
  selectComposeStarted,
  selectTurnSurfaceCounts,
} from "@/lib/canvas-store";
import {
  subscribeTurn,
  getTurnSnapshot,
  selectRunningStepLabel,
} from "@/lib/turn-store";

/**
 * Current activity shared by banner and toolbar status group (appearance refresh batch 4b): title + optional detail.
 * Read only canvas-store and turn-store (REPORT_WRITING contract 1), without new events or local timing —
 * rotating timer only chooses fallback copy item.
 */
export function useStreamingActivity(turnId: string | null): { title: string; detail: string | null } {
  const t = useTranslations("streaming");

  // Rotating activity copy (loading-UX): show current activity when exact progress is unavailable.
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setTick((n) => n + 1), 2200);
    return () => window.clearInterval(timer);
  }, []);
  const steps = useMemo(
    () => [
      t("activity_structuring"),
      t("activity_writing"),
      t("activity_verifying"),
      t("activity_polishing"),
    ],
    [t],
  );

  // WP3: current-turn "Section N of M" progress (store primitive key, Object.is-stable, avoiding loops).
  const progressKey = useSyncExternalStore(
    subscribeCanvas,
    () => {
      const p = selectStreamProgress(getCanvasSnapshot());
      return p ? `${p.done}/${p.total}` : "";
    },
    () => "",
  );
  let progressLabel: string | null = null;
  if (progressKey) {
    const [done, total] = progressKey.split("/");
    progressLabel = t("section_progress", { done, total });
  }

  // report-streaming D3: has this turn's compose started + category counts. Filter all by turnId,
  // subscribe to primitive key (Object.is-stable), not new object — same as progressKey.
  const phaseKey = useSyncExternalStore(
    subscribeCanvas,
    () => {
      const s = getCanvasSnapshot();
      const c = selectTurnSurfaceCounts(s, turnId);
      return `${selectComposeStarted(s, turnId) ? 1 : 0}|${c.figures}|${c.reportParts}`;
    },
    () => "0|0|0",
  );
  const [composeFlag, figuresStr, partsStr] = phaseKey.split("|");
  const composeStarted = composeFlag === "1";
  const figures = Number(figuresStr) || 0;
  const reportParts = Number(partsStr) || 0;

  // report-streaming D3: currently running step — backend already supplies Chinese headline ("查询数据").
  const subRunning = useCallback(
    (cb: () => void) => (turnId ? subscribeTurn(turnId, cb) : () => {}),
    [turnId],
  );
  const runningStep = useSyncExternalStore(
    subRunning,
    () => (turnId ? selectRunningStepLabel(getTurnSnapshot(turnId)) : ""),
    () => "",
  );

  // report-streaming D3 — banner must describe what is actually happening now.
  // Old implementation always said "正在更新报告 · 已生成 N 个部分", while production p50 includes 62s when
  // peers still query data with no report yet, and N counts charts (discovery §2.6).
  // Priority: true progress > running tool > writing > produced-chart count > rotating fallback.
  const analysing = !composeStarted;
  const title = analysing ? t("activity_title_analyzing") : t("activity_title");
  // Trustworthiness of "第 N / M 节" is guarded by `selectStreamProgress` itself (D3 added
  // `outlineReady` prerequisite): writer-loop path never emits outline, so always returns null there,
  // preventing "第 3 / 3 节" while peers still query data. Making it **true progress** there is
  // option C work (supplement canvas.opened + outline_ready).
  // D2: status bar **exclusively owns current activity**; canvas band exclusively owns output counts.
  // Formerly gaps fell back to "已产出 N 张图", repeating what canvas band already said —
  // duplicate on-screen copy (design r1 review adjustment 4). Say nothing without an active action.
  const detail =
    progressLabel
    ?? (analysing && runningStep ? runningStep : null)
    ?? (composeStarted ? t("activity_composing") : null)
    ?? (reportParts > 0 ? t("activity_sections", { count: reportParts }) : null)
    ?? (analysing ? null : steps[tick % steps.length]);
  return { title, detail };
}

export function StreamingBanner({
  turnId,
  onCancel,
}: {
  turnId: string | null;
  onCancel?: () => void;
}) {
  const t = useTranslations("streaming");
  const { title, detail } = useStreamingActivity(turnId);

  // Design D6: simple accent-tint bar — rc-spinner + "AI is writing — Section N of M" + Stop ghost pill.
  // (Remove merged canvas-activity-strip: orb pulse / bottom scanline / frosted background.)
  return (
    <div role="status" className="rc-stream-banner canvas-streaming-banner">
      <span className="rc-spinner" aria-hidden />
      <span className="rc-stream-copy">
        <b>{title}</b>
        {/* report-streaming P1-2: separator must follow detail. D2 adjustment 4 deliberately makes
            detail null during gaps (status bar does not narrate), but unconditional {" — "} formerly rendered here
            ⇒ dangling "进行中 —" in production. Confirmed on real device in 1.0.74. */}
        {detail ? (
          <>
            {" — "}
            {detail}
          </>
        ) : null}
      </span>
      <span className="legacy-rc-push" aria-hidden />
      {turnId && (
        <button
          type="button"
          className="rc-stream-turn"
          title={t("turn_copy_title")}
          aria-label={t("turn_copy_title")}
          onClick={() => void navigator.clipboard?.writeText(turnId)}
        >
          turn {turnId.slice(-8)}
        </button>
      )}
      {onCancel && (
        <button type="button" onClick={onCancel} className="rc-pill-btn ghost legacy-rc-pill--compact">
          <span aria-hidden>■</span> {t("cancel")}
        </button>
      )}
    </div>
  );
}
