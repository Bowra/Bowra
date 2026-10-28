"use client";

/**
 * message-actions — message-level action bar after a turn answers.
 *
 * chat-ux-2026-08 batch F. On 116, answered messages had **no action entries**:
 * copying conclusions elsewhere required manual selection (also selecting badges and step text),
 * while requesting another answer required retyping the question.
 *
 * The original design's「有帮助 / 没帮助」thumbs were removed because nothing consumed them. The
 * 2026-10-19 decision brings back a **thumbs-down only**, on team-Agent answers only, because the
 * Agent's Owner now consumes it (workbench Pending list and evaluation-case candidates):
 *   - Copy → existing `lib/clipboard.ts` copyTextWithFallback
 *   - Edit and send → restore the original question to the composer for users to recheck references and attachments
 *   - Thumbs-down → `AnswerFeedbackDialog` (reasons, comment, and the share consent when someone else
 *     owns the Agent); the receipt toast has Undo, and pressing the button again removes the
 *     thumbs-down together with the share (Claude Design "Agent Eval Alignment Loop" L0-a…L0-c)
 *   - "⋯" → "Share with the owner as an eval case" / withdraw, only when someone else owns the Agent
 * Both read one per-conversation listing (`useTurnEvalShare`), so ordinary answers keep the two-button
 * bar and the menu and the dialog's checkbox always agree.
 *
 * Visibility: desktop hover / keyboard focus (in `.bh-msg-actions` CSS),
 * always visible on touch (`hover: none`); design brief notes long-press conflicts with text-selection gestures.
 */
import { useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { stripCiteMarkers, stripLegacy, stripProvenanceTags } from "@/lib/citation-marks";
import { copyTextWithFallback } from "@/lib/clipboard";
import { stripFigureAnchors } from "@/lib/md";
import {
  Check as LucideCheck,
  Copy as LucideCopy,
  Pencil as LucidePencil,
  ThumbsDown as LucideThumbsDown,
} from "lucide-react";
import { LucideGlyph } from "@/components/icon-refresh";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import { ApiError } from "@/lib/api-client";
import { dismissToast, toast } from "@/lib/use-toast";
import { useTurnEvalShare } from "@/lib/use-turn-eval-share";
import { AnswerFeedbackDialog, type AnswerFeedbackValue } from "./answer-feedback-dialog";

/** Copy the **human-readable body**, removing machine markers rendered as badges / figure slots or hidden
 *  (`[figure:]`, `[cite:N]`, `[data:art_*]`, retired markers, provenance tags). Badges have no clickable sources
 *  outside the page and are noise; KB answers also carry `[cite:N]` since citations shipped (116 1.3.51 observation). */
export function answerTextForCopy(text: string): string {
  const plain = stripCiteMarkers(stripProvenanceTags(stripLegacy(stripFigureAnchors(text)))).replace(/\[data:art_[A-Za-z0-9_-]+\]/g, "");
  return plain.replace(/[ \t]+([，。；：、！？）)】」,.;:!?])/g, "$1").trim();
}

export interface MessageActionsProps {
  /** Body to copy. Empty hides「复制」: BI/report answers live on canvas, with no chat body. */
  answerText?: string | null;
  /** Original question for user editing. Empty hides this button. */
  resendText?: string | null;
  onResend?: (text: string) => void;
  /** Latest answer in the conversation. Appearance refresh keeps its actions visible on pointer devices (older answers require hover/focus). */
  latest?: boolean;
  /** Conversation and Turn of this answer: enable sharing it with the answering Agent's Owner when allowed. */
  sessionId?: string | null;
  turnId?: string | null;
}

export function MessageActions({ answerText, resendText, onResend, latest = false, sessionId, turnId }: MessageActionsProps) {
  const t = useTranslations("chat");
  const evalShare = useTurnEvalShare(sessionId, turnId);
  // Icon-only buttons (the text is the accessible name / tooltip); "edit and send" uses a pencil icon.
  const [copied, setCopied] = useState(false);
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  // The receipt toast whose Undo would take back the current thumbs-down; closed once it no longer applies.
  const receiptToast = useRef<string | null>(null);

  // Copy **human-readable text** (see answerTextForCopy).
  const copyText = answerTextForCopy(answerText ?? "");
  const retryText = (resendText ?? "").trim();
  const canCopy = copyText.length > 0;
  const canResend = Boolean(onResend) && retryText.length > 0;

  // With no action, render no bar; an empty action bar is worse than none.
  if (!canCopy && !canResend && !evalShare) return null;

  async function toggleEvalShare() {
    if (!evalShare) return;
    const agent = evalShare.item.agent_name;
    try {
      const shared = await evalShare.toggle();
      toast({ kind: "success", text: shared ? t("msg_eval_shared", { agent }) : t("msg_eval_unshared") });
    } catch (cause) {
      toast({ kind: "error", text: shareErrorText(cause) });
    }
  }

  const markedBad = Boolean(evalShare?.item.feedback);

  function shareErrorText(cause: unknown) {
    return cause instanceof ApiError && cause.status === 404 ? t("msg_eval_share_unavailable") : t("msg_eval_share_failed");
  }

  function feedbackErrorText(cause: unknown) {
    return cause instanceof ApiError && cause.status === 404 ? t("msg_feedback_unavailable") : t("msg_feedback_failed");
  }

  /** Clear the thumbs-down and, when `withdraw`, the share it came with (Claude Design L0-c: undo takes both back). */
  async function clearFeedback(withdraw: boolean): Promise<boolean> {
    if (!evalShare) return false;
    if (receiptToast.current) dismissToast(receiptToast.current, "action");
    receiptToast.current = null;
    try {
      await evalShare.setFeedback(null);
      if (withdraw) await evalShare.setShared(false);
      return true;
    } catch (cause) {
      toast({ kind: "error", text: feedbackErrorText(cause) });
      return false;
    }
  }

  async function submitFeedback(value: AnswerFeedbackValue) {
    if (!evalShare) return;
    const wasShared = evalShare.item.shared;
    try {
      await evalShare.setFeedback({ reason_codes: value.reason_codes, comment: value.comment });
    } catch (cause) {
      const unavailable = cause instanceof ApiError && cause.status === 404;
      toast({ kind: "error", text: feedbackErrorText(cause) });
      if (unavailable) setFeedbackOpen(false);
      return;
    }
    setFeedbackOpen(false);
    let shared = evalShare.shareable && wasShared;
    if (evalShare.shareable && value.share !== wasShared) {
      // The checkbox is the same consent as the "⋯" menu, written through the same route.
      try {
        shared = await evalShare.setShared(value.share);
      } catch (cause) {
        toast({ kind: "error", text: `${t("msg_feedback_saved")} ${shareErrorText(cause)}` });
        return;
      }
    }
    const text = shared ? t("msg_feedback_saved_shared") : wasShared ? t("msg_feedback_saved_unshared") : t("msg_feedback_saved");
    receiptToast.current = toast({
      kind: "info",
      undo: true,
      text,
      // Undo takes back this thumbs-down and a share this dialog just added; an earlier share stays.
      action: { label: t("msg_feedback_undo"), onAction: () => void clearFeedback(shared && !wasShared) },
    });
  }

  async function removeFeedback() {
    if (!evalShare) return;
    // Pressing the button again takes back the thumbs-down and the share with the owner (L0-c).
    const withdraw = evalShare.shareable && evalShare.item.shared;
    if (await clearFeedback(withdraw)) {
      toast({ kind: "info", text: withdraw ? t("msg_feedback_removed_unshared") : t("msg_feedback_removed") });
    }
  }

  async function doCopy() {
    const ok = await copyTextWithFallback(copyText, t("msg_copy"));
    if (!ok) return; // Prompt fallback is not a successful copy; never falsely claim「已复制」.
    setCopied(true);
    setTimeout(() => setCopied(false), 1600);
  }

  return (
    <div className="bh-msg-actions" data-testid="message-actions" data-latest={latest ? "1" : undefined}>
      {canCopy ? (
        <button
          type="button"
          className="bh-msg-actions__btn"
          data-testid="msg-action-copy"
          data-state={copied ? "copied" : undefined}
          onClick={doCopy}
          aria-label={copied ? t("msg_copied") : t("msg_copy")}
          title={!copied ? t("msg_copy") : undefined}
        >
          {copied ? (
            <LucideGlyph icon={LucideCheck} size={13} aria-hidden />
          ) : (
            <LucideGlyph icon={LucideCopy} size={13} aria-hidden />
          )}
          <span>{copied ? t("msg_copied") : t("msg_copy")}</span>
        </button>
      ) : null}

      {canResend ? (
        <button
          type="button"
          className="bh-msg-actions__btn"
          data-testid="msg-action-resend"
          onClick={() => onResend?.(retryText)}
          aria-label={t("msg_resend")}
          title={t("msg_resend_hint")}
        >
          <LucideGlyph icon={LucidePencil} size={13} aria-hidden />
          <span>{t("msg_resend")}</span>
        </button>
      ) : null}

      {evalShare && feedbackOpen ? (
        <AnswerFeedbackDialog
          ownerName={evalShare.item.owner_name ?? null}
          canShare={evalShare.shareable}
          shared={evalShare.item.shared}
          busy={evalShare.busy}
          onCancel={() => setFeedbackOpen(false)}
          onSubmit={(value) => void submitFeedback(value)}
        />
      ) : null}

      {evalShare?.shareable ? (
        <RowActionsMenu
          busy={evalShare.busy}
          busyLabel={t("msg_eval_share_busy")}
          actions={[
            {
              id: "eval-share",
              label: evalShare.item.shared ? t("msg_eval_unshare") : t("msg_eval_share"),
              onSelect: () => void toggleEvalShare(),
            },
          ]}
        />
      ) : null}

      {evalShare ? (
        <button
          type="button"
          className="bh-msg-actions__btn"
          data-testid="msg-action-thumbs-down"
          aria-pressed={markedBad}
          aria-label={markedBad ? t("msg_feedback_down_remove") : t("msg_feedback_down")}
          title={markedBad ? t("msg_feedback_down_remove") : t("msg_feedback_down")}
          disabled={evalShare.busy}
          onClick={() => (markedBad ? void removeFeedback() : setFeedbackOpen(true))}
        >
          <LucideGlyph icon={LucideThumbsDown} size={13} aria-hidden />
          <span>{markedBad ? t("msg_feedback_down_remove") : t("msg_feedback_down")}</span>
        </button>
      ) : null}
    </div>
  );
}
