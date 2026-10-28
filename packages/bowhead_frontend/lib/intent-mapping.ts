/**
 * Sprint θ Phase 8d: dispatch_role enum carried by the `done` event.
 */

/** Canonical 5-way dispatch_role enum (matches backend lead_orchestrator). */
export type DispatchRole =
  | "lead_self_answer"     // chat / concept self-answer (no peer)
  | "kb_analyst"           // KB query peer
  | "data_analyst"         // BI data peer (data-analyst / compute alias)
  | "clarification_pending"; // turn-terminating clarification
