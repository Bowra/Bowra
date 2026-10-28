import { appFetch as fetch, appPath } from "@/lib/base-path";
import { randomUuid } from "./random-id";

/**
 * V2.8 A2 · Frontend telemetry helper.
 *
 * Fire-and-forget POST /api/v1/metrics/event; backend allowlist validates values and writes
 * Prometheus counters. Failure silent (must not affect user experience).
 */

const ENDPOINT = appPath("/api/v1/metrics/event");
const BATCH_ENDPOINT = appPath("/api/v1/metrics/events");
const COMPOSER_ENDPOINT = appPath("/api/v1/metrics/composer-event");

/** Allowed metric names only (backend allowlist aligned, IDE hints).
 *  ⚠ Add names to metrics_ingest.py `_ALLOWED` **simultaneously**, or backend silently drops
 *  (currently bowhead_sidebar_session_action_total, another owned surface, recorded issue). */
export type MetricName =
  | "bowhead_trace_strip_expanded_total"
  | "bowhead_sql_chip_clicked_total"
  | "bowhead_plan_step_mismatch_total"
  | "bowhead_tool_drawer_opened_total"
  | "bowhead_tool_drawer_copy_total"
  | "bowhead_canvas_citation_total"
  | "bowhead_canvas_ai_rewrite_total"
  | "bowhead_canvas_data_focus_total"
  | "bowhead_results_workspace_total"
  | "bowhead_canvas_report_perf_total"
  | "bowhead_chart_fallback_table_total"
  // ── Batch 0 0-F chart interaction metrics + 0-A export degradation notice (batch-2 density decision data) ──
  | "bowhead_chart_action_total"
  | "bowhead_chart_kind_switch_total"
  | "bowhead_export_legacy_fallback_total"
  // Batch 2 inspection layer.
  | "bowhead_chart_inspect_total"
  | "bowhead_report_library_total"
  | "bowhead_canvas_export_total"
  | "bowhead_composer_layout_mode_total"
  | "bowhead_report_link_total"
  // ── Former lib/track.ts callers (merged here; see track comment below) ──
  | "bowhead_mobile_drawer_total"
  | "bowhead_sidebar_state_changed_total"
  | "bowhead_nav_destination_opened_total"
  | "bowhead_sidebar_recent_opened_total"
  | "bowhead_sidebar_search_total"
  | "bowhead_sidebar_session_action_total"
  | "bowhead_settings_opened_total"
  | "bowhead_admin_destination_opened_total"
  | "bowhead_dark_toggle_total"
  | "bowhead_cmd_k_total"
  | "bowhead_section_chrome_menu_total"
  | "bowhead_user_menu_total"
  | "bowhead_citation_popover_total"
  | "bowhead_design_flatten_ui_total"
  | "bowhead_formatting_toolbar_total"
  | "bowhead_home_proposal_ui_total"
  | "bowhead_section_split_total"
  | "bowhead_session_tab_total"
  | "frontend_source_lookup_empty";

/* ────────────────────────────────────────────────────────────────────────
 * Transport: batching + single-connection flush
 *
 * 2026-08-08 device (30-section report hydration): bowhead_canvas_report_perf_total emitted
 * over a dozen events per hydration, previously **one fetch(keepalive) per event**, exhausting browser slots
 * with ERR_INSUFFICIENT_RESOURCES and consuming backend 120/60s quota per event → 429.
 *
 * ⚠ **Deliberately no deduplication here**: counters must record 14 setContent calls 14 times. Transport deduplication
 * erases precisely what instrument measures; those 14 calls are the reported performance issue.
 * Callers needing dedupe (hover probing/repeated clicks) pass dedupe: true; semantics belong to caller, not transport.
 * ──────────────────────────────────────────────────────────────────────── */

const _FLUSH_INTERVAL_MS = 250;
/** Match backend `_BATCH_MAX`; overflow sends another batch, never drops events. */
const _BATCH_MAX = 50;

type QueuedEvent = { name: string; labels: Record<string, string> };

let _queue: QueuedEvent[] = [];
let _flushTimer: ReturnType<typeof setTimeout> | null = null;
let _lifecycleBound = false;

function _post(url: string, body: string): void {
  // sendBeacon completes on unload/pagehide without consuming ordinary connection pool.
  if (typeof navigator !== "undefined" && navigator.sendBeacon) {
    try {
      if (navigator.sendBeacon(url, new Blob([body], { type: "application/json" }))) return;
    } catch {
      // fall through to fetch
    }
  }
  try {
    void fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
      keepalive: true,
    }).catch(() => {});
  } catch {
    // Telemetry failure must never affect user experience.
  }
}

/** Send queue immediately (also on page hide/unload). */
export function flushMetrics(): void {
  if (_flushTimer !== null) {
    clearTimeout(_flushTimer);
    _flushTimer = null;
  }
  if (_queue.length === 0) return;
  const pending = _queue;
  _queue = [];
  for (let i = 0; i < pending.length; i += _BATCH_MAX) {
    const chunk = pending.slice(i, i + _BATCH_MAX);
    // Single event uses old /event without envelope, retaining existing backend test-path coverage.
    if (chunk.length === 1) {
      _post(ENDPOINT, JSON.stringify(chunk[0]));
    } else {
      _post(BATCH_ENDPOINT, JSON.stringify({ events: chunk }));
    }
  }
}

function _bindLifecycle(): void {
  if (_lifecycleBound || typeof document === "undefined") return;
  _lifecycleBound = true;
  // pagehide more reliable than unload (bfcache-friendly); visibilitychange covers backgrounding.
  window.addEventListener("pagehide", flushMetrics);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flushMetrics();
  });
}

const _DEDUPE_WINDOW_MS = 100;
const _dedupeMap = new Map<string, number>();

export interface TrackOptions {
  /** Emit same (name, labels) once within 100ms. Default **false**: transport must not dedupe counters;
   *  only callers like hover probing/repeated clicks explicitly enable it. */
  dedupe?: boolean;
}

/**
 * Frontend fire-and-forget telemetry, **sole repository entry**.
 *
 * History: lib/track.ts and lib/metrics.ts both defined track() to
 * /api/v1/metrics/event, one with 100ms dedupe, one ungated; same backend endpoint,
 * two transport policies, failures dependent on caller import. Unified here 2026-08-08.
 */
export function track(
  name: MetricName,
  labels: Record<string, string> = {},
  options: TrackOptions = {},
): void {
  // SSR no-op.
  if (typeof window === "undefined") return;

  if (options.dedupe) {
    const key = `${name}::${JSON.stringify(labels)}`;
    const now = Date.now();
    const last = _dedupeMap.get(key);
    if (last !== undefined && now - last < _DEDUPE_WINDOW_MS) return;
    _dedupeMap.set(key, now);
    if (_dedupeMap.size > 1000) {
      const firstKey = _dedupeMap.keys().next().value;
      if (firstKey !== undefined) _dedupeMap.delete(firstKey);
    }
  }

  _bindLifecycle();
  _queue.push({ name, labels });
  if (_queue.length >= _BATCH_MAX) {
    flushMetrics();
    return;
  }
  if (_flushTimer === null) {
    _flushTimer = setTimeout(flushMetrics, _FLUSH_INTERVAL_MS);
  }
}

/**
 * Interaction telemetry: 100ms dedupe by default.
 *
 * Former lib/track.ts semantics (frequent hover/repeated clicks/StrictMode double calls).
 * Valid for **interaction**, not **performance counters**, which must record 14 events 14 times.
 * Two named entry points make policy visible, rather than import-module-dependent and opaque during investigation.
 */
export function trackInteraction(
  name: MetricName,
  labels: Record<string, string> = {},
): void {
  track(name, labels, { dedupe: true });
}

/** Test-only: clear queue/dedupe map. */
export function _resetMetricsForTesting(): void {
  if (_flushTimer !== null) {
    clearTimeout(_flushTimer);
    _flushTimer = null;
  }
  _queue = [];
  _dedupeMap.clear();
}

type ComposerEventCommon = {
  surface: "home" | "workbench";
  composer_mode: "comfortable" | "compact" | "ultra";
};

export type ComposerProductEvent = ComposerEventCommon & (
  | { event_name: "composer_tools_opened" }
  | { event_name: "composer_model_panel_opened"; carrier: "popover" | "sheet" }
  | { event_name: "composer_model_preference_changed"; source: "team_default" | "personal" }
  | { event_name: "composer_thinking_effort_changed"; effort: "auto" | "fast" | "deep" }
  | { event_name: "composer_planning_toggled"; enabled: boolean; entry: "toolbar" | "tools_sheet" }
  | { event_name: "composer_send"; action: "start" | "steer" | "replay" }
  | {
      event_name: "composer_attachment_action";
      action: "select" | "upload" | "remove";
      result: "accepted" | "rejected" | "success" | "failed" | "cancelled";
      kind: "file" | "bundle";
      size_band: "small" | "large";
    }
  | {
      event_name: "composer_send_result";
      result: "transport_error";
      model_control_batch: "a" | "b" | "c";
      has_attachments: boolean;
    }
  | {
      event_name: "composer_runtime_fallback_shown";
      kind: "model" | "effort" | "both";
      reason: string;
    }
);

function telemetryEventId(): string {
  return randomUuid();
}

/** Returns the stable event ID so an explicit caller retry can reuse it. */
export function trackComposerEvent(
  event: ComposerProductEvent,
  eventId = telemetryEventId(),
): string {
  if (typeof window === "undefined" || typeof fetch === "undefined") return eventId;
  const payload = {
    event_id: eventId,
    occurred_at: new Date().toISOString(),
    ...event,
  };
  try {
    void fetch(COMPOSER_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      keepalive: true,
    }).catch(() => {});
  } catch {
    // Product telemetry is intentionally non-blocking.
  }
  return eventId;
}

/** Browser-only failure classification. HTTP and local failures remain server-owned. */
export function trackComposerTransportFailure(
  error: unknown,
  context: {
    surface: "home" | "workbench";
    modelControlBatch: "a" | "b" | "c";
    hasAttachments: boolean;
  },
): boolean {
  if (!(error instanceof TypeError)) return false;
  const activeComposer = Array.from(
    document.querySelectorAll<HTMLElement>(
      `[data-layout-mode][data-composer-surface="${context.surface}"]`,
    ),
  ).find((node) => node.getClientRects().length > 0);
  const rawMode = activeComposer?.dataset.layoutMode;
  const composerMode = rawMode === "compact" || rawMode === "ultra"
    ? rawMode
    : "comfortable";
  trackComposerEvent({
    event_name: "composer_send_result",
    result: "transport_error",
    model_control_batch: context.modelControlBatch,
    has_attachments: context.hasAttachments,
    surface: context.surface,
    composer_mode: composerMode,
  });
  return true;
}
