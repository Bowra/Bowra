// Local types for the step-list/* surface. These are placeholder shapes
// that mirror Claude Design v8 source data structures so leaf components
// can be authored standalone. Phase 1.2 wiring replaces these with the
// real `turn-store.ts` Step / TurnState types backed by SSE event payloads
// from `lib/sse-events.ts`.

import type { ErrorDiagnostics } from "@/lib/error-presentation";

/**
 * ⚠ New values **must be enumerated at every consumer**, not just added to this line.
 * Both `running-band-model.toneOf` and `AgentTrace.deriveRightSummary` use
 * enumerated known states with an else fallback; adding a type without updating them treats a new terminal state as
 * still running, leaving the spinner active (observed when adding `cancelled` on 2026-08-29).
 */
export type StepState = "pending" | "active" | "done" | "failed" | "partial" | "cancelled";

export type StepKind =
  | "parse"
  | "tool_run"
  | "lead_thinking_writing"
  | "subagent";

export type ToolName =
  | "parse_file"
  | "bi_query"
  | "web_search"
  | "kb_search"
  | "subagent_spawn"
  | "agent_message"
  | "lead_thinking_writing_python"
  | "lead_thinking_writing_sql"
  | "python_sandbox";

export type FailureKind = "transient" | "permanent";

export interface PlanItem {
  id: string;
  status: "todo" | "doing" | "done" | "skip";
  text: string;
}

/**
 * Result of one sub-step inside a batch (bi_query) or sequential nest
 * (subagent_spawn). Backend emits these inside step_done.sub_steps[] for
 * batch tools; subagent peers emit their own step_started/step_done that
 * the FE rolls up into the parent's nested view.
 *
 * Phase 1.2 shape mirrors Claude Design v8 scene.js substep shape so the
 * leaf renderer is grep-equal across design ↔ React.
 */
export interface SubStepResult {
  id: string;
  /** Display label — for bi_query this is the SQL summary; for subagent it's the inner tool's headline. */
  label: string;
  /** Short result line — e.g. "234 rows" / "5 / 5 成功" / "ok · 0.6s". */
  summary?: string;
  /** Status of this individual sub-step. */
  status?: StepState;
  /** Optional inline code preview (e.g. SQL text for a single bi_query lane). */
  code?: string;
  /** When the parent is a subagent, this is the inner tool's name. */
  tool?: ToolName;
}

/** Stable Results object emitted by a tool step (for example python_sandbox).
 * Bytes/content never ride the trace event; Results fetches them with the
 * current user's permission when this reference is opened. */
export interface StepOutputArtifact {
  artifactId: string;
  filename?: string;
  mime?: string;
  size?: number;
  dataRole?: "stdout" | "structured";
}

/** Bounded executable source captured on step_started for durable Code results.
 * It is deliberately separate from the human-readable/truncated args preview. */
export interface StepCodeInput {
  language: "python";
  code: string;
  truncated: boolean;
}

/**
 * Error envelope on a failed step. Mirrors the backend StepError emitted
 * on step_done with state=failed.
 *
 * `kind` preserves the backend's full failure taxonomy (so telemetry and
 * future routing can switch on the original cause).
 *
 * `uiKind` is the narrowed failure severity used by the trace renderer.
 * Coercion lives in lib/turn-store.inferFailureUiKind without overwriting
 * the original kind. Neither variant establishes a retry dispatch.
 */
export interface StepError {
  kind: FailureKind | "needs_user_input" | "unknown";
  uiKind?: FailureKind;
  message: string;
  retryable?: boolean;
  code?: string;
  diagnostics?: ErrorDiagnostics;
  /** Runtime name of the Skill a `skill_unavailable_<reason>` denial is about. */
  skill?: string;
}

/**
 * Detail blob for a step — content rendered inside the expanded body.
 * Variant by `kind` per the v8 scene.js StepDetails union.
 *
 * Phase 1.2 keeps this loose; turn-store will tighten once SSE event
 * payloads are typed.
 */
export type StepDetail =
  | { kind: "table"; cols: string[]; rows: (string | number)[][] }
  | { kind: "sql"; code: string }
  | { kind: "python"; code: string }
  | { kind: "stdout"; text: string }
  | {
      kind: "hits";
      query: string;
      hits: { title: string; source: string; page?: number }[];
    }
  | {
      kind: "chunks";
      query: string;
      chunks: { src: string; text: string }[];
    };

/**
 * One row in the step list. Field names mirror tech-design §3.3.1
 * turn-store Step type so the Phase 1.2 wiring is a 1:1 prop pass.
 * Phase 1.2 leaf components consume this shape directly.
 */
export interface StepView {
  stepId: string;
  parentStepId: string | null;
  concurrencyGroupId: string | null;
  stepType: StepKind;
  /** Tool name (per backend tool catalog) — present when stepType=tool_run / lead_thinking_writing / parse / subagent. */
  toolName: ToolName | null;
  /** Headline (Chinese) — backend supplies localized text. */
  headerZh: string;
  /** Free-text label (e.g. file name / SQL summary). */
  label?: string;
  state: StepState;
  /** Right-side summary line shown next to the meta col. */
  summary?: string;
  /** Localized duration string — "0.4s" / "1.2s". */
  durationText?: string;
  /** Expanded body content; renderers pick a sub-component by stepType. */
  detail?: StepDetail | null;
  /** Substeps — present for bi_query batch / subagent_spawn. */
  subSteps?: SubStepResult[] | null;
  /** Bounded stable refs for produced stdout/Markdown/files. */
  outputArtifacts?: StepOutputArtifact[] | null;
  /** Streaming code/sql tokens append into this as `step_progress` fires. */
  partialContent?: string;
  /** When state=failed. */
  error?: StepError | null;
  /** Subagent peer display name — e.g. "Compute Peer". */
  subagentName?: string;
  /** Indicates this step's args (code/sql) are still streaming. */
  isStreaming?: boolean;
  /** dogfood s_3e854 ④: the peer's id, set on the subagent_spawn step. The render
   *  threads live peer prose via peerProse[peerProseKey] so it shows ONCE on the
   *  spawn row (only the spawn step carries this → no per-step fan-out). */
  peerProseKey?: string;
  /** P4a (chat-redesign): the id of the agent that emitted this step. A peer's
   *  inner steps carry the peer's subagent id (resolvable via turn-store
   *  `peerNames[subagentId]`); the lead's steps carry the lead id / null. Drives
   *  agent-block grouping (see agent-grouping.ts). */
  subagentId?: string;
  /** chat-ux-2026-08: bus's monotonically increasing per-turn event sequence (see `turn-store.Step.evtSeq`).
   *  The running band uses it to order steps and peer terminal states in one history; without it, only array order is available. */
  evtSeq?: number;
}
