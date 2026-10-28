/**
 * One report-status read per canvas for every reader on the page.
 *
 * 116 dogfood: opening a report fired the same `GET /canvas/{id}/status` twice. The status chip remounts when the
 * results workspace capability arrives (header placement → toolbar placement), and the report "⋯" menu items and
 * the chip each read the status, so every mount and every `bowhead.canvas.changed` listener sent its own request.
 *
 * Rules:
 * - Concurrent requests for a canvas share one in-flight GET.
 * - A mount reuses the last result for {@link REPORT_STATUS_REUSE_MS}; older results are neither reused nor painted.
 * - Every `bowhead.canvas.changed` event starts a new change generation: the store drops all entries, including
 *   reads in flight that started before the change (their answers are discarded), whether or not a reader is
 *   mounted. Readers reacting to the same event share one new GET; a read never joins a GET from an earlier
 *   generation.
 * - {@link invalidateReportStatus} drops one entry the same way, for a status write.
 *
 * Kept free of imports so the test setup can reset it without loading the API module.
 */

export interface ReportStatusSnapshot {
  status: string;
  effectiveStatus: string;
  quarantinedCount: number;
}

/** A remount right after a read (placement change, menu reopen) reuses that read instead of sending another. */
export const REPORT_STATUS_REUSE_MS = 5_000;

export const CANVAS_CHANGED_EVENT = "bowhead.canvas.changed";

interface Entry {
  meta: ReportStatusSnapshot | null;
  fetchedAt: number;
  pending: Promise<ReportStatusSnapshot> | null;
}

const entries = new Map<string, Entry>();
const listeners = new Map<string, Set<(meta: ReportStatusSnapshot) => void>>();
let lastChangeEvent: Event | null = null;
let changeListenerAttached = false;

/**
 * Start a new change generation once per `bowhead.canvas.changed` dispatch. Called by the store's own window
 * listener and by readers' handlers with the same event object, so the order of listeners does not matter and
 * every reader of one dispatch sees the entries already dropped.
 */
export function noteCanvasChanged(event: Event): void {
  if (event === lastChangeEvent) return;
  lastChangeEvent = event;
  entries.clear();
}

function ensureChangeListener(): void {
  if (changeListenerAttached || typeof window === "undefined") return;
  changeListenerAttached = true;
  // Hears changes made while no reader is mounted (e.g. the reports list), so a later mount does not reuse them.
  window.addEventListener(CANVAS_CHANGED_EVENT, noteCanvasChanged);
}

function isFresh(entry: Entry | undefined): entry is Entry & { meta: ReportStatusSnapshot } {
  return Boolean(entry?.meta) && Date.now() - (entry as Entry).fetchedAt < REPORT_STATUS_REUSE_MS;
}

/** The last result if it is recent enough to show; null otherwise (never paint a status of unknown age). */
export function cachedReportStatus(canvasId: string): ReportStatusSnapshot | null {
  ensureChangeListener();
  const entry = entries.get(canvasId);
  return isFresh(entry) ? entry.meta : null;
}

export function subscribeReportStatus(canvasId: string, listener: (meta: ReportStatusSnapshot) => void): () => void {
  ensureChangeListener();
  let set = listeners.get(canvasId);
  if (!set) {
    set = new Set();
    listeners.set(canvasId, set);
  }
  set.add(listener);
  return () => {
    set.delete(listener);
    if (set.size === 0) listeners.delete(canvasId);
  };
}

/**
 * Read a canvas status. `force` skips a settled result (the status may have moved) but still joins a GET in flight,
 * which is always from the current change generation: a change drops older entries before anyone reads again.
 * `cause` is the `bowhead.canvas.changed` event a reader is reacting to.
 */
export function requestReportStatus<T extends ReportStatusSnapshot>(
  canvasId: string,
  load: (canvasId: string) => Promise<T>,
  { force = false, cause }: { force?: boolean; cause?: Event } = {},
): Promise<ReportStatusSnapshot> {
  ensureChangeListener();
  if (cause) noteCanvasChanged(cause);
  const entry = entries.get(canvasId);
  if (entry?.pending) return entry.pending;
  if (!force && isFresh(entry)) return Promise.resolve(entry.meta);
  const pending: Promise<ReportStatusSnapshot> = load(canvasId).then(
    (meta) => {
      if (entries.get(canvasId)?.pending === pending) {
        entries.set(canvasId, { meta, fetchedAt: Date.now(), pending: null });
        for (const listener of [...(listeners.get(canvasId) ?? [])]) listener(meta);
      }
      return meta;
    },
    (error: unknown) => {
      const current = entries.get(canvasId);
      if (current?.pending === pending) {
        if (current.meta) current.pending = null;
        else entries.delete(canvasId);
      }
      throw error;
    },
  );
  entries.set(canvasId, { meta: entry?.meta ?? null, fetchedAt: entry?.fetchedAt ?? 0, pending });
  return pending;
}

/** After a status write: forget the cached value and any read that started before it. */
export function invalidateReportStatus(canvasId: string): void {
  entries.delete(canvasId);
}

export function resetReportStatusStoreForTests(): void {
  entries.clear();
  listeners.clear();
  lastChangeEvent = null;
}
