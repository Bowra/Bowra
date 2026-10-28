/**
 * Subscribe to gateway SSE canvas.* events and dispatch to canvas-store.
 *
 * attach(es) installs 7 listener types and returns detach function.
 * Called by useTurnRunner and other callers after EventSource creation.
 */

import {
  onCanvasDone,
  onCanvasOpened,
  onCanvasOpFailed,
  onCanvasReordered,
  onOutlineReady,
  onSectionAdded,
  onSectionPatched,
  onSectionsHidden,
  onSectionsSuperseded,
  onTurnStreaming,
  onSectionStreaming,
  onSectionChunk,
  onSectionDone,
  upsertPyCell,
  getCanvasSnapshot,
} from "./canvas-store";
import { normalizeCanvasFailureReasonCode } from "./error-messages";
import type { PyCellSpec } from "./types";

type Parser = (data: string) => Record<string, unknown> | null;
const parseJson: Parser = (d) => {
  try {
    return JSON.parse(d) as Record<string, unknown>;
  } catch {
    return null;
  }
};

/** canvas-comprehensive Batch 1 (TD-2): monotonic per-attachStream cursor, held by caller
 *  (use-turn-runner attachStream) and reused across reconnects — creating it inside attachCanvasSSE
 *  resets it on each reattachment, failing exactly when reconnect deduplication is most needed. */
export interface CanvasSeqGate {
  maxEvtSeq: number;
}

export function attachCanvasSSE(es: EventSource, gate?: CanvasSeqGate): () => void {
  // Single deduplication gate: server replays all canvas events on reconnect; drop previously seen events (evt_seq <= max),
  // eliminating duplicate accumulation from unconditional section_chunk append (F2). No evt_seq (old persisted events
  // before shipment / synthetic events) → allow (fail-open), backed by section_done overwrite + terminal reconciliation.
  // Wrap addEventListener attachment once rather than modifying 17 handlers individually.
  const gated = (fn: (e: MessageEvent) => void) => (e: MessageEvent): void => {
    if (gate) {
      let evtSeq: number | undefined;
      try {
        const p = JSON.parse((e as MessageEvent).data) as Record<string, unknown>;
        evtSeq = typeof p.evt_seq === "number" ? p.evt_seq : undefined;
      } catch {
        evtSeq = undefined;
      }
      if (typeof evtSeq === "number") {
        if (evtSeq <= gate.maxEvtSeq) return; // Duplicate replay frame → discard
        gate.maxEvtSeq = evtSeq;
      }
    }
    fn(e);
  };
  const handlers: Array<[string, (e: MessageEvent) => void]> = [
    ["canvas.opened", (e) => {
      const p = parseJson(e.data);
      if (!p) return;
      onCanvasOpened({
        canvas_id: String(p.canvas_id),
        session_id: String(p.session_id),
        title: (p.title as string | null) ?? null,
        // canvas-update-turn B1-6: revision-turn supplementary frame carries title only, not a lifecycle signal
        revised: p.revised === true,
      });
    }],
    ["canvas.outline_ready", (e) => {
      const p = parseJson(e.data);
      if (!p) return;
      onOutlineReady({
        sections: (p.sections as { id: string; kind: string }[]) || [],
        meta_intent: p.meta_intent as string | undefined,
      });
    }],
    ["canvas.section_added", (e) => {
      const p = parseJson(e.data);
      if (!p) return;
      onSectionAdded({
        section: p.section as never,
        seq: p.seq as number | undefined,
      });
    }],
    ["canvas.section_patched", (e) => {
      const p = parseJson(e.data);
      if (!p) return;
      if (p.ok === false) {
        // Surface failed patch as op-level failure pill so the UI can render an
        // actionable hint (e.g. no_safe_fields_changed → "请在主聊天框新发问"),
        // instead of letting the LLM wrap it as opaque prose partial_answer.
        onCanvasOpFailed({
          op: "patch",
          reasonCode: normalizeCanvasFailureReasonCode(p.error),
          sectionId: typeof p.section_id === "string" ? p.section_id : null,
          at: Date.now(),
        });
        return;
      }
      onSectionPatched({
        section_id: String(p.section_id),
        patch: (p.patch as Record<string, unknown>) || {},
        rev: typeof p.rev === "number" ? p.rev : undefined,
      });
    }],
    // chat-solidify 2026-07-03: remove canvas.section_removed handler — backend has 0 emit
    // sites (section deletion uses REST + rehydration); dead name removed from EventType.
    ["canvas.reordered", (e) => {
      const p = parseJson(e.data);
      if (!p) return;
      if (p.ok === false) return;
      onCanvasReordered({ order: (p.order as string[]) || [] });
    }],
    // chat-solidify 2026-07-03: remove canvas.export_enqueued handler — backend has 0 emit
    // sites (export uses REST polling, not SSE); dead name removed from EventType.
    ["canvas.done", (e) => {
      const p = parseJson(e.data);
      if (!p) return;
      // prod-bug-bash-038 #7: forward `source` and `section_count` so canvas-store
      // can ignore fallback-emit when streaming already delivered sections (chart
      // would otherwise blank until F5 because fallback hardcoded section_count=0).
      onCanvasDone({
        errors: (p.errors as string[]) || [],
        source: typeof p.source === "string" ? p.source : undefined,
        section_count: typeof p.section_count === "number" ? p.section_count : undefined,
        // WP-3 (TD-3): turn_id lets onCanvasDone reveal buffered content per turn (one of several reveal paths).
        turn_id: typeof p.turn_id === "string" ? p.turn_id : undefined,
      });
    }],
    ["canvas.sections_hidden", (e) => {
      // D6 (canvas-empty bug 2026-05-25): backend marked turn's sections
      // hidden_for_chat_mode after finalize. Remove from canvas tab view;
      // chat bubble inline_artifacts wire still has them.
      const p = parseJson(e.data);
      if (!p) return;
      const ids = (p.hidden_section_ids as string[]) || [];
      if (ids.length > 0) {
        onSectionsHidden({ hidden_section_ids: ids });
      }
    }],
    ["canvas.sections_superseded", (e) => {
      // report-agent-authorship α v1 (2026-06-24): a later report-worthy turn
      // superseded prior report blocks (BOWHEAD_CANVAS_LATEST_WINS). Unlike
      // sections_hidden, these are MARKED (not removed) → the history accordion
      // renders them collapsed in place (recoverable, no vanish).
      const p = parseJson(e.data);
      if (!p) return;
      const ids = (p.superseded_section_ids as string[]) || [];
      const byTurn = typeof p.superseded_by_turn === "string" ? p.superseded_by_turn : null;
      // canvas-update-turn B1-2: `folded_by="revise"` distinguishes old charts replaced by revision from entire old reports.
      const foldedBy = typeof p.folded_by === "string" ? p.folded_by : null;
      if (ids.length > 0) {
        onSectionsSuperseded({ superseded_section_ids: ids, superseded_by_turn: byTurn, folded_by: foldedBy });
      }
    }],
    // P3.5 multi-segment streaming
    ["canvas.turn_streaming", (e) => {
      const p = parseJson(e.data);
      if (!p) return;
      onTurnStreaming({ turn_id: String(p.turn_id) });
    }],
    ["canvas.section_streaming", (e) => {
      const p = parseJson(e.data);
      if (!p) return;
      onSectionStreaming({
        section_id: String(p.section_id),
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        kind: p.kind as any,
        partial_payload: (p.partial_payload as Record<string, unknown>) ?? undefined,
      });
    }],
    ["canvas.section_chunk", (e) => {
      const p = parseJson(e.data);
      if (!p) return;
      onSectionChunk({
        section_id: String(p.section_id),
        delta: String(p.delta ?? ""),
      });
    }],
    ["canvas.section_done", (e) => {
      const p = parseJson(e.data);
      if (!p) return;
      onSectionDone({
        section_id: String(p.section_id),
        payload: (p.payload as Record<string, unknown>) ?? {},
        is_partial: Boolean(p.is_partial),
      });
    }],
    ["pycell.in", (e) => {
      const p = parseJson(e.data);
      if (!p) return;
      // R1: explicitly use currentTurnId as fallback turnId tag; canvas-store.upsertPyCell has
      // same fallback; duplicate it here to ensure correct turnId early in race.
      const currentTurnId = getCanvasSnapshot().currentTurnId;
      const cell: PyCellSpec = {
        n: Number(p.n),
        name: String(p.name || ""),
        status: "running",
        code: String(p.code || ""),
        output: null,
        durMs: null,
        turnId: currentTurnId,
        // ξ C2: backend emits subagent_id in pycell.in/out (subagent.py:1228/1322).
        // Wire to PyCellSpec.subagentId so dedup key (n, subagent_id) doesn't
        // collapse multiple peers' cells (e.g. compute + diagnostic_analyst both n=1).
        subagentId: typeof p.subagent_id === "string" ? p.subagent_id : null,
      };
      upsertPyCell(cell);
    }],
    ["pycell.out", (e) => {
      const p = parseJson(e.data);
      if (!p) return;
      const n = Number(p.n);
      const sid = typeof p.subagent_id === "string" ? p.subagent_id : null;
      // ξ C2: find by (n, subagent_id) composite — was n-only.
      const existing = getCanvasSnapshot().pyCells.find(
        (c) => c.n === n && (c.subagentId ?? null) === sid,
      );
      if (!existing) return;
      const dur = Number(p.dur_ms ?? 0);
      upsertPyCell({
        ...existing,
        status: (p.status === "error" ? "error" : "success") as PyCellSpec["status"],
        output: String(p.output ?? ""),
        durMs: dur,
        durLabel: `${dur}ms`,
      });
    }],
    // Sprint θ Phase 8d-2: canvas_op_failed lives in canvas-sse (not turn-runner)
    // because failure is canvas-scoped — turn keeps running per PRD §3.8 S8(c).
    // canvas-store records last-failed op so InsightCard / report editor render
    // an inline pill ("报告生成失败 · canvas_section_not_found") instead of
    // tearing down the whole turn.
    ["canvas_op_failed", (e) => {
      const p = parseJson(e.data);
      if (!p) return;
      onCanvasOpFailed({
        op: String(p.op ?? "unknown"),
        reasonCode: normalizeCanvasFailureReasonCode(p.reason_code),
        sectionId: p.section_id != null ? String(p.section_id) : null,
        at: Date.now(),
      });
    }],
  ];

  // Attach gated wrappers; detach removes same wrapped references.
  const wired: Array<[string, EventListener]> = handlers.map(
    ([name, fn]) => [name, gated(fn) as EventListener],
  );
  wired.forEach(([name, fn]) => es.addEventListener(name, fn));
  return () => {
    wired.forEach(([name, fn]) => es.removeEventListener(name, fn));
  };
}
