"use client";

/**
 * Document revision receipt (Chat Cards Spec §4): one card under the user message of a Turn sent from the
 * Canvas "Revise in chat" action, following the job through generating → ready (or not generated) in place.
 *
 * The draft's title and number come from the Turn's persisted `turn_started.document_revision`, so the live
 * card, a refresh and history show the same draft; the state comes from the Turn's own terminal outcome.
 * Older receipts (not the latest Turn) settle into one row. No task identifiers are shown.
 */

import { SquarePen } from "lucide-react";
import { useTranslations } from "next-intl";

import { ChatCard, ChatCardButton } from "@/components/ui/card";
import type { DocumentRevisionReceipt as Receipt } from "@/lib/turn-store";

export type DocumentRevisionReceiptState = "generating" | "awaiting_input" | "ready" | "failed";

interface Props {
  receipt: Receipt;
  state: DocumentRevisionReceiptState;
  /** Not the latest Turn: a finished receipt settles into one row. */
  settled?: boolean;
  /** Opens the new draft in the Canvas (absent until the draft exists and its pointer is known). */
  onOpen?: () => void;
  /** Generating: elapsed time ("42 秒"); ready / sent: the Turn's time ("14:36"). */
  meta?: string;
}

/**
 * The receipt's state from the Turn outcome, shared by the live and archived views. A failure wins; a completed
 * Turn is ready only when it produced a successor draft (its `document.draft_open` pointer), otherwise no new
 * version was made; a paused Turn waits for the user's answer; anything else is generating.
 */
export function documentRevisionReceiptState(turn: {
  running: boolean;
  failed: boolean;
  terminalStatus?: string | null;
  hasSuccessorDraft: boolean;
}): DocumentRevisionReceiptState {
  if (turn.failed) return "failed";
  if (turn.running) return "generating";
  if (turn.terminalStatus === "completed") return turn.hasSuccessorDraft ? "ready" : "failed";
  if (turn.terminalStatus === "clarification_pending") return "awaiting_input";
  if (turn.terminalStatus === "aborted" || turn.terminalStatus === "cancelled" || turn.terminalStatus === "error" || turn.terminalStatus === "failed")
    return "failed";
  return "generating";
}

export function DocumentRevisionReceipt({ receipt, state, settled = false, onOpen, meta }: Props) {
  const t = useTranslations("chat_card.document_revision");
  const { title, version } = receipt;
  const next = version + 1;

  if (state === "failed") {
    return (
      <ChatCard
        intent="failure"
        size="row"
        live="off"
        data-testid="document-revision-receipt"
        data-state="failed"
        title={<b>{t("failed_title", { title })}</b>}
        meta={t("failed_meta", { n: version })}
      />
    );
  }
  if (state === "ready" && settled) {
    return (
      <ChatCard
        intent="info"
        size="row"
        live="off"
        icon={SquarePen}
        data-testid="document-revision-receipt"
        data-state="settled"
        title={<b>{t("history_title", { title })}</b>}
        meta={t("history_meta", { from: version, to: next })}
        trailing={onOpen ? (
          <ChatCardButton variant="ghost" size="sm" onClick={onOpen}>{t("open")}</ChatCardButton>
        ) : undefined}
      />
    );
  }
  if (state === "ready") {
    return (
      <ChatCard
        intent="success"
        data-testid="document-revision-receipt"
        data-state="ready"
        eyebrow={t("ready_eyebrow", { n: next })}
        meta={meta}
        title={title}
        primary={onOpen ? { label: t("review"), variant: "secondary", onSelect: onOpen } : undefined}
      />
    );
  }
  if (state === "awaiting_input") {
    return (
      <ChatCard
        intent="info"
        icon={SquarePen}
        data-testid="document-revision-receipt"
        data-state="awaiting_input"
        eyebrow={t("sent_eyebrow")}
        meta={meta}
        title={t("draft_n", { title, n: version })}
      >
        <p className="ui-card__text">{t("sent_body")}</p>
      </ChatCard>
    );
  }
  return (
    <ChatCard
      intent="progress"
      data-testid="document-revision-receipt"
      data-state="generating"
      eyebrow={t("generating_eyebrow")}
      meta={meta}
      progress="indeterminate"
      title={t("based_on", { title, n: version })}
    >
      <p className="ui-card__text">{t("generating_body")}</p>
    </ChatCard>
  );
}
