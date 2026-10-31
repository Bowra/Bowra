"use client";

/**
 * AgentTrace — renders a turn's tool-call trace in the production agent-block
 * model. Runs the pure `buildAgentGrouping` transformer over the turn's steps +
 * turn-store maps, then renders:
 *
 *   · lead-owned steps → a light Lead <AgentBlock> (only when the Lead ran its
 *     own tools — the SimpleTurns rule; pure dispatch turns show no Lead block),
 *   · each peer → an <AgentBlock> (agent identity from peer_name) with its inner
 *     <Step> rows + tool-result renderers + live prose,
 *   · ≥2 peers → wrapped in a neutral <MultiAgentGroup>. Peer count is not
 *     evidence of serial or parallel topology.
 *
 * The same component is used by live, archived, and deterministic spike paths.
 */
import { formatObservedValue } from "@/lib/chart/format";
import type { ReactNode } from "react";
import { AgentBlock } from "./AgentBlock";
import { CollaborationTrace } from "./CollaborationTrace";
import { ContextActivityRows, useContextActivities } from "./ContextActivity";
import { useCollaborationTrace } from "./collaboration-trace-state";
import { MultiAgentGroup, Prose } from "./block-frames";
import { ReadableProv } from "./block-chrome";
import type { StepStatus } from "./block-primitives";
import { useTranslations } from "next-intl";
import {
  normalizePeerFailureReasonCode,
  normalizeReasonCode,
  type PeerFailureReasonCode,
} from "@/lib/error-messages";
import { requestCanvasDataFocus } from "@/lib/canvas-workspace-focus";
import { extractWritingContent } from "@/lib/extract-writing-content";
import { SKILL_UNAVAILABLE_CODE_PREFIX } from "@/lib/sse-events";
import type { PeerTerminal } from "@/lib/turn-store";
import { FailureAckCard } from "./FailureAckCard";
import { Step } from "./Step";
import {
  buildAgentGrouping,
  stepStateToStatus,
  type PeerBlock,
} from "./agent-grouping";
import { isRecoverableStepFailure } from "./self-healing";
import { useTurnRunning } from "./turn-running";
import { Chunks, Code, Hits, ParallelRail, ResultTable } from "./tool-results";
import { stepDisplayToolName, writingDisplayKey } from "./tool-catalog";
import type { StepView } from "./types";
import { CharIcon } from "@/components/char-icon";
import { appPath } from "@/lib/base-path";
import { personalSettingsPath } from "@/lib/personal-settings-paths";

const POWERBI_STEP_ERROR_KEYS = {
  pbi_catalog_unavailable: "pbi_errors.catalog_unavailable",
  pbi_connection_credential_unavailable:
    "pbi_errors.connection_credential_unavailable",
  pbi_connection_credential_update_failed:
    "pbi_errors.connection_credential_update_failed",
  pbi_connection_load_failed: "pbi_errors.connection_load_failed",
  pbi_connection_unavailable: "pbi_errors.connection_unavailable",
  pbi_query_access_denied: "pbi_errors.query_access_denied",
  pbi_query_failed: "pbi_errors.query_failed",
  pbi_query_input_required: "pbi_errors.query_input_required",
  pbi_query_rate_limited: "pbi_errors.query_rate_limited",
  pbi_query_response_invalid: "pbi_errors.query_response_invalid",
  pbi_query_result_incomplete: "pbi_errors.query_result_incomplete",
  pbi_query_unreachable: "pbi_errors.query_unreachable",
  pbi_token_refresh_failed: "pbi_errors.token_refresh_failed",
  pbi_token_refresh_rejected: "pbi_errors.token_refresh_rejected",
  pbi_user_identity_unavailable: "pbi_errors.user_identity_unavailable",
} as const;

const PEER_FAILURE_MESSAGE_KEYS: Record<
  PeerFailureReasonCode,
  `peer_errors.${string}`
> = {
  peer_configuration_error: "peer_errors.configuration_error",
  peer_empty_result: "peer_errors.empty_result",
  peer_execution_failed: "peer_errors.execution_failed",
  peer_execution_uncertain: "peer_errors.execution_uncertain",
  peer_execution_timeout: "peer_errors.execution_timeout",
  peer_internal_error: "peer_errors.internal_error",
  peer_iteration_limit_exceeded: "peer_errors.iteration_limit_exceeded",
  peer_tool_failure_limit_exceeded:
    "peer_errors.tool_failure_limit_exceeded",
  peer_tool_call_limit_exceeded: "peer_errors.tool_call_limit_exceeded",
  peer_credit_exhausted: "peer_errors.credit_exhausted",
};

/**
 * Agent-to-agent messaging and new-presentation codes the server forwards on a failed step.
 * Each family shares one sentence that says what happened and what the user can do.
 */
const COLLABORATION_AND_DECK_ERROR_KEYS: Record<string, `collab_errors.${string}` | `deck_errors.${string}`> = {
  collaboration_unresolved: "collab_errors.waiting",
  collaboration_arguments_invalid: "collab_errors.format",
  collaboration_coordination_reply_only: "collab_errors.format",
  collaboration_wait_requires_settled_batch: "collab_errors.format",
  collaboration_wait_already_active: "collab_errors.format",
  collaboration_wait_invalid: "collab_errors.format",
  collaboration_body_invalid: "collab_errors.format",
  collaboration_reply_invalid: "collab_errors.format",
  collaboration_question_invalid: "collab_errors.format",
  collaboration_resolution_invalid: "collab_errors.format",
  collaboration_reply_closed: "collab_errors.closed",
  collaboration_dependency_closed: "collab_errors.closed",
  collaboration_question_unavailable: "collab_errors.closed",
  collaboration_evidence_superseded: "collab_errors.closed",
  collaboration_operation_limit: "collab_errors.limit",
  collaboration_question_limit: "collab_errors.limit",
  collaboration_followup_limit: "collab_errors.limit",
  collaboration_participant_limit: "collab_errors.limit",
  collaboration_payload_limit: "collab_errors.limit",
  collaboration_context_limit: "collab_errors.limit",
  collaboration_effort_exhausted: "collab_errors.limit",
  collaboration_resolution_forbidden: "collab_errors.forbidden",
  collaboration_tool_denied: "collab_errors.forbidden",
  collaboration_material_unavailable: "collab_errors.forbidden",
  collaboration_evidence_unavailable: "collab_errors.forbidden",
  collaboration_unavailable: "collab_errors.unavailable",
  collaboration_worker_unavailable: "collab_errors.unavailable",
  collaboration_recipient_unavailable: "collab_errors.unavailable",
  collaboration_execution_unavailable: "collab_errors.unavailable",
  collaboration_owner_unavailable: "collab_errors.unavailable",
  deck_render_not_admitted: "deck_errors.not_admitted",
  deck_storage_unavailable: "deck_errors.storage_unavailable",
  invalid_deck_arguments: "deck_errors.invalid",
  deck_validation_failed: "deck_errors.invalid",
  deck_delivery_missing: "deck_errors.missing",
};

/** A bare machine identifier ("wallclock_exceeded", "peer.timeout") — never shown as a sentence. */
const MACHINE_CODE = /^[a-z][a-z0-9]*(?:[_.:-][a-z0-9]+)+$/;

/** `skill_unavailable_<reason>` codes with their own copy under `step_list.skill_unavailable_reason_<reason>`. */
const SKILL_UNAVAILABLE_REASON_KEYS = {
  not_granted: "skill_unavailable_reason_not_granted",
  script_runtime_disabled: "skill_unavailable_reason_script_runtime_disabled",
  command_disabled: "skill_unavailable_reason_command_disabled",
  disabled_by_team: "skill_unavailable_reason_disabled_by_team",
  not_approved: "skill_unavailable_reason_not_approved",
  pending_reapproval: "skill_unavailable_reason_pending_reapproval",
  not_visible: "skill_unavailable_reason_not_visible",
  use_denied: "skill_unavailable_reason_use_denied",
  source_changed: "skill_unavailable_reason_source_changed",
} as const;

function skillDenialMessage(
  error: NonNullable<StepView["error"]>,
  tStep: ReturnType<typeof useTranslations<"step_list">>,
): string | null {
  const code = error.code;
  if (!code?.startsWith(SKILL_UNAVAILABLE_CODE_PREFIX)) return null;
  const reason = code.slice(SKILL_UNAVAILABLE_CODE_PREFIX.length);
  if (Object.hasOwn(SKILL_UNAVAILABLE_REASON_KEYS, reason)) {
    const skill = error.skill ? tStep("skill_unavailable_named", { name: error.skill }) : tStep("skill_unavailable_unnamed");
    return tStep(SKILL_UNAVAILABLE_REASON_KEYS[reason as keyof typeof SKILL_UNAVAILABLE_REASON_KEYS], { skill });
  }
  // A reason this page doesn't know yet: the server's sentence still names the Skill and the fix.
  const sentence = (error.message ?? "").trim();
  return sentence && !MACHINE_CODE.test(sentence) ? sentence : tStep("skill_unavailable");
}

/**
 * The step row's failure sentence. Known codes map to their localized copy (Power BI, peer failures, the
 * public turn reasons); a message that is itself a bare code never reaches the page (Chat Cards Spec: no
 * internal identifiers in the UI) and falls back to "This step didn't finish".
 */
export function stepErrorMessage(
  error: NonNullable<StepView["error"]>,
  tStep: ReturnType<typeof useTranslations<"step_list">>,
  tReasons: ReturnType<typeof useTranslations<"error_messages.reasons">>,
): string {
  if (error.code === "agent_admission_blocked") return tStep("agent_admission_blocked");
  const skillDenial = skillDenialMessage(error, tStep);
  if (skillDenial !== null) return skillDenial;
  if (error.code === "skill_unavailable" || error.code === "assigned_skill_unavailable" || error.code === "skill_scope_denied") {
    // The current server writes a sentence that names the Skill and the fix; older codes carry generic text.
    const sentence = error.code === "skill_unavailable" ? (error.message ?? "").trim() : "";
    return sentence && !MACHINE_CODE.test(sentence) ? sentence : tStep("skill_unavailable");
  }
  const key =
    error.code && error.code in POWERBI_STEP_ERROR_KEYS
      ? POWERBI_STEP_ERROR_KEYS[
          error.code as keyof typeof POWERBI_STEP_ERROR_KEYS
        ]
      : undefined;
  if (key) return tStep(key);
  if (error.code && Object.hasOwn(COLLABORATION_AND_DECK_ERROR_KEYS, error.code)) {
    return tStep(COLLABORATION_AND_DECK_ERROR_KEYS[error.code]);
  }
  // Historical producers also stored model instructions and exception prose here. Only finite codes establish user-facing explanations.
  const { message: published = "" } = error;
  const message = published.trim();
  const machine = MACHINE_CODE.test(message) ? message : null;
  for (const candidate of [error.code, machine]) {
    if (!candidate) continue;
    if (normalizeReasonCode(candidate) === candidate) return tReasons(candidate as Parameters<typeof tReasons>[0]);
    if (candidate in PEER_FAILURE_MESSAGE_KEYS) {
      return tStep(PEER_FAILURE_MESSAGE_KEYS[candidate as PeerFailureReasonCode]);
    }
  }
  return tStep("step_failed_generic");
}

export interface AgentTraceProps {
  steps: StepView[];
  /** turn-store `peerNames` — peer subagent id → backend agent_name. */
  peerNames: Record<string, string>;
  /** turn-store `peerDisplayNames` — peer id → configured name snapshot. */
  peerDisplayNames?: Record<string, string>;
  /** turn-store `peerProse` — peer subagent id → live prose. */
  peerProse: Record<string, string>;
  /** turn-store `peerTasks` — peer subagent id → task (plan-path header). */
  peerTasks?: Record<string, string>;
  /** Canonical terminal facts keyed by peer id. */
  peerTerminalById?: Record<string, PeerTerminal>;
  /** A completed Lead turn recovered from a failed peer, so render bail rather
   *  than misrepresenting the whole turn as a hard failure. */
  peerFailureRecovered?: boolean;
  /** Acknowledge a failed step (UI-only dismiss). O5: removed fake retry/skip buttons (stub endpoints do not actually rerun). */
}

/** Map a StepView's detail / subSteps onto the v2 tool-result renderers. */
function renderStepDetail(
  s: StepView,
  tStep: ReturnType<typeof useTranslations>,
): ReactNode {
  // Streaming lead-writing (python/sql): live tokens + caret until the finalized
  // `detail` lands. Preserves the s_3e854 ④ anti-dead-air reveal that the
  // flat→block swap had dropped (the body stayed empty until the step completed).
  if (s.isStreaming && s.partialContent) {
    const lang =
      s.detail?.kind === "sql" ? "SQL" : s.detail?.kind === "python" ? "Python" : undefined;
    // #5 (chat-solidify follow-up): partialContent is the accumulated raw
    // tool-args JSON; project it to the code/query being written so the <Code>
    // block drops the `{"code":…}` scaffold + sibling fields (python_sandbox's
    // trailing `"inputs":{…}` tail — dogfood 0.72.8).
    const code = extractWritingContent(s.partialContent, s.toolName);
    return <Code code={code} lang={lang} caret />;
  }
  // bi_query batch → parallel SQL rail
  if (s.toolName === "bi_query" && s.subSteps && s.subSteps.length > 0) {
    return (
      <ParallelRail
        rows={s.subSteps.map((ss) => ({
          sql: ss.label,
          meta: ss.summary,
          status: stepStateToStatus(ss.status ?? "done"),
          code: ss.code,
        }))}
      />
    );
  }
  const d = s.detail;
  if (!d) return null;
  switch (d.kind) {
    case "hits":
      return <Hits hits={d.hits.map((h) => ({ title: h.title, src: h.source, page: h.page }))} />;
    case "chunks":
      return <Chunks chunks={d.chunks.map((c) => ({ src: c.src, text: c.text }))} />;
    case "stdout":
      return <Code code={d.text} />;
    case "sql":
      return <Code code={d.code} lang="SQL" />;
    case "python":
      return <Code code={d.code} lang="Python" />;
    case "table": {
      // Cap the inline preview at 50 rows + footer (parity with legacy
      // StepContentToolRun); the swap had dumped the full result set inline
      // (overflow / perf risk). Full set lives in the canvas data tab.
      const CAP = 50;
      const total = d.rows.length;
      const shown = d.rows.slice(0, CAP);
      const nums = d.cols
        .map((_, i) => i)
        .filter((i) => shown.some((r) => typeof r[i] === "number"));
      return (
        <ResultTable
          cols={d.cols}
          rows={shown.map((r) => r.map((v) => (typeof v === "number" ? formatObservedValue(v) : v)))}
          nums={nums}
          foot={total > CAP ? tStep("preview_rows_footer", { shown: CAP, total }) : undefined}
        />
      );
    }
    default:
      return null;
  }
}

/**
 * A step summary that is a raw tool result (a JSON object or array, e.g. `{"ok": true, "id": "mem_…", …}`) is never
 * shown: the backend falls back to truncated raw JSON for tools without a summary branch, and old turns persisted
 * that fallback. Users see the label and duration only (Chat Cards Spec: no JSON or internal identifiers).
 */
export function isRawResultSummary(summary: string | null | undefined): boolean {
  const text = summary?.trim() ?? "";
  if (!/^[[{]/.test(text)) return false;
  // Truncated previews end in "…" and do not parse; a quoted key right after the bracket is enough.
  // Arrays only when the first element is clearly JSON: "[1] Source…" and "[3/5] …" are ordinary summaries.
  return (
    /^\{\s*"[^"]+"\s*:/.test(text) ||
    /^\[\s*(\{|\[|\]\s*$|("[^"]*"|-?\d+(\.\d+)?|true|false|null)\s*(,|\]\s*$))/.test(text)
  );
}

/** Tools whose completed step gets a fixed user-facing summary instead of the backend preview. */
const MEMORY_SAVE_TOOLS = new Set(["remember", "memory_write", "memory_add"]);

/** Right-rail status text: live status while active, error label while failed,
 *  else the done/partial summary. Ported from the legacy StepRow.deriveRightSummary
 *  so live rows keep showing running…/streaming… instead of going blank. */
function deriveRightSummary(
  s: StepView,
  tChat: ReturnType<typeof useTranslations>,
): ReactNode {
  if (s.state === "pending") return null;
  if (s.state === "active") {
    // The model is still writing this call's arguments; the tool has not run: say "准备中", not "运行中".
    // The execution step replaces this row upon arrival (`step-view.ts::foldWritingSteps`).
    // Code/query writing row names already say "正在写代码" / "正在写查询"; do not repeat them on the right.
    if (s.stepType === "lead_thinking_writing") {
      return writingDisplayKey(s.stepType, s.toolName) ? null : tChat("preparing");
    }
    if (s.isStreaming) return "streaming…";
    if (s.toolName === "bi_query" && s.subSteps?.length) {
      return tChat("running_parallel", { count: s.subSteps.length });
    }
    if (s.subSteps?.length) return tChat("running_progress", { count: s.subSteps.length });
    return tChat("running");
  }
  // User clicked Stop ⇒ this step will have no result. Explicitly enumerate it: this function lists each known state,
  // so omitting it falls through every branch, leaving the right blank (neither spinner nor explanation, more confusing than spinning).
  if (s.state === "cancelled") return tChat("cancelled_label");
  if (s.state === "done" || s.state === "partial") {
    // Remember: the label already quotes what was stored; never echo the stored record or its `mem_…` id.
    if (s.toolName && MEMORY_SAVE_TOOLS.has(s.toolName) && s.state === "done") return tChat("memory_step_saved");
    return isRawResultSummary(s.summary) ? null : (s.summary ?? null);
  }
  if (s.state === "failed") {
    const ui = s.error?.uiKind ?? s.error?.kind;
    if (ui === "transient" || ui === "needs_user_input") {
      // Feishu 07.06 #5: "临时失败" does not explain code-execution failures; use "运行失败" specifically for running code.
      return s.toolName === "python_sandbox"
        ? tChat("status_code_run_fail")
        : tChat("status_transient_fail");
    }
    return tChat("status_error");
  }
  return null;
}

interface StepFromViewProps {
  s: StepView;
}

function StepFromView({ s }: StepFromViewProps) {
  const tChat = useTranslations("chat");
  const turnRunning = useTurnRunning();
  const tStep = useTranslations("step_list");
  const tReasons = useTranslations("error_messages.reasons");
  const tResults = useTranslations("results_workspace");
  const detail = renderStepDetail(s, tStep) ?? (
    s.toolName && MEMORY_SAVE_TOOLS.has(s.toolName) && s.state === "done" ? (
      <p className="stp-memory-note">
        {tChat.rich("memory_step_detail", {
          link: (chunks) => <a href={appPath(personalSettingsPath("memory"))}>{chunks}</a>,
        })}
      </p>
    ) : null
  );
  const outputArtifacts = s.outputArtifacts?.length ? (
    <div className="stp-output-artifacts">
      {s.outputArtifacts.map((artifact, index) => {
        const filename = artifact.filename || `${tResults("evidence")} ${index + 1}`;
        const displayName = artifact.title && artifact.title !== filename ? `${artifact.title} · ${filename}` : filename;
        const openLabel = tResults("open_object", { filename: displayName });
        return (
          <button
            key={artifact.artifactId}
            type="button"
            className="stp-output-artifact"
            onClick={() => requestCanvasDataFocus(artifact.artifactId, {
              entryPoint: "chat",
            })}
            aria-label={openLabel}
            title={artifact.description || openLabel}
          >
            <span aria-hidden><CharIcon ch="→" aria-hidden /></span>
            <span>{displayName}</span>
          </button>
        );
      })}
    </div>
  ) : null;
  // Per-step failure UI: no buttons (O5 removed fake retry/skip; Feishu 07.06 #2 removed dead "知道了").
  // The card reads current task activity separately from the original step outcome.
  const ack =
    s.state === "failed" && s.error ? (
      <FailureAckCard
        kind={s.error.uiKind ?? (s.error.kind === "permanent" ? "permanent" : "transient")}
        message={stepErrorMessage(s.error, tStep, tReasons)}
        diagnostics={s.error.diagnostics}
        retryable={s.error.retryable}
        inset
      />
    ) : s.state === "partial" ? (
      // Only explicit completed substeps count as successes; absent outcomes stay unknown.
      <FailureAckCard
        kind={s.error?.uiKind ?? (s.error?.kind === "permanent" ? "permanent" : "transient")}
        message={tChat("partial_step_failure")}
        failedCount={s.subSteps?.length ? s.subSteps.filter((x) => x.status === "failed").length : undefined}
        succeededCount={s.subSteps?.length ? s.subSteps.filter((x) => x.status === "done").length : undefined}
        diagnostics={s.error?.diagnostics}
        retryable={s.error?.retryable}
        inset
      />
    ) : null;
  const body =
    detail || outputArtifacts || ack ? (
      <>
        {detail}
        {outputArtifacts}
        {ack}
      </>
    ) : null;
  // UX-1: code-like detail (python/sql/stdout: sandbox code/SQL/output) collapses automatically after completion;
  // result detail such as tables/hits stays open (it is what users want to see). bi_query is an exception:
  // its ParallelRail shows batch-query results, so do not collapse it as code.
  const codeLike =
    s.toolName !== "bi_query" &&
    (s.detail?.kind === "python" || s.detail?.kind === "sql" || s.detail?.kind === "stdout");
  // Preserve the existing severity distinction without treating it as proof of recovery.
  const recoverableFailure = isRecoverableStepFailure(s);
  const stepStatus =
    recoverableFailure && s.state === "failed" ? "part" : stepStateToStatus(s.state);
  // Recoverable failures do not seize focus while the task runs. Nonretryable
  // failures stay expanded; ended/archived tasks expose the original explanation.
  const forceOpen = ack ? !(turnRunning && recoverableFailure) : false;
  return (
    <Step
      toolName={stepDisplayToolName(s.stepType, s.toolName)}
      label={s.label}
      summary={deriveRightSummary(s, tChat)}
      time={s.state === "done" ? s.durationText : undefined}
      status={stepStatus}
      open={forceOpen ? true : undefined}
      autoCollapseOnDone={codeLike && !ack}
    >
      {body}
    </Step>
  );
}

function statusOf(steps: StepView[]): StepStatus {
  const states = steps.map((s) => s.state);
  if (states.some((x) => x === "active")) return "run";
  // a failed step is handled per-step (FailureAckCard) → the block reads "part",
  // not "fail" (block-level fail/bail needs an explicit peer-death signal).
  if (states.some((x) => x === "failed" || x === "partial")) return "part";
  return "done";
}

const PEER_PARTIAL_REASONS = new Set([
  "step_limit",
  "time_limit",
  "tool_call_limit",
  "no_progress",
  "budget",
  "agent_stalled",
]);

/** Copy for a peer that stopped at an agent run limit; unknown reasons use the generic line. */
function peerPartialMessageKey(stopReason: string): `peer_partial.${string}` {
  return PEER_PARTIAL_REASONS.has(stopReason)
    ? `peer_partial.${stopReason}`
    : "peer_partial.other";
}

const RESUME_REFUSED_REASONS = new Set(["expired", "agent_changed", "tools_changed", "unreadable"]);

/** Copy for a Continue that started the peer fresh instead of from its saved work. */
function resumeRefusedMessageKey(reason: string): `resume_refused.${string}` {
  return RESUME_REFUSED_REASONS.has(reason) ? `resume_refused.${reason}` : "resume_refused.other";
}

interface PeerBlockViewProps {
  block: PeerBlock;
}

function PeerBlockView({ block }: PeerBlockViewProps) {
  const t = useTranslations("step_list");
  const failureCode = block.failureReason
    ? normalizePeerFailureReasonCode(block.failureReason)
    : null;
  return (
    <AgentBlock
      agent={block.agent}
      displayName={block.displayName ?? undefined}
      task={block.task ?? undefined}
      time={block.time ?? undefined}
      status={block.status}
      outcomeReason={
        [
          block.resumeRefused !== null ? t(resumeRefusedMessageKey(block.resumeRefused)) : null,
          block.stepUsage ? t("peer_partial.step_usage", block.stepUsage) : null,
          block.stopReason !== null
            ? t(peerPartialMessageKey(block.stopReason))
            : failureCode
              ? t(PEER_FAILURE_MESSAGE_KEYS[failureCode])
              : null,
        ]
          .filter(Boolean)
          .join(" ") || undefined
      }
      outcomeDiagnostics={block.failureDiagnostics ?? undefined}
      open={block.status === "run"}
    >
      {block.steps.map((s) => (
        <StepFromView
          key={s.stepId}
          s={s}
        />
      ))}
      {block.prose ? <Prose>{block.prose}</Prose> : null}
      <ContextActivityRows subagentId={block.peerId} />
    </AgentBlock>
  );
}

export function AgentTrace({
  steps,
  peerNames,
  peerDisplayNames = {},
  peerProse,
  peerTasks,
  peerTerminalById = {},
  peerFailureRecovered = false,
}: AgentTraceProps) {
  const turnRunning = useTurnRunning();
  const contextActivities = useContextActivities();
  const leadContext = contextActivities.filter((activity) => activity.subagent_id === null);
  const collaboration = useCollaborationTrace();
  const { leadSteps, peerBlocks: groupedBlocks } = buildAgentGrouping(
    collaboration?.enabled ? steps.filter((step) => step.toolName !== "agent_message" || step.state !== "done") : steps,
    peerNames,
    peerProse,
    peerTasks,
    peerDisplayNames,
    peerTerminalById,
    peerFailureRecovered,
  );
  // A peer killed with its process (e.g. a service restart) never emits its own
  // terminal fact, so its block derives "run" forever. Once the turn has ended
  // nothing is running any more: show it as partial instead of a live spinner.
  const peerBlocks = turnRunning
    ? groupedBlocks
    : groupedBlocks.map((b) => (b.status === "run" ? { ...b, status: "part" as const } : b));

  // Neutral answer provenance. The event contract identifies participants but
  // does not yet prove peer-to-peer ordering or concurrency topology.
  const provenanceParticipants: Array<{ agent: string; displayName?: string }> = [];
  if (peerBlocks.length > 0) {
    provenanceParticipants.push({ agent: "lead" });
    peerBlocks.forEach((b) => {
      provenanceParticipants.push({
        agent: b.agent,
        displayName: b.displayName ?? undefined,
      });
    });
  }

  return (
    <div className="ag-trace">
      {(leadSteps.length > 0 || leadContext.length > 0) && (
        <AgentBlock
          agent="lead"
          status={turnRunning && leadContext.some((activity) => activity.phase === "running") ? "run" : statusOf(leadSteps)}
          open
        >
          {leadSteps.map((s) => (
            <StepFromView
              key={s.stepId}
              s={s}
            />
          ))}
          <ContextActivityRows subagentId={null} />
        </AgentBlock>
      )}

      {peerBlocks.length === 1 && (
        <PeerBlockView
          block={peerBlocks[0]}
        />
      )}

      {peerBlocks.length > 1 && (
        <MultiAgentGroup n={peerBlocks.length}>
          {peerBlocks.map((b) => (
            <PeerBlockView
              key={b.peerId}
              block={b}
            />
          ))}
        </MultiAgentGroup>
      )}

      <CollaborationTrace />

      {provenanceParticipants.length > 0 && (
        <ReadableProv participants={provenanceParticipants} />
      )}
    </div>
  );
}
