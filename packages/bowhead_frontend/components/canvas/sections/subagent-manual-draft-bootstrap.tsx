"use client";

import { captureSubagentDraftContext, type SubagentDraftRequestContext } from "@/lib/subagent-draft-context";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { AlertCircle } from "@/components/icons";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { CanvasObjectSurface, StateBlock } from "@/components/canvas/object";
import { getCurrentLocale } from "@/lib/locale";
import { randomUuid } from "@/lib/random-id";
import { createManualSubagentDraft, type ManualSubagentDraftDetail } from "@/lib/subagent-drafts-api";

/** Reserve the task identity before showing any editable fields. The tab × closes it (D1). */
export function SubagentManualDraftBootstrap({
  purposeHint,
  sourceMode = "manual",
  requestContext,
  onDraft,
}: {
  purposeHint: string | null;
  sourceMode?: "manual" | "intent";
  requestContext?: SubagentDraftRequestContext;
  onDraft: (draft: ManualSubagentDraftDetail) => void;
}) {
  const t = useTranslations("create_subagent");
  const context = useRef(Object.freeze({ ...(requestContext ?? captureSubagentDraftContext()) }));
  const requestKeyRef = useRef(randomUuid());
  const onDraftRef = useRef(onDraft);
  onDraftRef.current = onDraft;
  const mountedRef = useRef(false);
  const inFlightRef = useRef(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const load = useCallback(async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    setLoading(true);
    setError(false);
    try {
      const draft = await createManualSubagentDraft(purposeHint?.trim() ?? "", requestKeyRef.current, getCurrentLocale(), sourceMode, context.current);
      if (mountedRef.current) onDraftRef.current(draft);
    } catch {
      if (mountedRef.current) setError(true);
    } finally {
      inFlightRef.current = false;
      if (mountedRef.current) setLoading(false);
    }
  }, [purposeHint, sourceMode]);
  useEffect(() => {
    mountedRef.current = true;
    void load();
    return () => { mountedRef.current = false; };
  }, [load]);

  return (
    <CanvasObjectSurface label={t("entry_title")} busy={loading}>
      {error ? (
        <StateBlock tone="error" icon={<AlertCircle size={20} />} title={t("draft_open_failed_title")} body={t("draft_bootstrap_failed")}
          secondaryActions={[{ label: t("draft_retry"), onClick: () => void load() }]} />
      ) : (
        <LoadingSkeleton variant="form" rows={4} label={t("draft_loading")} />
      )}
    </CanvasObjectSurface>
  );
}
