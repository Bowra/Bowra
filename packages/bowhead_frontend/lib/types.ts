/**
 * Shared types — align with Gateway SSE events (TD §2.4 / bowhead_core/events.py).
 */

import type { ErrorDiagnostics } from "./error-presentation";
import type { TransportSample } from "./transport-health";

export type IntentType =
  | "lookup"
  | "trend"
  | "comparison"
  | "causal"
  | "compute"
  | "report"
  | "modeling"
  | "ambiguous";

export type ChipState = "running" | "success" | "error" | "approval";

export interface ToolChipState {
  id: string;
  name: string;
  state: ChipState;
  dur: number | null;
}

/** @deprecated chat-polish-r3 Phase 1.5 — legacy trace_step accumulator
 *  for mock-mode scenarios + dual-emit window. Live SSE writes through
 *  lib/turn-store via step_started/step_done now. archived.traceSteps
 *  is no longer read by any production code path (Phase 1 redirected
 *  ArchivedTurnView to turn-store via getTurnSnapshot(turnId).steps).
 *  Kept as internal cache for backward compat with mock scenarios +
 *  legacy dual-emit handlers in use-turn-runner.ts until those handlers
 *  retire (Phase 1.5 follow-up). */
export interface TraceStepDetail {
  intent?: string;
  complexity?: number;
  [key: string]: unknown;
}

export type TraceStepKind = "running" | "success" | "error" | "pending";

export interface TraceStep {
  id: string;
  title: string;
  kind: TraceStepKind;
  dur?: string;
  detail?: TraceStepDetail;
  input?: string;
  output?: string;
  sql?: string;
  /** S layer (V2.8): first sentence of previous thinking, gray above title to explain why this tool is called. */
  thinkingHint?: string;
  /** M layer (V2.8): raw tool_name, for collapsing ≥3 consecutive calls to the same tool. */
  toolName?: string;
  /** L layer (V2.8): owning subagent_id (when present, render under corresponding spawn parent). */
  subagentId?: string;
  /** L layer (V2.8): step start timestamp (ms epoch), for elapsed-tick rendering. */
  startedAt?: number;
  /** L layer (V2.8): heuristic total ETA (ms); progress bars only for long steps. */
  etaMs?: number;
  /** V5 PRD-E3: raw result_preview JSON for ToolDetailDrawer.
   * input field (= arguments_preview) covers Request side; rawResult covers Response. */
  rawResult?: string;
  /** chat-trace-fix (2026-05-15) P0 root cause: backend `tool_result` event
   * carries `preview_payload` (kind=kpi/table/hits/stdout/error). Previously
   * only stored at turn-level (runner.inlinePreview, first tool only); per-tool
   * traceStep now keeps its own copy so synthesise-v3-events.ts can pass it
   * intact to v3-reducer.detectPreviewKind (was reverse-parsing rawResult that
   * never contained preview_payload — every bi_query KPI tile rendered "—"). */
  previewPayload?: ToolResultPreviewDTO | null;
  /** chat-style-fix Phase B3 — Phase A backend `tool_call.kind` (8-enum). */
  toolKind?: string;
  /** chat-style-fix Phase B3 — Phase A backend `tool_call.display_category`
   *  (3-enum: read/full/mutate). FE primary signal for tile rendering. */
  toolDisplayCategory?: "read" | "full" | "mutate";
}

/** V2.8 L1: trace plan preview — planned actions strip at turn top. */
export type PlanStepStatus = "pending" | "running" | "done" | "failed" | "skipped";

export interface PlanPreviewStep {
  id: string;
  verb: string;
  hint: string;
  etaMs: number;
  skillTag: string;
  status: PlanStepStatus;
  /** Actual completed duration (ms), populated for failed steps too. */
  actualMs?: number;
  /** Error for failed state (mono · neg on next line). */
  errMsg?: string;
}

export interface PlanPreview {
  steps: PlanPreviewStep[];
  estimatedDurationMs: number;
  /** Turn start timestamp (ms), for total elapsed in header. */
  startedAt: number;
  /** End timestamp; stop elapsed ticks after done/failed. */
  finishedAt?: number;
  status: "running" | "done" | "failed";
}

export type DataCardPayload =
  | { type: "metric"; kw: string; value: string; currency: string; approx: string; delta: string; deltaPos: boolean; sql?: string; meta?: Record<string, string> }
  | { type: "trend" }
  | { type: "attribution" }
  | { type: "datarc_iframe"; cardUrl: string; title?: string; height?: number };

// ─────────────────────────────────────────────────────────────
// v1.4 Canvas Mode — ReportSection schema
// Same shape as backend canvas_sections.payload JSON, shared across backend/frontend.
// See docs/canvas-mode-design.md §1.
// ─────────────────────────────────────────────────────────────

export type Tone = "pos" | "neg" | "neutral";

export interface KPICell {
  lbl: string;
  val: string;
  sub?: string;
  subTone?: Tone;
}

export interface TableCol {
  key: string;
  header: string;
  numeric?: boolean;
  format?: import("./chart/types").ChartColumnFormat;
  unit?: string;
  currency_code?: string;
  display?: import("./chart/types").NumericDisplay;
  percent_scale?: import("./chart/types").ChartPercentScale;
  kind?: "delta" | "bar";
}

export type TableCellValue =
  | string
  | number
  | { delta: string; pos: boolean }
  | { bar: number; pos: boolean };

export type TableRow = Record<string, TableCellValue> & { _total?: boolean };

export interface AttribSummary {
  label: string;
  value: string;
  pct: string;
  pctTone: Tone;
}

export interface AttribRow {
  rank: number;
  name: string;
  dim: string;
  from: string;
  to: string;
  sharePct: number;
  pct: string;
  pctTone: Tone;
}

export interface NextItem {
  title: string;
  detail: string;
  action?: "tool" | "skill" | "approval";
}

export type DesignTextAlign = "left" | "center" | "right";

export interface DesignTextLayer {
  id: string;
  type: "text";
  text: string;
  x: number;
  y: number;
  w: number;
  h: number;
  font_family?: string;
  font_size: number;
  font_weight?: number;
  color: string;
  align: DesignTextAlign;
  line_height?: number;
  letter_spacing?: number;
  rotation?: number;
  z?: number;
  locked?: boolean;
}

export interface DesignBackground {
  type: "artifact";
  artifact_id: string;
  fit?: "cover" | "contain" | "fill";
  opacity?: number;
}

/** S3 spec_block structured-graph nodes/edges. */
export interface SpecGraphNode {
  id: string;
  label: string;
  /** Visual emphasis: highlight (emphasis/conclusion) / muted (secondary) / default. */
  emphasis?: "default" | "highlight" | "muted";
}
export interface SpecGraphEdge {
  from: string;
  to: string;
  label?: string;
}

/** Discriminated union of 24 section kinds. */
export type ReportSection = {
  rev?: number;
  /** report-agent-authorship α v1 (2026-06-24): collapse-in-place. A prior report
   *  block superseded by a later report-worthy turn — the FE renders it inside the
   *  history accordion (collapsed, expandable) instead of in the live report. */
  superseded?: boolean;
  superseded_by_turn?: string;
  /** the turn that produced this section — used to GROUP superseded sections into
   *  per-turn "previous report" entries in the history accordion. */
  turn_id?: string;
} & (
  | { id: string; kind: "eyebrow"; text: string }
  | { id: string; kind: "h1"; text: string }
  | { id: string; kind: "deck"; markdown: string }
  | { id: string; kind: "meta"; pills: string[] }
  | { id: string; kind: "kpi_row"; cells: KPICell[] }
  | { id: string; kind: "section"; label: string; h2: string; body: ReportSection[] }
  | {
      id: string;
      kind: "narrative";
      markdown: string;
      /** Unsupported numbers in paragraphs (absent from tool_invocations results; possible hallucinations). */
      unverified_numbers?: string[];
      /** **Actual count** of unverified numbers (`unverified_numbers` is a truncated display list).
       *  Use this as trust-ratio denominator; list length falsely inflates perceived trust. */
      unverified_total?: number;
      /** P3.3: source IDs referenced by [num:V:U:src:S] / [ref:S] in narrative;
       *  resolve full SourceRef metadata through CanvasContext.sourceLookup. */
      source_refs?: string[];
    }
  | {
      id: string;
      kind: "data_table";
      caption: string;
      subCaption?: string;
      columns: TableCol[];
      rows: TableRow[];
      totalRow?: TableRow;
      /** Source metadata; sandbox-produced tables carry source="python_sandbox" + artifact_id
       * for frontend badge / download link. */
      meta?: {
        source?: string;            // "python_sandbox" | "bi" | …
        artifact_id?: string;
        filename?: string;
        mime?: string;
      };
    }
  | { id: string; kind: "callout"; label: string; markdown: string }
  | { id: string; kind: "next_list"; items: NextItem[] }
  | { id: string; kind: "datarc_iframe"; cardUrl: string; title?: string; height?: number }
  | {
      /** AI-generated, materialized static HTML rendered in a sandbox iframe.
       *  Active markup is removed before srcdoc; scripts and network stay disabled. */
      id: string;
      kind: "html_artifact";
      artifact_id: string;
      title: string;
      height?: number;
      sandbox?: boolean;
      /** Generation-input snapshot retained for provenance. It is not injected
       *  into the iframe; the artifact must already contain readable output. */
      data?: Record<string, unknown> | unknown[];
      /** Missing generation data / unchecked output → amber provenance warning. */
      unverified?: boolean;
      /** Citation indexes associated with the generation-input snapshot. */
      sources?: number[];
    }
  | {
      /** #125 Case A: external HTTPS URL iframe. Most public sites show blank due to X-Frame-Options;
       *  frontend offers "新标签打开" fallback. */
      id: string;
      kind: "external_iframe";
      url: string;
      title: string;
      height?: number;
      /** Set for files the Lead wrote to Google: render as a Google card, not a bare iframe. */
      provider?: "google_workspace";
      file_kind?: "document" | "spreadsheet" | "presentation";
      file_id?: string;
      open_url?: string;
      snapshot_at?: string;
      snapshot?:
        /** part "written": only what this edit wrote (edits of existing files never snapshot the whole file). */
        | { format: "markdown"; content: string; truncated: boolean; part?: "written" }
        | { format: "table"; rows: string[][]; range: string; truncated: boolean };
    }
  | {
      /** multimodal-v1: AI-generated image (image_generate → add_image).
       *  Display inline via GET /api/v1/files/:artifact_id?disposition=inline;
       *  canvas-writer may patch caption/alt; read-only for users (image itself immutable). */
      id: string;
      kind: "image";
      artifact_id: string;
      caption?: string;
      alt?: string;
    }
  | {
      /** multimodal-v2A: design with background image + editable text layers.
       *  Background is a non-deliverable artifact from image_generate(purpose=design_background);
       *  V2A permits text-layer edits only, without extracting/editing text inside the raster image. */
      id: string;
      kind: "design";
      title: string;
      size_preset: "square" | "portrait_3_4" | "portrait_9_16" | "banner_16_9" | string;
      canvas_width: number;
      canvas_height: number;
      background: DesignBackground;
      layers: DesignTextLayer[];
      flattened_artifact_id?: string | null;
      flattened_source_rev?: number | null;
      flattened_at?: string | null;
    }
  | {
      /** Native interactive chart (Chart.js). Replaces main datarc_iframe path;
       *  full spec contract: lib/chart/types.ts. */
      id: string;
      kind: "chart";
      spec: import("./chart/types").ChartSpec;
    }
  /** Fallback target for unrecognized sections (canvas-to-pm.ts creates it; backend never emits it).
   *  original_kind stores original type; text_content preserves readable content where possible, so emergency rollbacks
   *  do not lose user content merely due to older frontend. See sections/unknown-section.tsx. */
  | { id: string; kind: "unknown"; original_kind: string; text_content?: string }
  /** S3: structured graph embedding (attribution chains / metric trees / flowcharts), using nodes/edges instead of heavy DSL
   *  (mermaid too heavy at 75MB; dagre layout ~45KB + custom SVG, on-brand + safe). format→
   *  renderer uses spec-block.tsx registry; rendering failure gracefully falls back to JSON source. */
  | {
      id: string;
      kind: "spec_block";
      /** Rendering format: currently "graph" (directed graph; trees are a special case). */
      format: string;
      title?: string;
      /** Layout direction: TB top-to-bottom / LR left-to-right, used by graph. */
      direction?: "TB" | "LR";
      nodes: SpecGraphNode[];
      edges: SpecGraphEdge[];
    }
  | {
      /** Sprint ι (PRD §4.A): unified code-block section. python_sandbox + bi.ask_question +
       *  bi.attribute synthetic-SQL fallback use this kind. sql_block shares component
       *  for dual-kind compatibility (sql-block.tsx later renamed code-block.tsx). */
      id: string;
      kind: "code_block";
      call_id: string;
      /** Sprint ι r2 P1 #3: cross-turn safety origin (PRD §10.1). */
      turn_id?: string;
      lang: "sql" | "python" | "shell";
      code: string;
      dur_ms?: number;
      stdout_preview?: string;
      input_artifact_ids?: string[];
      output_artifact_ids?: string[];
      /** Viewer projection only: report-scoped aliases of the output artifacts, equal to the
       *  `source_alias` of the matching sources entry. Links a numbered "Run Code" citation to this run.
       *  Absent on old payloads and outside the viewer projection. */
      output_aliases?: string[];
      /** When the run started (ISO 8601). Absent on code blocks written before it was recorded. */
      ran_at?: string;
      input_artifacts?: { artifact_id: string; filename?: string; mime?: string }[];
      output_artifacts?: { artifact_id: string; filename?: string; mime?: string }[];
      summary_payload?: {
        n_lines: number;
        statement_summary: string;
        dur_ms: number;
      };
      /** Legacy alias for sql-block.tsx renderer compat (90d grace). */
      meta?: Record<string, string>;
      sql?: string;
    }
  | {
      id: string;
      kind: "sources";
      label: string;
      entries: {
        id?: string;
        source_alias?: string;
        /** "descriptive" (1.0.95 P3) = identity-less descriptive source, with no object to authorize;
         *  safe_label/safe_query are author text, shown as plain rows outside access registry. */
        /** Batch 2 six states (tech-design §4.1); linked_unavailable is a legacy-path value,
         *  removed after batch 3 deletes old branches. */
        access_state?:
          | "linked_accessible"
          | "linked_labeled"
          | "linked_restricted"
          | "linked_missing"
          | "unsupported"
          | "linked_unavailable"
          | "descriptive";
        generic_type?: "query_result" | "document" | "web_source" | "generated_artifact" | "other_source";
        ordinal?: number;
        safe_label?: string;
        safe_query?: string;
        safe_preview?: string;
        safe_preview_url?: string;
        safe_url?: string;
        safe_preview_columns?: string[];
        safe_preview_rows?: Record<string, string>[];
        safe_total_rows?: number;
        /** KB answer citations batch 3: only linked_accessible KB sources carry this (projected by viewer read permission). */
        kb_file_id?: string;
        kb_chunk_id?: string;
        kb_scope?: string;
        kb_space_id?: string;
        // payload-path: subagent-reported sources, usually {label, artifact_id}.
        artifact_id?: string;
        // compose-path: NL2SQL tokenized sources, usually {tool, query, tokens}.
        tool?: string;
        query?: string;
        tokens?: { word: string; prop: string }[];
        // Generic label, available to both paths.
        label?: string;
        // citation-hover original text: bi_query sample_rows folded by the backend so the
        // citation popover renders original rows inline (the tl_ artifact_id 404s on
        // /files). Read by sourceRefFromNumberedEntry; ignored by SourcesSection.
        preview_columns?: string[];
        preview_rows?: Record<string, string>[];
        total_rows?: number;
      }[];
    }
  // M137 #142 M1: subagent.return → AnalysisPayload → composer output.
  // M3 (#144) will refine rendering; M1 stub already prevents blank output.
  | {
      id: string;
      kind: "insight_card";
      title: string;
      thesis?: string;
      evidence?: Array<{
        kind: "chart" | "computation" | "table" | "callout";
        caption?: string;
        subcaption?: string;
        display_hint?: "full_width" | "half" | "third" | "collapsed";
        chart_section_id?: string;
        computation_artifact_id?: string;
        table_artifact_id?: string;
        callout_text?: string;
      }>;
      narrative?: string;
      impact?: {
        quantity?: number;
        unit?: string;
        direction?: "+" | "-" | "=";
        timeframe?: string;
        note?: string;
      };
      confidence?: "high" | "med" | "low";
      source_refs?: string[];
      /** Unsupported numbers in card text (composer scans thesis + narrative +
       *  evidence[].callout_text). Same field name, counting criterion
       *  (report-trust) and render block (UnverifiedNumbersNote) as narrative. */
      unverified_numbers?: string[];
      /** **Actual count** of unverified numbers (`unverified_numbers` is a truncated display list).
       *  Use this as trust-ratio denominator; list length falsely inflates perceived trust. */
      unverified_total?: number;
    }
  | {
      id: string;
      kind: "recommendations";
      items: Array<{
        priority: "P0" | "P1" | "P2";
        action: string;
        owner?: string;
        timeline?: string;
        expected_impact?: string;
        rationale?: string;
      }>;
      /** P3.3: source IDs referenced by [ref:S] in recommendations;
       *  resolve full SourceRef metadata through CanvasContext.sourceLookup. */
      source_refs?: string[];
    }
);

// ─────────────────────────────────────────────────────────────
// v1.4 — PyCell (Jupyter-style agent execution)
// ─────────────────────────────────────────────────────────────

export interface PyCellSpec {
  n: number;
  name: string;
  status: "running" | "success" | "error";
  code: string;
  output: string | null;
  durMs: number | null;
  durLabel?: string;
  /** W11-16: owning turn (canvas-store tags automatically via currentTurnId), for multi-turn folding. */
  turnId?: string | null;
  /** ξ C2: peer subagent_id (backend pycell.in/out already emitted @ subagent.py:1228/1322).
   *  Dedup key: (n, subagent_id) composite, so peers emitting n=1 in the same turn do not overwrite each other. */
  subagentId?: string | null;
}


export interface TurnSummary {
  total: string;
  cost: string;
  llm: number;
  tools: number;
  tokensInput?: number;   // UX-8: backend done event already emits tokens_input/output.
  tokensOutput?: number;
  usagePartial?: boolean;
}

export interface AttachmentRef {
  fileId: string;
  filename: string;
  size: number;
  mime: string;
  /** Results Workspace M2-D: a stable result referenced from the current
   * session. Missing means the existing upload contract. */
  referenceKind?: "upload" | "result";
  selectedContext?: import("./chart/selected-context").ChartSelectedContext;
  /** Sprint file-upload-presign Phase 3 — for zip bundles uploaded via the
   * KB presign path, this carries the child artifact_ids so the chat
   * orchestrator sees per-entry attachments (instead of the opaque zip
   * artifact which has no useful content). When present, the chat send
   * pipeline expands these into the attachments[] list it sends to the
   * backend. When undefined → ordinary single-file attachment. */
  bundleArtifactIds?: string[];
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  /** Related turn; assistant-message turnId also attached here for navigation / trace. */
  turnId?: string | null;
  /** User-message attachments (meaningful for user role only). */
  attachments?: AttachmentRef[];
  /** W2-1: ISO 8601 timestamp, populated after backend TurnDetail.created_at ships.
   *  If absent, turn-stream day separator falls back to "今日". */
  createdAt?: string | null;
  /** W2-3: assistant-message owner (Lead/Analyst/Modeler/Thinker).
   *  Populated when backend trace.spawn_subagent is wired; defaults to "Lead". */
  agentType?: "Lead" | "Analyst" | "Modeler" | "Thinker" | string | null;
}

/** W3-3: streaming pipeline state; idle = no turn / active = normal SSE / paused = intermediate stall / error = disconnected stream. */
export type StreamingState = "idle" | "active" | "paused" | "error";

export interface AgentProgressItem {
  subagentId: string;
  profile: string;
  displayName: string;
  step: number;
}

export interface AgentProgress {
  agents: AgentProgressItem[];
}

export interface TurnRunnerState {
  user: string | null;
  intent: IntentType | null;
  complexity: number | null;
  chips: ToolChipState[];
  answer: string;
  /** Chat turn feedback D3: `answer` came from a legacy system failure fallback
   *  (`final_answer.reason_code` ∈ public failure reasons ∪ `silent_done_fallback`). It is
   *  cleared when the turn's failure terminal arrives; the failure card states the failure. */
  answerIsLegacyFailureFallback?: boolean;
  /** W3-3: SSE stream state (UI indicators for stalled/error). */
  streamingState?: StreamingState;
  /** Agent run limits, phase 3: transient `turn.progress` snapshot (agents running and their
   *  current step). Never persisted or replayed; absent when another process runs the turn. */
  agentProgress?: AgentProgress | null;
  /** W2-3: owning agent of current turn (Lead/Analyst/Modeler/...). */
  agentType?: ChatMessage["agentType"];
  /** Sprint ν2: response_kind from done event hydrate (chat | canvas) */
  responseKind?: "chat" | "canvas";
  artifact: DataCardPayload | null;
  source: { text: string; url: string } | null;
  summary: TurnSummary | null;
  /** @deprecated chat-polish-r3 Phase 1.5 — mock-mode + legacy dual-emit
   *  cache. ArchivedTurnView / BeatStream read from turn-store via
   *  getTurnSnapshot(turnId).steps instead. Empty in live SSE since
   *  backend stopped emitting trace_step in chat-redesign Phase 1.1. */
  traceSteps: TraceStep[];
  turnId: string | null;
  /** Server-issued execution generation used to fence steer/cancel races. */
  executionGeneration?: number | null;
  /** Persisted Terminal Protocol discriminator. Null means legacy. */
  terminalProtocolVersion?: number | null;
  sessionId: string | null;
  sessionTitle: string | null;
  running: boolean;
  /** Durable terminal status. Success UI must require `completed`; absence is
   *  unknown, never proof of success. */
  terminalStatus?: string | null;
  effectDisclosure?: import("./api").EffectDisclosureOverlayDTO | null;
  /** Keep the first authoritative disclosure read alive across terminal archive. */
  effectDisclosureDiscoveryPending?: boolean;
  /** Persist a monotonic disclosure poll result before live→archived remount. */
  updateEffectDisclosure?: (
    turnId: string,
    executionGeneration: number,
    overlay: import("./api").EffectDisclosureOverlayDTO | null,
  ) => void;
  started: boolean;
  /** Canvas-only hydrate for grant-readable shared reports without owner session detail. */
  reportOnly?: boolean;
  /** Archived conversation history (all user/assistant pairs before current turn). */
  messages: ChatMessage[];
  /** User-message attachments of currently running turn (chips below user bubble). */
  userAttachments: AttachmentRef[];
  /** Abbreviated reasoning summary from thinking models (Doubao pro / Claude extended thinking). */
  thinking: string;
  /** Original total thinking length (UI omitted-character count). */
  thinkingLength: number;
  /** S layer (V2.8): thinking already attached as a trace-step hint, for deduplication
   * (when Lead thinks once then calls several tools, attach hint to first step only, without repeating later). */
  lastThinkingHintAttachedAt: string;
  /** M156-M3b: HITL clarification in current turn (verdict=clarify), retained until user selects a candidate. */
  clarifications: ClarificationPayload[];
  /** cca P2 (C3): set by the cca.tool_create_open SSE event → opens the inline create-tool card.
   *  Persists across turns (a fill-out flow); cleared when the card is completed/dismissed. */
  createToolOpen: CreateToolOpenPayload | null;
  /** cca P3 (S5): set by the cca.subagent_creator_open SSE event → opens the inline
   *  create-subagent card. Persists across turns; cleared when completed/dismissed. */
  subagentCreatorOpen: CreateSubagentOpenPayload | null;
  configurationCanvasOpen?: { turnId: string; target: import("./settings-canvas-target").SettingsCanvasTarget } | null;
  settingsProposalIds?: string[];
  /** workflow-solidify S2: set by the cca.workflow_solidify_open SSE event → opens the inline
   *  solidification card. Persists across turns; cleared when completed/dismissed. */
  solidifyOpen: SolidifyOpenPayload | null;
  /** V2.8 L1: current-turn plan preview (heuristic ≤5 steps; mark ☑ when actual running steps pass them). */
  plan: PlanPreview | null;
  /** M1.4: whether user stopped current turn. Backend M2.1 persists and emits turn.cancelled; M1
   * uses client-only close SSE + set running=false + cancelled=true. */
  cancelled?: boolean;
  /** V6 Phase A B3: interrupted streaming (SSE error / network drop), rather than normal done.
   *  DoneCard uses this flag to show "DONE_PARTIAL · 流式中断" instead of "DONE" and omit streaming-error banner
   *  (avoid production state-machine conflict showing both banner and DONE card). */
  interrupted?: boolean;
  /** V5 PRD-E2: first successful tool's inline preview in current turn (inside answer stream). null = not triggered. */
  inlinePreview?: ToolResultPreviewDTO | null;
  /** Sprint θ Phase 8d: turn-terminating clarification (different from V2.7 HITL — see
   *  use-turn-runner.ts clarification.required handler discriminator). */
  clarificationPending?: ThetaClarificationState | null;
  /** Sprint θ S2: user-initiated cancel completed; turn-stream renders aborted card. */
  aborted?: boolean;
  /** Turn/canvas error reason for inline error UI. Peer failures live in the
   *  canonical peer terminal map and do not terminate the Lead turn. */
  errorReason?: SseErrorReason | null;
  /** A failure before the Gateway creates a turn (for example credit/model
   *  preflight). Kept separate from errorReason so the composer does not offer
   *  a meaningless replay action for a request that was never accepted. */
  sendErrorReason?: string | null;
  /** Sprint θ: dispatch_role from done event (replaces legacy meta_intent for chip rendering). */
  dispatchRole?: string | null;
  /** Honest planning fall-through: user **explicitly** selected planning, but this turn produced no plan card
   *  (empty graph / failed reify / agent outside roster). Backend sends reason codes only; copy belongs to i18n.
   *  Absent key = not triggered (Auto/off and normal-card turn shapes unchanged). */
  planningFallback?: string;
  /** Sprint θ: callable_peer dispatch progress (peer_dispatched events accumulate; cleared on done). */
  peersDispatched?: PeerDispatchInfo[];
  /** Sprint canvas-pipeline-fix B4: full state snapshot for archived (prior) turns
   *  in this session. Each entry contains everything V3TurnStream needs to render
   *  the historical turn's chat panel content (Lead prose + AgentSection timeline +
   *  canvas handoff + final answer + errorReason). New turn at line 377+ snapshots
   *  current state into archivedTurns before reset. V3TurnStream renders these
   *  before current turn. Replaces the limited `messages` (text-only) for V3 path. */
  archivedTurns?: ArchivedTurn[];
  /** chat-style-fix Phase B1 — Lead turn-top introduction (≤30 characters).
   *  Set by SSE `headline` event handler in use-turn-runner. undefined until
   *  event arrives — V3TurnStream renders `<Headline>` only when defined. */
  headline?: string;
  /** chat-redesign Phase 1.2 — staging slots populated by use-turn-runner
   *  SSE handlers; carried into ArchivedTurn at archive time. Phase 1.4
   *  refactor will fold these into turn-store directly, retiring the
   *  runner-state mirror entirely. Stored as `unknown` to keep the base
   *  state type free of downstream dependencies. */
  subagentSummaries?: Record<string, unknown>;
  finalPayload?: unknown;
  /** chat-redesign Phase 1.2 — dispatch signal set on first observation
   *  of any tool/agent dispatch this turn. Used by hydrate to mark
   *  archived turns as "had activity" so the post-load surface doesn't
   *  show the empty state for a turn that lacked a final payload.
   *  Cleared in INITIAL_STATE and on hydrate-fresh paths. */
  hasDispatched?: boolean;
  /** Number of tools Lead invoked this turn (from done event payload).
   *  Used by hydrate to discriminate prose-only turns from data turns. */
  toolInvocationCount?: number;
  /** Terminal done report_sections_saved (report sections persisted this turn, see lib/report-sections-saved.ts); null when unknown. */
  reportSectionsSaved?: number | null;
  /** 0.26.3: re-attach EventSource to a turn's SSE stream. TurnContainer
   *  HITL picker calls this after /resume_turn so the FE consumes the
   *  resumed Lead pass without page reload. Closes the prior EventSource
   *  + clears watchdog before opening the new connection. Set only by
   *  useSSERunner (mock runner leaves it undefined). */
  attachStream?: (
    streamUrl: string,
    executionGeneration?: number,
    terminalProtocolVersion?: number,
  ) => void;
  /** dogfood-0708 F3 — clear the live clarificationPending (collapse the card)
   *  without reconnecting a stream. Used by "换个问法" dismissal, which — unlike
   *  resolve — has no resume/attachStream to clear pending as a side effect.
   *  Set only by useSSERunner (mock runner leaves it undefined). */
  clearClarificationPending?: (reqId?: string | null) => void;
  /** stream-liveness ① — measurement samples for this turn's SSE transport (lib/transport-health.ts).
   *  **Raw samples only, no verdicts**: consumers call msSinceLastEvent (current-row copy,
   *  no attribution and thus no false diagnosis) or verdictOf (troubleshooting/attribution, with all guards there).
   *  ⚠ A ref, not state: every inbound event changes it, so state would rerender the entire turn subtree
   *  each frame; readers already have their own per-second tickers.
   *  Set only by useSSERunner (mock runner leaves it undefined). */
  transportRef?: { current: TransportSample | null };
  /** chat-redesign Phase 1.7-A — stable msg-id for the current turn's user
   *  bubble (`bub-msg-${ulid}` shape). Allocated at submit time in
   *  lib/use-turn-runner.ts; threaded through UserBubble's `data-msg-id`
   *  so emoji_ack lookup can find the bubble target. null until the user
   *  sends a message; copied into ArchivedTurn.userMsgId at archive time. */
  userMsgId?: string | null;
  /** chat-redesign Phase 1.7-A — submit timestamp for the current turn
   *  (Date.now() at runner.setState time when `s.user = text`). Used by
   *  BotByline (1.7-B) to render the turn-start time stamp during
   *  streaming. null until the user sends a message. */
  startedAt?: number | null;
  /* O5 (2026-07-03): lastFailedTurnId removed; only supplied sticky targets for fake retry/skip buttons,
   * with no consumers after removing those buttons. */
  /** prod-bug-bash-039 Phase 5 (0.39.3) — turn-scoped citation registry,
   *  populated by `turn.citation_registered` SSE events. ArtifactChip looks
   *  up `citations[seq-1]` to resolve `[cite:N]` markers to actual artifact
   *  ids for scroll-to-section. Empty in mock mode + on fresh turn start. */
  citations?: Citation[];
}

/** prod-bug-bash-039 #6 — one entry in the turn-scoped citation registry.
 *  Mirrors `bowhead_core.citations.Citation` dataclass byte-for-byte (per
 *  [[feedback_ut_must_assert_emit_contract]]). Field order matters because
 *  the SSE payload is `json.dumps(asdict(citation))`. */
export interface Citation {
  /** 1-based seq — LLM/BE emits `[cite:N]` where N == seq. */
  seq: number;
  /** The actual artifact id this citation resolves to (e.g. `sec_abc` for
   *  a chart section or `art_xxx` for a computation/table artifact). */
  artifactId: string;
  /** Human-friendly hint (insight title); FE chip tooltip. May be null. */
  label: string | null;
  /** Citation kind — drives styling differentiation (chart vs table vs KB
   *  chunk vs external web clip). web_clip (Sprint pptx-from-template M1) is an
   *  external, UNVERIFIED source → distinct chip styling. */
  kind: "data_ref" | "chart_section" | "tool_output" | "kb_chunk" | "kb_row" | "web_clip";
  /** source-accessibility batches 2/4: extra viewer-projection fields (outside dataclass mirror);
   *  event six-state contract; safe_anchor_id only for OWNED (canvas DOM sec_* anchors).
   *  Old archived snapshots lack both; optional, defaulting to legacy behavior. */
  accessState?:
    | "linked_accessible"
    | "linked_labeled"
    | "linked_restricted"
    | "linked_missing"
    | "unsupported";
  safeAnchorId?: string;
  /** KB answer citations: supplied only for viewable kb_chunk (file, chunk, scope, space). */
  kb?: CitationKbRef;
}

export interface CitationKbRef {
  fileId: string;
  chunkId: string;
  generationId?: string;
  sourceCheckState?: string;
  scope?: string;
  spaceId?: string;
}

/** Sprint canvas-pipeline-fix B4: full historical turn snapshot. */
export interface ArchivedTurn {
  turnId: string | null;
  /** Durable terminal status copied from the event or session detail. */
  terminalStatus?: string | null;
  /** Exact execution generation owning the effect disclosure below. */
  executionGeneration?: number | null;
  terminalProtocolVersion?: number | null;
  effectDisclosure?: import("./api").EffectDisclosureOverlayDTO | null;
  /** The live Turn ended before its first generation-fenced disclosure read. */
  effectDisclosureDiscoveryPending?: boolean;
  user: string;
  userAttachments: AttachmentRef[];
  answer: string;
  /** 0708 ⑩: this turn was killed by a gateway restart (status=failed +
   *  error_kind=interrupted). Drives a lightweight retry affordance on the
   *  archived turn (its steps are already failed via failActiveSteps). */
  interrupted?: boolean;
  /** @deprecated chat-polish-r3 Phase 1.5 — empty in current archive
   *  snapshots; ArchivedTurnView reads turn-store via archived.turnId
   *  instead. Kept on type for backward compat with mock scenarios. */
  traceSteps: TraceStep[];
  peersDispatched: PeerDispatchInfo[];
  errorReason: SseErrorReason | null;
  responseKind?: "chat" | "canvas";
  /** See TurnRunnerState.planningFallback: retain this notice after archiving, otherwise
   *  historical turns silently disown it, making live and archived output inconsistent. */
  planningFallback?: string;
  toolFindings?: Array<{
    callId: string;
    finding: string;
    parentAgentId?: string | null;
    seq?: number;
  }>;
  /** chat-style-fix Phase B1 — headline preserved across archive so replay
   *  renders the same turn introduction above the historical agsec timeline. */
  headline?: string;
  /** chat-redesign Phase 1.2 — final payload + subagent summaries carried
   *  into archive so historical hydrate reconstructs the answer surface.
   *  Phase 1.4 will fold these into turn-store-backed archive state. */
  subagentSummaries?: Record<string, unknown>;
  finalPayload?: unknown;
  /** chat-redesign Phase 1.2 — dispatch signal carried into archive so
   *  hydrate distinguishes "had activity" from "empty turn". */
  hasDispatched?: boolean;
  /** chat-redesign Phase 1.7-A — stable msg-id of the user bubble for this
   *  turn (`bub-msg-${ulid}` shape). Threaded from runner.userMsgId at
   *  archive time so steer 🤝 emoji_ack lookup can find the bubble across
   *  archived turns. May be null for pre-1.7 archived turns. */
  userMsgId?: string | null;
  /** chat-redesign Phase 1.7-A/B — turn-store beats[] snapshot at archive time.
   *  ArchivedTurnView iterates these to render the same composition as live.
   *  Captured via `getTurnSnapshot(turnId)?.beats ?? []` in
   *  lib/use-turn-runner.ts snapshotArchivedTurn. Type-only import of
   *  `TurnBeat` from `./turn-store` keeps this module type-cycle free
   *  (turn-store imports value `ThetaClarificationState` from here; the
   *  reverse direction is type-only so the runtime graph stays acyclic). */
  beats?: import("./turn-store").TurnBeat[];
  /** prod-bug-bash-039 Phase 5 (0.39.3) — citation registry snapshot. Copied
   *  from runner.citations at archive time. Empty array for legacy archived
   *  turns; ArtifactChip falls through to chip-as-no-op (toast) for those. */
  citations?: Citation[];
  archivedAt: number;
  /** User submitted this turn at (ms). User bubble shows this, **not** archivedAt (completion time).
   *  Live archival uses first turn_started (fallback: send time); hydration uses turns.started_at.
   *  See lib/turn-user-time.ts. Old snapshots default to archivedAt. */
  userSentAt?: number | null;
}

/** Sprint θ turn-terminating clarification (set on clarification.required with clarification_id). */
export interface ThetaClarificationQuestion {
  question: string;
  options: string[];
  /** rank_priorities removed from tool schema on 2026-09-07 (frontend never had sorting UI;
   *  checkbox groups lose order). Backend normalization maps to multi_select; retain | string fallback
   *  so persisted-event replay does not crash. */
  type?: "single_select" | "multi_select" | "free_text" | string;
  /** 2026-09-01: option **display names** (one-to-one and equal length with options). In English turns,
   *  options contains Datarc canonical Chinese names (一级品牌名称英文), giving non-Chinese readers
   *  a row of unreadable Chinese buttons; add locale-specific names alongside them.
   *  ⚠ Render these, **submit options[i] unchanged**: model needs that string to build bi_query,
   *  and Datarc recognizes only canonical Chinese names. Absent = no alternative, so frontend falls back to options. */
  option_labels?: string[];
}

export type ClarificationAnswer =
  | {
      question: string;
      source: "candidate" | "custom";
      answer: string;
      option_index?: number;
    }
  | {
      question: string;
      source: "skip";
      skipped: true;
    };

export interface ThetaClarificationState {
  candidates: string[];
  fallbackText?: string | null;
  clarificationId: string;
  questions?: ThetaClarificationQuestion[];
  /** Backend decides whether this card supports multi-select (CH2 merges N≥2 / any multi_select question).
   *  Wired on 2026-09-07: backend previously computed/sent this, but frontend **never read it**, so paths sending
   *  candidates without questions became single-select. **Fallback, not replacement**:
   *  true ⇒ force multi-select; otherwise use questions[0].type. Absent (e.g. heuristic
   *  payload without this key) ⇒ false ⇒ existing behavior byte-identical. */
  multiSelect?: boolean;
  /** Batch 2 (workspace-space-split PRD §3.3b): dedicated kind (e.g. tool_context_select=
   *  Datarc workspace selection). Absent = generic options card, unchanged behavior. */
  kind?: string;
  /** Batch 2: structured (value,label) options; value=Datarc workspace ID sent to resolve, label displayed.
   *  Backend supplies only for kind=tool_context_select; generic cards use undefined. */
  options?: { value: string; label: string }[];
  /** Batch 2: render "记住选择" checkbox (explicit vs session soft-preference states). */
  rememberOption?: boolean;
  /** connector-context T1#6: connector discriminator (e.g. "datarc"). Informational passthrough; rendering
   *  still branches by kind, resolve omits it (server store value authoritative). */
  tool?: string;
  /** `document_draft`: the question pauses a document task (Chat Cards Spec §3). The card then reads
   *  "More information needed · Document draft" and continues with "Continue generating". */
  purpose?: "document_draft";
}

/** Sprint θ inline error reason — feeds error-messages.ts CN template lookup. */
export interface SseErrorReason {
  /** Source event so UI can pick variant. */
  source: "turn_failed" | "canvas_op_failed" | "clarification_resume";
  /** ReasonCode enum string (e.g. "wallclock_exceeded", "section_not_found"). */
  reasonCode: string;
  /** Versioned, authenticated facts published by the Chat terminal boundary. */
  diagnostics?: ErrorDiagnostics;
  /** Backend `recovery_action` on `turn_failed` / failed `done`: the failure card's one primary
   *  action. Absent on old turns; `resolveRecoveryAction` then falls back to `retryable`. */
  recoveryAction?: import("./turn-feedback").RecoveryAction;
  /** Optional structured context for non-terminal canvas errors. */
  sectionId?: string;
  retryUrl?: string;
}

/** Sprint θ callable_peer dispatch progress entry. */
export interface PeerDispatchInfo {
  peerName: string;
  /** User-facing Agent name from configuration; absent uses localized profile fallback. */
  displayName?: string;
  dispatchedAt: number;
  parentCtxSummary: string;
  /** Sprint λ F5 (R2-a): dedup key — backend `peer_dispatched` event may fire
   * multiple times for same peer (subagent.py:694/1390/1505 — 3 emit sites).
   * When subagentId present, upsert by it; otherwise fall back to peerName. */
  subagentId?: string;
}


/** V5 PRD-E2 inline tool result preview (matches backend tool_result_preview.py schema). */
export interface CanvasAnchorDTO {
  canvas_id: string;
  section_id: string;
}

interface PreviewBaseDTO {
  source_label?: string | null;
  canvas_anchor?: CanvasAnchorDTO | null;
}

export interface KpiPreviewDTO extends PreviewBaseDTO {
  kind: "kpi";
  label: string;
  value: string;
  raw_value?: string | number | boolean | null;
  delta_pct: number | null;
  delta_tone: "pos" | "neg" | "neu";
}

export interface TablePreviewDTO extends PreviewBaseDTO {
  kind: "table";
  columns: string[];
  rows: string[][];
  typed_rows?: Array<Array<string | number | boolean | null>>;
  total_rows: number;
}

export interface ChartThumbnailPreviewDTO extends PreviewBaseDTO {
  kind: "chart_thumbnail";
  title: string;
  chart_kind: "bar" | "line" | "pie" | "stacked_bar";
  chart_spec: Record<string, unknown>;
}

export interface CountPreviewDTO extends PreviewBaseDTO {
  kind: "count";
  label: string;
  value: number;
  qualifier: string | null;
}

export interface TextPreviewDTO extends PreviewBaseDTO {
  kind: "text";
  preview: string;
  full_length: number;
}

export type ToolResultPreviewDTO =
  | KpiPreviewDTO
  | TablePreviewDTO
  | ChartThumbnailPreviewDTO
  | CountPreviewDTO
  | TextPreviewDTO;

/** M156-M3b: one clarification request (from SSE clarification.required)
 *
 * V2.7 P1/P2: kind determines UI rendering.
 *   - "token"     (V2.4): single-select clarification for the current task
 *   - "options"   (V2.7 P1): multi-select chips, N Lead-proposed analysis directions for user choice
 *   - "task_plan" (V2.7 P2): multi-select + sorting, choose sub-tasks to spawn
 */
export type ClarificationKind =
  | "token"
  | "options"
  | "task_plan"
  // Retired 2026-10-16; old persisted cards still replay as plain choices.
  | "business_rule_review"
  | "ambiguous_turn"
  | "formula_confirm"
  | "concept_resolution"
  | "metric_formula";

export interface ClarificationPayload {
  request_id: string;
  turn_id: string;
  workspace_id: string;
  token: string;
  rationale: string;
  kind: ClarificationKind;
  multi_select: boolean;
  candidates: Array<{
    value: string;
    label: string;
    hint?: string;
    confidence?: string;
  }>;
}

/** cca P2 (C3): the `cca.tool_create_open` SSE event payload — opens the inline create-tool card.
 *  Carries ONLY a scrubbed spec-URL hint (never a secret); the card calls the P1 endpoints directly. */
export interface CreateToolOpenPayload {
  spec_url_hint: string | null;
  /** The Lead's own classification of that address, when it made one — the card opens on the
   *  matching input mode instead of always defaulting to OpenAPI (a pasted MCP endpoint used to
   *  land under a field labelled "接口文档 (OpenAPI)"). Null = it could not tell. */
  kind_hint: "openapi" | "mcp" | null;
}

/** Config copilot batch 0.5: config.lite_propose payload, all inline-confirmation facts (self-contained small payload,
 *  no pointers / draft table, all enum values, no secrets / resource IDs). turn_id = beat anchor (dispatcher sees only
 *  payload); apply/undo is FE session state (config-copilot-lite.ts overlay), without event writeback. */
export type ConfigurationJson = string | number | boolean | null | ConfigurationJson[] | { [key: string]: ConfigurationJson };

export interface ConfigLiteProposeRow {
  baseline_revision?: number | string;
  field: string;
  from: ConfigurationJson;
  to: ConfigurationJson;
  from_unset: boolean;
  operation?: "set" | "reset";
  reset_all?: boolean;
  label?: string;
}

export interface ConfigLiteProposePayload {
  proposal_id: string;
  turn_id: string;
  form_id: string;
  review_page?: string;
  team_id?: string;
  settings_target?: {
    form_id: "workspace_i18n" | "workspace_metadata";
    user_id: string;
    team_id: string;
    workspace_id: string;
    edit_generation: number;
  };
  workspace_id: string | null;
  workspace_name: string | null;
  rows: ConfigLiteProposeRow[];
  notes: Record<string, string>;
}

/** Config copilot batch 1-b: tier-2 configuration draft (ConfigDrawer). Self-contained payload; chip rendering uses
 *  draft_id/form_id only, while open drawers apply field deltas directly (server draft is AI-suggestion truth). */
export interface ConfigDraftFieldMeta {
  origin: "generated" | "server";
  needs_confirm: boolean;
  note: string | null;
}

export interface ConfigDraftUpdatedPayload {
  draft_id: string;
  turn_id: string;
  form_id: string;
  created: boolean;
  updated_fields: string[];
  fields: Record<string, unknown>;
  field_meta: Record<string, ConfigDraftFieldMeta>;
  notes: Record<string, string>;
  workspace_id: string | null;
  target?: { id: string; label: string };
  changed_fields_cumulative?: string[];
}

/** Conversational Subagent authoring: a durable pointer to a server-owned draft.
 *  The event deliberately carries no generated definition, prompt, or tool grants; the
 *  review surface reloads owner-scoped detail from the draft API. Legacy/manual events
 *  may contain only `purpose_hint`. */
export interface CreateSubagentOpenPayload {
  purpose_hint: string | null;
  draft_id: string | null;
  draft_status: string | null;
  draft_title: string | null;
  draft_version?: number | null;
  /** User-stated slug-shaped name; retained if draft review switches to manual creation. */
  name_hint?: string | null;
  /** User-stated display name; retained if draft review switches to manual creation. */
  display_name_hint?: string | null;
}

/** workflow-solidify S2: the cca.workflow_solidify_open SSE event payload — opens the inline
 *  solidification card. turn_id = distillation target (null on conversational entry → FE resolves
 *  most recent completed turn); hint = one-line reuse-intent prefill. Card directly calls
 *  /api/v1/workflow-solidify/* + /api/v1/team-skills endpoints. */
export interface SolidifyOpenPayload {
  turn_id: string | null;
  hint: string | null;
}

// ─────────────────────────────────────────────────────────
// Scenario script (mock / dev mode)
// ─────────────────────────────────────────────────────────
export type TimelineEvent =
  | { at: number; kind: "turn_started" }
  | { at: number; kind: "intent"; intent: IntentType; complexity: number; rationale?: string }
  | { at: number; kind: "trace_step"; step: TraceStep }
  | {
      at: number;
      kind: "tool_call";
      id: string;
      name: string;
      input: string;
      /** Explicit owner for production-shaped multi-Agent mock traces. */
      subagentId?: string;
    }
  | { at: number; kind: "tool_result"; id: string; dur: number; output: string; sql?: string }
  | { at: number; kind: "partial_answer"; text: string }
  | { at: number; kind: "artifact"; card: DataCardPayload }
  | { at: number; kind: "source"; text: string; url: string }
  | { at: number; kind: "done"; summary: TurnSummary }
  /** D8 e2e: inject clarification.required (kind=options/task_plan) via mock scenarios to test frontend UI. */
  | { at: number; kind: "clarification_required"; payload: ClarificationPayload }
  /** Sprint ρ Phase C/D — peer dispatch event (mock surfaces peer block in rho layout). */
  | { at: number; kind: "peer_dispatched"; peer: PeerDispatchInfo }
  /** Sprint ρ Phase C/D — error reason set (mock surfaces fail banner). */
  | { at: number; kind: "error_set"; reason: SseErrorReason }
  /** Sprint ρ Phase C/D — θ-style turn-terminating clarification (drives rho InlineClarification inside agent body). */
  | { at: number; kind: "clarification_pending"; pending: ThetaClarificationState }
  /** prod-bug-bash-039 Phase 5 (0.39.3) — mock-mode citation registration so
   *  scenarios can exercise ArtifactChip cite:N resolution without backend.
   *  `citationKind` (not `kind`) carries the Citation.kind value — the
   *  outer `kind` field is the TimelineEvent discriminant. */
  | {
      at: number;
      kind: "turn_citation_registered";
      seq: number;
      artifactId: string;
      label?: string | null;
      citationKind?: Citation["kind"];
    };

export interface Scenario {
  id: string;
  label: string;
  user: string;
  sessionTitle: string;
  turnId: string;
  timeline: TimelineEvent[];
}

// ─────────────────────────────────────────────────────────
// Sidebar
// ─────────────────────────────────────────────────────────
export interface SessionItem {
  id: string;
  title: string;
  intents: string[];
  time: string;
  /**
   * Last activity as an ISO instant. When present the sidebar formats it with the shared list-date rule
   * ("Yesterday") in the viewer's time zone; `time` is the fallback for older Gateways and local rows.
   */
  updatedAt?: string | null;
  state: "active" | "done" | "error" | "pending";
  /** W3-1: unread by user (not visited after assistant finishes); populated by backend follow-up. */
  unread?: boolean;
  /** W3-1: user-pinned (top of sidebar group); backend follow-up. */
  pinned?: boolean;
  /** W3-1: archived (dimmed, hidden in expandable group by default); backend follow-up. */
  archived?: boolean;
  /** W3-1: associated skill (display skill label), value is skill ID/name. */
  skillApplied?: string | null;
}

export interface SessionGroup {
  id: string;
  label: string;
  items: SessionItem[];
}
