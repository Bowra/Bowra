"use client";

import { useEffect, useId, useState } from "react";
import { useTranslations } from "next-intl";

import {
  listAgentEvalConversationCandidates,
  suggestAgentEvalCasesFromTurns,
  type AgentEvalCase,
  type AgentEvalConversationCandidate,
} from "@/lib/agents-api";
import { ApiError } from "@/lib/api-client";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { CardStatus, useCiFormat } from "./agent-ci-shared";

/**
 * Eval credibility R14: the Owner picks past questions they asked this Agent, or Turns members shared
 * with them one at a time; each becomes a case whose task is the question, with Coach-suggested points
 * the Owner confirms later. A shared Turn names its sharer; answers are never listed.
 */
export function AgentEvalConversationPicker({
  agentId,
  limit,
  onDrafted,
  onClose,
}: {
  agentId: string;
  /** Room left in the suite; the selection never exceeds it or the server's maximum. */
  limit: number;
  onDrafted: (cases: AgentEvalCase[]) => void;
  onClose: () => void;
}) {
  const t = useTranslations("agents_marketplace");
  const format = useCiFormat();
  const listId = useId();
  const [items, setItems] = useState<AgentEvalConversationCandidate[] | null>(null);
  const [max, setMax] = useState(0);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; cause?: unknown; retry?: () => void } | null>(null);

  // `keepMessage` keeps an explanation (a vanished conversation) visible while the list refreshes.
  const load = (keepMessage = false) => {
    if (!keepMessage) setError(null);
    void listAgentEvalConversationCandidates(agentId).then((value) => {
      setItems(value.items);
      setMax(value.max_selection);
    }).catch((cause) => setError({ message: t("eval_conversations_load_failed"), cause, retry: () => load() }));
  };
  useEffect(() => load(), [agentId]); // eslint-disable-line react-hooks/exhaustive-deps

  const cap = Math.max(0, Math.min(max, limit));
  const toggle = (turnId: string) => setSelected((current) => (
    current.includes(turnId) ? current.filter((item) => item !== turnId) : current.length < cap ? [...current, turnId] : current
  ));

  const draft = async () => {
    if (!selected.length || busy) return;
    setBusy(true);
    try {
      const value = await suggestAgentEvalCasesFromTurns(agentId, selected);
      onDrafted(value.cases);
    } catch (cause) {
      // A conversation deleted after the list loaded: say so and reload, rather than a generic failure.
      if (cause instanceof ApiError && cause.status === 404) {
        setSelected([]);
        setError({ message: t("eval_conversations_unavailable"), cause });
        load(true);
      } else {
        setError({ message: t("eval_conversations_failed"), cause });
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={t("eval_conversations_title")}
      variant="drawer"
      width={480}
      busy={busy}
      onClose={onClose}
      footer={<>
        <span className="am-ci-note" role="status">{t("eval_conversations_selected", { count: format.count(selected.length), max: format.count(cap) })}</span>
        <span className="am-ci-sp" />
        <Button onClick={onClose} disabled={busy}>{t("version_cancel")}</Button>
        <Button variant="primary" disabled={!selected.length || busy} loading={busy} onClick={() => void draft()}>{t("eval_conversations_draft")}</Button>
      </>}
    >
      <div className="am-ci-form">
        <p className="am-ci-note">{t("eval_conversations_body")}</p>
        <CardStatus status={error ? { tone: "error", message: error.message, cause: error.cause, onRetry: error.retry } : null} />
        {items === null ? (error ? null : <LoadingSkeleton rows={4} label={t("loading")} />) : items.length === 0 ? (
          <p className="am-ci-note">{t("eval_conversations_empty")}</p>
        ) : (
          <fieldset className="am-ci-scopes" aria-describedby={listId}>
            <legend id={listId} className="sr-only">{t("eval_conversations_title")}</legend>
            {items.map((item) => {
              const checked = selected.includes(item.turn_id);
              const disabled = busy || (!checked && selected.length >= cap);
              return (
                <label key={item.turn_id} className="am-ci-ctl" data-disabled={disabled || undefined}>
                  <input type="checkbox" checked={checked} disabled={disabled} onChange={() => toggle(item.turn_id)} />
                  <span>{item.question}</span>
                  <small>
                    {item.shared_by ? `${t("eval_conversations_shared_by", { name: item.shared_by })} · ` : null}
                    {format.dateTime(item.answered_at)}
                  </small>
                </label>
              );
            })}
          </fieldset>
        )}
      </div>
    </Dialog>
  );
}
