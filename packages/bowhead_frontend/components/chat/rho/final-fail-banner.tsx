"use client";

/**
 * <FinalFailBanner> — the one failure card of a turn (Chat Cards Spec §7, D3/D5).
 *
 * A failure ChatCard: eyebrow "This turn didn't finish", a title saying what happened (mapped from the
 * reason code, never guessed), a body explaining the next step and screened provider facts,
 * collapsed technical details and "Copy diagnostic information", and exactly one primary recovery action chosen by the backend's
 * `recovery_action` (D5). Reason codes and turn ids never appear on the page; they go into the copied text.
 * The title is announced once when the card appears (`live="off"` for replayed history).
 */

import { Copy, Play, Plus, RotateCcw } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { clarificationFailureStage } from "@/lib/clarification-failure-stage";
import { copyDiagnostics } from "@/components/error-notice";
import { ChatCard, type ChatCardAction } from "@/components/ui/card";
import { normalizeReasonCode, type ReasonCode } from "@/lib/error-messages";
import type { ErrorDiagnostics } from "@/lib/error-presentation";
import { normalizeErrorDiagnostics } from "@/lib/error-presentation";
import { chooseFailureCardAction, type RecoveryAction } from "@/lib/turn-feedback";
import { toast } from "@/lib/use-toast";

interface Props {
  kind: ReasonCode;
  /** Eyebrow override. Defaults to "This turn didn't finish" (documents: "The document didn't finish"). */
  header?: string;
  /** Stable turn correlation id — goes into diagnostics (copy), not onto the page. */
  turnId?: string;
  /** Authenticated diagnostics published once for this terminal failure. */
  diagnostics?: ErrorDiagnostics;
  /** Saved state comes only from the original card's read-only resume authority. */
  clarificationAnswerState?: "saved" | "unconfirmed";
  /**
   * Chat turn feedback D5: the resolved recovery action (`resolveRecoveryAction`). The card shows exactly
   * one primary action for it, using the handlers below; an action the surface cannot perform degrades to
   * editing the question, then to copying diagnostics.
   */
  recoveryAction?: RecoveryAction;
  onRetry?: () => void;
  /** `onRetry` only restores the question into the composer (no server replay): degrade to Edit and send. */
  retryRestoresDraft?: boolean;
  onEditAndResend?: () => void;
  onNewSession?: () => void;
  /**
   * `plan_blocked`: resume the blocked plan through its plan-card approval flow instead of editing the question.
   * `available` is the synchronous check (plan still blocked, same Space); `onResume` re-checks the plan
   * revision and access with the server and resolves `false` when it could not resume, which falls back to
   * Edit and send with the reason written in the card.
   */
  resumePlan?: { available: boolean; onResume: () => Promise<boolean>; revision?: string };
  /** Legacy single primary action (used when `recoveryAction` is not given). */
  onPrimary?: () => void;
  primaryLabel?: string;
  /** Replayed history: do not announce the card again. */
  live?: "assertive" | "off";
  className?: string;
}

/**
 * Failure copy groups (spec "失败原因对照"): the title says what happened, the body the known outcome and
 * what to do. Reason codes outside the table keep their own localized sentence as the title.
 */
const FAILURE_GROUPS = {
  agent_step_limit: "agent_step_limit",
  agent_tool_call_limit: "agent_tool_call_limit",
  agent_stalled: "agent_stalled",
  llm_provider_unavailable: "llm_provider_unavailable",
  llm_provider_timeout: "llm_provider_timeout",
  llm_provider_rate_limited: "llm_provider_rate_limited",
  llm_stream_interrupted: "llm_stream_interrupted",
  llm_response_invalid: "llm_response_invalid",
  llm_provider_auth_failed: "llm_provider_auth_failed",
  llm_request_rejected: "llm_request_rejected",
  llm_context_overflow: "llm_context_overflow",
  llm_stream_deadline_exceeded: "llm_stream_deadline_exceeded",
  wallclock_exceeded: "wallclock_exceeded",
  hop_cap_exceeded: "hop_cap_exceeded",
  peer_depth_exceeded: "peer_depth_exceeded",
  cost_cap_exceeded: "cost_cap_exceeded",
  token_cap_exhausted: "token_cap_exhausted",
  plan_runtime_unavailable: "plan_runtime_unavailable",
  single_write_leaf_violation: "single_write_leaf_violation",
  user_credit_exhausted: "user_credit_exhausted",
  team_pool_exhausted: "team_pool_exhausted",
  clarification_resume_unavailable: "clarification_resume_unavailable",
  session_deleted: "session_deleted",
  reference_policy_changed: "reference_policy",
  service_interrupted: "service_interrupted",
  terminal_projection_not_committed: "terminal_projection_not_committed",
  lead_internal_error: "system_internal",
} as const satisfies Partial<Record<ReasonCode, string>>;

const LIMIT_REASONS = new Set<ReasonCode>([
  "agent_step_limit", "agent_tool_call_limit",
  "wallclock_exceeded", "llm_stream_deadline_exceeded", "token_cap_exhausted",
  "hop_cap_exceeded", "peer_depth_exceeded", "cost_cap_exceeded",
]);

const DOCUMENT_REASONS = new Set<ReasonCode>(["document_information_incomplete", "document_planning_failed", "document_authoring_script_failed", "document_candidate_invalid"]);

/** Narrower copy is used only for facts published for this same model failure. */
function modelFailureGroup(
  reason: ReasonCode,
  diagnostics: ErrorDiagnostics | undefined,
): string | undefined {
  if (diagnostics?.diagnosticCode !== "chat_llm_failed" || diagnostics.code !== reason) return undefined;
  const detail = diagnostics.safeDetail ?? "";
  const status = /(?:^|;\s*)http_status=(\d{3})(?:;|$)/.exec(detail)?.[1];
  if (reason === "llm_provider_auth_failed" && status === "401"
    && /(?:^|;\s*)upstream_code=invalid_api_key(?:;|$)/.test(detail)) return "llm_provider_key_rejected";
  if (reason === "llm_provider_auth_failed" && status === "403") return "llm_provider_access_denied";
  if (reason === "llm_request_rejected") {
    if (status === "404") return "llm_model_or_endpoint_missing";
    if (/(?:^|;\s*)upstream_param=[A-Za-z0-9][A-Za-z0-9_.\[\]:-]{0,47}(?:;|$)/.test(detail)) {
      return "llm_request_parameter_rejected";
    }
  }
  if (reason === "llm_provider_unavailable" && /(?:^|;\s*)failure_class=network(?:;|$)/.test(detail)) {
    return "llm_provider_network_unavailable";
  }
  return undefined;
}

/** Failed-turn diagnostics: the server-published facts plus this turn's id; minimal facts from the code otherwise. */
function bannerDiagnostics(
  published: ErrorDiagnostics | undefined,
  code: string,
  turnId: string | undefined,
): ErrorDiagnostics | undefined {
  const base = published ?? { schemaVersion: 1, disclosure: "authenticated", code };
  const withCode = { ...base, code: base.code ?? code };
  const withTurn = turnId && !base.turnId ? { ...withCode, turnId } : withCode;
  return normalizeErrorDiagnostics(withTurn) ?? normalizeErrorDiagnostics(base);
}

function timeOf(observedAt: string | undefined): string | undefined {
  if (!observedAt) return undefined;
  const date = new Date(observedAt);
  if (Number.isNaN(date.getTime())) return undefined;
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

export function FinalFailBanner({
  kind,
  header,
  turnId,
  diagnostics,
  clarificationAnswerState,
  recoveryAction,
  onRetry,
  retryRestoresDraft,
  onEditAndResend,
  onNewSession,
  resumePlan,
  onPrimary,
  primaryLabel,
  live = "assertive",
  className,
}: Props) {
  const [resumeBusy, setResumeBusy] = useState(false);
  const [resumeRejected, setResumeRejected] = useState(false);
  // A rejection belongs to the plan revision it was checked against; a new revision may be resumable again.
  const [rejectedRevision, setRejectedRevision] = useState<string | undefined>(undefined);
  if (resumeRejected && resumePlan?.revision !== rejectedRevision) {
    setResumeRejected(false);
    setRejectedRevision(undefined);
  }
  const t = useTranslations("chat");
  const tCard = useTranslations("chat_card");
  const tReasons = useTranslations("error_messages.reasons");
  const normalizedKind = normalizeReasonCode(kind);
  const modelFailure = normalizedKind.startsWith("llm_");
  const group = normalizedKind in FAILURE_GROUPS
    ? FAILURE_GROUPS[normalizedKind as keyof typeof FAILURE_GROUPS]
    : undefined;
  const document = DOCUMENT_REASONS.has(normalizedKind);
  const parsedDiagnostics = normalizeErrorDiagnostics(diagnostics);
  const safeDiagnostics =
    parsedDiagnostics?.disclosure === "authenticated" &&
    (!parsedDiagnostics.code || parsedDiagnostics.code === normalizedKind) &&
    (!parsedDiagnostics.turnId || !turnId || parsedDiagnostics.turnId === turnId) &&
    (parsedDiagnostics.diagnosticCode === "chat_turn_failed" ||
      parsedDiagnostics.diagnosticCode === "chat_llm_failed" ||
      parsedDiagnostics.diagnosticCode === "chat_canvas_failed" ||
      (normalizedKind === "clarification_resume_unavailable" &&
        parsedDiagnostics.diagnosticCode === "rule_clarification_failure"))
      ? parsedDiagnostics
      : undefined;
  const specificModelGroup = modelFailureGroup(normalizedKind, safeDiagnostics);
  const phase = safeDiagnostics?.diagnosticCode === "chat_llm_failed"
    ? /(?:^|;\s*)phase=(invoke|stream)(?:;|$)/.exec(safeDiagnostics.safeDetail ?? "")?.[1]
    : undefined;
  const modelDetail = safeDiagnostics?.diagnosticCode === "chat_llm_failed" ? safeDiagnostics.safeDetail ?? "" : "";
  const modelStatus = /(?:^|;\s*)http_status=(\d{3})(?:;|$)/.exec(modelDetail)?.[1];
  const rejectedParameter = normalizedKind === "llm_request_rejected"
    ? /(?:^|;\s*)upstream_param=([A-Za-z0-9][A-Za-z0-9_.\[\]:-]{0,47})(?:;|$)/.exec(modelDetail)?.[1] : undefined;
  const responsesRejected = normalizedKind === "llm_request_rejected"
    && /(?:^|;\s*)api_surface=responses(?:;|$)/.test(modelDetail)
    && ["400", "404", "405"].includes(modelStatus ?? "");
  const outcomeUnknown = /(?:^|;\s*)outcome=unknown(?:;|$)/.test(modelDetail);
  const location = modelFailure
    ? t("failure_card.location_model")
    : t("failure_card.location_chat");
  const copyBundle = bannerDiagnostics(safeDiagnostics, normalizedKind, turnId);
  const clarificationStage = clarificationFailureStage(normalizedKind, copyBundle);
  const action = recoveryAction
    ? chooseFailureCardAction(recoveryAction, {
        retry: Boolean(onRetry),
        retryRestoresDraft: Boolean(retryRestoresDraft),
        editAndResend: Boolean(onEditAndResend || (retryRestoresDraft && onRetry)),
        newSession: Boolean(onNewSession),
      })
    : undefined;

  const copyAction = async () => {
    if (!copyBundle) return;
    const ok = await copyDiagnostics(copyBundle, "chat", tCard("copy_fallback_prompt"));
    if (ok) toast({ kind: "success", text: tCard("copied") });
  };
  const retryAction: ChatCardAction | null = onRetry
    ? { label: t("final_fail_retry"), icon: RotateCcw, onSelect: onRetry }
    : null;
  // When Replay only restores the draft (no server replay), Edit and send runs that Replay: it brings back the
  // question with its real references and attachments, which a plain text prefill would drop (D4: the composer
  // no longer shows its own Replay button, so the card is the only way back to it).
  const editAction: ChatCardAction | null = retryRestoresDraft && onRetry
    ? { label: t("msg_resend"), onSelect: onRetry }
    : onEditAndResend
      ? { label: t("msg_resend"), onSelect: onEditAndResend }
      : null;

  // plan_blocked: the way forward is the plan itself, so the primary action resumes it on the plan card.
  const resumeAction: ChatCardAction | null =
    normalizedKind === "plan_blocked" && resumePlan?.available && !resumeRejected
      ? {
          label: t("failure_card.resume_plan"),
          icon: Play,
          busy: resumeBusy,
          busyLabel: t("failure_card.resume_plan_busy"),
          testId: "final-fail-resume-plan",
          onSelect: async () => {
            if (resumeBusy) return;
            setResumeBusy(true);
            let resumed = false;
            try {
              resumed = await resumePlan.onResume();
            } catch {
              resumed = false;
            } finally {
              setResumeBusy(false);
            }
            if (!resumed) {
              setRejectedRevision(resumePlan.revision);
              setResumeRejected(true);
            }
          },
        }
      : null;

  let primary: ChatCardAction | undefined;
  if (resumeAction) primary = resumeAction;
  else if (action === "retry" && retryAction) primary = retryAction;
  else if (action === "edit_and_resend" && editAction) primary = editAction;
  else if (action === "new_session" && onNewSession) {
    primary = { label: t("final_fail_new_session"), icon: Plus, onSelect: onNewSession };
  } else if (action === "copy_diagnostics" && copyBundle) {
    primary = { label: tCard("copy_diagnostics"), icon: Copy, onSelect: copyAction, testId: "final-fail-copy-diagnostics" };
  } else if (!action && onPrimary) {
    primary = { label: primaryLabel ?? t("final_fail_retry"), icon: RotateCcw, onSelect: onPrimary };
  }
  // The second way forward: edit after a retry, retry after an edit (spec D5 table, secondary column).
  const secondary: ChatCardAction[] = [];
  if (primary === resumeAction && editAction) secondary.push(editAction);
  else if (primary === retryAction && editAction) secondary.push(editAction);
  else if (primary === editAction && retryAction && !retryRestoresDraft && LIMIT_REASONS.has(normalizedKind)) secondary.push(retryAction);

  const copyGroup = specificModelGroup ?? group;
  const title = clarificationStage ? t(`failure_card.${clarificationStage}_title`)
    : copyGroup ? t(`failure_card.${copyGroup}_title`) : tReasons(normalizedKind);
  const answerBody = normalizedKind === "clarification_resume_unavailable" && clarificationAnswerState
    && clarificationStage !== "clarification_capture"
      ? t(clarificationAnswerState === "saved" ? "failure_card.clarification_answer_saved_body" : "failure_card.clarification_answer_unconfirmed_body") : null;
  const body = clarificationStage ? t(`failure_card.${clarificationStage}_body`)
    : answerBody ?? (copyGroup ? t(`failure_card.${copyGroup}_body`) : null);
  const stageLabel = clarificationStage ?? (phase === "stream" ? "stream" : phase === "invoke" ? "invoke" : undefined);

  return (
    <ChatCard
      intent="failure"
      className={className}
      data-testid="final-fail"
      data-state={normalizedKind}
      live={live}
      eyebrow={header ?? (document ? t("failure_card.document_eyebrow") : t("final_fail_header"))}
      meta={timeOf(safeDiagnostics?.observedAt)}
      title={modelFailure ? t("failure_card.model_assessment", { message: title }) : title}
      details={copyBundle ? { diagnostics: copyBundle, surface: "chat", location,
        content: modelFailure || stageLabel ? <>
          {modelFailure ? <div>
            <strong>{t("failure_card.provider_message_label")}</strong>
            <p className="ui-card__text">{safeDiagnostics?.providerMessage ?? t("failure_card.provider_message_unavailable")}</p>
          </div> : null}
          {stageLabel ? <dl><dt>{t("failure_card.details_stage")}</dt><dd>{t(`failure_card.stage_${stageLabel}`)}</dd></dl> : null}
        </> : undefined } : undefined}
      primary={primary}
      secondary={secondary.length ? secondary : undefined}
      error={resumeRejected ? { message: t("failure_card.resume_plan_unavailable") } : undefined}
    >
      {modelFailure && modelStatus ? <p className="ui-card__text">{t("failure_card.model_http_status", { status: modelStatus })}</p> : null}
      {rejectedParameter ? <p className="ui-card__text">{t("failure_card.model_rejected_parameter", { parameter: rejectedParameter })}</p> : null}
      {modelFailure && safeDiagnostics?.providerMessage ? <p className="ui-card__text"><strong>{t("failure_card.provider_message_label")}</strong> {safeDiagnostics.providerMessage}</p> : null}
      {body ? <p className="ui-card__text">{modelFailure ? <strong>{t("failure_card.recommended_step_label")}</strong> : null}{body}</p> : null}
      {safeDiagnostics?.stepUsage ? <p className="ui-card__text">{t("failure_card.step_usage", { ...safeDiagnostics.stepUsage })}</p> : null}
      {responsesRejected ? <p className="ui-card__text">{t("failure_card.responses_rejected_hint")}</p> : null}
      {outcomeUnknown ? <p className="ui-card__text">{t("failure_card.model_outcome_unknown")}</p> : null}
      {clarificationStage && answerBody ? <p className="ui-card__text">{answerBody}</p> : null}
      {normalizedKind === "clarification_resume_unavailable" && primary === editAction
        ? <p className="ui-card__text">{t("failure_card.new_request_notice")}</p>
        : null}
    </ChatCard>
  );
}
