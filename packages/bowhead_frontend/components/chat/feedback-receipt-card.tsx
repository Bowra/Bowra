"use client";

/**
 * agent-feedback-loop batch 5 · inline suggestion-result receipt in chat.
 *
 * The proposer suggested something in chat days earlier (`feedback_escalate`); after an admin decides, show the result **when they next
 * enter chat**, right there, rather than waiting unread in the notification center (observed on 116: 139 notifications, 0 clicks).
 *
 * This receipt **informs**, not approves: its only action, “Got it”, marks that notification read so it will not reappear.
 * No “view details” link—the inbox lives under team settings, inaccessible to ordinary proposers; such a link would be dead.
 *
 * Chat Cards Spec: one settled info row per outcome (the same receipt shape as the memory rows).
 */

import { useTranslations } from "next-intl";

import { ChatCard, ChatCardButton } from "@/components/ui/card";
import type { FeedbackReceipt } from "@/lib/feedback-candidates-api";

interface Props {
  items: FeedbackReceipt[];
  actingId: string | null;
  acknowledge: (notificationId: string) => Promise<boolean>;
}

/**
 * Terminal states with receipt copy.
 *
 * ★ Use an allowlist rather than constructing `t(`state_${s}`)`: next-intl does not throw on a missing key; it
 * renders the **bare message key** (`feedback_receipt.state_superseded`) and logs `MISSING_MESSAGE`.
 * The allowlist guards future terminal states and dirty data.
 */
const _STATES = ["accepted", "rejected", "expired"] as const;
type ReceiptState = (typeof _STATES)[number];

function _stateOf(s: string): ReceiptState | null {
  return (_STATES as readonly string[]).includes(s) ? (s as ReceiptState) : null;
}

/**
 * Decision times have day precision only. ICU `{when, date, long}` formats with the provider's locale.
 * Return null on parse failure: next-intl renders the bare key for an invalid Date, so omit the meta instead.
 */
function _day(iso: string): Date | null {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function FeedbackReceiptCard({ items, actingId, acknowledge }: Props) {
  const t = useTranslations("feedback_receipt");
  const rows = items
    .map((r) => ({ r, state: _stateOf(r.final_state) }))
    .filter((x): x is { r: FeedbackReceipt; state: ReceiptState } => x.state !== null);
  if (rows.length === 0) return null;
  return (
    <div className="ui-card-stack" data-testid="feedback-receipt-card" aria-label={t("aria_label")} role="group">
      {rows.map(({ r, state }) => {
        const when = _day(r.decided_at);
        return (
          <ChatCard
            key={r.notification_id}
            intent={state === "accepted" ? "success" : "info"}
            size="row"
            title={t(`state_${state}`, { target: r.target_label })}
            meta={when ? ` · ${t(r.mine ? "meta_mine" : "meta_cosigned", { when })}` : undefined}
            trailing={
              <ChatCardButton
                disabled={actingId !== null}
                onClick={() => void acknowledge(r.notification_id)}
              >
                {t("ack")}
              </ChatCardButton>
            }
          />
        );
      })}
    </div>
  );
}
