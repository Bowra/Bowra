"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { getCurrentLocale } from "@/lib/locale";
import {
  generateSubagentDraft,
  generateCanvasDraftSuggestion,
  getManualSubagentDraft,
  saveManualSubagentDraft,
  SubagentDraftApiError,
  type CanvasDraftSuggestion,
  type ManualSubagentDraftDetail,
  type SubagentDraftDetail,
} from "@/lib/subagent-drafts-api";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/field";
import { Notice } from "@/components/ui/notice";
import { ErrorDiagnosticsInline, type ShownError, shownError } from "@/components/error-notice";
import { draftGenerationFailureKey, subagentDraftFailureKey } from "./subagent-draft-errors";
import { DialogShell } from "@/components/ui/dialog";
import { randomUuid } from "@/lib/random-id";
import { CharIcon } from "@/components/char-icon";

const TERMINAL_RETRYABLE_DRAFT_CODES = new Set([
  "subagent_draft_model_timeout",
  "subagent_draft_model_failed",
  "subagent_draft_model_unavailable",
  "subagent_draft_model_unsupported",
  "subagent_draft_schema_invalid",
  "subagent_draft_catalog_unavailable",
  "subagent_draft_materialization_failed",
  "subagent_draft_secret_pattern_rejected",
  "subagent_draft_prompt_security_rejected",
  "team_model_unconfigured",
  "user_credit_exhausted",
  "team_pool_exhausted",
  "subagent_draft_version_conflict",
]);

export function SubagentIntentCard({
  initialPurpose = "",
  embedded = false,
  onDraft,
  canvasDraft,
  onCanvasDraftSaved,
  onCanvasSuggestion,
  onManual,
  onClose,
}: {
  initialPurpose?: string;
  embedded?: boolean;
  onDraft: (draft: SubagentDraftDetail) => void;
  canvasDraft?: ManualSubagentDraftDetail | null;
  onCanvasDraftSaved?: (draft: ManualSubagentDraftDetail) => void;
  onCanvasSuggestion?: (suggestion: CanvasDraftSuggestion) => void;
  onManual: (purposeHint: string | null) => void;
  onClose: () => void;
}) {
  const t = useTranslations("settings.subagents");
  const fieldId = useId();
  const titleId = useId();
  const [purpose, setPurpose] = useState(initialPurpose);
  const [busy, setBusy] = useState(false);
  // Show only localized explanations; status codes and request IDs go into Copy Diagnostics (outside Notice's alert).
  const [error, setErrorState] = useState<ShownError | null>(null);
  const setError = useCallback((message: string | null, cause?: unknown) => {
    setErrorState(message === null ? null : shownError(message, cause));
  }, []);
  const idempotencyKeyRef = useRef<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const canvasDraftRef = useRef(canvasDraft ?? null);
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);
  if (canvasDraft && (!canvasDraftRef.current || canvasDraft.version > canvasDraftRef.current.version)) {
    canvasDraftRef.current = canvasDraft;
  }

  const generate = async () => {
    const trimmed = purpose.trim();
    if (!trimmed || busy) return;
    if (!idempotencyKeyRef.current) {
      idempotencyKeyRef.current = randomUuid();
    }
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setBusy(true);
    setError(null);
    try {
      if (canvasDraftRef.current) {
        let bound = canvasDraftRef.current;
        if (bound.purpose !== trimmed) {
          const definition = {
            name: "", display_name: "", description: "", system_prompt: "",
            tool_refs: [], max_iterations: 12, model: "analyst", output_schema: null,
            output_schema_text: "", visibility: "creator_only" as const,
            ...bound.definition,
          };
          bound = await saveManualSubagentDraft(
            bound, trimmed, definition,
            bound.warnings.some((warning) => warning.code === "subagent_text_only_confirmation"),
          );
          canvasDraftRef.current = bound;
          if (mountedRef.current) onCanvasDraftSaved?.(bound);
        }
        const suggestion = await generateCanvasDraftSuggestion(bound, trimmed, {
          locale: getCurrentLocale(), idempotencyKey: idempotencyKeyRef.current,
          signal: controller.signal,
        });
        if (mountedRef.current) onCanvasSuggestion?.(suggestion);
        return;
      }
      const draft = await generateSubagentDraft(trimmed, {
        locale: getCurrentLocale(),
        idempotencyKey: idempotencyKeyRef.current,
        signal: controller.signal,
      });
      if (mountedRef.current) onDraft(draft);
    } catch (nextError) {
      if (!mountedRef.current || (nextError instanceof DOMException && nextError.name === "AbortError")) {
        return;
      }
      if (
        nextError instanceof SubagentDraftApiError
        && (
          nextError.code === "purpose_insufficient"
          || nextError.code === "subagent_purpose_insufficient"
        )
      ) {
        setError(t("intent_insufficient"), nextError);
      } else if (nextError instanceof SubagentDraftApiError) {
        if (canvasDraftRef.current && nextError.code === "subagent_draft_version_conflict") {
          try {
            const latest = await getManualSubagentDraft(canvasDraftRef.current.draft_id, canvasDraftRef.current.requestContext);
            canvasDraftRef.current = latest;
            onCanvasDraftSaved?.(latest);
          } catch { /* keep the typed purpose for retry */ }
          idempotencyKeyRef.current = null;
          setError(t("intent_canvas_conflict"), nextError);
          return;
        }
        const messageKey = draftGenerationFailureKey(nextError);
        // A canonical server failure has already terminalized this request.
        // A user-initiated retry needs a new attempt key; a local polling
        // timeout deliberately keeps the same key so it can recover an
        // in-flight request without duplicating generation.
        if (
          nextError.code
          && TERMINAL_RETRYABLE_DRAFT_CODES.has(nextError.code)
        ) {
          idempotencyKeyRef.current = null;
        }
        setError(t(messageKey), nextError);
      } else {
        setError(t(subagentDraftFailureKey(nextError)), nextError);
      }
    } finally {
      if (controllerRef.current === controller) {
        controllerRef.current = null;
        setBusy(false);
      }
    }
  };

  const leaveCanvasIntent = async (manual: boolean) => {
    if (busy && !canvasDraftRef.current) return;
    if (canvasDraftRef.current && canvasDraftRef.current.purpose !== purpose.trim()) {
      setBusy(true);
      setError(null);
      try {
        const bound = canvasDraftRef.current;
        const saved = await saveManualSubagentDraft(bound, purpose.trim(), {
          name: "", display_name: "", description: "", system_prompt: "",
          tool_refs: [], max_iterations: 12, model: "analyst", output_schema: null,
          output_schema_text: "", visibility: "creator_only", ...bound.definition,
        }, bound.warnings.some((warning) => warning.code === "subagent_text_only_confirmation"));
        canvasDraftRef.current = saved;
        onCanvasDraftSaved?.(saved);
      } catch (cause) {
        setError(t(subagentDraftFailureKey(cause)), cause);
        setBusy(false);
        return;
      }
      setBusy(false);
    }
    if (manual) onManual(purpose.trim() || null);
    else onClose();
  };

  const close = () => {
    controllerRef.current?.abort();
    void leaveCanvasIntent(false);
  };
  const style = {
    border: embedded ? "none" : "1px solid var(--c-line)",
    borderRadius: embedded ? 0 : 12,
    background: "var(--c-surface)",
    boxShadow: embedded ? "none" : "0 18px 60px color-mix(in srgb, var(--c-ink) 16%, transparent)",
    overflow: "hidden",
    color: "var(--c-ink)",
    fontFamily: "var(--font-v2-sans)",
  } as const;

  const content = (
    <>
      <header
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          padding: "15px 18px",
          borderBottom: "1px solid var(--c-line)",
        }}
      >
        <div style={{ flex: 1 }}>
          <h2 id={titleId} style={{ margin: 0, fontSize: "0.9375rem", fontWeight: 650 }}>
            {t("intent_title")}
          </h2>
          <p style={{ margin: "4px 0 0", color: "var(--c-ink-3)", fontSize: "0.71875rem" }}>
            {t("intent_recommended")}
          </p>
        </div>
        <Button
          variant="ghost"
          size="sm"
          aria-label={t("draft_close")}
          disabled={busy && !canvasDraft}
          onClick={close}
        >
          <CharIcon ch="×" />
        </Button>
      </header>
      <div style={{ display: "grid", gap: 16, padding: 18 }}>
        <p style={{ margin: 0, color: "var(--c-ink-2)", fontSize: "0.8125rem", lineHeight: 1.65 }}>
          {t("intent_body")}
        </p>
        <Notice tone="muted">{t(canvasDraft ? "intent_canvas_tip" : "create_chat_unbound")}</Notice>
        <Field
          id={fieldId}
          label={t("intent_label")}
          hint={t("intent_hint", { count: purpose.length })}
        >
          <textarea
            id={fieldId}
            className="settings-input"
            rows={6}
            maxLength={2000}
            autoFocus
            disabled={busy}
            value={purpose}
            onChange={(event) => {
              setPurpose(event.target.value);
              idempotencyKeyRef.current = null;
              setError(null);
            }}
            placeholder={t("intent_placeholder")}
          />
        </Field>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))",
            gap: 8,
          }}
          aria-label={t("intent_examples_label")}
        >
          {[
            t("intent_example_analysis"),
            t("intent_example_research"),
            t("intent_example_operations"),
          ].map((example) => (
            <button
              key={example}
              type="button"
              disabled={busy}
              onClick={() => {
                setPurpose(example);
                idempotencyKeyRef.current = null;
                setError(null);
              }}
              style={{
                border: "1px solid var(--c-line)",
                borderRadius: 7,
                background: "var(--c-surface-2)",
                color: "var(--c-ink-3)",
                cursor: "pointer",
                padding: "9px 10px",
                textAlign: "left",
                fontSize: "0.71875rem",
                lineHeight: 1.45,
              }}
            >
              {example}
            </button>
          ))}
        </div>
        {error ? <Notice tone="error">{error.message}</Notice> : null}
        {error ? <ErrorDiagnosticsInline error={error.error} surface="settings" /> : null}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 8,
            flexWrap: "wrap",
          }}
        >
          <Button
            variant="ghost"
            disabled={busy && !canvasDraft}
            onClick={() => {
              if (canvasDraft) controllerRef.current?.abort();
              void leaveCanvasIntent(true);
            }}
          >
            {busy && canvasDraft ? t("manual_while_generating") : t("manual_create")}
          </Button>
          <Button
            variant="primary"
            disabled={busy || !purpose.trim()}
            onClick={() => void generate()}
          >
            {busy ? t("draft_generating") : t("intent_generate")}
          </Button>
        </div>
      </div>
    </>
  );

  // Embedded in canvas, this is a non-modal region; standalone it is a modal dialog whose backdrop is supplied by the page, so omit the shell backdrop.
  if (embedded) {
    return (
      <section role="region" aria-labelledby={titleId} aria-busy={busy || undefined} tabIndex={-1} style={style}>
        {content}
      </section>
    );
  }
  return (
    <DialogShell
      onClose={close}
      busy={busy && !canvasDraft}
      labelledBy={titleId}
      mount="inline"
      overlay="none"
      closeOnBackdrop={false}
      panelStyle={style}
    >
      {content}
    </DialogShell>
  );
}
