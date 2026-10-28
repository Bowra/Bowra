"use client";

/**
 * Thumbs-down dialog for an answer a team Agent gave (Claude Design "Agent Eval Alignment Loop" L0-b).
 * Everything is optional: reasons as shared checkboxes in two columns, a comment, and, only when
 * someone else owns the Agent, a consent checkbox that shares the Turn with that owner as an
 * evaluation case. The consent starts from the Turn's current share state (unchecked unless the
 * member already shared it from the "⋯" menu) and writes through the same share route as that menu.
 */

import { useId, useState } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { ANSWER_FEEDBACK_REASONS } from "@/lib/answer-feedback-reasons";

export const FEEDBACK_COMMENT_MAX = 500;

export interface AnswerFeedbackValue {
  reason_codes: string[];
  comment: string | null;
  share: boolean;
}

export function AnswerFeedbackDialog({
  ownerName,
  canShare,
  shared,
  busy,
  onCancel,
  onSubmit,
}: {
  /** The Agent owner's name, shown under the consent; null when unknown. */
  ownerName: string | null;
  /** Someone else owns the Agent, so the Turn can be shared with them. */
  canShare: boolean;
  /** Current share state of this Turn. */
  shared: boolean;
  busy: boolean;
  onCancel: () => void;
  onSubmit: (value: AnswerFeedbackValue) => void;
}) {
  const t = useTranslations("chat");
  const tCommon = useTranslations("common");
  const introId = useId();
  const commentId = useId();
  const consentHintId = useId();
  const [reasons, setReasons] = useState<string[]>([]);
  const [comment, setComment] = useState("");
  const [share, setShare] = useState(shared);

  const toggleReason = (code: string, checked: boolean) => setReasons((current) => (
    checked ? [...current.filter((item) => item !== code), code] : current.filter((item) => item !== code)
  ));
  const submit = () => onSubmit({
    // Display order, whatever order they were ticked in.
    reason_codes: ANSWER_FEEDBACK_REASONS.filter((code) => reasons.includes(code)),
    comment: comment.trim() || null,
    share: canShare && share,
  });

  return (
    <Dialog
      title={t("msg_feedback_dialog_title")}
      describedBy={introId}
      onClose={onCancel}
      busy={busy}
      footer={(
        <>
          <Button onClick={onCancel} disabled={busy}>{tCommon("cancel")}</Button>
          <Button variant="primary" onClick={submit} disabled={busy}>{t("msg_feedback_submit")}</Button>
        </>
      )}
    >
      <div className="bh-feedback-dialog">
        <p id={introId} className="bh-feedback-dialog__intro">{t("msg_feedback_dialog_intro")}</p>
        <fieldset className="bh-feedback-dialog__reasons">
          <legend className="settings-field__label">
            {t("msg_feedback_reasons_label")}<span className="bh-feedback-dialog__optional">{t("msg_feedback_optional")}</span>
          </legend>
          <div className="bh-feedback-dialog__grid">
            {ANSWER_FEEDBACK_REASONS.map((code) => (
              <label key={code} className="bh-feedback-dialog__option">
                <input
                  type="checkbox"
                  name="answer_feedback_reason"
                  value={code}
                  checked={reasons.includes(code)}
                  disabled={busy}
                  onChange={(event) => toggleReason(code, event.target.checked)}
                />
                <span>{t(`msg_feedback_reason_${code}` as never)}</span>
              </label>
            ))}
          </div>
        </fieldset>
        <div className="settings-field">
          <label className="settings-field__label" htmlFor={commentId}>
            {t("msg_feedback_comment_label")}<span className="bh-feedback-dialog__optional">{t("msg_feedback_optional")}</span>
          </label>
          <textarea
            id={commentId}
            name="answer_feedback_comment"
            rows={3}
            maxLength={FEEDBACK_COMMENT_MAX}
            disabled={busy}
            value={comment}
            placeholder={t("msg_feedback_comment_placeholder")}
            onChange={(event) => setComment(event.target.value)}
          />
        </div>
        {canShare ? (
          <label className="bh-feedback-dialog__option bh-feedback-dialog__consent">
            <input
              type="checkbox"
              name="answer_feedback_share"
              checked={share}
              disabled={busy}
              aria-describedby={consentHintId}
              onChange={(event) => setShare(event.target.checked)}
            />
            <span>
              {t("msg_feedback_consent")}
              <small id={consentHintId}>
                {ownerName ? t("msg_feedback_consent_owner", { owner: ownerName }) : t("msg_feedback_consent_withdraw")}
              </small>
            </span>
          </label>
        ) : null}
      </div>
    </Dialog>
  );
}
