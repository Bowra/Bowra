/**
 * useDebouncedSync — debounce PATCH on Tiptap editor changes.
 *
 * Call flow:
 *   editor.onUpdate → enqueue dirty sectionIds → debounce 800ms → batch PATCH
 *
 * Reduce requests: continuous typing issues one PATCH (merging multiple transactions).
 * Concurrent changes to several sections: PATCH each independently.
 */

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { Editor } from "@tiptap/react";
import type { ReportSection } from "@/lib/types";
import { pmDocToSections, type SectionPatch } from "@/lib/canvas-pm-adapter";
import { projectSectionsThroughEditor } from "@/lib/canvas-pm-adapter/projected-baseline";
import { commitPendingInlineEdits } from "@/lib/canvas-pm-adapter/inline-edit-registry";
import { patchCanvasSection, type PatchResult } from "@/lib/canvas-pm-adapter/canvas-patch";
import { reorderCanvas } from "@/lib/api";
import {
  extractTopLevelSectionOrder,
  mapDisplayOrderToFullOrder,
  ordersEqual,
} from "@/lib/section-order";
import { dismissToast, toast, type Toast } from "./use-toast";
import { reportSaveCopyLabels, reportSaveFailureMessage } from "@/lib/report-save-errors";
import { staticTranslator } from "@/lib/locale-messages";
import { copyDiagnosticsToastAction } from "@/components/error-notice";

// i18n: this module runs outside the React tree (toast/lastError emitted from a debounced
// callback + a global retry loop), so it resolves copy via `staticTranslator()` from the
// `report_autosave` messages namespace. Resolved per-call so the active locale wins.
//   · save_degraded_markdown: ⚠ never say "请撤销本次改动": degradation is unrelated to this edit (section contains nodes
//     unknown to write path); undo cannot make it savable — that would send users on pointless action.
//   · save_degraded_markdown_all: if all changes are in degraded sections, **never** say "其余段落已正常保存" —
//     no section was saved. All three exits formerly shared wording above, misleading users on total degradation.
type AutosaveMessageKey =
  | "retry_success"
  | "section_missing"
  | "section_deleted_synced"
  | "remote_newer"
  | "save_conflict"
  | "save_degraded_markdown"
  | "save_degraded_markdown_all";

// Deduplicate degradation toasts: `report-view` recreates hook callback per render → effect repeatedly remounts →
// frequent cleanup flushes. Without deduplication, eight typed characters produce eight identical **persistent** toasts,
// covering most of screen. Deduplicate by canvas + section set, reset on save success.
let _lastDegradedToastKey: string | null = null;
function _t(key: AutosaveMessageKey): string {
  return staticTranslator("report_autosave")(key);
}
function _retryFailed(message: string): string {
  return staticTranslator("report_autosave")("retry_failed", { message });
}
/**
 * Already queued for automatic retry: say only recorded and will save again automatically, omitting failure instructions
 * that request manual saving and contradict automatic retry.
 */
function _saveFailedQueued(): string {
  return staticTranslator("report_autosave")("save_failed_queued");
}
function _saveFailed(message: string): string {
  return staticTranslator("report_autosave")("save_failed", { message });
}
function _forceOverwriteFailed(message: string): string {
  return staticTranslator("report_autosave")("force_overwrite_failed", { message });
}

// Save failures outside tree (component unmounted) show persistent error Toast with "复制诊断" action; use-toast does not merge by text:
// repeated failures for same report stack up. Keep latest per report here; close on save success.
const _saveFailureToastByCanvas = new Map<string, string>();
function _showSaveFailureToast(canvasId: string | null | undefined, input: Omit<Toast, "id" | "count">): void {
  const key = canvasId ?? "";
  const previous = _saveFailureToastByCanvas.get(key);
  if (previous) dismissToast(previous);
  _saveFailureToastByCanvas.set(key, toast(input));
}
function _clearSaveFailureToast(canvasId: string | null | undefined): void {
  const key = canvasId ?? "";
  const previous = _saveFailureToastByCanvas.get(key);
  if (!previous) return;
  dismissToast(previous);
  _saveFailureToastByCanvas.delete(key);
}

type PendingSaveTask = {
  canvasId: string;
  sectionId: string;
  patch: Partial<ReportSection>;
  expectedRev?: number;
  attempts: number;
  lastError: string;
};

type PendingSaveConflict = {
  canvasId: string;
  sectionId: string;
  patch: Partial<ReportSection>;
  expectedRev?: number;
  currentRev: number;
  lastError: string;
  savedAt: number;
};

const SAVE_RETRY_MAX_ATTEMPTS = 3;
const SAVE_RETRY_DELAY_MS = 5000;
const _pendingSaveRetries = new Map<string, PendingSaveTask>();
const _pendingSaveConflicts = new Map<string, PendingSaveConflict>();
let _retryTimer: number | null = null;
let _retrying = false;

function retryKey(canvasId: string, sectionId: string): string {
  return `${canvasId}:${sectionId}`;
}

function sectionRevision(section: ReportSection): number | undefined {
  const rev = section.rev;
  return typeof rev === "number" && Number.isFinite(rev) ? rev : undefined;
}

function mergeSavedSection(
  sections: ReportSection[],
  sectionId: string,
  patch: Partial<ReportSection>,
  rev: number,
): ReportSection[] {
  return sections.map((section) => (
    section.id === sectionId
      ? { ...section, ...patch, id: section.id, kind: section.kind, rev } as ReportSection
      : section
  ));
}

function acceptSavedSection(
  sections: ReportSection[],
  incoming: ReportSection,
): ReportSection[] {
  let replaced = false;
  const next = sections.map((section) => {
    if (section.id !== incoming.id) return section;
    replaced = true;
    return incoming;
  });
  if (!replaced) next.push(incoming);
  return next;
}

function mergeIncomingBaseline(
  current: ReportSection[],
  incoming: ReportSection[],
): ReportSection[] {
  const currentById = new Map(current.map((section) => [section.id, section]));
  return incoming.map((section) => {
    const saved = currentById.get(section.id);
    const savedRev = saved ? sectionRevision(saved) : undefined;
    const incomingRev = sectionRevision(section);
    if (
      saved
      && savedRev !== undefined
      && incomingRev !== undefined
      && savedRev > incomingRev
    ) {
      return saved;
    }
    return section;
  });
}

function scheduleSaveRetry(): void {
  if (typeof window === "undefined") return;
  if (_retryTimer) return;
  _retryTimer = window.setTimeout(() => {
    _retryTimer = null;
    void retryPendingCanvasSaves();
  }, SAVE_RETRY_DELAY_MS);
}

function queueSaveRetry(task: Omit<PendingSaveTask, "attempts">): void {
  const key = retryKey(task.canvasId, task.sectionId);
  const prev = _pendingSaveRetries.get(key);
  _pendingSaveRetries.set(key, {
    ...task,
    attempts: prev?.attempts ?? 0,
  });
  scheduleSaveRetry();
}

function queueSaveConflict(task: Omit<PendingSaveConflict, "savedAt">): void {
  const key = retryKey(task.canvasId, task.sectionId);
  _pendingSaveRetries.delete(key);
  _pendingSaveConflicts.set(key, {
    ...task,
    savedAt: Date.now(),
  });
}

function findSaveConflict(canvasId: string): PendingSaveConflict | null {
  for (const task of _pendingSaveConflicts.values()) {
    if (task.canvasId === canvasId) return task;
  }
  return null;
}

function clearSaveConflict(canvasId: string, sectionId: string): void {
  _pendingSaveConflicts.delete(retryKey(canvasId, sectionId));
}

export async function retryPendingCanvasSaves(): Promise<void> {
  if (_retrying || _pendingSaveRetries.size === 0) return;
  _retrying = true;
  try {
    for (const [key, task] of Array.from(_pendingSaveRetries.entries())) {
      const res = await patchCanvasSection(
        task.canvasId,
        task.sectionId,
        task.patch,
        task.expectedRev,
      );
      if (res.ok) {
        _pendingSaveRetries.delete(key);
        _clearSaveFailureToast(task.canvasId);
        toast({ kind: "success", text: _t("retry_success") });
        continue;
      }
      const attempts = task.attempts + 1;
      const message =
        res.status === "not_found"
          ? _t("section_missing")
          : res.status === "conflict"
            ? _t("remote_newer")
            : res.message;
      if (res.status === "conflict") {
        queueSaveConflict({
          canvasId: task.canvasId,
          sectionId: task.sectionId,
          patch: task.patch,
          expectedRev: task.expectedRev,
          currentRev: res.currentRev,
          lastError: message,
        });
        _showSaveFailureToast(task.canvasId, { kind: "error", text: _t("save_conflict") });
        continue;
      }
      const retryable = res.status === "error";
      if (!retryable || attempts >= SAVE_RETRY_MAX_ATTEMPTS) {
        _pendingSaveRetries.delete(key);
        _showSaveFailureToast(task.canvasId, {
          kind: "error",
          text: _retryFailed(message),
          action: res.status === "error" ? copyDiagnosticsToastAction(res.error, "task", reportSaveCopyLabels()) : undefined,
        });
      } else {
        _pendingSaveRetries.set(key, { ...task, attempts, lastError: message });
      }
    }
  } finally {
    _retrying = false;
  }
  if (_pendingSaveRetries.size > 0) scheduleSaveRetry();
}

export function _getPendingSaveRetryCount(): number {
  return _pendingSaveRetries.size;
}

export function _getPendingSaveConflictCount(): number {
  return _pendingSaveConflicts.size;
}

export function _clearPendingSaveRetries(): void {
  _pendingSaveRetries.clear();
  _saveFailureToastByCanvas.clear();
  _pendingSaveConflicts.clear();
  if (_retryTimer) {
    clearTimeout(_retryTimer);
    _retryTimer = null;
  }
  _retrying = false;
}

if (typeof window !== "undefined") {
  window.addEventListener("online", () => { void retryPendingCanvasSaves(); });
  window.addEventListener("focus", () => { void retryPendingCanvasSaves(); });
}

export interface SyncState {
  /** Section ids awaiting save (during debounce) */
  dirty: string[];
  /** Currently saving */
  saving: boolean;
  /** Last failing section id + localized user explanation; error is raw failure, used only by "复制诊断". */
  lastError: { sectionId: string; message: string; error?: unknown } | null;
  /** Show modal on rev conflict: { sectionId, currentRev, localPatch } */
  conflict: {
    sectionId: string;
    currentRev: number;
    localPatch: Partial<ReportSection>;
  } | null;
}

export type FlushResult =
  | { ok: true; saved_revisions: Record<string, number> }
  | {
      ok: false;
      /** `degraded_not_savable` = edit belongs to degraded section **permanently** rejected by write path. Unlike other
       *  reasons, retries cannot help, but database content is intact — read-only operations (export/flatten)
       *  can continue; inability to write back must not become inability to read. */
      reason:
        | "save_failed"
        | "revision_conflict"
        | "reorder_failed"
        | "rewrite_in_progress"
        | "degraded_not_savable";
      section_id?: string;
      current_rev?: number;
      message: string;
    };

export type FlushFailureReason = Extract<FlushResult, { ok: false }>["reason"];

/**
 * Should this flush failure block **read-only** operations (export / design flatten)?
 *
 * Sole criterion: database content trustworthy and complete?
 * - `degraded_not_savable`: sections **permanently** cannot write back (retry useless), but original database content untouched,
 *   and read-only operations read that content ⇒ **do not block**. Blocking merely degrades exports to legacy,
 *   turning charts into floating-point tables, without saving a character of unsavable local edits.
 * - Others (save_failed / revision_conflict / reorder_failed / rewrite_in_progress):
 *   retryable genuine failure or changing database state ⇒ still block.
 *
 * Single criterion is deliberate: half of this sprint's defects come from duplicated criteria;
 * adding reason forces a decision here through exhaustive switch.
 */
export function flushFailureBlocksReadOnlyAction(
  reason: FlushFailureReason,
  // Type predicate rather than boolean: caller `prepareForExport` puts remaining reason directly
  // into `PrepareExportFailureReason`; plain boolean fails to narrow and causes type error there.
): reason is Exclude<FlushFailureReason, "degraded_not_savable"> {
  switch (reason) {
    case "degraded_not_savable":
      return false;
    case "save_failed":
    case "revision_conflict":
    case "reorder_failed":
    case "rewrite_in_progress":
      return true;
  }
}

export interface UseDebouncedSyncOpts {
  editor: Editor | null;
  baseline: ReportSection[];           // Original sections (diff baseline)
  /** Feishu 07.06 #12: reorder comparison baseline — pass display order (top-level section ids,
   *  after preprocessSections/canvasToPMDoc). Default falls back to stored baseline order,
   *  but stored vs display (title first/code last/chart deduplication) never match → false reorder on every edit.
   *  Display order gives baselineOrder==currentOrder without user reordering, eliminating false requests. */
  reorderBaseline?: string[];
  canvasId: string | null;
  /** Debounce milliseconds, default 800 */
  debounceMs?: number;
  /** Do not trigger when edit mode false (save resources) */
  enabled?: boolean;
  /** WP3 review #1: suspend during AI rewrite streaming — do not enqueue insertContent updates, otherwise
   *  debounce PATCHes dirty intermediate original + partial new text. Ref-backed, outside effect
   *  deps (avoid toggle causing cleanup-flush of intermediate state). */
  suspended?: boolean;
  /** PATCH success callback (caller updates store rev) */
  /** #3: return patch too — caller writes saved content into store, making store(initialDoc)
   *  reflect local saves without stale server setContent overwriting them. */
  onSaved?: (sectionId: string, newRev: number, patch: Partial<ReportSection>) => void;
  /** A2: target section vanished server-side (deleted) → caller rehydrates from server. */
  onSectionMissing?: (sectionId: string) => void;
}

export function useDebouncedSync({
  editor,
  baseline,
  reorderBaseline,
  canvasId,
  debounceMs = 800,
  enabled = true,
  suspended = false,
  onSaved,
  onSectionMissing,
}: UseDebouncedSyncOpts): SyncState & {
  /** Flush the current editor diff immediately, used by Retry / leaving edit mode. */
  flushNow: () => Promise<FlushResult>;
  /** Read the newest known server rev for an action that needs optimistic concurrency. */
  getSectionRev: (sectionId: string) => number | undefined;
  /** Accept a section returned by a server-side action that bypassed PATCH. */
  acceptServerSection: (section: ReportSection) => void;
  /** Force override: PATCH again without expected_rev */
  resolveConflictByOverride: () => Promise<void>;
  /** Reload: discard local, dispatch store fetch of latest server content (caller implements) */
  resolveConflictByReload: () => void;
  /** Called when user chooses reload (clear conflict state) */
  dismissConflict: () => void;
  /** Discard pending local edits: caller subsequently restores server content with setContent. */
  discardLocalChanges: () => void;
} {
  const [state, setState] = useState<SyncState>({
    dirty: [],
    saving: false,
    lastError: null,
    conflict: null,
  });

  const debounceTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const flushChainRef = useRef<Promise<FlushResult>>(
    Promise.resolve({ ok: true, saved_revisions: {} }),
  );
  const revisionsRef = useRef<Map<string, number>>(new Map());
  const savedBaselineRef = useRef<ReportSection[]>(baseline);
  const conflictRef = useRef(state.conflict);
  conflictRef.current = state.conflict;
  // #1: suspension flag uses ref, outside effect deps (avoid toggle cleanup-flush writing intermediate state)
  const suspendedRef = useRef(suspended);
  suspendedRef.current = suspended;
  // #12: compare reorder against display-order baseline (ref-backed, refreshed each render, outside doSync deps).
  const reorderBaselineRef = useRef(reorderBaseline);
  reorderBaselineRef.current = reorderBaseline;
  // Remember successful new order so later text edits do not repeatedly resend same reorder against old display order.
  const lastReorderRef = useRef<string[] | null>(null);
  // New content arrives (display order changes) → clear last-reorder memory and use new display order.
  useEffect(() => {
    lastReorderRef.current = null;
  }, [reorderBaseline]);

  useEffect(() => {
    savedBaselineRef.current = mergeIncomingBaseline(savedBaselineRef.current, baseline);
    const nextIds = new Set<string>();
    for (const section of savedBaselineRef.current) {
      nextIds.add(section.id);
      const rev = sectionRevision(section);
      if (rev !== undefined) {
        const known = revisionsRef.current.get(section.id);
        if (known === undefined || rev > known) {
          revisionsRef.current.set(section.id, rev);
        }
      }
    }
    for (const id of Array.from(revisionsRef.current.keys())) {
      if (!nextIds.has(id)) revisionsRef.current.delete(id);
    }
  }, [baseline]);

  useEffect(() => {
    if (!canvasId || !enabled) return;
    const pendingConflict = findSaveConflict(canvasId);
    if (!pendingConflict) return;
    setState((s) => ({
      ...s,
      saving: false,
      lastError: {
        sectionId: pendingConflict.sectionId,
        message: pendingConflict.lastError,
      },
      conflict: s.conflict ?? {
        sectionId: pendingConflict.sectionId,
        currentRev: pendingConflict.currentRev,
        localPatch: pendingConflict.patch,
      },
    }));
  }, [canvasId, enabled]);

  const runFlushPatches = useCallback(async (updateState = true): Promise<FlushResult> => {
    if (suspendedRef.current) {
      return {
        ok: false,
        reason: "rewrite_in_progress",
        message: staticTranslator("ai_edit")("resolve_before_leaving"),
      };
    }
    const pendingConflict = conflictRef.current ?? (canvasId ? findSaveConflict(canvasId) : null);
    if (pendingConflict) {
      return {
        ok: false,
        reason: "revision_conflict",
        section_id: pendingConflict.sectionId,
        current_rev: pendingConflict.currentRev,
        message: "lastError" in pendingConflict ? pendingConflict.lastError : _t("remote_newer"),
      };
    }
    if (!editor || !canvasId) return { ok: true, saved_revisions: {} };
    // Batch B (P1-2): first synchronously commit blur-only inline edits (eyebrow/h1/KPI…) into PM doc —
    // commit uses updateAttributes (synchronous dispatch); subsequent getJSON() contains new values. Otherwise
    // editing only KPI then clicking done/export excludes unblurred input from flush.
    commitPendingInlineEdits();
    if (!enabled) {
      // Read-only (`enabled = isEditMode && !!canvasId`) **has no write path at all**: editor is
      // `editable:false`, user cannot edit and debounce does not run. Differences from database then
      // all come from canonical → PM **projection**, not unsaved edits —
      // `markdownToPMNodes` deliberately strips `[cite:N]` (canvas has no cite resolver),
      // **without degraded marker**, so round trip naturally loses text and diff never empties.
      //
      // Cost of reporting save_failed for this diff (116 v1.1.0 observed `cv_51ac5cfb5a5a`):
      // open read-only → export → first `prepareForExport` flush returns !ok → returns before even
      // `GET /api/v1/canvas/{id}` → V2 entirely degrades to legacy (PDF has zero images,
      // only raw floats). This gate **blocks nothing**: chat-workbench still calls
      // `exportCtl.start(..., undefined)` for non-hardReject failures, exporting same database content,
      // merely lower rendering quality. Export is **read-only**; write-back capability must not penalize it.
      //
      // Genuine unsaved edits are handled elsewhere: `handleEditToggle` flushes before exit and
      // stays editing on failure; `discardAndExitEdit` first calls discardLocalChanges — both paths
      // ensure doc matches database when entering read-only mode.
      return { ok: true, saved_revisions: {} };
    }
    const update = (...args: Parameters<typeof setState>) => {
      if (updateState) setState(...args);
    };
    const showSilentFailure = (message: string, queued: false | "retry" | "conflict" = false, error?: unknown) => {
      if (!updateState) {
        const text = queued === "retry"
          ? _saveFailedQueued()
          : queued === "conflict"
            ? _t("save_conflict")
            : _saveFailed(message);
        _showSaveFailureToast(canvasId, {
          kind: "error",
          text,
          action: error === undefined ? undefined : copyDiagnosticsToastAction(error, "task", reportSaveCopyLabels()),
        });
      }
    };
    const setFailure = (
      sectionId: string,
      message: string,
      retryPatch?: { patch: Partial<ReportSection>; expectedRev?: number },
      error?: unknown,
    ) => {
      update((s) => ({
        ...s,
        saving: false,
        lastError: { sectionId, message, error },
      }));
      const queued = !updateState && Boolean(retryPatch) && Boolean(canvasId);
      if (queued && retryPatch && canvasId) {
        queueSaveRetry({
          canvasId,
          sectionId,
          patch: retryPatch.patch,
          expectedRev: retryPatch.expectedRev,
          lastError: message,
        });
      }
      showSilentFailure(message, queued ? "retry" : false, error);
    };
    /**
     * User-visible feedback when degraded sections cannot persist.
     *
     * Degradation is **not retryable**: while sections contain unknown write-path nodes, ten thousand retries still degrade.
     * Unlike transient network failures, skip `setFailure` retry queue and never rely only on an automatically
     * disappearing toast (116: appeared +23.1s, vanished +26.4s, then permanent badge merely
     * said "未保存" without reason, leaving users endlessly editing without saving).
     * Therefore: with component mounted, **unconditionally** set lastError to keep reason visible; when unmounted use global toast with
     * `durationMs=0` for persistence, deduplicated by degraded sections (unmemoized callback ⇒ repeated effect
     * remounts ⇒ without deduplication, eight typed characters produce eight identical screen-covering toasts).
     */
    const reportDegradedRefusal = (degradedPatches: SectionPatch[], othersSaved: boolean) => {
      if (degradedPatches.length === 0) return;
      const message = _t(othersSaved ? "save_degraded_markdown" : "save_degraded_markdown_all");
      setState((s) => ({
        ...s,
        saving: false,
        lastError: { sectionId: degradedPatches[0].sectionId, message },
      }));
      if (updateState) return;
      const key = `${canvasId ?? ""}:${degradedPatches.map((p) => p.sectionId).sort().join(",")}`;
      if (key === _lastDegradedToastKey) return;
      _lastDegradedToastKey = key;
      toast({ kind: "error", text: message, durationMs: 0 });
    };
    const setConflictFailure = (
      sectionId: string,
      currentRev: number,
      localPatch: Partial<ReportSection>,
      expectedRev?: number,
    ) => {
      update((s) => ({
        ...s,
        saving: false,
        lastError: { sectionId, message: _t("remote_newer") },
      }));
      if (!updateState && canvasId) {
        queueSaveConflict({
          canvasId,
          sectionId,
          patch: localPatch,
          expectedRev,
          currentRev,
          lastError: _t("remote_newer"),
        });
        showSilentFailure(_t("remote_newer"), "conflict");
      }
    };
    const doc = editor.getJSON();
    const savedBaseline = savedBaselineRef.current;

    // Patch calculation is pure, independent of reorder result; place first.
    // Diff against the baseline as the editor represents it, not the stored text: an untouched section
    // must never be written back just because canonical → PM → canonical normalizes it.
    const patches: SectionPatch[] = pmDocToSections(
      doc,
      savedBaseline,
      projectSectionsThroughEditor(savedBaseline, editor.schema),
    );
    const changed = patches.filter((p) => p.changed);
    const degraded = changed.filter((p) => p.degraded);
    const savable = changed.filter((p) => !p.degraded);

    // ⚠ Degradation gate **blocks only content persistence**, not reorder — data-wise independent operations.
    // Original early return for wholly degraded changes preceded reorder, claiming batch all-or-
    // nothing atomicity; but that never held (mixed `degraded.length > 0 && savable.length > 0`
    // branch below already saves partially). Cost: **any unsavable section in report**
    // permanently produces changes but no savable changes, and gate swallows even simple drag reorder
    // (observed reorderCanvas called 0 times / previously 1); split/merge newly using result become permanently unavailable,
    // while hint still says "其余段落已正常保存".
    // W1-5: reorder first — changed order calls /reorder before per-section PATCH.
    // #12: use display-order baseline (reorderBaseline: top-level section ids), not stored order —
    // stored vs display (title first/code last/chart deduplication) never match → false reorder on every text edit.
    // After genuine reorder record lastReorderRef, preventing subsequent edits resending same reorder against old display order.
    // Missing reorderBaseline (old callers/tests) → stored-order fallback (old behavior).
    const baselineOrder =
      lastReorderRef.current ??
      reorderBaselineRef.current ??
      savedBaseline.map((s) => s.id);
    const currentOrder = extractTopLevelSectionOrder(doc);
    if (!ordersEqual(baselineOrder, currentOrder) && currentOrder.length > 0) {
      try {
        // A3 (report-edit-audit): send full stored order — editor has only 6 top-level nodes (inline charts/
        // collapsed code_block); sending only those 6 collides with other sections' seq (P1-1 cause).
        // Baseline is full stored order (sections prop); refill displayed slots in new order to reconstruct full order.
        const fullOrder = mapDisplayOrderToFullOrder(
          savedBaselineRef.current.map((section) => section.id),
          currentOrder,
        );
        await reorderCanvas(canvasId, fullOrder);
        lastReorderRef.current = currentOrder;
      } catch (e) {
        const message = reportSaveFailureMessage(e);
        setFailure("(reorder)", message, undefined, e);
        return {
          ok: false,
          reason: "reorder_failed",
          section_id: "(reorder)",
          message,
        };
      }
    }

    if (changed.length === 0) {
      update((s) => ({ ...s, dirty: [] }));
      return { ok: true, saved_revisions: {} };
    }
    if (degraded.length > 0) {
      // Refuse per section: degraded sections do not persist, **clean sections in same batch still save** (rejecting batch expands impact
      // from section containing unknown nodes to whole report).
      reportDegradedRefusal(degraded, savable.length > 0);
    }
    if (savable.length === 0) {
      // **All** changes in degraded sections: persist no content, but reorder above already sent user's
      // dragged order — separate operation, not constrained by section write-back capability.
      return {
        ok: false,
        reason: "degraded_not_savable",
        section_id: degraded[0].sectionId,
        message: _t("save_degraded_markdown_all"),
      };
    }

    update((s) => ({ ...s, saving: true, dirty: savable.map((p) => p.sectionId) }));
    const savedRevisions: Record<string, number> = {};

    for (const p of savable) {
      const expectedRev = revisionsRef.current.get(p.sectionId);
      let res: PatchResult;
      try {
        res = await patchCanvasSection(canvasId, p.sectionId, p.patch, expectedRev);
      } catch (e) {
        const message = reportSaveFailureMessage(e);
        setFailure(
          p.sectionId,
          message,
          { patch: p.patch, expectedRev },
          e,
        );
        return {
          ok: false,
          reason: "save_failed",
          section_id: p.sectionId,
          message,
        };
      }
      if (res.ok) {
        revisionsRef.current.set(p.sectionId, res.rev);
        savedRevisions[p.sectionId] = res.rev;
        savedBaselineRef.current = mergeSavedSection(
          savedBaselineRef.current,
          p.sectionId,
          p.patch,
          res.rev,
        );
        onSaved?.(p.sectionId, res.rev, p.patch);
      } else if (res.status === "conflict") {
        if (updateState) {
          update((s) => ({
            ...s,
            saving: false,
            conflict: {
              sectionId: p.sectionId,
              currentRev: res.currentRev,
              localPatch: p.patch,
            },
          }));
        } else {
          setConflictFailure(
            p.sectionId,
            res.currentRev,
            p.patch,
            expectedRev,
          );
        }
        return {
          ok: false,
          reason: "revision_conflict",
          section_id: p.sectionId,
          current_rev: res.currentRev,
          message: _t("remote_newer"),
        }; // Stop subsequent patches on conflict (user resolves first)
      } else if (res.status === "not_found") {
        // A2 (report-edit-audit): section deleted (undo after menu deletion in this session revived local
        // node, or deleted elsewhere/another tab) — not save failure; retries never succeed. Converge gracefully:
        // discard patch + remove baseline/rev bookkeeping + tell user synchronized, without error bar
        // or retry queue. Caller proceeds with ok:true (ghost patches must not block export prerequisites).
        savedBaselineRef.current = savedBaselineRef.current.filter(
          (section) => section.id !== p.sectionId,
        );
        revisionsRef.current.delete(p.sectionId);
        toast({ kind: "info", text: _t("section_deleted_synced") });
        onSectionMissing?.(p.sectionId);
      } else {
        const message = (res as { message: string }).message;
        setFailure(
          p.sectionId,
          message,
          res.status === "error" ? { patch: p.patch, expectedRev } : undefined,
          res.status === "error" ? res.error : undefined,
        );
        return {
          ok: false,
          reason: "save_failed",
          section_id: p.sectionId,
          message,
        };
      }
    }
    if (degraded.length > 0) {
      // Clean sections persisted, but some remain unsavable — cannot return ok:true (export/navigation would
      // believe all saved), or clear lastError (user must see remaining unsaved sections).
      update((s) => ({
        ...s,
        saving: false,
        dirty: degraded.map((p) => p.sectionId),
      }));
      return {
        ok: false,
        reason: "degraded_not_savable",
        section_id: degraded[0].sectionId,
        message: _t("save_degraded_markdown"),
      };
    }
    _lastDegradedToastKey = null; // All saved ⇒ reset, allowing hint on next genuine degradation
    _clearSaveFailureToast(canvasId); // Old save-failure hint no longer applies
    update((s) => ({ ...s, saving: false, dirty: [], lastError: null }));
    return { ok: true, saved_revisions: savedRevisions };
  }, [editor, canvasId, enabled, onSaved, onSectionMissing]);

  const flushPatches = useCallback((updateState = true): Promise<FlushResult> => {
    const run = async (): Promise<FlushResult> => {
      try {
        return await runFlushPatches(updateState);
      } catch (error) {
        return {
          ok: false,
          reason: "save_failed",
          message: reportSaveFailureMessage(error),
        };
      }
    };
    const next = flushChainRef.current.then(run, run);
    flushChainRef.current = next;
    return next;
  }, [runFlushPatches]);

  const wasSuspended = useRef(suspended);
  // Track real edits during review, but defer writes until the candidate is resolved.
  useEffect(() => {
    if (!editor || !enabled) return;
    const handler = () => {
      // Calculate current dirty (leave saving state unchanged; UI hint only)
      const doc = editor.getJSON();
      const patches: SectionPatch[] = pmDocToSections(
        doc,
        savedBaselineRef.current,
        projectSectionsThroughEditor(savedBaselineRef.current, editor.schema),
      );
      const changed = patches.filter((p) => p.changed).map((p) => p.sectionId);
      setState((s) => ({ ...s, dirty: changed }));

      if (suspendedRef.current) return;
      if (debounceTimer.current) clearTimeout(debounceTimer.current);
      debounceTimer.current = setTimeout(() => {
        debounceTimer.current = null;
        void flushPatches(true);
      }, debounceMs);
    };
    editor.on("update", handler);
    const resumed = wasSuspended.current && !suspended;
    wasSuspended.current = suspended;
    if (resumed) handler();
    return () => {
      editor.off("update", handler);
      if (debounceTimer.current) {
        clearTimeout(debounceTimer.current);
        debounceTimer.current = null;
        // #1: while suspended (rewrite streaming), never cleanup-flush intermediate state
        if (!suspendedRef.current) void flushPatches(false);
      }
    };
  }, [editor, enabled, debounceMs, flushPatches, suspended]);

  const resolveConflictByOverride = useCallback(async () => {
    const c = state.conflict;
    if (!c || !canvasId) return;
    // #5: override with currentRev user saw at conflict, not blind no-rev. If newer version arrives
    // during override, another 409 occurs (conflict branch below refreshes currentRev and reopens), never silently
    // overwriting a version user never saw.
    const res = await patchCanvasSection(canvasId, c.sectionId, c.localPatch, c.currentRev);
    if (res.ok) {
      clearSaveConflict(canvasId, c.sectionId);
      revisionsRef.current.set(c.sectionId, res.rev);
      savedBaselineRef.current = mergeSavedSection(
        savedBaselineRef.current,
        c.sectionId,
        c.localPatch,
        res.rev,
      );
      onSaved?.(c.sectionId, res.rev, c.localPatch);
      setState((s) => ({ ...s, conflict: null, lastError: null }));
    } else {
      const message =
        res.status === "not_found"
          ? _t("section_missing")
          : res.status === "conflict"
            ? _t("remote_newer")
            : res.message;
      setState((s) => ({
        ...s,
        conflict: res.status === "conflict"
          ? { ...c, currentRev: res.currentRev }
          : c,
        lastError: {
          sectionId: c.sectionId,
          message: _forceOverwriteFailed(message),
          error: res.status === "error" ? res.error : undefined,
        },
      }));
    }
  }, [state.conflict, canvasId, onSaved]);

  const resolveConflictByReload = useCallback(() => {
    // Caller fetches latest sections + reloads (canvas-store)
    if (state.conflict && canvasId) clearSaveConflict(canvasId, state.conflict.sectionId);
    setState((s) => ({ ...s, conflict: null }));
  }, [canvasId, state.conflict]);

  const dismissConflict = useCallback(() => {
    if (state.conflict && canvasId) clearSaveConflict(canvasId, state.conflict.sectionId);
    setState((s) => ({ ...s, conflict: null }));
  }, [canvasId, state.conflict]);

  const discardLocalChanges = useCallback(() => {
    if (debounceTimer.current) {
      clearTimeout(debounceTimer.current);
      debounceTimer.current = null;
    }
    if (state.conflict && canvasId) clearSaveConflict(canvasId, state.conflict.sectionId);
    setState((s) => ({ ...s, dirty: [], saving: false, lastError: null, conflict: null }));
  }, [canvasId, state.conflict]);

  const flushNow = useCallback(() => flushPatches(true), [flushPatches]);
  const getSectionRev = useCallback((sectionId: string) => {
    return revisionsRef.current.get(sectionId);
  }, []);
  const acceptServerSection = useCallback((section: ReportSection) => {
    savedBaselineRef.current = acceptSavedSection(savedBaselineRef.current, section);
    const rev = sectionRevision(section);
    if (rev !== undefined) {
      revisionsRef.current.set(section.id, rev);
      const patch = Object.fromEntries(
        Object.entries(section).filter(([key]) => key !== "id" && key !== "kind"),
      ) as Partial<ReportSection>;
      onSaved?.(section.id, rev, patch);
    }
  }, [onSaved]);

  return {
    ...state,
    flushNow,
    getSectionRev,
    acceptServerSection,
    resolveConflictByOverride,
    resolveConflictByReload,
    dismissConflict,
    discardLocalChanges,
  };
}
