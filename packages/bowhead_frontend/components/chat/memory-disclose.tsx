"use client";

/**
 * Batch 1c · **disclosure band** of the per-turn memory receipt (design B).
 *
 * ★★ Its purpose: when this turn wrote memory without a card (zero-click capture or the Lead's explicit
 * `remember`), the user clicked nothing. Zero-click + undo is justified only if the user immediately knows
 * and can immediately undo. It also states, once, when an explicit request was not remembered.
 *
 * Chat Cards Spec §2: no pending decision, so every outcome is a settled row card — saved as a personal
 * preference (Undo, View in Memory), undone, not remembered (only for an explicit request, D7), or a failed
 * undo with Retry. Every row renders server state from `GET /memory/turn-receipt`, so an undone entry still
 * reads "Undone" after a refresh.
 */

import { RotateCcw } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState, type ReactNode } from "react";

import { ChatCard, ChatCardButton } from "@/components/ui/card";
import { appPath } from "@/lib/base-path";
import { personalSettingsPath } from "@/lib/personal-settings-paths";
import type {
  TurnMemoryExplicitRequest,
  TurnMemoryReceiptEntry,
} from "@/lib/memory-api";

/**
 * Refusal codes the backend may name (`memory_turn_requests.DISPLAYABLE_REASONS`). Each has a
 * `memory_card.not_remembered_reason.<code>` string in every locale; any other code renders only the
 * generic 「没有记住」 chip.
 */
export const NOT_REMEMBERED_REASONS = [
  "language_is_a_setting",
  "one_off",
  "no_attribution",
  "contains_secret",
  "locked",
  "entry_locked",
  "trigger_language_setting",
  "draft_says_not_lasting",
  "rate_limited",
  "draft_too_long",
  "kind_not_allowed",
  "draft_echoed_sanitizer_marker",
  "key_too_long",
  "blocked_by_stronger_entry",
] as const;

type NotRememberedReason = (typeof NOT_REMEMBERED_REASONS)[number];

function isNotRememberedReason(
  reason: string | null | undefined,
): reason is NotRememberedReason {
  return (NOT_REMEMBERED_REASONS as readonly string[]).includes(reason ?? "");
}

interface Props {
  /** Entries without a candidate card (origin `remember_tool` or `capture`). */
  entries: TurnMemoryReceiptEntry[];
  /**
   * This turn's structured explicit request. Only `not_remembered` renders here; remembered and
   * candidate outcomes are already visible as entries or candidate cards.
   */
  explicitRequest?: TurnMemoryExplicitRequest | null;
  // ⚠ Deliberately no settings link for the language case: the answer-language setting (`output_language`)
  // has no control on the settings page. Changes happen in chat (say「以后都用英文回答」and Lead calls
  // config_propose to persist it directly, measured on 116 on 2026-09-14). There is nowhere to navigate.
  undo: (entryId: string) => Promise<boolean>;
  /**
   * True when this turn already has a pending candidate or a created entry (any origin). The user's
   * sentence then has a landing place, so a 「没有记住」 line from another path would contradict it.
   */
  hasLandedMemory?: boolean;
}

export function MemoryDisclose({
  entries,
  explicitRequest,
  undo,
  hasLandedMemory = false,
}: Props) {
  const t = useTranslations("memory_card");
  const [undoing, setUndoing] = useState<string | null>(null);
  const [undoFailed, setUndoFailed] = useState<Record<string, boolean>>({});
  // D7: a "not remembered" row appears only when this turn explicitly asked to remember (structured request).
  const notRemembered =
    explicitRequest?.outcome === "not_remembered" && !hasLandedMemory;
  if (entries.length === 0 && !notRemembered) return null;

  const bold = (chunks: ReactNode) => <b>{chunks}</b>;
  const memoryLink = (
    <ChatCardButton variant="ghost" href={appPath(personalSettingsPath("memory"))}>
      {t("view_in_memory")}
    </ChatCardButton>
  );
  const runUndo = (id: string) => {
    setUndoing(id);
    setUndoFailed((prev) => ({ ...prev, [id]: false }));
    void undo(id)
      .then((ok) => {
        if (!ok) setUndoFailed((prev) => ({ ...prev, [id]: true }));
      })
      .finally(() => setUndoing(null));
  };

  return (
    <div className="ui-card-stack" data-testid="memory-disclose">
      {notRemembered ? (
        <ChatCard
          intent="info"
          size="row"
          data-testid="memory-disclose-notice"
          title={
            isNotRememberedReason(explicitRequest?.reason)
              ? t.rich("settled_not_remembered", {
                  b: bold,
                  reason: t(`not_remembered_reason.${explicitRequest.reason}`),
                })
              : <b>{t("not_remembered")}</b>
          }
        />
      ) : null}
      {entries.map((e) => {
        const undone = e.state === "undone";
        if (!undone && undoFailed[e.id]) {
          return (
            <ChatCard
              key={e.id}
              intent="failure"
              size="row"
              data-testid={`memory-disclose-entry-${e.id}`}
              data-state="undo_failed"
              title={t.rich("undo_failed_row", { b: bold })}
              trailing={
                <ChatCardButton
                  variant="secondary"
                  loading={undoing === e.id}
                  disabled={undoing === e.id}
                  data-testid={`memory-disclose-undo-${e.id}`}
                  onClick={() => runUndo(e.id)}
                >
                  {t("retry")}
                </ChatCardButton>
              }
            />
          );
        }
        return (
          <ChatCard
            key={e.id}
            intent={undone ? "info" : "success"}
            size="row"
            icon={undone ? RotateCcw : undefined}
            data-testid={`memory-disclose-entry-${e.id}`}
            data-state={e.state}
            title={
              undone
                ? t.rich("settled_undone", { b: bold })
                : e.display_value
                  ? t.rich("settled_disclosed", { b: bold, text: e.display_value })
                  // The key is an internal identifier: without a display value say what happened, not the key.
                  : t.rich("settled_disclosed_plain", { b: bold })
            }
            trailing={
              <>
                {e.undoable && !undone ? (
                  <ChatCardButton
                    loading={undoing === e.id}
                    disabled={undoing === e.id}
                    data-testid={`memory-disclose-undo-${e.id}`}
                    onClick={() => runUndo(e.id)}
                  >
                    {t("undo")}
                  </ChatCardButton>
                ) : null}
                {memoryLink}
              </>
            }
          />
        );
      })}
    </div>
  );
}
