"use client";

/**
 * ToolConfirmationCard — a "confirm before this action runs" request shown under
 * its waiting tool step, and the one-line record of what happened afterwards.
 *
 * The request and its outcome come only from the Turn's persisted events
 * (`confirmation_requested` / `confirmation_resolved`, plus the terminal events
 * that derive "stopped" / "ended"), so live, archived and replayed views agree.
 * The preview (arguments, email, Shopify diff) is never in those events: only the
 * requester fetches it, once, from the owner-only queue.
 *
 * Not gated by the rollout capability: records written during a rollout must still
 * render after a rollback.
 */
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { useLocale, useTranslations } from "next-intl";

import { ApiError } from "@/lib/api-client";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { formatTime } from "@/lib/locale";
import type { ToolConfirmationChoice, ToolConfirmationKind } from "@/lib/sse-events";
import {
  decideToolConfirmation,
  getToolConfirmation,
  type ToolConfirmationDetail,
} from "@/lib/tool-confirmation-api";
import {
  consumeConfirmationFocus,
  focusConfirmationCard,
  useConfirmationCardPresence,
} from "@/lib/tool-confirmation-presence";
import type { StepConfirmation, StepConfirmationOutcome } from "@/lib/turn-store";

import { EmailSendPreview, McpArgumentList, ShopifyContentPreview } from "./previews";

export interface ToolConfirmationCardProps {
  confirmation: StepConfirmation;
  /** Agent label from the trace; the server's `agent_name` wins when it is known. */
  agentName?: string;
}

/** Namespace holding each kind's existing, reviewed button labels. */
const KIND_NAMESPACE: Record<ToolConfirmationKind, string> = {
  personal_mcp: "personalMcp",
  team_mcp: "teamToolConfirmation",
  email_send: "emailSendConfirmation",
  shopify_content: "shopifyContentConfirmation",
};

const CHOICE_LABEL_KEY: Record<ToolConfirmationKind, Partial<Record<ToolConfirmationChoice, string>>> = {
  personal_mcp: { reject: "reject", approve: "approveOnce", approve_session: "approveSession" },
  team_mcp: { reject: "reject", approve: "approveOnce", approve_session: "approveSession" },
  email_send: { reject: "reject", approve: "approve" },
  shopify_content: { reject: "reject", approve: "approve" },
};

const CHOICE_ORDER: readonly ToolConfirmationChoice[] = ["reject", "approve", "approve_session"];

/** True when the viewer is the user who may decide this confirmation. */
export function useIsConfirmationRequester(confirmation: StepConfirmation | undefined): boolean {
  const viewerId = useOptionalAuthSession()?.me?.id ?? null;
  return Boolean(confirmation && viewerId && viewerId === confirmation.requesterUserId);
}

export function ToolConfirmationCard({ confirmation, agentName }: ToolConfirmationCardProps) {
  const requester = useIsConfirmationRequester(confirmation);
  if (confirmation.outcome) return <ToolConfirmationRecord confirmation={confirmation} />;
  if (!requester) return <WaitingForRequester confirmation={confirmation} />;
  return <PendingConfirmation key={confirmation.id} confirmation={confirmation} agentName={agentName} />;
}

// ── Record ────────────────────────────────────────────────────────────────

function recordKey(outcome: StepConfirmationOutcome, kind: ToolConfirmationKind): string {
  switch (outcome) {
    case "approved":
      return kind === "email_send"
        ? "recordApprovedEmail"
        : kind === "shopify_content"
          ? "recordApprovedShopify"
          : "recordApproved";
    case "approved_session":
      return "recordApprovedSession";
    case "rejected":
      return "recordRejected";
    case "expired":
      return "recordExpired";
    case "stopped":
      return "recordStopped";
    case "ended":
      return "recordEnded";
    case "unavailable":
      return "recordUnavailable";
  }
}

function recordTone(outcome: StepConfirmationOutcome): "pos" | "neg" | "muted" {
  if (outcome === "approved" || outcome === "approved_session") return "pos";
  if (outcome === "rejected") return "neg";
  return "muted";
}

export function ToolConfirmationRecord({ confirmation }: { confirmation: StepConfirmation }) {
  const t = useTranslations("toolConfirmation");
  const locale = useLocale();
  const outcome = confirmation.outcome;
  if (!outcome) return null;
  const decided = outcome === "approved" || outcome === "approved_session" || outcome === "rejected";
  const time = decided && confirmation.decidedAt ? formatTime(confirmation.decidedAt, locale) : "";
  const label = t(recordKey(outcome, confirmation.kind));
  return (
    <div
      className={`tcf tcf-record is-${recordTone(outcome)}`}
      data-tool-confirmation-id={confirmation.id}
      data-outcome={outcome}
      tabIndex={-1}
    >
      <span className="tcf-record-glyph" aria-hidden="true" />
      <span className="tcf-record-text">{time ? `${label} · ${time}` : label}</span>
    </div>
  );
}

// ── Not the requester ─────────────────────────────────────────────────────

function WaitingForRequester({ confirmation }: { confirmation: StepConfirmation }) {
  const t = useTranslations("toolConfirmation");
  return (
    <div className="tcf tcf-waiting" data-tool-confirmation-id={confirmation.id} tabIndex={-1}>
      <span className="tcf-record-glyph" aria-hidden="true" />
      <span className="tcf-record-text">{t("waitingForRequester")}</span>
    </div>
  );
}

// ── Pending, viewer is the requester ──────────────────────────────────────

type DetailState =
  | { status: "loading" }
  | { status: "ready"; detail: ToolConfirmationDetail; anchorAt: number }
  | { status: "gone" }
  | { status: "failed" };

type DecisionError = "unavailable" | "failed";

function useSecondTick(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);
  return now;
}

function formatRemaining(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

/**
 * Countdown deadline on the local monotonic-enough clock: the server's
 * `remaining_seconds` from the detail read, else the event's `timeout_seconds`
 * at receipt. Never `expires_at` against the local clock (clocks drift).
 */
export function confirmationDeadline(
  confirmation: StepConfirmation,
  detail: { remainingSeconds: number; anchorAt: number } | null,
): number | null {
  if (detail && Number.isFinite(detail.remainingSeconds)) {
    return detail.anchorAt + Math.max(0, detail.remainingSeconds) * 1000;
  }
  if (confirmation.timeoutSeconds > 0) return confirmation.receivedAt + confirmation.timeoutSeconds * 1000;
  return null;
}

function PendingConfirmation({ confirmation, agentName }: ToolConfirmationCardProps) {
  const t = useTranslations("toolConfirmation");
  const tKind = useTranslations(KIND_NAMESPACE[confirmation.kind]);
  const tTeam = useTranslations("teamToolConfirmation");
  const headingId = useId();
  const ref = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<DetailState>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  const [phase, setPhase] = useState<"idle" | "submitting" | "submitted">("idle");
  const [decisionError, setDecisionError] = useState<DecisionError | null>(null);

  useConfirmationCardPresence(confirmation.id, ref, true);

  // Arriving through the notice's View action scrolls here and focuses the card. The card never
  // takes focus on its own: it must not pull focus from the composer when it appears.
  useEffect(() => {
    if (consumeConfirmationFocus(confirmation.id)) focusConfirmationCard(confirmation.id);
  }, [confirmation.id]);

  useEffect(() => {
    let active = true;
    setState({ status: "loading" });
    getToolConfirmation(confirmation.id).then(
      (detail) => {
        if (active) setState({ status: "ready", detail, anchorAt: Date.now() });
      },
      (failure: unknown) => {
        if (!active) return;
        setState(failure instanceof ApiError && failure.status === 404 ? { status: "gone" } : { status: "failed" });
      },
    );
    return () => {
      active = false;
    };
  }, [confirmation.id, attempt]);

  const ready = state.status === "ready" ? state : null;
  const deadline = state.status === "gone"
    ? null
    : confirmationDeadline(
        confirmation,
        ready ? { remainingSeconds: ready.detail.remaining_seconds, anchorAt: ready.anchorAt } : null,
      );
  const now = useSecondTick(deadline !== null);
  const remaining = deadline === null ? null : Math.max(0, Math.ceil((deadline - now) / 1000));

  async function decide(choice: ToolConfirmationChoice) {
    if (phase !== "idle") return;
    setPhase("submitting");
    setDecisionError(null);
    try {
      await decideToolConfirmation(confirmation.id, choice);
      // The outcome is recorded only when `confirmation_resolved` arrives on the Turn.
      setPhase("submitted");
    } catch (failure) {
      if (failure instanceof ApiError && (failure.status === 404 || failure.status === 409)) {
        setDecisionError("unavailable");
        setPhase("submitted");
      } else {
        setDecisionError("failed");
        setPhase("idle");
      }
    }
  }

  const agent = (ready?.detail.agent_name || agentName || "").trim() || t("unknownAgent");
  const tool = confirmation.title || ready?.detail.title || "";
  const source = confirmation.sourceLabel || ready?.detail.source_label || null;
  const category = confirmation.category ?? ready?.detail.category ?? null;
  const choices = CHOICE_ORDER.filter((choice) =>
    (confirmation.choices.length ? confirmation.choices : (ready?.detail.choices ?? [])).includes(choice),
  );

  let headline: string;
  let risk: string;
  let countdownKey: string;
  switch (confirmation.kind) {
    case "email_send":
      headline = t("requestEmail", { agent });
      risk = tKind("warning");
      countdownKey = "countdownEmail";
      break;
    case "shopify_content":
      headline = source ? t("requestShopify", { agent, source }) : t("requestShopifyNoSource", { agent });
      risk = tKind("notice");
      countdownKey = "countdownShopify";
      break;
    case "team_mcp":
      headline = source ? t("requestCallFrom", { agent, tool, source }) : t("requestCall", { agent, tool });
      risk = tTeam(
        category === "external_write" ? "riskExternalWrite" : category === "destructive" ? "riskDestructive" : "riskUnknown",
      );
      countdownKey = "countdownSkip";
      break;
    case "personal_mcp":
      headline = source ? t("requestCallFrom", { agent, tool, source }) : t("requestCall", { agent, tool });
      risk =
        category === "external_write"
          ? t("riskExternalWrite")
          : category === "destructive"
            ? t("riskDestructive")
            : category === "unknown"
              ? t("riskUnknown")
              : tKind("confirmWarning");
      countdownKey = "countdownSkip";
      break;
  }

  let preview: ReactNode = null;
  if (ready) {
    const detail = ready.detail;
    switch (detail.kind) {
      case "personal_mcp":
      case "team_mcp":
        preview = (
          <>
            {detail.item.description ? <p className="tcf-desc">{detail.item.description}</p> : null}
            <McpArgumentList args={detail.item.arguments} />
          </>
        );
        break;
      case "email_send":
        preview = <EmailSendPreview item={detail.item} />;
        break;
      case "shopify_content":
        preview = <ShopifyContentPreview item={detail.item} />;
        break;
    }
  }

  const showButtons = ready !== null && phase !== "submitted";
  return (
    <div
      ref={ref}
      className="tcf is-pending"
      role="group"
      aria-labelledby={headingId}
      data-tool-confirmation-id={confirmation.id}
      tabIndex={-1}
    >
      <p className="tcf-head" id={headingId}>
        {headline}
      </p>
      <p className="tcf-risk">{risk}</p>
      {state.status === "loading" ? <p className="tcf-note">{t("loadingDetails")}</p> : null}
      {state.status === "gone" ? <p className="tcf-note">{t("detailsGone")}</p> : null}
      {state.status === "failed" ? (
        <div className="tcf-note" role="alert">
          <span>{t("detailsFailed")}</span>{" "}
          <button type="button" className="tcf-link" onClick={() => setAttempt((n) => n + 1)}>
            {t("retry")}
          </button>
        </div>
      ) : null}
      {preview ? <div className="tcf-preview">{preview}</div> : null}
      {remaining !== null && phase !== "submitted" ? (
        <p className="tcf-countdown">
          {remaining > 0 ? t(countdownKey, { time: formatRemaining(remaining) }) : t("timeUp")}
        </p>
      ) : null}
      {decisionError === "unavailable" ? <p role="alert">{t("decisionUnavailable")}</p> : null}
      {decisionError === "failed" ? <p role="alert">{t("decisionFailed")}</p> : null}
      {phase === "submitting" ? <p className="tcf-note" role="status">{t("submitting")}</p> : null}
      {phase === "submitted" && decisionError === null ? (
        <p className="tcf-note" role="status">{t("submitted")}</p>
      ) : null}
      {showButtons ? (
        <div className="tcf-actions">
          {choices.map((choice) => (
            <button
              key={choice}
              type="button"
              className={`tcf-btn is-${choice}`}
              disabled={phase !== "idle"}
              onClick={() => void decide(choice)}
            >
              {tKind(CHOICE_LABEL_KEY[confirmation.kind][choice] ?? choice)}
            </button>
          ))}
        </div>
      ) : null}
      {showButtons && choices.includes("approve_session") ? (
        <p className="tcf-note">{tKind("approveSessionNote")}</p>
      ) : null}
    </div>
  );
}
