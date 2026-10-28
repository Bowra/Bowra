"use client";

/**
 * Members' thumbs-downs on the Owner's Pending list (agent-eval alignment loop R2).
 *
 * A member's thumbs-down appears here only when they also shared that Turn with the Agent's Owner;
 * the server rechecks the share on every read. An item leaves the list when the Owner dismisses it
 * (after the usual Undo toast) or starts "Use this to improve the Agent", and disappears when the
 * member withdraws the share or removes the thumbs-down. The Turn stays a case candidate either way.
 */

import { useId } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { toast } from "@/lib/use-toast";
import { handleAgentMemberFeedback, type AgentMemberFeedback } from "@/lib/agents-api";
import { ANSWER_FEEDBACK_REASONS } from "@/lib/answer-feedback-reasons";
import { useCiFormat } from "./agent-ci-shared";

const KNOWN_REASONS = new Set<string>(ANSWER_FEEDBACK_REASONS);

export function memberFeedbackKey(agentId: string, turnId: string): string {
  return `${agentId}:member-feedback:${turnId}`;
}

export function MemberFeedbackView({
  agentId,
  agentName,
  items,
  hidden,
  onDismiss,
  onImprove,
  reload,
}: {
  agentId: string;
  agentName: string;
  items: AgentMemberFeedback[];
  /** Keys whose dismissal is waiting on its Undo toast. */
  hidden: ReadonlySet<string>;
  onDismiss: (item: AgentMemberFeedback) => void;
  /**
   * Open Coach in a new Chat with `text` staged. `markHandled` takes the item off Pending; the caller
   * runs it after any unsaved-change confirmation and before leaving the page.
   */
  onImprove?: (text: string, markHandled: () => Promise<void>) => Promise<void>;
  reload: () => Promise<void>;
}) {
  const t = useTranslations("agents_marketplace");
  const tChat = useTranslations("chat");
  const format = useCiFormat();
  const headingId = useId();
  const visible = items.filter((item) => !hidden.has(memberFeedbackKey(agentId, item.turn_id)));
  if (!visible.length) return null;

  const reasonLabel = (code: string) => tChat(`msg_feedback_reason_${KNOWN_REASONS.has(code) ? code : "other"}` as never);

  const improve = async (item: AgentMemberFeedback) => {
    if (!onImprove) return;
    const reasons = item.feedback.reason_codes.map(reasonLabel).join(t("version_list_separator"));
    const text = t("version_member_feedback_coach_prefill", {
      name: agentName,
      question: item.question,
      reasons: reasons || t("version_member_feedback_no_reasons"),
      comment: item.feedback.comment || t("version_member_feedback_no_comment"),
      turn: item.turn_id,
    });
    try {
      await onImprove(text, async () => {
        await handleAgentMemberFeedback(agentId, item.turn_id, "coach");
      });
    } catch {
      toast({ kind: "error", text: t("version_member_feedback_failed") });
      await reload();
    }
  };

  // Claude Design "Agent Eval Alignment Loop" L0-d: one row per item, one neutral type label, one
  // primary action. The comment line says "No comment" rather than leaving a gap.
  return (
    <section id="agent-pending-feedback" className="am-ci-pending" aria-labelledby={headingId}>
      <h2 id={headingId} className="am-ci-pending-head">{t("version_member_feedback_title", { count: visible.length })}</h2>
      <ul className="am-ci-pending-list">
        {visible.map((item) => {
          const reasons = item.feedback.reason_codes.map(reasonLabel);
          const comment = item.feedback.comment ? `「${item.feedback.comment}」` : t("version_member_feedback_no_comment");
          return (
            <li key={item.turn_id} className="am-ci-pending-row" data-testid="member-feedback-item">
              <p className="am-ci-pending-title">
                <span className="am-ci-pending-type">{t("version_member_feedback_label")}</span>
                <span>{item.question}</span>
              </p>
              <div className="am-ci-pending-acts">
                <Button size="sm" variant="ghost" onClick={() => onDismiss(item)}>{t("version_member_feedback_dismiss")}</Button>
                {onImprove ? <Button size="sm" variant="primary" onClick={() => void improve(item)}>{t("version_member_feedback_improve")}</Button> : null}
              </div>
              <small className="am-ci-pending-meta">
                {[item.shared_by, format.dateTime(item.feedback.given_at), ...reasons, comment].join(" · ")}
              </small>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
