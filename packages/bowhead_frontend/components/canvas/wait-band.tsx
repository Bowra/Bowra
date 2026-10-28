/**
 * WaitBand — canvas in-progress band (report-streaming D2).
 *
 * Addresses the largest issue measured in 116 production: **77% of report turns have their longest idle period before artifacts
 * start being generated** (p50 62s, p90 173s), with only a few completed charts and nothing new on the canvas.
 * See docs/sprints/report-streaming/discovery.md §2.5 measurement 4.
 *
 * Responsibilities (top status bar vs this band), with no repeated wording:
 *   Top status bar = what is happening now (action phrases, changing step by step)
 *   This band      = what has been produced so far + where conclusions will appear after completion
 *
 * ── Three load-bearing constraints (each learned from a failure; read before changing) ────────────────────────
 * ① **Never imply an artifact is certain to arrive**. No report-title placeholder or document skeleton in the band.
 *    canvas-summon (2026-08-03) established that no criterion predicts whether this turn will produce a report
 *    (self-declaration recall only ~20% across 48 measured turns); drawing early produces a brief flash then disappearance,
 *    which becomes a **permanent** rendering error in the Feishu mirror.
 * ② **Never appear before the first artifact**. Earliest display = a canvas artifact has landed in this turn.
 *    Earlier display = opening canvas early = a variant of ① (design r1 review hard conflict 2).
 * ③ **No narration during gaps**. With no active action, keep every word unchanged and replace spinner with breathing dot.
 *    No active step can mean thinking / unreported subagent work / a stall,
 *    which we cannot distinguish; production measurements show subagents still working in 91% of those idle periods,
 *    so claiming the previous step is complete is usually wrong (r1 review hard conflict 3).
 *
 * Design: Claude Design project 8b6a92a0 waiting-state/ (r1 revision).
 * Styles translated verbatim in wait-band.css.
 */

"use client";

import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { useTranslations } from "next-intl";

import {
  subscribeCanvas,
  getCanvasSnapshot,
  FIG_FIELD_SEP,
  FIG_ROW_SEP,
  selectComposeStarted,
  selectTurnFigureKey,
  selectTurnSurfaceCounts,
} from "@/lib/canvas-store";
import {
  subscribeTurn,
  getTurnSnapshot,
  selectRunningStepLabel,
  selectElapsedSeconds,
} from "@/lib/turn-store";

import "./wait-band.css";
import {
  Check as LucideCheck,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";

/** Timer tick, once per second, but **the baseline lives in the store** (TurnState.startedAt) —
 *  in 0.7.10 the top status bar stayed at 0s because the baseline was component-local state;
 *  frequent unmount/remount reset the interval. Only the current time is local here. */
function useNowTick(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [active]);
  return now;
}

function formatElapsed(sec: number, t: ReturnType<typeof useTranslations>): string {
  if (sec < 60) return t("elapsed_sec", { s: sec });
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return s === 0 ? t("elapsed_min", { m }) : t("elapsed_min_sec", { m, s });
}

export function WaitBand({ turnId }: { turnId: string | null }) {
  const t = useTranslations("waitBand");

  // Canvas side: has compose started (monotonic latch) + category counts + artifact chips. All filtered by turnId,
  // subscribing to primitive keys (Object.is-stable), rather than new objects.
  const canvasKey = useSyncExternalStore(
    subscribeCanvas,
    () => {
      const s = getCanvasSnapshot();
      const c = selectTurnSurfaceCounts(s, turnId);
      return `${selectComposeStarted(s, turnId) ? 1 : 0}|${c.figures}|${c.reportParts}`;
    },
    () => "0|0|0",
  );
  const figureKey = useSyncExternalStore(
    subscribeCanvas,
    () => selectTurnFigureKey(getCanvasSnapshot(), turnId),
    () => "",
  );

  // Turn side: any active action + has this turn ended + start time.
  const subTurn = useMemo(
    () => (cb: () => void) => (turnId ? subscribeTurn(turnId, cb) : () => {}),
    [turnId],
  );
  const turnKey = useSyncExternalStore(
    subTurn,
    () => {
      if (!turnId) return "0|0";
      const st = getTurnSnapshot(turnId);
      return `${selectRunningStepLabel(st) ? 1 : 0}|${st?.cardFinalized ? 1 : 0}`;
    },
    () => "0|0",
  );

  const [composeFlag, figuresStr] = canvasKey.split("|");
  const composeStarted = composeFlag === "1";
  const figures = Number(figuresStr) || 0;
  const [runningFlag, finalizedFlag] = turnKey.split("|");
  const hasRunningStep = runningFlag === "1";
  const settled = finalizedFlag === "1";

  // Constraint ②: do not appear before this turn has any canvas artifacts.
  // Yielding constraint: once compose starts (report header/KPI lands), band exits and yields its place to report header.
  const visible = Boolean(turnId) && !composeStarted && figures > 0;

  const now = useNowTick(visible && !settled);
  const elapsedSec = useSyncExternalStore(
    subTurn,
    () => (turnId ? selectElapsedSeconds(getTurnSnapshot(turnId), now) : null),
    () => null,
  );

  const chips = useMemo(() => {
    if (!figureKey) return [] as { id: string; title: string }[];
    return figureKey.split(FIG_ROW_SEP).map((row, i) => {
      const [id, title] = row.split(FIG_FIELD_SEP);
      return { id: id || `fig-${i}`, title: title || "" };
    });
  }, [figureKey]);

  if (!visible) return null;

  const elapsedText = elapsedSec === null ? null : formatElapsed(elapsedSec, t);

  return (
    <div
      className={"wb-band" + (settled ? " settled" : "")}
      role="status"
      aria-live="polite"
      aria-label={
        settled
          ? t("aria_settled")
          : t("aria_running", { n: figures })
      }
    >
      <div className="wb-band-top">
        <span className="wb-mkr" aria-hidden="true">
          {settled ? (
            <span className="fin">
              <LucideGlyph icon={LucideCheck} size={12} />
            </span>
          ) : hasRunningStep ? (
            <span className="run" />
          ) : (
            /* Constraint ③: during gaps change only marker shape, never wording. */
            <span className="idle" />
          )}
        </span>
        <span className="wb-eyebrow">{settled ? t("eyebrow_done") : t("eyebrow_running")}</span>
        {/* Unknown start (older backend lacks started_at) → hide instead of showing an incorrect 0. */}
        {elapsedText && (
          <span className="wb-timer">
            {settled ? t("timer_total", { d: elapsedText }) : t("timer_elapsed", { d: elapsedText })}
          </span>
        )}
      </div>

      <div className="wb-lede">
        {settled ? (
          <span>{t("lede_settled")}</span>
        ) : (
          <span>
            {t("lede_produced", { n: figures })}
            <span className="muted"> · {t("lede_continuing")}</span>
          </span>
        )}
      </div>

      <div className="wb-note">
        {settled ? t("note_settled") : elapsedSec !== null && elapsedSec >= 120 ? t("note_long") : t("note")}
      </div>

      {!settled && chips.length > 0 && (
        <div className="wb-chips">
          {chips.map((f, i) => (
            <span key={f.id} className="wb-chip">
              <span className="n">{t("chip_index", { i: i + 1 })}</span>
              <span className="t">{f.title}</span>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
