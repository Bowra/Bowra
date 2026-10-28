"use client";

/**
 * Memory receipt (Chat Cards Spec §2, D7/D8): a pending memory candidate is a "needs your input" card
 * (Remember / Just this time / Change it); its outcome settles in place as a row — remembered (with Undo
 * and View in Memory), just this time, undone (a state of its own, never the "once" copy), or a failed undo
 * with Retry. Failures stay inside the card; nothing is reported through toasts.
 *
 * The candidates API drives the pending cards, not turn events; an approval removes the candidate, so the
 * settled rows come from the per-turn receipt entries (server-derived, identical after a refresh) or, for an
 * approval that returned no entry and for "just this time", from local state.
 */

import { Brain, RotateCcw } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState, type ReactNode } from "react";

import { ChatCard, ChatCardButton } from "@/components/ui/card";
import { useConfirmDialog } from "@/components/ui/confirm-dialog";
import { appPath } from "@/lib/base-path";
import { errorDiagnosticsFrom } from "@/lib/error-presentation";
import {
  deleteEntry,
  type PendingCandidate,
  type TurnMemoryReceiptEntry,
} from "@/lib/memory-api";
import { useMemoryScopeLabel, withoutMemoryIds } from "@/lib/memory-display";
import { personalSettingsPath } from "@/lib/personal-settings-paths";
import type { CandidateDecision } from "@/lib/use-memory-candidates";

interface Props {
  items: PendingCandidate[];
  actingId: string | null;
  decide: (
    cid: string,
    action: "remember" | "dismiss",
  ) => Promise<CandidateDecision>;
  /** "Change it". Omit to hide this button. */
  redraft?: (cid: string, comment: string) => Promise<CandidateDecision>;
  /**
   * Current turn ID. ★ **Clear terminal state** when turns change: this component has one instance per
   * session (held atop TurnContainer, following `latestDoneTurnId`); without the reset, turn 1's
   * "Remembered" would stay below every later turn. Intrinsic to the component, not a call-site `key`.
   */
  turnId?: string | null;
  /** Receipt entries approved from this turn's candidates (`origin === "candidate"`). */
  entries?: TurnMemoryReceiptEntry[];
  /** Undo a receipt entry (marks it `undone` in the shared receipt state). */
  undoEntry?: (entryId: string) => Promise<boolean>;
}

type CardState =
  | { kind: "pending"; error?: { message: string; cause?: unknown } }
  | { kind: "revising"; comment: string; error?: string }
  | {
      kind: "settled";
      as: "remembered" | "once";
      text: string;
      /** Written entry ID; absent means no undo target, so hide the button. */
      entryId?: string | null;
      /** Local fallback only; with a receipt entry, its server-derived `state` decides. */
      undone?: boolean;
    };

/** What a settled row shows, after merging the receipt entry over local state. */
type SettledView = {
  tone: "remembered" | "undone" | "once";
  text: string;
  undoTarget: string | null;
};

export function MemoryCandidateCard({
  items,
  actingId,
  decide,
  redraft,
  turnId,
  entries = [],
  undoEntry,
}: Props) {
  const t = useTranslations("memory_card");
  const scopeLabel = useMemoryScopeLabel();
  const { confirm, dialog: confirmDialog } = useConfirmDialog();
  const [states, setStates] = useState<Record<string, CardState>>({});
  const [undoing, setUndoing] = useState<string | null>(null);
  const [undoFailed, setUndoFailed] = useState<Record<string, boolean>>({});
  const lastTurnRef = useRef<string | null | undefined>(turnId);
  const reviseIdBase = useId().replaceAll(":", "");
  useEffect(() => {
    if (lastTurnRef.current === turnId) return;
    lastTurnRef.current = turnId;
    setStates({});
    setUndoFailed({});
  }, [turnId]);
  const stateOf = (cid: string): CardState => states[cid] ?? { kind: "pending" };
  const setState = (cid: string, next: CardState) =>
    setStates((prev) => ({ ...prev, [cid]: next }));

  const entryByCandidate = new Map(
    entries
      .filter((e) => e.origin === "candidate" && e.candidate_id)
      .map((e) => [e.candidate_id as string, e]),
  );
  // ★ Settled rows must stay visible; approval removes the candidate from `items`. After a refresh there is
  // no local state: the receipt entries carry the settled rows.
  const settledIds = Array.from(
    new Set([
      ...Object.entries(states)
        .filter(([, v]) => v.kind === "settled")
        .map(([cid]) => cid),
      ...entryByCandidate.keys(),
    ]),
  );
  if (items.length === 0 && settledIds.length === 0) return null;

  const settledView = (cid: string): SettledView | null => {
    const local = stateOf(cid);
    const entry = entryByCandidate.get(cid);
    if (entry) {
      const undone = entry.state === "undone";
      return {
        tone: undone ? "undone" : "remembered",
        text: entry.display_value ?? (local.kind === "settled" ? local.text : entry.key),
        undoTarget: entry.undoable && !undone ? entry.id : null,
      };
    }
    if (local.kind !== "settled") return null;
    if (local.as === "once") return { tone: "once", text: local.text, undoTarget: null };
    return {
      tone: local.undone ? "undone" : "remembered",
      text: local.text,
      undoTarget: local.undone ? null : (local.entryId ?? null),
    };
  };

  const onDecide = async (cid: string, action: "remember" | "dismiss") => {
    const candidate = items.find((item) => item.id === cid);
    if (
      action === "remember" &&
      candidate?.default_target?.requires_confirmation &&
      !(await confirm({
        title: t("confirm_global_title"),
        description: t("confirm_global"),
        confirmLabel: t("remember"),
        tone: "primary",
      }))
    ) {
      return;
    }
    const text = candidateText(candidate ?? ({} as PendingCandidate));
    const result = await decide(cid, action);
    if (!result.ok) {
      // ★ Policy rejection ≠ temporary failure: retrying write-gate-rejected content can never succeed,
      // so give the real reason (users can revise wording or choose "Just this time").
      setState(cid, {
        kind: "pending",
        error: {
          message: result.refusal
            ? t(`refused_${result.refusal}`)
            : action === "remember"
              ? t("remember_failed")
              : t("action_failed"),
          cause: result.refusal ? undefined : result.error,
        },
      });
      return;
    }
    // ★ The card settles in place as a row, keeping the receipt and "Undo" together.
    setState(cid, {
      kind: "settled",
      as: action === "remember" ? "remembered" : "once",
      text,
      entryId: result.ok ? result.entryId : null,
    });
  };

  const onUndo = async (cid: string, entryId: string) => {
    setUndoing(cid);
    setUndoFailed((prev) => ({ ...prev, [cid]: false }));
    try {
      if (entryByCandidate.has(cid) && undoEntry) {
        // Shared receipt state: the hook marks the entry `undone`, as the server derives after a refresh.
        if (!(await undoEntry(entryId))) setUndoFailed((prev) => ({ ...prev, [cid]: true }));
        return;
      }
      const local = stateOf(cid);
      try {
        await deleteEntry(entryId);
        // ★ Keep `as: "remembered"`: the undone row is its own state, never the "once" copy.
        if (local.kind === "settled") setState(cid, { ...local, undone: true });
      } catch {
        setUndoFailed((prev) => ({ ...prev, [cid]: true }));
      }
    } finally {
      setUndoing(null);
    }
  };

  const onRedraft = async (cid: string) => {
    const st = stateOf(cid);
    if (st.kind !== "revising" || !redraft) return;
    const comment = st.comment.trim();
    if (!comment) {
      // Rerunning with empty feedback returns nearly identical content.
      setState(cid, { ...st, error: t("revise_empty") });
      return;
    }
    const result = await redraft(cid, comment);
    if (!result.ok) {
      setState(cid, {
        ...st,
        error: result.refusal ? t(`refused_${result.refusal}`) : t("action_failed"),
      });
      return;
    }
    setState(cid, { kind: "pending" });
  };

  const bold = (chunks: ReactNode) => <b>{chunks}</b>;
  const memoryLink = (
    <ChatCardButton variant="ghost" href={appPath(personalSettingsPath("memory"))}>
      {t("view_in_memory")}
    </ChatCardButton>
  );

  const settledRow = (cid: string, view: SettledView) => {
    if (undoFailed[cid] && view.undoTarget) {
      return (
        <ChatCard
          key={cid}
          intent="failure"
          size="row"
          data-testid={`memory-candidate-settled-${cid}`}
          data-state="undo_failed"
          title={t.rich("undo_failed_row", { b: bold })}
          trailing={
            <ChatCardButton
              variant="secondary"
              loading={undoing === cid}
              disabled={undoing === cid}
              data-testid={`memory-candidate-undo-${cid}`}
              onClick={() => void onUndo(cid, view.undoTarget as string)}
            >
              {t("retry")}
            </ChatCardButton>
          }
        />
      );
    }
    if (view.tone === "once") {
      return (
        <ChatCard
          key={cid}
          intent="info"
          size="row"
          data-testid={`memory-candidate-settled-${cid}`}
          data-state="once"
          title={t.rich("settled_once", { b: bold })}
        />
      );
    }
    if (view.tone === "undone") {
      return (
        <ChatCard
          key={cid}
          intent="info"
          size="row"
          icon={RotateCcw}
          data-testid={`memory-candidate-settled-${cid}`}
          data-state="undone"
          title={t.rich("settled_undone", { b: bold })}
          trailing={memoryLink}
        />
      );
    }
    return (
      <ChatCard
        key={cid}
        intent="success"
        size="row"
        data-testid={`memory-candidate-settled-${cid}`}
        data-state="remembered"
        title={t.rich("settled_remembered", { b: bold, text: view.text })}
        trailing={
          <>
            {/* ★ Undo only with a **real target**: without an entry id the click would fail or fake success. */}
            {view.undoTarget ? (
              <ChatCardButton
                loading={undoing === cid}
                disabled={undoing === cid}
                data-testid={`memory-candidate-undo-${cid}`}
                onClick={() => void onUndo(cid, view.undoTarget as string)}
              >
                {t("undo")}
              </ChatCardButton>
            ) : null}
            {memoryLink}
          </>
        }
      />
    );
  };

  const pendingCard = (c: PendingCandidate) => {
    const st = stateOf(c.id);
    const busy = actingId !== null;
    const reason = withoutMemoryIds(c.display_reason ?? c.reason);
    const body = (
      <>
        <p className="ui-card__text">{candidateText(c)}</p>
        {reason ? <p className="ui-card__note">{reason}</p> : null}
      </>
    );
    if (st.kind === "revising") {
      const inputId = `${reviseIdBase}-${c.id}`;
      return (
        <ChatCard
          key={c.id}
          intent="input"
          icon={Brain}
          data-testid={`memory-candidate-${c.id}`}
          eyebrow={t("eyebrow")}
          title={t("ask_title")}
          error={st.error ? { message: st.error } : undefined}
          secondary={[{ label: t("revise_cancel"), onSelect: () => setState(c.id, { kind: "pending" }), disabled: busy }]}
          primary={{
            label: t("revise_submit"),
            onSelect: () => onRedraft(c.id),
            busy,
            testId: "memory-candidate-redraft",
          }}
        >
          {body}
          <label className="ui-card__sr-only" htmlFor={inputId}>
            {t("revise_label")}
          </label>
          <textarea
            id={inputId}
            className="ui-input ui-card-textarea"
            rows={2}
            value={st.comment}
            placeholder={t("revise_placeholder")}
            data-testid={`memory-candidate-revise-input-${c.id}`}
            onChange={(e) => setState(c.id, { kind: "revising", comment: e.target.value })}
          />
          {/* ★ Explicit "changing it is not a rejection": the backend writes no suppression. */}
          <p className="ui-card__note">{t("revise_note")}</p>
        </ChatCard>
      );
    }
    const error = st.kind === "pending" ? st.error : undefined;
    const diagnostics = error?.cause !== undefined ? errorDiagnosticsFrom(error.cause) : undefined;
    return (
      <ChatCard
        key={c.id}
        intent="input"
        icon={Brain}
        data-testid={`memory-candidate-${c.id}`}
        eyebrow={t("eyebrow")}
        title={t("ask_title")}
        error={error ? { message: error.message } : undefined}
        details={diagnostics ? { diagnostics, surface: "chat" } : undefined}
        secondary={[
          {
            label: t("just_once"),
            onSelect: () => onDecide(c.id, "dismiss"),
            disabled: busy,
            testId: "memory-candidate-dismiss",
          },
          // ★ "Change it" applies only to draft-origin candidates: the backend returns 400 for distilled ones.
          ...(redraft && c.origin === "feedback_draft"
            ? [{
                label: t("revise"),
                onSelect: () => setState(c.id, { kind: "revising", comment: "" }),
                disabled: busy,
                testId: "memory-candidate-revise",
              }]
            : []),
        ]}
        primary={{
          label: t("remember"),
          onSelect: () => onDecide(c.id, "remember"),
          disabled: busy,
          testId: "memory-candidate-remember",
        }}
      >
        {body}
        <p className="ui-card__note">
          {/* `default_target.label` already says scope and destination ("仅供你使用 · 零售分析"); fall back
              to the spec sentence only without a target. */}
          {c.default_target ? `${t("target_prefix")}: ${scopeLabel(c.default_target)}` : t("scope_note")}
          {c.source_space?.space_name ? ` · ${t("source_prefix")}: ${c.source_space.space_name}` : null}
        </p>
      </ChatCard>
    );
  };

  return (
    <div className="ui-card-stack" data-testid="memory-candidate-card">
      {settledIds
        .filter((cid) => !items.some((c) => c.id === cid))
        .map((cid) => {
          const view = settledView(cid);
          return view ? settledRow(cid, view) : null;
        })}
      {items.map((c) => {
        const st = stateOf(c.id);
        if (st.kind === "settled") {
          const view = settledView(c.id);
          return view ? settledRow(c.id, view) : null;
        }
        return pendingCard(c);
      })}
      {confirmDialog}
    </div>
  );
}

function candidateText(c: PendingCandidate): string {
  if (c.display_value !== undefined) return c.display_value ?? "";
  // Clamp 300: value has no server-side cap; an exceptionally long value must not blow up the card.
  const raw = typeof c.value === "string" ? c.value : safeStringify(c.value);
  return raw.length > 300 ? `${raw.slice(0, 300)}…` : raw;
}

function safeStringify(v: unknown): string {
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}
