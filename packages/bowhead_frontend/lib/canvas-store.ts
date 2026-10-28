/**
 * Canvas store — external store subscribed through React useSyncExternalStore.
 *
 * State shape:
 *   canvasId / sessionId / title / sections / pyCells / meta_intent / status
 *
 * Incoming events:
 *   canvas.opened / canvas.outline_ready / canvas.section_added /
 *   canvas.section_patched / canvas.section_removed / canvas.reordered / canvas.done
 *
 * Write operations (frontend initiated):
 *   patchSection / deleteSection / reorder / setTitle
 */

import type { ReportSection, PyCellSpec } from "./types";
import { projectCanvasForDiff, diffProjections } from "./canvas-projection";
import {
  isViewerSourceAlias,
  redactViewerSourceIdentitiesInValue,
} from "./canvas-pm-adapter/citation-types";

export type CanvasStatus = "idle" | "opened" | "outline" | "streaming" | "done" | "error";

// chat-solidify 2026-07-03: removed ExportJobState/exportJob slice — sole writer was
// dead canvas.export_enqueued handler (backend emits zero); export UI uses REST polling.

export interface CanvasState {
  canvasId: string | null;
  sessionId: string | null;
  title: string | null;
  metaIntent: string | null;
  status: CanvasStatus;
  sections: ReportSection[];
  pyCells: PyCellSpec[];
  errors: string[];
  /** P3.5: current streaming turn id (null = idle) */
  currentTurnId: string | null;
  /** P3.5: section ids currently streaming tokens (NodeView blue border + ⏳) */
  streamingSectionIds: Set<string>;
  /** P3.5: partial section ids interrupted by cancel (NodeView renders ⚠ "已中断") */
  cancelledSectionIds: Set<string>;
  /** Sprint κ: most-recently-emitted streaming section id; only this one
   *  shows the typewriter cursor (per PRD r2 §4.4.2). null = no active stream. */
  currentlyStreamingSectionId: string | null;
  /** Viewer-safe source presentations; canonical source identities never enter this store. */
  sourceRefs: import("./canvas-pm-adapter/citation-types").SourceRef[];
  /** A3 (report-edit-audit): is server seq authoritative display order? → ReportView skips forced rank sorting. */
  seqAuthoritative: boolean;
  /** source-accessibility c2: turn_id → viewer-safe citation rows ([cite:N] resolution data supplied with detail). */
  turnCitations: import("./canvas-pm-adapter/citation-types").TurnCitationsMap;
  /** Sprint θ Phase 8d-2: last canvas op failure (S8.c). Inline pill on the
   *  affected section; turn keeps running. Cleared on next canvas.* success
   *  event so subsequent retries don't keep showing stale errors. */
  lastCanvasOpFailed: {
    op: string;
    reasonCode: string;
    sectionId: string | null;
    at: number;
  } | null;
  /** canvas-pipeline-fix B1 — sec_lead_* sections streamed by Lead StreamingDecomposer
   *  default to buffer-hide during turn. response_kind decides at done:
   *    - "canvas" → revealBufferedSectionsForTurn (show all at once)
   *    - "chat"   → clearSectionsForChatTurn also clears them (user sees chat bubble, no canvas)
   *  Avoid jarring prose appearing in canvas then disappearing. Payload-driven sections (eager
   *  compose uses section_added, bypassing here) show immediately, unaffected. */
  bufferedLeadSectionIds: Set<string>;
  /** #10: starting index of current outline skeleton in sections[] (turn boundary). Previously untyped field smuggled through `as`
   *  cast; promoting to real field lets all reducers carry it via `...s`, removing fragility.
   *  onCanvasOpened/onOutlineReady write; onSectionAdded/selectStreamProgress read. */
  skeletonStartIdx: number;
  /** report-streaming D3: did current turn **actually** receive `canvas.outline_ready`?
   *  `skeletonStartIdx` alone cannot tell — defaults 0, while writer-loop path emits no
   *  lifecycle events (discovery §2③), making `sections.slice(0)` all canvas
   *  sections (including other turns), so `selectStreamProgress` always returns **false** "第 N / M 节".
   *  Only actual onOutlineReady makes denominator current turn's skeleton count. */
  outlineReady: boolean;
}

const initial: CanvasState = {
  canvasId: null,
  sessionId: null,
  title: null,
  metaIntent: null,
  status: "idle",
  sections: [],
  pyCells: [],
  errors: [],
  currentTurnId: null,
  streamingSectionIds: new Set(),
  cancelledSectionIds: new Set(),
  currentlyStreamingSectionId: null,
  sourceRefs: [],
  seqAuthoritative: false,
  turnCitations: {},
  lastCanvasOpFailed: null,
  bufferedLeadSectionIds: new Set(),
  skeletonStartIdx: 0,
  outlineReady: false,
};

type Listener = () => void;

let state: CanvasState = initial;
const listeners = new Set<Listener>();

export function getCanvasSnapshot(): CanvasState {
  return state;
}

export function subscribeCanvas(l: Listener): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

function setState(patch: Partial<CanvasState> | ((s: CanvasState) => CanvasState)): void {
  state = typeof patch === "function" ? patch(state) : { ...state, ...patch };
  listeners.forEach((l) => l());
}

// ─── Reducers (SSE-event-driven) ──────────────────────────
export function resetCanvas(): void {
  setState(initial);
}

/**
 * B1a current-page revocation seam. This only mutates the viewer presentation
 * copy; server canonical provenance remains intact and viewer PATCH is
 * rehydrated against the current canonical revision.
 */
export function redactSourceIdentityInView(sourceAlias: string): void {
  if (!isViewerSourceAlias(sourceAlias)) return;
  const aliases = new Set([sourceAlias]);
  const sections = redactViewerSourceIdentitiesInValue(state.sections, aliases);
  const sourceRefs = redactViewerSourceIdentitiesInValue(state.sourceRefs, aliases);
  // Already redacted (or source absent): do not notify subscribers; keep revocation idempotent.
  if (sections === state.sections && sourceRefs === state.sourceRefs) return;
  setState({ sections, sourceRefs });
}

export function onCanvasOpened(p: { canvas_id: string; session_id: string; title?: string | null; revised?: boolean }): void {
  // canvas-update-turn B1-6: revision turn sends extra canvas.opened frame **at wrap-up** only to identify revised report downstream
  // (IM card lacks h1, needs title for first line). Not lifecycle signal: turn not done; changing streaming status
  // back to opened enables editing early and breaks chat done fallback. ⇒ Accept title only, preserve other state.
  if (p.revised) {
    setState((s) => (s.canvasId === p.canvas_id && p.title ? { ...s, title: p.title } : s));
    return;
  }
  // canvas_append: continue same canvas, retaining sections; clear only for new canvas
  setState((s) => {
    const isSameCanvas = s.canvasId === p.canvas_id;
    return {
      ...s,
      canvasId: p.canvas_id,
      sessionId: p.session_id,
      title: p.title ?? s.title ?? null,
      status: "opened",
      sections: isSameCanvas ? s.sections : [],
      errors: [],
      // Mark current skeleton starting index (used later by onSectionAdded)
      skeletonStartIdx: isSameCanvas ? s.sections.length : 0,
      // D3: new turn begins → previous outline no longer counts (avoid denominator mixing turns)
      outlineReady: false,
    };
  });
}

export function onOutlineReady(p: { sections: { id: string; kind: string }[]; meta_intent?: string }): void {
  // Append skeleton: keep existing sections, add current turn's skeleton placeholders at end
  setState((s) => {
    const startIdx = s.skeletonStartIdx;
    const prefix = s.sections.slice(0, startIdx);

    // Defense: ID already in prefix → append `_dup` to skeleton, avoiding React key collision.
    // Server should guarantee globally unique IDs (turn_id prefix); fallback for old data / events from old code.
    const existingIds = new Set(
      prefix.map((sec) => (sec as unknown as { id?: string }).id).filter(Boolean),
    );
    const skeletons = p.sections.map((x, i) => {
      let id = x.id;
      let suffix = 1;
      while (existingIds.has(id)) {
        id = `${x.id}_dup${suffix++}`;
      }
      existingIds.add(id);
      return { id, kind: x.kind } as unknown as ReportSection;
    });
    return {
      ...s,
      status: "outline",
      metaIntent: p.meta_intent ?? s.metaIntent,
      sections: [...prefix, ...skeletons],
      skeletonStartIdx: startIdx,
      // D3: only reaching here makes `selectStreamProgress` denominator current turn's skeleton count.
      outlineReady: true,
    };
  });
}

export function onSectionAdded(p: { section: ReportSection; seq?: number }): void {
  setState((s) => {
    const startIdx = s.skeletonStartIdx;

    // Defense 1: incoming section.id already in sections[] → replace, never append
    // (avoid React "duplicate key" errors from ID collisions at any source)
    const newId = (p.section as unknown as { id?: string }).id;
    if (newId) {
      const existingIdx = s.sections.findIndex(
        (sec) => (sec as unknown as { id?: string }).id === newId,
      );
      if (existingIdx >= 0) {
        if (_sameSection(s.sections[existingIdx], p.section)) {
          return s;
        }
        const next = s.sections.slice();
        next[existingIdx] = p.section;
        return { ...s, status: "streaming", sections: next };
      }
    }

    // Normal path: replace empty same-kind placeholder only within current turn's skeleton range
    let idx = -1;
    for (let i = startIdx; i < s.sections.length; i++) {
      const sec = s.sections[i];
      if (sec.kind === p.section.kind && !_hasContent(sec)) {
        idx = i;
        break;
      }
    }
    if (idx >= 0) {
      const next = s.sections.slice();
      next[idx] = p.section;
      return { ...s, status: "streaming", sections: next };
    }
    return { ...s, status: "streaming", sections: [...s.sections, p.section] };
  });
}

export function onSectionPatched(p: {
  section_id: string;
  patch: Record<string, unknown>;
  rev?: number;
}): void {
  setState((s) => {
    // report-streaming B: patch **is** final streamed-section draft (writer finishes → backend completes
    // whole-text hygiene → `write_report` update persists and patches). Remove section from
    // streaming set now, returning it from read-only preview to editor document.
    // No new terminal event for this: patch already is sole authoritative content-finalized signal.
    const nextStreaming = s.streamingSectionIds.has(p.section_id)
      ? new Set([...s.streamingSectionIds].filter((id) => id !== p.section_id))
      : s.streamingSectionIds;
    const nextCurrent =
      s.currentlyStreamingSectionId === p.section_id ? null : s.currentlyStreamingSectionId;
    const idx = s.sections.findIndex(
      (sec) => (sec as unknown as { id: string }).id === p.section_id,
    );
    if (idx >= 0) {
      const next = s.sections.slice();
      next[idx] = {
        ...next[idx], ...p.patch, ...(p.rev !== undefined ? { rev: p.rev } : {}),
      } as ReportSection;
      return {
        ...s,
        sections: next,
        streamingSectionIds: nextStreaming,
        currentlyStreamingSectionId: nextCurrent,
      };
    }
    // canvas-comprehensive WP-4 (B2/B3): late-join upsert. FE never saw section (missing
    // section_added / disorder / reconnect lost frame), but patch supplies full section (with kind) → insert instead of
    // silently dropping (sources-repatch and write_report update patches both supply full sections).
    const patchKind = (p.patch as { kind?: string }).kind;
    if (typeof patchKind === "string" && patchKind) {
      const inserted = {
        id: p.section_id, ...p.patch, ...(p.rev !== undefined ? { rev: p.rev } : {}),
      } as unknown as ReportSection;
      return { ...s, sections: [...s.sections, inserted] };
    }
    // Field-only patch for absent section = disorder/loss; warn, never silently drop (F3 observable), with terminal reconciliation fallback.
    // eslint-disable-next-line no-console
    console.warn("[canvas-store] onSectionPatched: section not found, patch dropped", p.section_id);
    return s;
  });
}

// chat-solidify 2026-07-03: removed onSectionRemoved — sole caller was dead event
// canvas.section_removed canvas-sse handler (backend emits zero).

/**
 * D6 (canvas-empty bug 2026-05-25): backend marked these sections hidden
 * because the turn is chat-mode (no insight_card/recommendations). Filter
 * them out of canvas tab view. Chat bubble inline_artifacts wire still
 * surfaces them — chat is the visible surface for chat-mode turns.
 */
export function onSectionsHidden(p: { hidden_section_ids: string[] }): void {
  if (!p.hidden_section_ids || p.hidden_section_ids.length === 0) return;
  const hidden = new Set(p.hidden_section_ids);
  setState((s) => ({
    ...s,
    sections: s.sections.filter(
      (sec) => !hidden.has((sec as unknown as { id: string }).id),
    ),
  }));
}

/**
 * report-agent-authorship α v1 (2026-06-24): collapse-in-place "latest report wins".
 * A later report-worthy turn superseded prior report blocks. Unlike onSectionsHidden
 * (which REMOVES sections from the canvas), this MARKS them `superseded` and keeps
 * them in the store — the history accordion (ReportHistory) renders them collapsed +
 * expandable IN PLACE, so nothing vanishes and the prior reports stay recoverable.
 */
export function onSectionsSuperseded(p: {
  superseded_section_ids: string[];
  superseded_by_turn?: string | null;
  folded_by?: string | null;
}): void {
  if (!p.superseded_section_ids || p.superseded_section_ids.length === 0) return;
  const sup = new Set(p.superseded_section_ids);
  const byTurn = p.superseded_by_turn ?? undefined;
  // canvas-update-turn B1-2: `folded_by` explains folding **reason**. Backend stores it in section `_meta`, arriving at frontend only on next
  // hydrate; omitting in live turn makes ReportHistory render just-replaced chart as "较早的报告",
  // exactly shape this batch fixes. Write event value into `_meta` immediately, matching post-hydration shape.
  const foldedBy = typeof p.folded_by === "string" && p.folded_by ? p.folded_by : undefined;
  setState((s) => ({
    ...s,
    sections: s.sections.map((sec) => {
      const id = (sec as unknown as { id: string }).id;
      if (!sup.has(id)) return sec;
      const next = { ...sec, superseded: true, superseded_by_turn: byTurn };
      if (!foldedBy) return next;
      const meta = ((sec as unknown as { _meta?: Record<string, unknown> })._meta ?? {}) as Record<string, unknown>;
      return { ...next, _meta: { ...meta, folded_by: foldedBy } };
    }),
  }));
}

export function onCanvasReordered(p: { order: string[] }): void {
  setState((s) => {
    const byId = new Map(s.sections.map((sec) => [(sec as unknown as { id: string }).id, sec]));
    const next: ReportSection[] = [];
    const consumed = new Set<string>();
    p.order.forEach((id) => {
      const sec = byId.get(id);
      if (sec && !consumed.has(id)) {
        next.push(sec);
        consumed.add(id);
      }
    });
    // #6: preserve sections absent from order, appending in original relative order. Partial backend order /
    // races omitting just-streamed sections must not make them vanish from canvas (data loss).
    for (const sec of s.sections) {
      const id = (sec as unknown as { id: string }).id;
      if (!consumed.has(id)) next.push(sec);
    }
    return { ...s, sections: next };
  });
}

export function onCanvasDone(p: {
  errors?: string[];
  source?: string;
  section_count?: number;
  turn_id?: string;
}): void {
  // canvas-comprehensive review-fix (P0): onCanvasDone updates status only, **never reveals**.
  // Event wire order: canvas.done → sections_hidden → turn done (backend D6 runs after canvas.done).
  // Revealing buffer at canvas.done: ① chat-mode sections **flash** before sections_hidden arrives;
  // ② emptying buffer early makes later turn `done` handler falsely conclude no canvas content this turn → clear
  // just-revealed report sections (P0 confirmed by three-perspective review). Reveal only at turn terminal state (use-turn-runner
  // done/failed/aborted, after sections_hidden); lost individual signals recover through full reconnect replay (done persisted)
  // + terminal reconciliation (prevents F1 recurrence). turn_id remains only in signature (future turn-scoped uses).
  setState((s) => {
    // prod-bug-bash-038 #7: state-aware fallback handling.
    // When `source === "turn_done_chokepoint_fallback"`:
    //   (a) sections already streamed in → trust them, just close status
    //   (b) sections empty → DO NOT flip to "done" — leave for synthetic
    //       backstop in workspace.tsx:141-155 or a subsequent real canvas.done.
    //       Otherwise blank canvas gets stuck forever and user needs F5.
    const isFallback = p.source === "turn_done_chokepoint_fallback";
    if (isFallback && s.sections.length === 0) {
      // prod-bug-bash-039 P1: don't get stuck if multiple empty fallbacks fire.
      // Reset turn pin + streaming markers + status=idle so a later canvas-mode
      // turn in the same session doesn't mis-tag pycells (per CTO FE review).
      return {
        ...s,
        status: "idle",
        currentTurnId: null,
        streamingSectionIds: new Set(),
      };
    }
    return {
      ...s,
      status: "done",
      errors: p.errors ?? [],
      currentTurnId: null,
      streamingSectionIds: new Set(),
    };
  });
}

// ─── P3.5 Multi-segment Streaming reducers ────────────────

/** SSE: canvas.turn_streaming — whole-turn lock (translucent canvas + banner) */
export function onTurnStreaming(p: { turn_id: string }): void {
  setState((s) => {
    // W12-11 race fallback: pycell.in before turn_streaming gives cell.turnId=null.
    // Tag null cells with current turn_id here, correcting workbench grouping.
    const patchedCells = s.pyCells.map((c) =>
      c.turnId == null ? { ...c, turnId: p.turn_id } : c
    );
    return {
      ...s,
      status: "streaming",
      currentTurnId: p.turn_id,
      streamingSectionIds: new Set(),
      cancelledSectionIds: new Set(),
      pyCells: patchedCells,
    };
  });
}

/** SSE: canvas.section_streaming — section begins streaming (empty payload placeholder).
 * Sprint κ Phase C: also tracks `currentlyStreamingSectionId` (most recent),
 * used by NarrativeSection to render the typewriter cursor only on the active
 * streaming section (per PRD r2 §4.4.2 + PM#4 reviewer note). */
export function onSectionStreaming(p: {
  section_id: string;
  kind: ReportSection["kind"];
  partial_payload?: Record<string, unknown>;
}): void {
  setState((s) => {
    const existing = s.sections.find((x) => x.id === p.section_id);
    const newStreaming = new Set(s.streamingSectionIds);
    newStreaming.add(p.section_id);
    // canvas-pipeline-fix B1: sec_lead_* sections from Lead StreamingDecomposer
    // default buffer-hide until turn done reveals (response_kind=canvas) or discards
    // (response_kind=chat). Only UI visibility changes; chunk accumulation unaffected.
    const isLeadStream = p.section_id.startsWith("sec_lead_");
    const newBuffered = isLeadStream
      ? new Set([...s.bufferedLeadSectionIds, p.section_id])
      : s.bufferedLeadSectionIds;
    if (existing) {
      return {
        ...s,
        streamingSectionIds: newStreaming,
        currentlyStreamingSectionId: p.section_id,
        bufferedLeadSectionIds: newBuffered,
      };
    }
    // Create empty section placeholder (simplify through cast as)
    const placeholder = {
      id: p.section_id,
      kind: p.kind,
      ...(p.partial_payload ?? {}),
    } as unknown as ReportSection;
    return {
      ...s,
      sections: [...s.sections, placeholder],
      streamingSectionIds: newStreaming,
      currentlyStreamingSectionId: p.section_id,
      bufferedLeadSectionIds: newBuffered,
    };
  });
}

/** SSE: canvas.section_chunk — tokens arrive, accumulating markdown in sections (ReportView renders
 *  via initialDoc changes through setContent path).
 *
 *  WP3 review #8: old token-level chunkListeners/subscribeStreamChunk dead
 *  seam claimed incremental ReportView insertContentAt, but only connected to report-view
 *  `_unusedSubscribe`, never actually subscribed → removed, eliminating documented-yet-unused misdirection. Genuine
 *  incremental streaming rendering (without setContent remount) is larger change with regression history (frozen editor), requiring
 *  streaming dogfood validation; separate task, not slipped into this review. */
export function onSectionChunk(p: { section_id: string; delta: string }): void {
  setState((s) => {
    const i = s.sections.findIndex((x) => x.id === p.section_id);
    if (i < 0) return s;
    const sec = s.sections[i] as ReportSection & { markdown?: string };
    const newMarkdown = (sec.markdown ?? "") + p.delta;
    const updated = { ...sec, markdown: newMarkdown } as ReportSection;
    const newSections = [...s.sections];
    newSections[i] = updated;
    return { ...s, sections: newSections };
  });
}

/** SSE: canvas.section_done — terminal section state (is_partial=true may indicate cancellation) */
export function onSectionDone(p: {
  section_id: string;
  payload: Record<string, unknown>;
  is_partial?: boolean;
}): void {
  setState((s) => {
    const i = s.sections.findIndex((x) => x.id === p.section_id);
    const newStreaming = new Set(s.streamingSectionIds);
    newStreaming.delete(p.section_id);
    const newCancelled = new Set(s.cancelledSectionIds);
    if (p.is_partial) newCancelled.add(p.section_id);
    // Sprint κ Phase C: clear currentlyStreaming if it matches.
    const newCurrent = s.currentlyStreamingSectionId === p.section_id
      ? null : s.currentlyStreamingSectionId;
    if (i < 0) {
      return {
        ...s, streamingSectionIds: newStreaming,
        cancelledSectionIds: newCancelled,
        currentlyStreamingSectionId: newCurrent,
      };
    }
    const newSections = [...s.sections];
    newSections[i] = {
      ...(newSections[i] as ReportSection),
      ...(p.payload as Partial<ReportSection>),
    } as ReportSection;
    return {
      ...s, sections: newSections,
      streamingSectionIds: newStreaming,
      cancelledSectionIds: newCancelled,
      currentlyStreamingSectionId: newCurrent,
    };
  });
}

/** Sprint κ Phase C: prune streaming-state sections on turn cancel.
 * Per PRD r2 §4.4.4 / U2: cleanliness matters more than drafts. tool result-based sections (state=done) preserved. */
export function pruneStreamingSections(): void {
  setState((s) => {
    if (s.streamingSectionIds.size === 0) return s;
    const ids = s.streamingSectionIds;
    const remaining = s.sections.filter((sec) => !ids.has(sec.id));
    return {
      ...s,
      sections: remaining,
      streamingSectionIds: new Set(),
      currentlyStreamingSectionId: null,
    };
  });
}

/** ν2 v2: clear sections produced by a turn when post-hoc response_kind=chat.
 * Section ids from Lead's StreamingDecomposer follow `sec_lead_{turn_id}_NNN`
 * (per canvas_composers/streaming.py:234). Called from use-turn-runner on
 * `done` event when response_kind === "chat" — the canvas surface is not
 * the right place for this turn's output; chat bubble (final_answer) is.
 */
export function clearSectionsForChatTurn(turnId: string): void {
  if (!turnId) return;
  setState((s) => {
    const turnPrefix = `sec_lead_${turnId}_`;
    const filtered = s.sections.filter(
      (sec) => !(sec as unknown as { id: string }).id.startsWith(turnPrefix),
    );
    // canvas-pipeline-fix B1: also clear same-turn ids in bufferedLeadSectionIds,
    // otherwise sections clear but buffered set retains stale ids.
    const newBuffered = new Set(s.bufferedLeadSectionIds);
    let bufferedChanged = false;
    for (const sid of Array.from(newBuffered)) {
      if (sid.startsWith(turnPrefix)) {
        newBuffered.delete(sid);
        bufferedChanged = true;
      }
    }
    if (filtered.length === s.sections.length && !bufferedChanged) return s;  // nothing to clear
    const newStreaming = new Set(s.streamingSectionIds);
    for (const sid of Array.from(newStreaming)) {
      if (sid.startsWith(turnPrefix)) newStreaming.delete(sid);
    }
    return {
      ...s,
      sections: filtered,
      streamingSectionIds: newStreaming,
      bufferedLeadSectionIds: newBuffered,
      currentlyStreamingSectionId:
        s.currentlyStreamingSectionId?.startsWith(turnPrefix)
          ? null
          : s.currentlyStreamingSectionId,
    };
  });
}

/** canvas-pipeline-fix B1: turn done with response_kind="canvas" → remove buffered
 * sec_lead_${turnId}_* from bufferedLeadSectionIds, showing this turn's
 * Lead narrative sections at once. Payload-driven sections (eager compose) bypass this path.
 */
export function revealBufferedSectionsForTurn(turnId: string): void {
  if (!turnId) return;
  setState((s) => {
    const turnPrefix = `sec_lead_${turnId}_`;
    const newBuffered = new Set(s.bufferedLeadSectionIds);
    let changed = false;
    for (const sid of Array.from(newBuffered)) {
      if (sid.startsWith(turnPrefix)) {
        newBuffered.delete(sid);
        changed = true;
      }
    }
    if (!changed) return s;
    return { ...s, bufferedLeadSectionIds: newBuffered };
  });
}

/** Selector: render-visible sections (excludes Lead-streaming sections still
 * in buffer-hide state). UI uses this instead of raw `state.sections`, avoiding chat-mode
 * prose flashing then disappearing. */
export function selectVisibleSections(s: CanvasState): ReportSection[] {
  if (s.bufferedLeadSectionIds.size === 0) return s.sections;
  return s.sections.filter(
    (sec) => !s.bufferedLeadSectionIds.has(
      (sec as unknown as { id: string }).id,
    ),
  );
}

/** Sections that can materialize the Report surface.
 *
 * SQL, Python and raw tables are workspace auxiliaries. They remain in the
 * canvas store for Code/Data tabs and export/replay, but must not by themselves
 * open an empty Report pane. This is the frontend half of DF-04's delivery
 * boundary; the backend terminal verdict remains authoritative at `done`.
 */
export function selectReportSurfaceSections(s: CanvasState): ReportSection[] {
  return selectVisibleSections(s).filter(
    (section) =>
      section.kind !== "code_block"
      && section.kind !== "data_table",
  );
}

/**
 * WP3 (design D6): current streaming progress "Section N of M".
 * total = current turn outline skeleton count (sections from skeletonStartIdx);
 * done = populated entries (section_added replaces skeleton with _hasContent=true).
 * Outside outline/streaming, or no skeleton this turn → null (banner falls back to plain "生成中").
 */
/** report-streaming D3 — has current compose started (report header/KPI arrived)?
 *
 * **Monotonic latch**: never revert once compose emits header. Do not determine phase by whether
 * a step is running now — LLM thinking gaps between tools would make banner flicker
 * "分析中 ↔ 撰写中".
 *
 * Filtering by `turn_id` is mandatory: previous header remains in `sections` without superseded
 * marker until current compose arrives; without filtering, follow-up turn wrongly starts in writing phase.
 */
export function selectComposeStarted(s: CanvasState, turnId: string | null): boolean {
  if (!turnId) return false;
  return s.sections.some((sec) => {
    const x = sec as unknown as { turn_id?: string; kind?: string };
    if (x.turn_id !== turnId) return false;
    return x.kind === "eyebrow" || x.kind === "h1" || x.kind === "meta" || x.kind === "kpi_row";
  });
}

/** report-streaming D3 — classified counts of current turn's delivered sections.
 *
 * Old banner used `filtered.length` (all sections, including charts) for "已生成 N 个部分",
 * **reporting chart count as report-part count** during peer phase, before report existed
 * (production p50 62s, see docs/sprints/report-streaming/discovery.md §2.6).
 * Separate counts let each message describe truthfully.
 */
export function selectTurnSurfaceCounts(
  s: CanvasState,
  turnId: string | null,
): { figures: number; reportParts: number } {
  if (!turnId) return { figures: 0, reportParts: 0 };
  let figures = 0;
  let reportParts = 0;
  for (const sec of s.sections) {
    const x = sec as unknown as { turn_id?: string; kind?: string };
    if (x.turn_id !== turnId) continue;
    if (x.kind === "chart" || x.kind === "image") figures++;
    else if (x.kind === "narrative" || x.kind === "section" || x.kind === "kpi_row") reportParts++;
  }
  return { figures, reportParts };
}

/** Compact encoding separators for `selectTurnFigureKey`: ASCII record / unit separators,
 *  absent from chart titles. **Must use escape notation**; rationale in function comment below. */
export const FIG_ROW_SEP = "\u001e";
export const FIG_FIELD_SEP = "\u001f";

/** report-streaming D2 — output chips in waiting strip: current turn's delivered charts, with titles.
 *
 *  Return **compact string**, not array: caller uses `useSyncExternalStore`, comparing Object.is;
 *  a new array per call causes infinite rerenders (same reason `selectStreamProgress` returns primitive key).
 *  Caller parses key into array through `useMemo`.
 *  Row/field delimiters use **named constants + explicit escapes** (`FIG_ROW_SEP` / `FIG_FIELD_SEP`) —
 *  literal control characters are invisible in editor/diff, hiding swapped delimiters (occurred this batch:
 *  consumer swapped both, splitting one chart into two chips; caught only by tests).
 */
export function selectTurnFigureKey(s: CanvasState, turnId: string | null): string {
  if (!turnId) return "";
  const out: string[] = [];
  for (const sec of s.sections) {
    const x = sec as unknown as {
      turn_id?: string;
      kind?: string;
      id?: string;
      spec?: { title?: string };
      caption?: string;
    };
    if (x.turn_id !== turnId) continue;
    if (x.kind !== "chart" && x.kind !== "image") continue;
    const title = (x.spec?.title || x.caption || "").trim();
    out.push(`${x.id ?? ""}${FIG_FIELD_SEP}${title}`);
  }
  return out.join(FIG_ROW_SEP);
}

/** report-streaming B — comma-joined streaming section ids.
 *
 *  Return **string**, not Set: caller uses `useSyncExternalStore` with Object.is;
 *  new Set per call causes infinite rerenders (same as `selectStreamProgress` / `selectTurnFigureKey`). */
export function selectStreamingSectionIdKey(s: CanvasState): string {
  return s.streamingSectionIds.size === 0 ? "" : [...s.streamingSectionIds].sort().join(",");
}

export function selectStreamProgress(s: CanvasState): { done: number; total: number } | null {
  if (s.status !== "streaming" && s.status !== "outline") return null;
  // report-streaming D3: without current outline, no trustworthy denominator. `skeletonStartIdx`
  // defaults 0; writer-loop never emits lifecycle events → slice(0) includes all canvas sections
  // (including other turns) → old implementation said "第 3 / 3 节" while peers still queried data. Prefer no label.
  if (!s.outlineReady) return null;
  const startIdx = s.skeletonStartIdx;
  const turnSections = s.sections.slice(startIdx);
  const total = turnSections.length;
  if (total === 0) return null;
  const done = turnSections.filter((sec) => _hasContent(sec)).length;
  return { done, total };
}

/** chat-truth Phase Q (P1-Q1): predicate — did turn produce buffered Lead canvas content?
 * In turn_failed / turn_aborted, `responseKind` is unreliable (only `done` sets
 * it). These terminal paths choose partial canvas preservation (reveal) versus clearing —
 * branch on whether canvas has content, not responseKind. Any buffered id beginning
 * `sec_lead_${turnId}_` = canvas has content = reveal.
 */
export function hasBufferedCanvasContentForTurn(
  s: CanvasState,
  turnId: string,
): boolean {
  if (!turnId) return false;
  const prefix = `sec_lead_${turnId}_`;
  for (const sid of s.bufferedLeadSectionIds) {
    if (sid.startsWith(prefix)) return true;
  }
  return false;
}

// Sprint θ Phase 8d-2: canvas op-level failure pill state.
export function onCanvasOpFailed(p: {
  op: string;
  reasonCode: string;
  sectionId: string | null;
  at: number;
}): void {
  setState({ lastCanvasOpFailed: p });
}

// ─── PyCell reducers ──────────────────────────────────────
export function upsertPyCell(cell: PyCellSpec): void {
  setState((s) => {
    // R1 (W11-16 wiring): no explicit cell turnId → tag with currentTurnId fallback.
    // Prefer retaining old turnId on existing-cell upsert (normally turn_started precedes pycell.in;
    // race fallback in onTurnStreaming).
    const tagged: PyCellSpec = cell.turnId !== undefined
      ? cell
      : { ...cell, turnId: s.currentTurnId };
    // ξ C2: composite (n, subagent_id) dedup key — several peers emitting n=1 in same turn
    // made old n-only key let second peer overwrite first. Backend pycell.in/out
    // payloads now carry subagent_id (subagent.py:1228/1322), wired in canvas-sse.
    const sid = tagged.subagentId ?? null;
    const i = s.pyCells.findIndex(
      (c) => c.n === tagged.n && (c.subagentId ?? null) === sid,
    );
    if (i >= 0) {
      const next = s.pyCells.slice();
      next[i] = { ...tagged, turnId: s.pyCells[i].turnId ?? tagged.turnId };
      return { ...s, pyCells: next };
    }
    return { ...s, pyCells: [...s.pyCells, tagged] };
  });
}

export function clearPyCells(): void {
  setState((s) => ({ ...s, pyCells: [] }));
}

// ─── Initialization (fetch existing sections from GET /canvas) ───────────
// Session switches use this too. Reset all per-session state (sections/pyCells/
// errors/metaIntent/skeletonStartIdx), otherwise previous session's tool chips /
// py cells / error banners leak into new session. Function form **fully** replaces (not partial
// merge), with every field in initial, clearing all at once.
export function hydrateCanvas(payload: {
  canvas: { id: string; sessionId: string; title: string | null } | null;
  sections: ReportSection[];
  /** Viewer-safe source presentations. Defaults to an empty list. */
  sourceRefs?: import("./canvas-pm-adapter/citation-types").SourceRef[];
  /** A3: canvas detail seqAuthoritative (default false = forced rank-sorting fallback). */
  seqAuthoritative?: boolean;
  /** source-accessibility c2: detail turnCitations sidecar (default {} = keep all hidden as before). */
  turnCitations?: import("./canvas-pm-adapter/citation-types").TurnCitationsMap;
}): void {
  if (!payload.canvas) {
    setState(() => ({ ...initial }));
    return;
  }
  // P4.A: physical canvas without user-visible sections (e.g. D6 chat-only) still counts as
  // idle, omitting canvasId to avoid enabling report editing/export or occupying right pane. Retain hydrated
  // session identity so auto-export distinguishes pending request from confirmed absence of report content.
  if (payload.sections.length === 0) {
    setState(() => ({
      ...initial,
      sessionId: payload.canvas!.sessionId,
      title: payload.canvas!.title,
    }));
    return;
  }
  setState(() => ({
    ...initial,
    canvasId: payload.canvas!.id,
    sessionId: payload.canvas!.sessionId,
    title: payload.canvas!.title,
    status: "done",
    sections: payload.sections,
    sourceRefs: payload.sourceRefs ?? [],
    seqAuthoritative: payload.seqAuthoritative ?? false,
    turnCitations: payload.turnCitations ?? {},
  }));
}

/** W12-5: store-level refresh action — replaces old `bowhead:canvas-refresh-needed` window event.
 *
 * Call after mutation (delete/merge/split/...) to refetch from server + hydrate.
 * Without sessionId, silent no-op (canvas not ready).
 *
 * Note: lib/api import is one-way here (api.ts never imports canvas-store), so no cycle.
 */
export async function refreshCanvas(): Promise<void> {
  const sid = state.sessionId;
  if (!sid) return;
  try {
    const { fetchCanvasBySession } = await import("./api");
    const dto = await fetchCanvasBySession(sid);
    hydrateCanvas({
      canvas: dto.canvas
        ? { id: dto.canvas.id, sessionId: dto.canvas.sessionId, title: dto.canvas.title ?? null }
        : null,
      sections: dto.sections ?? [],
      sourceRefs: dto.sourceRefs,
      seqAuthoritative: dto.seqAuthoritative,
      turnCitations: dto.turnCitations,
    });
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn("[canvas-store.refreshCanvas] fetch failed", err);
  }
}

/** canvas-comprehensive Batch 1 (WP-2 / TD-5 / FR-1.1): terminal reconciliation.
 *
 * After terminal turn, use-turn-runner fetches `GET /canvas/by-session` and calls this, converging canvas
 * to **server authority**, building F5 self-healing into system behavior (INV-2). One reconciliation removes
 * user-visible consequences of any individual lost canvas.* event (missing emit / frame loss / hidden in buffer).
 *
 * Difference from hydrateCanvas (why not reuse): hydrateCanvas **fully replaces** (resets to initial,
 * clears pyCells / streaming / buffer). Reconciliation **changes only sections + canvas metadata**, retaining pyCells
 * and other Data/Code tab state, without damaging other tabs in same turn.
 *
 * server-wins: empty server sections (chat-mode hidden / B7 failure) also converge to empty — F5
 * behavior, defining INV-2; canvas_op_failed banner explains empty report (FR-1.6).
 *
 * Normalized projection drift count (AC-1.1c / RK-6): diff pre-reconciliation local vs post-reconciliation server projections;
 * >0 = local/server disagreement = hidden emit/frame-loss bug detector; should approach 0 when stable.
 */
export function reconcileCanvasWithServer(
  detail: {
    canvas: { id: string; sessionId: string; title: string | null } | null;
    sections: ReportSection[];
    sourceRefs?: import("./canvas-pm-adapter/citation-types").SourceRef[];
    /** A3 (report-edit-audit): server seq authority flag; defaults to current store value. */
    seqAuthoritative?: boolean;
    /** source-accessibility c2: defaults to current store value (omitted reconciliation payload never clears it). */
    turnCitations?: import("./canvas-pm-adapter/citation-types").TurnCitationsMap;
  },
  opts: { turnId: string },
): void {
  const serverSections = detail.sections ?? [];
  setState((s) => {
    const before = projectCanvasForDiff(s.sections);
    const after = projectCanvasForDiff(serverSections);
    const diff = diffProjections(before, after);
    __reconcileStats.count += 1;
    __reconcileStats.lastDiff = diff;
    if (diff > 0) {
      // eslint-disable-next-line no-console
      console.warn("[canvas-reconcile] drift", {
        turnId: opts.turnId, diff, localCount: before.length, serverCount: after.length,
      });
    }
    // Clear current turn's buffer residue (server returns visible content, no longer hide it).
    const prefix = `sec_lead_${opts.turnId}_`;
    const newBuffered = new Set(s.bufferedLeadSectionIds);
    for (const sid of Array.from(newBuffered)) {
      if (sid.startsWith(prefix)) newBuffered.delete(sid);
    }
    // Banner self-healing (FR-1.6): server sections exist → report actually delivered → clear canvas_op_failed
    // (turn-level banner never disappears automatically).
    const healBanner = serverSections.length > 0 && s.lastCanvasOpFailed != null;
    return {
      ...s,
      canvasId: detail.canvas?.id ?? (serverSections.length > 0 ? s.canvasId : null),
      sessionId: detail.canvas?.sessionId ?? s.sessionId,
      title: detail.canvas?.title ?? s.title,
      status: serverSections.length > 0 ? "done" : "idle",
      sections: serverSections,
      sourceRefs: detail.sourceRefs ?? s.sourceRefs,
      seqAuthoritative: detail.seqAuthoritative ?? s.seqAuthoritative,
      turnCitations: detail.turnCitations ?? s.turnCitations,
      bufferedLeadSectionIds: newBuffered,
      currentTurnId: null,
      streamingSectionIds: new Set(),
      lastCanvasOpFailed: healBanner ? null : s.lastCanvasOpFailed,
      // Deliberately leave pyCells untouched (Data/Code tabs).
    };
  });
}

function _hasContent(sec: ReportSection): boolean {
  // 0.39.7 #2 fix (canvas stream→F5 inconsistency): missing kind-specific
  // content fields here cause `onSectionAdded`'s skeleton-replacement loop
  // at line 181 to keep treating a just-filled section as "empty" → next
  // section_added with the same kind overwrites the previous one. Repro:
  // 3 consecutive insight_cards at seqs 98/99/100 only render the LAST.
  // Same class of bug hits chart/code_block/html_artifact/external_iframe/
  // trend_chart/sources/section (all previously unchecked).
  const anySec = sec as unknown as Record<string, unknown>;
  if ("text" in anySec && anySec.text) return true;
  if ("markdown" in anySec && anySec.markdown) return true;
  if ("cells" in anySec && Array.isArray(anySec.cells) && anySec.cells.length) return true;
  if ("rows" in anySec && Array.isArray(anySec.rows) && anySec.rows.length) return true;
  if ("summary" in anySec && anySec.summary) return true;
  if ("pills" in anySec && Array.isArray(anySec.pills) && anySec.pills.length) return true;
  if ("items" in anySec && Array.isArray(anySec.items) && anySec.items.length) return true;
  if ("cardUrl" in anySec && anySec.cardUrl) return true;
  if ("sql" in anySec && anySec.sql) return true;
  // 0.39.7 #2 — kind-specific fields not previously covered.
  if ("spec" in anySec && anySec.spec) return true;                    // chart
  if ("title" in anySec && anySec.title) return true;                  // insight_card / html_artifact / external_iframe
  if ("thesis" in anySec && anySec.thesis) return true;                // insight_card
  if ("narrative" in anySec && anySec.narrative) return true;          // insight_card (when no thesis)
  if ("evidence" in anySec && Array.isArray(anySec.evidence) && anySec.evidence.length) return true;  // insight_card
  if ("code" in anySec && anySec.code) return true;                    // code_block
  if ("points" in anySec && Array.isArray(anySec.points) && anySec.points.length) return true;  // trend_chart
  if ("entries" in anySec && Array.isArray(anySec.entries) && anySec.entries.length) return true;  // sources
  if ("body" in anySec && Array.isArray(anySec.body) && anySec.body.length) return true;  // nested section
  if ("artifact_id" in anySec && anySec.artifact_id) return true;      // html_artifact
  if ("background" in anySec && anySec.background) return true;        // design
  if ("layers" in anySec && Array.isArray(anySec.layers) && anySec.layers.length) return true;  // design
  if ("url" in anySec && anySec.url) return true;                      // external_iframe
  return false;
}

// origin/main (shared reports): section dedup helpers used by onSectionAdded.
function _sameSection(a: ReportSection, b: ReportSection): boolean {
  try {
    return JSON.stringify(_stableJsonValue(a)) === JSON.stringify(_stableJsonValue(b));
  } catch {
    return false;
  }
}

function _stableJsonValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(_stableJsonValue);
  if (!value || typeof value !== "object") return value;
  const obj = value as Record<string, unknown>;
  return Object.keys(obj)
    .sort()
    .reduce<Record<string, unknown>>((acc, key) => {
      acc[key] = _stableJsonValue(obj[key]);
      return acc;
    }, {});
}

// ─── E2E introspection hooks（canvas-comprehensive Batch 1 WP-0） ──────────
// Expose to window only with NEXT_PUBLIC_BOWHEAD_E2E==="1", letting Playwright obtain snapshots/projections/reconciliation counts from **actual browser
// rendering state**, avoiding circular claims that SSE-reconstructed server state is browser state and thus
// meaningless passing tests (review P0-1). Production build lacks env → zero exposure.
//
// __reconcileStats populated by reconcileCanvasWithServer (WP-2): count=reconciliation count,
// lastDiff=latest normalized projection diff (should approach 0 when stable; >0 = hidden emit/frame-loss bug, RK-6).
export const __reconcileStats = { count: 0, lastDiff: 0 };

if (typeof window !== "undefined" && process.env.NEXT_PUBLIC_BOWHEAD_E2E === "1") {
  const w = window as unknown as Record<string, unknown>;
  w.__CANVAS_SNAPSHOT__ = () => getCanvasSnapshot();
  // Projection is fully serializable plain array (no Set), safe across CDP; snapshot Set
  // fields serialize as {}, so tests use projection for exact comparison.
  w.__CANVAS_PROJECTION__ = () => projectCanvasForDiff(getCanvasSnapshot().sections);
  w.__CANVAS_RECONCILE_STATS__ = () => ({ ...__reconcileStats });
  // Workbench batch 4a e2e: frontend mock mode (gate profile) lacks SSE canvas events; inject report into store here,
  // using same F5 hydration entry (hydrateCanvas) to verify output-area divider, maximize shortcut and new-content dot.
  w.__CANVAS_TEST_HYDRATE__ = (payload: Parameters<typeof hydrateCanvas>[0]) => hydrateCanvas(payload);
  // Canvas batch 4b e2e: live writing (toolbar progress, active section) uses same store entries as SSE canvas events.
  w.__CANVAS_TEST_EVENTS__ = {
    opened: onCanvasOpened,
    outline: onOutlineReady,
    added: onSectionAdded,
    turnStreaming: onTurnStreaming,
    sectionStreaming: onSectionStreaming,
    chunk: onSectionChunk,
    sectionDone: onSectionDone,
    done: onCanvasDone,
  };
}
