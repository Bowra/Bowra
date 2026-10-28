"use client";

/**
 * RunningBand — 1e scrolling band; persistent disclosure in the left column while running.
 *
 * Authority: Claude Design `chat-ux-2026-08/运行带-实现规格.html`
 * (shape = selected 1e; this adds only six states and behavioral specifications absent from 1e).
 * Implementation criteria: `docs/sprints/chat-interaction-ux-2026-08-07/运行态披露面-不变量清点.md`.
 *
 * ## Why it exists
 *
 * PRD P0-1 requires the post-turn shape **to exist from the start**. The completed half
 * (`StepSummary`) was ready long ago, but the earlier half remained undone; on 116 v1.0.89, running showed
 * `处理中 → 思考中 → 开始处理 → 内容闪一下 → 整条塌成 Bowhead ✓ 三秒 → 内容再回来`.
 *
 * ## Three structural constraints (the component's reasons for existing, not styling)
 *
 * 1. **Lifetime = entire turn**. Mount in `TurnContainer`'s running branch **beside** `BeatStream`,
 *    rather than inside it. Today's collapse to one ✓ came from disclosure following the agent block,
 *    rebuilt wholesale when Lead handed off to a peer.
 * 2. **One JSX location only**. Two locations make React unmount/remount it when the first beat arrives,
 *    whereas avoiding reconstruction is precisely its purpose.
 * 3. **Constant height**. Always three rows (2 history + 1 current), with skeleton padding when short;
 *    PRD A3 requires a ≤24px range throughout long turns.
 *
 * All data rules live in `running-band-model.ts` (pure functions, easy to test); this file handles
 * throttling, timing, i18n and rendering.
 *
 * ⚠ **No terminal-failure state**. Design §2-E shows the band turning red in place with actions, but production cannot reach it:
 * the band mounts only while `running` is true; terminal failure and `running=false` occur in the same frame
 * (`use-turn-runner.ts` error / turn_failed branches). See the correction in inventory §2.1.
 * Hard step-level failures **still show red** (history `is-fail`); the full explanation is one click away.
 */
import { useEffect, useMemo, useRef, useState } from "react";

import {
  msSinceLastEvent,
  NO_SIGNAL_MS,
  type TransportSample,
} from "@/lib/transport-health";
import { useTranslations } from "next-intl";

import { turnElapsedMs } from "@/lib/turn-elapsed";
import type { ModelRetryStatus, PeerTerminal } from "@/lib/turn-store";
import type { AgentProgress } from "@/lib/types";

import { AgentBadge } from "./AgentBadge";
import { collaborationTopicState, useCollaborationTrace } from "./collaboration-trace-state";
import { buildBandModel, type PeerIdentity } from "./running-band-model";
import type { StepView } from "./types";
import {
  Check as LucideCheck,
  ChevronDown as LucideChevronDown,
  Circle as LucideCircle,
  TriangleAlert as LucideTriangleAlert,
  X as LucideX,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";

/**
 * Minimum dwell per row. On 116, one turn contained **two 0ms steps** (`查询数据 · 0ms`);
 * without throttling they crossed all three rows within one frame, visible only as a flash.
 */
export const MIN_DWELL_MS = 450;
/** Jump to latest once backlog exceeds this count; queuing until dawn is worse than flashing. */
export const DWELL_BACKLOG_MAX = 6;

/**
 * Release the step stream according to minimum dwell, returning a prefix of `steps`.
 *
 * ★**Throttle, not debounce**. Naively resetting a 450ms timer on every `steps.length` change
 * never releases when steps arrive every 200–400ms: the timer is repeatedly cleared until backlog exceeds
 * `DWELL_BACKLOG_MAX`, then jumps seven steps, exactly the flash throttling should prevent.
 * Thus calculate wait **from the last release time**; new steps must not reset that clock.
 */
export function useDwellPacedSteps(steps: StepView[], enabled = true): StepView[] {
  const [revealed, setRevealed] = useState(steps.length);
  const lastRevealAtRef = useRef(0);

  useEffect(() => {
    if (!enabled || steps.length < revealed) {
      // Throttling disabled, or fewer steps due to turn change/replay: align immediately, leaving no prior-turn cursor.
      if (revealed !== steps.length) setRevealed(steps.length);
      return;
    }
    if (steps.length === revealed) return;
    if (steps.length - revealed > DWELL_BACKLOG_MAX) {
      lastRevealAtRef.current = Date.now();
      setRevealed(steps.length);
      return;
    }
    const wait = Math.max(0, MIN_DWELL_MS - (Date.now() - lastRevealAtRef.current));
    const timer = window.setTimeout(() => {
      lastRevealAtRef.current = Date.now();
      setRevealed((n) => Math.min(steps.length, n + 1));
    }, wait);
    return () => window.clearTimeout(timer);
  }, [steps.length, enabled, revealed]);

  return revealed >= steps.length ? steps : steps.slice(0, revealed);
}

/**
 * Minimum-dwell throttling for a string changing **token by token** (prose tail).
 *
 * ★Same rule as `useDwellPacedSteps`: **throttle, not debounce**. Calculate wait from the last release,
 * and **do not reset** the clock on new values. Naive debounce never releases when tokens arrive
 * every few tens of milliseconds, leaving prose at its first frame.
 * ⚠Why needed: `applyPeerProse` appends every delta (`turn-store.ts`); without throttling,
 * copy changes on every token, producing unreadable blurred jitter.
 */
export function useThrottledText(value: string, ownerKey: string, ms = MIN_DWELL_MS): string {
  const [shown, setShown] = useState(value);
  const lastAtRef = useRef(0);
  const [owner, setOwner] = useState(ownerKey);
  // ★**Ownership changes must update immediately**, without waiting for the throttle window. Attribution (`footMeta`) updates synchronously;
  //   prose lagging 450ms would show a new agent's name with the previous agent's words at handoff,
  //   reintroducing through render throttling the misattribution model-level `allowFallback:false` blocked
  //   (caught by independent review). Align ownership changes in the same frame; throttle only continuous text from one owner.
  //
  // ⚠Store ownership in **state, not ref** (React's documented adjusting-state-during-render pattern).
  //   The previous ref implementation changed refs during rendering; discarded renders (transition / Suspense retries /
  //   any interrupted render) **partially committed**: ref advanced to the new owner, but `setShown` was discarded,
  //   disabling the next render's guard so reset **never happened**, permanently showing new name with old words,
  //   exactly what this code guards against (caught by the third independent review).
  //   State comparison avoids this: discarded renders discard both states together.
  if (owner !== ownerKey) {
    setOwner(ownerKey);
    if (shown !== value) setShown(value);
  }
  useEffect(() => {
    if (value === shown) return;
    const wait = Math.max(0, ms - (Date.now() - lastAtRef.current));
    const timer = window.setTimeout(() => {
      lastAtRef.current = Date.now();
      setShown(value);
    }, wait);
    return () => window.clearTimeout(timer);
  }, [value, shown, ms]);
  return shown;
}

/**
 * "3 分 25 秒" / "18 秒": plain language, not `205.4s`.
 *
 * ⚠Use i18n, not Chinese string assembly here. The previous hardcoded "秒 / 分" rendered
 * `3 分 25 秒` in en-US, the site's sole user-visible copy bypassing next-intl.
 */
type BandT = ReturnType<typeof useTranslations<"step_list">>;

export function formatElapsed(ms: number, t: BandT): string {
  const total = Math.max(0, Math.round(ms / 1000));
  if (total < 60) return t("band_elapsed_sec", { count: total });
  const m = Math.floor(total / 60);
  const s = total % 60;
  return s === 0 ? t("band_elapsed_min", { m }) : t("band_elapsed_min_sec", { m, s });
}

/**
 * `runner.startedAt` is epoch ms (not Date); align the signature with real data rather than wrapping at call sites.
 *
 * ⚠Calculate differences via `turnElapsedMs`, **never** directly `now - startedAt`:
 * `startedAt` uses server time, `Date.now()` browser time; measured difference on 116 was 31.4 seconds.
 * Direct subtraction kept the band at "0 秒" for the first half minute (measured frame-by-frame on-device). See that module's header.
 */
/** Second-resolution now, used for time since the last received bytes.
 *  Same frequency as `useElapsedText`'s ticker but independent: that hook returns only formatted copy,
 *  without the raw timestamp. */
function useNowTick(running: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [running]);
  return now;
}

export function useElapsedText(
  startedAt: number | null, running: boolean, t: BandT, elapsedKey: string | null,
): string {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running || !startedAt) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [running, startedAt]);
  if (!startedAt) return "";
  return formatElapsed(turnElapsedMs(startedAt, now, elapsedKey), t);
}

/** i18n key for a pending model retry; reasons without dedicated copy share the generic one. */
export function modelRetryMessageKey(reason: string):
  | "band_model_retry_timeout"
  | "band_model_retry_network"
  | "band_model_retry_unavailable"
  | "band_model_output_recovery" {
  if (reason === "output_limit") return "band_model_output_recovery";
  if (reason === "timeout") return "band_model_retry_timeout";
  if (reason === "network") return "band_model_retry_network";
  return "band_model_retry_unavailable";
}

export function formatModelRetry(status: ModelRetryStatus, t: BandT): string {
  return t(modelRetryMessageKey(status.reason), { retry: status.retry, max: status.maxRetries });
}

const DoneGlyph = () => (
  <LucideGlyph icon={LucideCheck} size={13} aria-hidden />
);
const FailGlyph = () => (
  <LucideGlyph icon={LucideX} size={13} aria-hidden />
);
const ActiveGlyph = () => (
  <LucideGlyph icon={LucideCircle} size={13} aria-hidden />
);
const IssueGlyph = () => (
  <LucideGlyph icon={LucideTriangleAlert} size={13} aria-hidden />
);

export interface RunningBandProps extends PeerIdentity {
  steps: StepView[];
  running: boolean;
  /** turn-store `peerTerminalById`: without it, a peer failing before any internal step is
   *  completely invisible on the band (lost for the entire turn once the running ledger is hidden). */
  peerTerminalById?: Record<string, PeerTerminal>;
  /** turn-store `peerProse`: the sole destination for peer prose after hiding the running ledger. */
  peerProse?: Record<string, string>;
  /** Turn start (epoch ms, like `runner.startedAt`), for the disclosure timer. */
  startedAt?: number | null;
  /**
   * Answer streaming has begun. Pure-chat turns with zero steps use this to collapse three skeleton rows to one
   * (decision in inventory §4 ②); **the band itself stays**.
   */
  answerStreaming?: boolean;
  /**
   * Backend `streaming.state{paused}` after 30s without chunks (connection remains healthy; keepalive feeds watchdog).
   *
   * ★Previously carried in a separate `MidTurnLivenessNote` row. Once the band owns running disclosure, **do not**
   * merely hide that row: it would lose capability, not move it. Rendering both also creates a second onscreen
   * `role=status`, causing double announcements (invariant inventory §2.7). Merge its semantics into the current row.
   */
  paused?: boolean;
  /**
   * turn-store `modelRetry`: a model call failed before replying and the Router is retrying it.
   * Shown as the current action until the next model output or terminal event clears it.
   * Ranked above `paused`: the retry is the specific reason the model is silent.
   */
  modelRetry?: ModelRetryStatus | null;
  /**
   * stream-liveness ③: this turn's SSE transport sample (`lib/transport-health.ts`).
   *
   * ★Use it only to answer: **how long since we received any bytes?**
   * This reports **our own observation**, always true regardless of buffered transport, crashed backend,
   * or disconnection. **No cause attribution, hence no false alarms** (administrator diagnostics attribute causes
   * and need many guards). Omitted ⇒ exactly the old behavior.
   */
  transport?: TransportSample | null;
  /**
   * Timing anchor key: `turnId#executionGeneration`.
   * ⚠Not `startedAt`: it switches from browser to server clock within one turn,
   *   making two values = two anchors = immediate reanchoring (observed `0→1→0` on 116).
   * ⚠Not turnId alone either: clarification resume resets startedAt on that same turnId,
   *   so a shared anchor counts paused wall time toward the new generation. See `lib/turn-elapsed.ts`.
   */
  elapsedKey?: string | null;
  /** Clicking disclosure expands the full trace; when `expanded`, the same control collapses it. */
  onExpand?: () => void;
  /**
   * Expanded in place: **render only the disclosure strip**, yielding its three rows to the full trace below.
   *
   * ★Why this state exists: expansion was a **one-way door**. Once `traceRequestOrigin` became
   *   `"band"`, only a new turn cleared it (`TurnContainer.tsx`); `onExpand` had no opposite action
   *   anywhere in the repo. After opening, users **could never return to the band**, seeing only the ledger
   *   (user reported "缩回去就是之前的控件样子了，不是最新的流").
   *   Reusing disclosure for collapse gives **two states of the existing shape**, not a new control.
   * ⚠Expanded state **omits** the three rows: band + trace would give two progress surfaces, exactly what ③ removes.
   */
  expanded?: boolean;
  /**
   * Agent run limits, phase 3: transient `turn.progress` snapshot. After {@link PROGRESS_QUIET_MS}
   * without a new step the action line says which agent is still running and at which step.
   */
  agentProgress?: AgentProgress | null;
}

/** A step list this long without a new row shows the progress line instead of the last action. */
export const PROGRESS_QUIET_MS = 60_000;

export function RunningBand({
  steps, running, startedAt = null, answerStreaming = false, paused = false, modelRetry = null, onExpand,
  expanded = false, elapsedKey = null,
  peerNames, peerDisplayNames, peerTerminalById, peerProse, transport = null, agentProgress = null,
}: RunningBandProps) {
  const t = useTranslations("step_list");
  const paced = useDwellPacedSteps(steps, running);
  // This peer has **real internal steps** (spawn rows do not count: they are Lead's tool calls).
  // ⚠Feed **unthrottled** `steps`: `step_done` changes state in place while later steps remain queued for throttling;
  //   using the throttled prefix would show "未完成", then change back to "已重试" 450ms later,
  //   retracting an already stated terminal fact.
  const peerIdsWithSteps = useMemo(
    () => new Set(
      steps.filter((s) => !s.peerProseKey)
        .map((s) => s.subagentId).filter((v): v is string => Boolean(v)),
    ),
    [steps],
  );
  // Same unthrottled `steps` as above: separate sources would first show one fact in an independent row,
  // then rewrite it 450ms later; changed React key replays animation and shifts position.
  const peerIdsWithSpawnRow = useMemo(
    () => new Set(steps.map((s) => s.peerProseKey).filter((v): v is string => Boolean(v))),
    [steps],
  );
  const model = buildBandModel({
    steps: paced, answerStreaming, peerNames, peerDisplayNames,
    peerTerminalById, peerProse, peerIdsWithSteps, peerIdsWithSpawnRow,
  });
  const elapsed = useElapsedText(startedAt, running, t, elapsedKey);
  const preparing = model.phase === "preparing";

  // ★Disclosure and current row have **two granularities**; do not render the same sentence.
  //   Design: disclosure "正在分析 · 第 7 步 · 3 分 25 秒" / current row "正在按门店汇总客流量并排名".
  //   The first version rendered `what` in both places, duplicating copy; content-presence tests missed it,
  //   but spike screenshots exposed it immediately. Disclosure therefore uses coarse **tool-category** phase vocabulary.
  const PHASE_KEY: Record<string, string> = {
    query: "band_phase_query",
    run: "band_phase_run",
    write: "band_phase_write",
    parse: "band_phase_parse",
    agent: "band_phase_agent",
  };
  const cat = model.current?.toolCat ?? "";
  const barText = preparing
    ? t("band_preparing")
    : t((PHASE_KEY[cat] ?? "band_working") as never);
  // Only the current row states the specific action. During silence use "仍在等模型响应", more honest than continuing
  // "正在运行代码": backend explicitly says no new chunks arrived in this interval. Keep disclosure/spinner unchanged (healthy connection).
  // stream-liveness ③: **first in the decision chain**.
  // Once no bytes (including heartbeat) arrive for NO_SIGNAL_MS, every following branch reports
  // facts at least 30 seconds old; continuing "正在查询数据" would present old facts as current state.
  // ★Heartbeats arrive every 15 seconds and count as bytes, so this branch never fires on a healthy connection:
  //   firing means transport has actually fallen silent.
  const nowTick = useNowTick(running);
  // When did the step list last change? A long tool or model call adds no row for minutes.
  const stepSignature = `${steps.length}:${steps[steps.length - 1]?.stepId ?? ""}:${steps[steps.length - 1]?.state ?? ""}`;
  const lastStepChange = useRef<{ signature: string; at: number }>({ signature: stepSignature, at: Date.now() });
  if (lastStepChange.current.signature !== stepSignature) {
    lastStepChange.current = { signature: stepSignature, at: Date.now() };
  }
  const progressAgents = agentProgress?.agents ?? [];
  const progressQuiet = running && progressAgents.length > 0 && nowTick - lastStepChange.current.at >= PROGRESS_QUIET_MS;
  const elapsedMinutes = startedAt !== null ? Math.max(0, Math.floor(turnElapsedMs(startedAt, nowTick, elapsedKey) / 60_000)) : 0;
  const silentMs = transport ? msSinceLastEvent(transport, nowTick, startedAt) : null;
  const noSignal = running && silentMs !== null && silentMs >= NO_SIGNAL_MS;

  const collaboration = useCollaborationTrace();
  const waitingCount = collaboration?.data?.participants.filter((participant) => participant.waiting && participant.status === "running").length ?? 0;
  const latestTopic = [...(collaboration?.data?.topics ?? [])].sort((left, right) =>
    (right.revision ?? 0) - (left.revision ?? 0),
  )[0];
  const collaborationSummary = latestTopic && collaboration && !collaboration.loading
    ? t("collaboration.summary", {
      from: collaboration.name(latestTopic.sender_id ?? latestTopic.messages[0]?.sender_id ?? ""),
      to: collaboration.name(latestTopic.recipient_id ?? latestTopic.messages[0]?.recipient_id ?? ""),
      status: t(`collaboration.${collaborationTopicState(latestTopic, !running)}`),
    }) : "";

  const action = noSignal
    ? t("band_no_signal", { sec: Math.floor((silentMs ?? 0) / 1000) })
    : modelRetry
    ? formatModelRetry(modelRetry, t)
    : progressQuiet
    ? progressAgents.length === 1
      ? t("still_running_progress", {
        agent: progressAgents[0].displayName || peerDisplayNames?.[progressAgents[0].subagentId] || progressAgents[0].profile,
        step: progressAgents[0].step,
        minutes: elapsedMinutes,
      })
      : t("still_running_many", { count: progressAgents.length, minutes: elapsedMinutes })
    : paused
    ? t("band_paused")
    // Waiting for a peer: name who is awaited, not backend jargon "正在派子 agent",
    // nor "正在想下一步" (Lead is waiting, not thinking). 116 dogfood P1.
    : model.current?.waitingOnPeer
      ? t("band_waiting_peer", { name: model.current.waitingOnPeer })
    // ⚠Before the name arrives, **do not name anyone**: the model fallback is Lead's own name,
    //   which would show "正在等 Bowhead 返回" = Lead waiting for itself (one frame observed on 116).
    : model.current?.waitingOnPeerUnknown
      ? t("band_waiting_peer_anon")
    : preparing
      ? t("band_thinking")
      : model.current?.what
        ? t("band_doing", { what: model.current.what })
        : t("band_working");

  // Prose tail: second line of the current row; changes per token, requiring throttling for readability.
  const prose = useThrottledText(model.current?.prose ?? "", model.current?.owner ?? "lead");
  // ⚠When prose exists, **replace `detail`** (argument summary + duration), preserving `agentName` and "接手自 X".
  //   `.bh-band__foot` is a single line: both together silently sacrifice the less-observed item under flex pressure
  //   (observed in this repo). Prefer prose because it is live, exactly what the user's "至少显得有内容" decision
  //   requires; click to read the full label.
  const footMeta = [
    waitingCount > 0 ? t("collaboration.waiting_count", { count: waitingCount }) : "",
    collaborationSummary,
    model.current?.agentName ?? "",
    ...(prose ? [] : (model.current?.detail ?? [])),
    model.current?.handoffFrom ? t("band_handoff", { from: model.current.handoffFrom }) : "",
  ].filter(Boolean).join(" · ");

  return (
    <div
      className="bh-band"
      data-testid="running-band"
      data-phase={model.phase}
      data-density={model.density}
      data-paused={paused ? "1" : undefined}
      data-model-retry={modelRetry ? "1" : undefined}
    >
      {/* Announce only action + step number; **do not** make the whole band a live region,
          or its seconds timer interrupts screen-reader users every second. */}
      {/* ⚠No announcements when expanded: the full trace below supplies details, and
          `MidTurnLivenessNote` takes over liveness announcements. Two onscreen `role=status` elements
          would announce twice (expressly forbidden in inventory §2.7). */}
      {expanded ? null : (
      <span className="bh-band__sr" role="status" aria-live="polite">
        {/* ⚠Include failure count here: the chip sits inside the `.bh-band__bar` button, whose
            `aria-label` **replaces its entire accessible name**, so screen readers never hear chip text.
            It is the persistent half (history rows scroll away in seconds); inaudibility makes this disclosure
            nonexistent to screen-reader users. Count changes only on peer failure, without announcement noise. */}
        {[action,
          model.stepOrdinal > 0 ? t("band_step_n", { count: model.stepOrdinal }) : "",
          model.peerFailCount > 0
            ? t("band_peer_fail_n", { count: model.peerFailCount }) : "",
          model.issueCount > 0 ? t("step_issues_n", { count: model.issueCount }) : "",
        ].filter(Boolean).join(" · ")}
      </span>
      )}
      <button
        type="button"
        // ⚠Expanded disclosure is the **last** child; its own `border-bottom`
        //   overlaps `.bh-band`'s outline into two differently colored hairlines (caught by independent review).
        //   Add .legacy-band-bar--expanded to remove the bottom line (formerly inline styles, workbench/canvas batch 1).
        className={expanded ? "bh-band__bar legacy-band-bar--expanded" : "bh-band__bar"}
        onClick={onExpand}
        data-testid="running-band-bar"
        aria-expanded={expanded}
        aria-label={expanded ? t("collapse_trace") : t("expand_trace")}
      >
        <span className="bh-band__spin" aria-hidden />
        <span className="bh-band__txt">{barText}</span>
        {/* Opening stepOrdinal is 0; "第 0 步" is internal, not for users. */}
        <span className="bh-band__meta">
          {[model.stepOrdinal > 0 ? t("band_step_n", { count: model.stepOrdinal }) : "", elapsed]
            .filter(Boolean).join(" · ")}
        </span>
        {/* ★The persistent half. With only 2 history rows, an early-failing peer scrolls away after seconds,
            leaving no trace on the turn's primary surface: precisely the dismiss-it-because-it-scrolls-away
            behavior forbidden in inventory §2.1. Keep the count here; click for the full reason.
            Put peer outcomes before the separate recorded step-issue count. */}
        {model.peerFailCount > 0 ? (
          <span className="bh-band__fail" data-testid="running-band-peer-fail">
            {t("band_peer_fail_n", { count: model.peerFailCount })}
          </span>
        ) : null}
        {model.issueCount > 0 ? (
          <span className="bh-band__retry" data-testid="running-band-issues">
            {t("step_issues_n", { count: model.issueCount })}
          </span>
        ) : null}
        <span
          className={expanded ? "bh-band__chev legacy-band-chev--expanded" : "bh-band__chev"}
          aria-hidden
        >
          <LucideGlyph icon={LucideChevronDown} size={15} />
        </span>
      </button>

      {/* ⚠Expanded state keeps only disclosure: three rows alongside the full trace below give two progress surfaces,
          exactly what hiding the ledger in ③ must eliminate. */}
      {expanded ? null : (
      <div className="bh-band__hist">
        {model.history.map((row, i) => (
          row.kind === "skeleton" ? (
            // Skeleton padding: its sole purpose is constant height from the first frame.
            <div key={row.key} className="bh-band__row bh-band__row--ghost" aria-hidden>
              <span className="bh-band__gbar legacy-band-gbar--lead" />
              <span className="bh-band__gbar" style={{ width: 78 + i * 32, opacity: 0.5 + i * 0.2 }} />
            </div>
          ) : (
            <div
              key={row.key}
              className={`bh-band__row bh-band__row--h${i + 1} is-${row.tone}`}
              data-row-kind={row.kind}
            >
              <span className="bh-band__glyph">
                {/* Neither warning severity nor peer failure proves that a retry occurred. */}
                {row.kind === "peer"
                  ? (row.peerOutcome === "fail" ? <FailGlyph /> : <DoneGlyph />)
                  : row.tone === "warn" ? <IssueGlyph />
                    : row.tone === "fail" ? <FailGlyph />
                      : row.tone === "active" ? <ActiveGlyph /> : <DoneGlyph />}
              </span>
              {/* If banned-word filtering empties the headline, use a generic fallback, **never** the skeleton branch,
                  which would make a finished step look like it is still loading. */}
              <span className="bh-band__lab">
                {row.kind === "peer"
                  ? (row.peerName
                    // Named: include attribution.
                    ? t(
                      row.peerOutcome === "fail" ? "band_peer_failed" : "band_peer_done",
                      { name: row.peerName },
                    )
                    // Unnamed (dispatch failed before agent_spawned): use anonymous copy,
                    // **never invent a name** here; that would be a Chinese constant leaking into en-US.
                    : t(row.peerOutcome === "fail"
                      ? "band_peer_failed_anon" : "band_peer_done_anon"))
                  : row.label || t("band_row_done")}
              </span>
              <span className="bh-band__rmeta">
                {row.kind === "step" && row.tone === "warn"
                  ? t(row.stepState === "cancelled" ? "band_row_cancelled"
                    : row.stepState === "partial" ? "band_row_partial" : "band_row_failed")
                  : row.meta}
              </span>
            </div>
          )
        ))}

        <div className="bh-band__now">
          {/* ⚠AgentBadge requires backend `agent_name`, not AgentKey;
              passing a key collapses all custom agents into one generic badge. */}
          <AgentBadge agent={model.current?.agentBadgeName ?? "lead"} size={20} />
          <div className="bh-band__now-main">
            <div className="bh-band__act">{action}</div>
            {footMeta || prose ? (
              <div className="bh-band__foot">
                {footMeta ? <span className="bh-band__foot-meta">{footMeta}</span> : null}
                {prose ? (
                  <span className="bh-band__foot-prose" data-testid="running-band-prose">
                    {prose}
                  </span>
                ) : null}
              </div>
            ) : null}
          </div>
        </div>
      </div>
      )}
    </div>
  );
}
