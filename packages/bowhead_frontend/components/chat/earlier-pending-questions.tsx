"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { ChatCard } from "@/components/ui/card";
import { dismissClarification, listPendingSessionQuestions, type PendingQuestionSummary } from "@/lib/api";
import { ApiError } from "@/lib/api-client";
import { isClarificationAnswered, markClarificationAnswered } from "@/lib/clarification-tombstone";
import type { ErrorPresentation } from "@/lib/error-presentation";
import { ruleClarificationErrorPresentation } from "@/lib/rule-clarification-error";

/** Older pauses remain discoverable without restoring them as the current Turn. */
export function EarlierPendingQuestions({ sessionId, currentTurnId, running }: {
  sessionId: string;
  currentTurnId: string | null;
  running: boolean;
}) {
  const t = useTranslations("clarification");
  const common = useTranslations("common");
  const reasons = useTranslations("error_messages.reasons");
  const errors = useTranslations("errors");
  const [loaded, setLoaded] = useState<{ sessionId: string; items: PendingQuestionSummary[] } | null>(null);
  const [failure, setFailure] = useState<{ sessionId: string; error: ErrorPresentation } | null>(null);
  const [actingId, setActingId] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const generation = useRef(0);
  useEffect(() => {
    const token = ++generation.current;
    listPendingSessionQuestions(sessionId).then((items) => {
      if (token !== generation.current) return;
      setLoaded({ sessionId, items });
      setFailure(null);
    }).catch((error) => {
      if (token === generation.current) setFailure({ sessionId, error: presentError(error) });
    });
    return () => { generation.current++; };
    // Refresh when a Turn starts or settles, including a native cancellation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, currentTurnId, running, refresh]);

  function presentError(error: unknown) {
    return ruleClarificationErrorPresentation(error, { surface: "chat", copy: {
      generic: t("submit_failed"), entitlementDenied: reasons("entitlement_denied"),
      transport: { requestTimedOut: errors("transport.requestTimedOut"), serviceUnreachable: errors("transport.serviceUnreachable"), retry: errors("transport.retry") },
    } });
  }

  async function cancel(requestId: string) {
    if (actingId) return;
    const token = generation.current;
    setActingId(requestId);
    try {
      const result = await dismissClarification(requestId);
      if (!result.dismissed) throw new ApiError(409, "clarification_not_resolvable", { code: "clarification_not_resolvable" });
      markClarificationAnswered(requestId, { values: [], dismissed: true });
      if (token !== generation.current) return;
      setLoaded((value) => value?.sessionId === sessionId ? { ...value, items: value.items.filter((item) => item.request_id !== requestId) } : value);
      setFailure(null);
    } catch (error) {
      if (token === generation.current) setFailure({ sessionId, error: presentError(error) });
    } finally {
      setActingId((value) => value === requestId ? null : value);
    }
  }

  const items = loaded?.sessionId === sessionId ? loaded.items.filter((item) => item.turn_id !== currentTurnId && !isClarificationAnswered(item.request_id)) : [];
  const error = failure?.sessionId === sessionId ? failure.error : null;
  return <>
    {items.map((item) => <ChatCard key={item.request_id} intent="input" eyebrow={t("needs_answer")} title={item.question}
      data-testid="earlier-pending-question"
      secondary={item.can_dismiss ? [{ label: common("cancel"), onSelect: () => cancel(item.request_id), busy: actingId === item.request_id, disabled: actingId !== null }] : []}
    />)}
    {error ? <ChatCard intent="failure" title={error.message} details={error.diagnostics ? { diagnostics: error.diagnostics, surface: "chat" } : undefined}
      primary={{ label: t("retry"), onSelect: () => setRefresh((value) => value + 1) }} /> : null}
  </>;
}
