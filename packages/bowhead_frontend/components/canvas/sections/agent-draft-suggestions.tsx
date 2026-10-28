"use client";

import { useEffect, useId, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { WandSparkles } from "@/components/icons";
import { MessageSquare } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui/notice";
import { ChatCard } from "@/components/ui/card";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { CanvasSection, SectionHeading } from "@/components/ui/section-heading";
import { ChangeRow, Spinner } from "@/components/canvas/object";
import type { ChangeValue } from "@/lib/canvas-change-value";
import type { CanvasDraftSuggestion } from "@/lib/subagent-drafts-api";
import styles from "./agent-draft-canvas.module.css";

export interface AgentDraftGenerationModel {
  open: boolean;
  purpose: string;
  generating: boolean;
  /** No description yet: the panel is the first thing a new draft shows. */
  initial: boolean;
  onPurpose: (value: string) => void;
  onOpen: () => void;
  onGenerate: () => void;
  onStop: () => void;
  onCancel: () => void;
}

export interface AgentDraftSuggestionsModel {
  items: readonly CanvasDraftSuggestion[];
  /** Definition fields of a suggestion that differ from the saved draft. */
  fieldsOf: (suggestion: CanvasDraftSuggestion) => string[];
  busy: boolean;
  error: string | null;
  generation: AgentDraftGenerationModel | null;
  /** Apply the accepted fields (or dismiss the suggestion when none were accepted); resolves true on success. */
  onCommit: (suggestion: CanvasDraftSuggestion, accepted: string[]) => Promise<boolean>;
  onDismiss: (suggestion: CanvasDraftSuggestion) => Promise<boolean>;
  /** Field decisions made but not yet applied; they count as unsaved work for the close guard. */
  onPendingDecisions?: (count: number) => void;
  /** Reveals the composer so the user answers a clarification in chat; absent when the chat cannot take it. */
  onAnswerInChat?: () => void;
}

type Decision = "accepted" | "dismissed";

/** Stable element ID of a suggestion row, used by the “Suggested” field badge (C6). */
export function suggestionRowId(base: string, field: string): string {
  return `${base}-suggestion-${field}`;
}

/**
 * D8 · AI suggestions: the first section of the Agent draft. Each changed field is a C7 row with a
 * secondary “Accept” and a text “Dismiss”; “Accept all” sits on the heading. A suggestion is applied
 * once every field has a decision (the server applies a suggestion in one step), then the section
 * folds into “AI suggestions · x accepted · y dismissed”.
 */
export function AgentDraftSuggestions({ model, idBase, label, current, format }: {
  model: AgentDraftSuggestionsModel;
  idBase: string;
  label: (field: string) => string;
  current: (field: string) => unknown;
  format: (field: string, before: unknown, after: unknown) => { before: ChangeValue; after: ChangeValue };
}) {
  const t = useTranslations("create_subagent");
  const headingId = useId();
  const [open, setOpen] = useState(true);
  const [decisions, setDecisions] = useState<Record<string, Decision>>({});
  const tCommon = useTranslations("common");
  const [summary, setSummary] = useState<{ accepted: number; dismissed: number } | null>(null);
  /** Decisions of a commit that failed, kept so “Retry” re-applies exactly what the user chose. */
  const [failed, setFailed] = useState<Record<string, Record<string, Decision>>>({});
  const [committingIds, setCommittingIds] = useState<ReadonlySet<string>>(new Set());
  const committing = useRef(new Set<string>());
  const committed = useRef(new Set<string>());
  // Commits run one at a time: the server applies a suggestion against the draft version it read.
  const queue = useRef<Promise<void>>(Promise.resolve());
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const modelRef = useRef(model);
  modelRef.current = model;
  const definitions = model.items.filter((item) => item.suggestion_kind === "definition");
  const clarifications = model.items.filter((item) => item.suggestion_kind === "clarification");
  const key = (suggestion: CanvasDraftSuggestion, field: string) => `${suggestion.suggestion_id}:${field}`;
  const pendingCount = definitions.reduce((count, suggestion) =>
    count + model.fieldsOf(suggestion).filter((field) => !decisions[key(suggestion, field)]).length, 0);

  const setCommitting = (id: string, on: boolean) => {
    if (on) committing.current.add(id); else committing.current.delete(id);
    if (mounted.current) setCommittingIds(new Set(committing.current));
  };

  // Commit a suggestion once every field has a decision. Only a successful commit is counted; a failed
  // one returns its rows to pending and offers “Retry” with the same decisions.
  useEffect(() => {
    for (const suggestion of definitions) {
      const id = suggestion.suggestion_id;
      const fields = model.fieldsOf(suggestion);
      if (!fields.length || committing.current.has(id) || committed.current.has(id)) continue;
      if (fields.some((field) => !decisions[key(suggestion, field)])) continue;
      const chosen = Object.fromEntries(fields.map((field) => [field, decisions[key(suggestion, field)]])) as Record<string, Decision>;
      const accepted = fields.filter((field) => chosen[field] === "accepted");
      setCommitting(id, true);
      queue.current = queue.current.then(async () => {
        let ok = false;
        try { ok = await modelRef.current.onCommit(suggestion, accepted); } catch { ok = false; }
        if (!mounted.current) return;
        if (ok) {
          committed.current.add(id);
          setSummary((value) => ({
            accepted: (value?.accepted ?? 0) + accepted.length,
            dismissed: (value?.dismissed ?? 0) + fields.length - accepted.length,
          }));
          setFailed((value) => { const next = { ...value }; delete next[id]; return next; });
        } else {
          setFailed((value) => ({ ...value, [id]: chosen }));
          setDecisions((value) => {
            const next = { ...value };
            for (const field of fields) delete next[key(suggestion, field)];
            return next;
          });
        }
        setCommitting(id, false);
      });
    }
    // Decisions drive commits; the model's callbacks are read at call time.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [decisions, definitions.map((item) => item.suggestion_id).join(",")]);

  // Decisions of suggestions that left the list (applied or replaced) are no longer pending.
  const liveIds = definitions.map((item) => item.suggestion_id).join(",");
  useEffect(() => {
    const live = new Set(liveIds.split(",").filter(Boolean));
    setDecisions((value) => {
      const next = Object.fromEntries(Object.entries(value).filter(([entry]) => live.has(entry.split(":")[0])));
      return Object.keys(next).length === Object.keys(value).length ? value : next;
    });
    for (const id of [...committed.current]) if (!live.has(id)) committed.current.delete(id);
  }, [liveIds]);

  const pendingDecisions = Object.keys(decisions).filter((entry) => !committed.current.has(entry.split(":")[0])).length;
  const reportPending = useRef(model.onPendingDecisions);
  reportPending.current = model.onPendingDecisions;
  useEffect(() => { reportPending.current?.(pendingDecisions); }, [pendingDecisions]);
  useEffect(() => () => reportPending.current?.(0), []);

  const retry = (suggestion: CanvasDraftSuggestion) => {
    const chosen = failed[suggestion.suggestion_id];
    if (!chosen) return;
    setFailed((value) => { const next = { ...value }; delete next[suggestion.suggestion_id]; return next; });
    setDecisions((value) => {
      const next = { ...value };
      for (const [field, decision] of Object.entries(chosen)) next[key(suggestion, field)] = decision;
      return next;
    });
  };

  const decide = (suggestion: CanvasDraftSuggestion, field: string, decision: Decision | null) => {
    setDecisions((value) => {
      const next = { ...value };
      if (decision) next[key(suggestion, field)] = decision;
      else delete next[key(suggestion, field)];
      return next;
    });
  };
  const acceptAll = () => setDecisions((value) => {
    const next = { ...value };
    for (const suggestion of definitions)
      for (const field of model.fieldsOf(suggestion)) next[key(suggestion, field)] ??= "accepted";
    return next;
  });

  const generation = model.generation;
  const hasContent = definitions.length > 0 || clarifications.length > 0 || Boolean(generation?.open) || Boolean(generation?.generating)
    || summary !== null || model.error !== null;
  if (!hasContent) {
    // A draft without a description was never generated, so offer a first draft, not "generate again".
    return generation ? (
      <Button size="sm" variant="ghost" className={styles.alignStart} onClick={generation.onOpen}>
        <WandSparkles size={14} aria-hidden />{t(generation.purpose.trim() ? "suggestion_generate_again" : "suggestion_generate_first")}
      </Button>
    ) : null;
  }
  const count = pendingCount > 0 ? t("suggestion_pending", { count: pendingCount })
    : summary ? t("suggestion_summary", { accepted: summary.accepted, dismissed: summary.dismissed }) : undefined;
  return (
    <CanvasSection labelledBy={headingId}>
      <SectionHeading
        id={headingId}
        icon={<WandSparkles size={14} />}
        title={t("suggestion_title")}
        count={count}
        collapsible
        open={open}
        controls={`${headingId}-body`}
        onToggle={() => setOpen((value) => !value)}
        action={pendingCount > 0 ? <Button size="sm" variant="ghost" disabled={model.busy} onClick={acceptAll}>{t("canvas_select_changes")}</Button> : undefined}
      />
      {open ? (
        <div id={`${headingId}-body`} className={styles.suggestions}>
          {model.error ? <Notice tone="error">{model.error}</Notice> : null}
          {generation?.generating ? (
            <>
              <div className={styles.busyRow} role="status">
                <Spinner />{t("canvas_generating")}
                <Button size="sm" variant="plain" onClick={generation.onStop}>{t("suggestion_stop")}</Button>
              </div>
              <LoadingSkeleton rows={2} label={t("canvas_generating")} />
            </>
          ) : null}
          {clarifications.map((suggestion) => (
            <ChatCard
              key={suggestion.suggestion_id}
              intent="input"
              eyebrow={t("suggestion_question_eyebrow")}
              data-testid="agent-draft-clarification"
              secondary={[
                { label: t("suggestion_dismiss"), disabled: model.busy, onSelect: () => void model.onDismiss(suggestion) },
                ...(model.onAnswerInChat ? [{ label: t("suggestion_answer_in_chat"), icon: MessageSquare, variant: "secondary" as const, onSelect: model.onAnswerInChat }] : []),
              ]}
            >
              <p className="ui-card__text">{suggestion.payload.question || t("suggestion_clarify")}</p>
            </ChatCard>
          ))}
          {definitions.flatMap((suggestion) => [
            failed[suggestion.suggestion_id] ? (
              <div key={`${suggestion.suggestion_id}:error`} className={styles.rowError} role="alert">
                <span>{t("suggestion_apply_failed")}</span>
                <Button size="sm" variant="plain" disabled={model.busy} onClick={() => retry(suggestion)}>{tCommon("retry")}</Button>
              </div>
            ) : null,
            ...model.fieldsOf(suggestion).map((field) => {
            const rowBusy = model.busy || committingIds.has(suggestion.suggestion_id);
            const decision = decisions[key(suggestion, field)];
            const values = format(field, current(field), suggestion.payload.definition?.[field as keyof typeof suggestion.payload.definition]);
            if (decision === "accepted") return (
              <ChangeRow key={key(suggestion, field)} id={suggestionRowId(idBase, field)} state="accepted" location={label(field)}
                badge={<Badge tone="positive" size="sm">{t("suggestion_accepted")}</Badge>}
                trailing={<Button size="sm" variant="plain" disabled={rowBusy} onClick={() => decide(suggestion, field, null)}>{t("suggestion_undo")}</Button>} />
            );
            if (decision === "dismissed") return (
              <ChangeRow key={key(suggestion, field)} id={suggestionRowId(idBase, field)} state="dismissed"
                location={t("suggestion_dismissed_row", { field: label(field) })}
                trailing={<Button size="sm" variant="plain" disabled={rowBusy} onClick={() => decide(suggestion, field, null)}>{t("suggestion_restore")}</Button>} />
            );
            return (
              <ChangeRow
                key={key(suggestion, field)}
                id={suggestionRowId(idBase, field)}
                location={label(field)}
                before={values.before}
                after={values.after}
                beforeLabel={t("suggestion_current")}
                strikeBefore={false}
                afterLabel={t("suggestion_proposed")}
                actions={<>
                  <Button size="sm" disabled={rowBusy} onClick={() => decide(suggestion, field, "accepted")}>{t("suggestion_apply")}</Button>
                  <Button size="sm" variant="plain" disabled={rowBusy} onClick={() => decide(suggestion, field, "dismissed")}>{t("suggestion_dismiss")}</Button>
                </>}
              />
            );
          })])}
          {generation?.open && !generation.generating ? (
            <div className={styles.generation}>
              <label className="settings-field__label" htmlFor={`${headingId}-purpose`}>
                {generation.initial ? t("canvas_generation_purpose") : t("canvas_generation_revise")}
              </label>
              <textarea
                id={`${headingId}-purpose`}
                className={`settings-input ${styles.textarea}`}
                rows={3}
                value={generation.purpose}
                placeholder={t("canvas_generation_placeholder")}
                onChange={(event) => generation.onPurpose(event.target.value)}
              />
              <span className={styles.generationActions}>
                <Button disabled={!generation.purpose.trim() || model.busy} onClick={generation.onGenerate}>
                  <WandSparkles size={14} aria-hidden />{t("canvas_generate")}
                </Button>
                {!generation.initial ? <Button variant="plain" onClick={generation.onCancel}>{t("canvas_keep_editing")}</Button> : null}
              </span>
              {generation.initial ? <p className={styles.hint}>{t("canvas_generation_manual_hint")}</p> : null}
            </div>
          ) : generation && !generation.generating ? (
            <Button size="sm" variant="ghost" className={styles.alignStart} onClick={generation.onOpen}>
              <WandSparkles size={14} aria-hidden />{t("suggestion_generate_again")}
            </Button>
          ) : null}
        </div>
      ) : null}
    </CanvasSection>
  );
}
