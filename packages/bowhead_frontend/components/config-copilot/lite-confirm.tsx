"use client";

/**
 * Settings suggestion card (Canvas Objects Spec part 2 §9, built on the Chat Cards ChatCard).
 *
 * Presentation only. A proposed suggestion is an `input` card: one C7 change row per field (display names only), a
 * plain "Ignore" and the one primary "Apply". Settled states collapse in place to `row` cards so chat history
 * stays short: applied (Undo trailing, shown only when the change can be fully restored — D11), restored,
 * dismissed and no-longer-available. "Settings changed" is an `info` card and "Couldn't apply" a `failure`
 * card whose Retry re-applies the same suggestion.
 */

import type { ReactNode } from "react";
import { useTranslations } from "next-intl";
import { CircleX, Info, RotateCcw, WandSparkles } from "lucide-react";

import { ChangeRow } from "@/components/canvas/object/change-row";
import { ChatCard, ChatCardButton } from "@/components/ui/card";

export type LiteConfirmState = "propose" | "busy" | "applied" | "reverted" | "fail" | "ignored" | "stale" | "conflict";

export interface LiteConfirmRow {
  label: string;
  /** Null when the setting had no value before. */
  from: string | null;
  to: string;
}

export interface LiteConfirmProps {
  state: LiteConfirmState;
  /** Rows with display names. A suggestion with any field lacking one is shown as no longer available (callers pass
   *  `stale`), and an empty list never renders an apply card. */
  rows: LiteConfirmRow[];
  onApply?: () => void;
  applyLabel?: string;
  onIgnore?: () => void;
  /** Absent when the change cannot be fully restored: Undo is then hidden, never shown disabled (D11). */
  onUndo?: () => void;
  onRetry?: () => void;
  /** An Undo request is in flight. */
  undoBusy?: boolean;
  /** Explains why an applied change has no Undo (name and description edits). */
  noUndoNote?: string;
  /** Replaces the default title of a proposed suggestion. */
  title?: string;
  /** Replaces the per-field change rows of a proposed suggestion, e.g. a line diff for long text. */
  preview?: ReactNode;
  /** Replaces the generated "field changed to value" summary of an applied suggestion. */
  appliedSummary?: string;
  /** Replaces the generic failure text when the cause is known, e.g. a permission the user no longer has. */
  failBody?: string;
}

export function LiteConfirm({
  state,
  rows,
  onApply,
  applyLabel,
  onIgnore,
  onUndo,
  onRetry,
  undoBusy = false,
  noUndoNote,
  title,
  preview,
  appliedSummary,
  failBody,
}: LiteConfirmProps) {
  const t = useTranslations("settings.config_copilot_lite");
  const changes = appliedSummary ?? rows.map((row) => t("change_to", { field: row.label, value: row.to })).join(t("list_separator"));
  const bold = (chunks: ReactNode) => <b>{chunks}</b>;

  if (state === "conflict") {
    return (
      <ChatCard
        intent="info"
        eyebrow={t("settings_changed")}
        data-testid="lite-conflict"
        secondary={onIgnore ? [{ label: t("ignore"), onSelect: onIgnore }] : undefined}
      >
        <p className="ui-card__text">{t("baseline_changed")}</p>
      </ChatCard>
    );
  }
  if (state === "applied") {
    if (!onUndo && noUndoNote) {
      return (
        <ChatCard intent="success" eyebrow={t("applied")} data-testid="lite-applied">
          <p className="ui-card__text">{changes ? `${changes}${t("sentence_end")}${noUndoNote}` : noUndoNote}</p>
        </ChatCard>
      );
    }
    return (
      <ChatCard
        intent="success"
        size="row"
        data-testid="lite-applied"
        title={changes ? t.rich("applied_line", { changes, b: bold }) : <b>{t("applied")}</b>}
        trailing={onUndo ? (
          <ChatCardButton variant="plain" loading={undoBusy} disabled={undoBusy} onClick={onUndo}>
            {t("undo")}
          </ChatCardButton>
        ) : undefined}
      />
    );
  }
  if (state === "reverted") {
    return <ChatCard intent="info" size="row" icon={RotateCcw} data-testid="lite-reverted" title={t("reverted")} />;
  }
  if (state === "ignored") {
    return <ChatCard intent="info" size="row" icon={CircleX} data-testid="lite-ignored" title={t("ignored")} />;
  }
  // Nothing to review (no row has a display name): never offer an empty apply card.
  if (state === "stale" || (rows.length === 0 && (state === "propose" || state === "busy" || state === "fail"))) {
    // Non-interactive line: a proposal the server no longer has must not offer an action that is bound to fail.
    return <ChatCard intent="info" size="row" icon={Info} data-testid="lite-stale" title={t("stale")} />;
  }
  if (state === "fail") {
    return (
      <ChatCard
        intent="failure"
        eyebrow={t("apply_fail")}
        data-testid="lite-fail"
        secondary={onIgnore ? [{ label: t("ignore"), onSelect: onIgnore }] : undefined}
        primary={onRetry ? { label: t("retry"), onSelect: onRetry } : undefined}
      >
        <p className="ui-card__text">{failBody ?? t("apply_fail_body")}</p>
      </ChatCard>
    );
  }

  const busy = state === "busy";
  return (
    <ChatCard
      intent="input"
      icon={WandSparkles}
      eyebrow={t("eyebrow")}
      title={title ?? t("title")}
      data-testid="lite-propose"
      secondary={onIgnore ? [{ label: t("ignore"), onSelect: onIgnore, disabled: busy }] : undefined}
      primary={{ label: applyLabel ?? t("apply"), onSelect: () => onApply?.(), busy }}
    >
      <div className="ui-card-stack">
        {preview ?? rows.map((row) => (
          <ChangeRow
            key={row.label}
            location={row.label}
            before={row.from === null ? { kind: "empty" } : { kind: "text", text: row.from }}
            after={{ kind: "text", text: row.to }}
            beforeLabel={t("current")}
            afterLabel={t("suggested")}
            strikeBefore={false}
          />
        ))}
      </div>
    </ChatCard>
  );
}
