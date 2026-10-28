"use client";

import { captureSubagentDraftContext } from "@/lib/subagent-draft-context";

import { useCallback, useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { draftGenerationFailureKey } from "./subagent-draft-errors";
import { randomUuid } from "@/lib/random-id";
import {
  applyCanvasDraftSuggestion,
  dismissCanvasDraftSuggestion,
  getManualSubagentDraft,
  isSubagentDraftReadUnavailable,
  generateCanvasDraftSuggestion,
  listCanvasDraftSuggestions,
  type CanvasDraftSuggestion,
  type ManualSubagentDraftDefinition,
  type ManualSubagentDraftDetail,
} from "@/lib/subagent-drafts-api";
import { useCanvasCloseGuard, type CanvasObjectMarker } from "@/components/canvas/object";
import { registerSubagentDraftNavigationGuard } from "@/lib/canvas-navigation-guard";
import { CreateSubagentCard, type AgentDraftEditorPort, type AgentDraftCanvasState } from "./create-subagent-card";
import type { AgentDraftSuggestionsModel } from "./agent-draft-suggestions";

// Reading order of a suggestion: what the owner names and describes first; the internal identifier last.
const SUGGESTED_FIELDS: Array<keyof ManualSubagentDraftDefinition> = [
  "display_name", "description", "system_prompt", "builtin_plugin_ids", "tool_refs", "skill_grants", "capability_ids",
  "model", "max_iterations", "output_schema_text", "name",
];

export function SubagentCanvasDraftEditor({
  manualDraft, purposeHint, nameHint, displayNameHint, refreshVersion,
  continuous = false, inlineGeneration = false, onActivate, onAccessDenied, onMetadataChange, viewRef, onDraftChange, chatDraftEditEnabled = Boolean(onDraftChange), onManualDraftSaved, onRequestAiDraft, onClose, onCreated, onMarker, onAnswerInChat,
}: {
  manualDraft: ManualSubagentDraftDetail;
  continuous?: boolean;
  inlineGeneration?: boolean;
  viewRef?: React.RefObject<{ requestClose: () => void } | null>;
  onActivate?: () => void;
  onAccessDenied?: () => void;
  onMetadataChange?: (title: string, name: string) => void;
  purposeHint?: string | null;
  nameHint?: string | null;
  displayNameHint?: string | null;
  refreshVersion?: number | null;
  onDraftChange?: (binding: { draftId: string; version: number; dirty: boolean } | null) => void;
  chatDraftEditEnabled?: boolean;
  onManualDraftSaved?: (draftId: string) => void;
  onRequestAiDraft: (draft: ManualSubagentDraftDetail) => void;
  onClose: () => void;
  onCreated: (name: string, toolCount: number) => void;
  /** C1 tab marker: pending suggestions, failed saves and creation in progress (unsaved comes from the binding). */
  onMarker?: (marker: { marker: CanvasObjectMarker; statusLabel: string | null } | null) => void;
  /** Reveals the chat composer to answer an AI clarification. */
  onAnswerInChat?: () => void;
}) {
  const t = useTranslations("create_subagent");
  const tObject = useTranslations("canvas_object");
  const tDraft = useTranslations("settings.subagents");
  const locale = useLocale();
  const context = useRef(manualDraft.requestContext ?? captureSubagentDraftContext(manualDraft.workspace_id));
  const controllerRef = useRef<AgentDraftEditorPort | null>(null);
  const [generationOpen, setGenerationOpen] = useState(inlineGeneration && manualDraft.source_mode === "intent");
  const [purpose, setPurpose] = useState(manualDraft.purpose);
  const [generating, setGenerating] = useState(false);
  const generationKey = useRef<string | null>(null);
  const generationRequest = useRef<AbortController | null>(null);
  const operationBusy = useRef(false);
  const [draft, setDraft] = useState(manualDraft);
  const [finished, setFinished] = useState(Boolean(manualDraft.result));
  const [receiptLabel, setReceiptLabel] = useState<string | null>(null);
  const [suggestions, setSuggestions] = useState<CanvasDraftSuggestion[]>([]);
  const suggestionReadGeneration = useRef(0);
  const [binding, setBinding] = useState<{ draftId: string; version: number; dirty: boolean } | null>(null);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [canvasState, setCanvasState] = useState<AgentDraftCanvasState>({ saveFailed: false, creating: false });
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; generationRequest.current?.abort(); };
  }, []);
  const readManualDraft = useCallback((id: string) => getManualSubagentDraft(id, context.current).catch((cause: unknown) => {
    if (mountedRef.current && isSubagentDraftReadUnavailable(cause)) onAccessDenied?.();
    throw cause;
  }), [onAccessDenied]);
  /** Suggestion fields decided but not yet applied (D8); closing asks before dropping them (C14). */
  const pendingDecisions = useRef(0);
  const [hasPendingDecisions, setHasPendingDecisions] = useState(false);
  const { confirmDiscard, dialog: guardDialog } = useCanvasCloseGuard();
  const confirmPendingDecisions = useCallback(async () => {
    if (pendingDecisions.current === 0) return true;
    onActivate?.();
    const name = controllerRef.current?.read().draft?.definition.display_name || manualDraft.definition.display_name || t("entry_untitled");
    return confirmDiscard(name);
  }, [confirmDiscard, manualDraft.definition.display_name, onActivate, t]);
  useEffect(() => {
    if (!viewRef) return;
    const view = { requestClose: () => {
      // Without pending decisions the close stays synchronous (the editor's own guard decides).
      if (pendingDecisions.current === 0) { controllerRef.current?.requestClose(); return; }
      void confirmPendingDecisions().then((allowed) => { if (allowed) controllerRef.current?.requestClose(); });
    } };
    viewRef.current = view;
    return () => { if (viewRef.current === view) viewRef.current = null; };
  }, [viewRef, confirmPendingDecisions]);
  useEffect(() => {
    if (!hasPendingDecisions) return;
    return registerSubagentDraftNavigationGuard({ hasUnsaved: () => pendingDecisions.current > 0, confirmLeave: confirmPendingDecisions });
  }, [hasPendingDecisions, confirmPendingDecisions]);

  const refreshSuggestions = useCallback(async () => {
    const generation = ++suggestionReadGeneration.current;
    try {
      const latest = await listCanvasDraftSuggestions(manualDraft.draft_id, context.current);
      if (mountedRef.current && generation === suggestionReadGeneration.current)
        setSuggestions(latest.filter((item) => item.draft_id === manualDraft.draft_id && item.status === "pending"));
    } catch {
      if (mountedRef.current && generation === suggestionReadGeneration.current) setError(t("suggestion_load_failed"));
    }
  }, [manualDraft.draft_id, t]);
  useEffect(() => {
    if (finished) return;
    void refreshSuggestions();
    if (manualDraft.source_mode === "manual" && !chatDraftEditEnabled) return;
    // Coach can save suggestions for a manual draft without changing its version.
    const timer = window.setInterval(() => void refreshSuggestions(), 5000);
    return () => window.clearInterval(timer);
  }, [manualDraft.source_mode, chatDraftEditEnabled, refreshSuggestions, finished]);

  const handleDraftChange = useCallback((next: typeof binding) => {
    setBinding(next);
  }, []);
  useEffect(() => {
    onDraftChange?.(binding ? { ...binding, dirty: binding.dirty || applying } : null);
  }, [binding, applying, onDraftChange]);
  useEffect(() => {
    const saved = controllerRef.current?.read().draft;
    if (saved?.draft_id === manualDraft.draft_id)
      // The tab shows the display name; the generated identifier ("agent-d9db64") is never a title.
      onMetadataChange?.(receiptLabel || saved.definition.display_name || (finished ? t("canvas_created_agent") : t("entry_untitled")), saved.definition.name ?? "");
  }, [binding?.draftId, binding?.version, manualDraft.draft_id, receiptLabel, finished, onMetadataChange, t]);
  useEffect(() => {
    if (!binding || binding.dirty || operationBusy.current || binding.version <= draft.version) return;
    const saved = controllerRef.current?.read().draft;
    if (saved?.draft_id === draft.draft_id && saved.version >= binding.version) setDraft(saved);
  }, [binding, draft.draft_id, draft.version]);

  const supportedFields = useCallback((suggestion: CanvasDraftSuggestion): string[] => {
    const definition = suggestion.payload.definition;
    const changed = suggestion.payload.changed_fields;
    return SUGGESTED_FIELDS.filter((field) => definition && field in definition
      && (!changed || changed.includes(field) || field === "output_schema_text" && changed.includes("output_schema"))
      && JSON.stringify(definition[field]) !== JSON.stringify(draft.definition[field]));
  }, [draft.definition]);

  const pendingCount = finished ? 0 : suggestions.filter((item) => item.suggestion_kind === "clarification" || supportedFields(item).length > 0).length;
  const markerRef = useRef(onMarker);
  markerRef.current = onMarker;
  useEffect(() => {
    markerRef.current?.(canvasState.saveFailed ? { marker: "failed", statusLabel: tObject("save_state_failed") }
      : canvasState.creating ? { marker: "running", statusLabel: tObject("status.creating") }
        : pendingCount > 0 ? { marker: "attention", statusLabel: t("marker_suggestions") } : null);
  }, [canvasState.saveFailed, canvasState.creating, pendingCount, t, tObject]);
  useEffect(() => () => markerRef.current?.(null), []);

  /**
   * Local edits are saved first (autosave flush), so accepting never asks the user to wait. Resolves true
   * only when the server applied (or dismissed) the suggestion; the suggestions section shows a failure
   * in the suggestion's own rows and offers a retry.
   */
  const commitSuggestion = async (suggestion: CanvasDraftSuggestion, accepted: string[]): Promise<boolean> => {
    if (!accepted.length) return dismissSuggestion(suggestion, false);
    const selected = accepted.filter((field) => supportedFields(suggestion).includes(field));
    if (!selected.length) return dismissSuggestion(suggestion, false);
    if (operationBusy.current) return false;
    operationBusy.current = true;
    setApplying(true);
    setError(null);
    try {
      if (!controllerRef.current) throw new Error("Draft editor is not ready");
      await controllerRef.current.flush();
      const adopt = (latest: ManualSubagentDraftDetail) => applyCanvasDraftSuggestion(latest, suggestion.suggestion_id, selected, randomUuid());
      const updated = await controllerRef.current.mutate(adopt);
      if (!mountedRef.current) return true;
      setDraft(updated);
      onManualDraftSaved?.(updated.draft_id);
      await refreshSuggestions();
      return true;
    } catch {
      return false;
    } finally {
      operationBusy.current = false;
      if (mountedRef.current) setApplying(false);
    }
  };

  /** `report` shows a section-level error (a clarification dismissed on its own); commits report in their rows. */
  const dismissSuggestion = async (suggestion: CanvasDraftSuggestion, report = true): Promise<boolean> => {
    if (operationBusy.current) return false;
    operationBusy.current = true;
    setApplying(true);
    setError(null);
    try {
      await dismissCanvasDraftSuggestion(draft.draft_id, suggestion.suggestion_id, context.current);
      await refreshSuggestions();
      return true;
    } catch {
      if (mountedRef.current && report) setError(t("suggestion_dismiss_failed"));
      return false;
    } finally {
      operationBusy.current = false;
      if (mountedRef.current) setApplying(false);
    }
  };

  const openAiDraft = async () => {
    if (applying) return;
    setApplying(true);
    setError(null);
    try {
      await controllerRef.current?.flush();
      const latest = await readManualDraft(draft.draft_id);
      if (!mountedRef.current) return;
      const current = controllerRef.current?.read();
      if (!current?.draft || current.dirty) return;
      if (latest.version !== current.draft.version) {
        setDraft(latest);
        setError(t("suggestion_version_changed"));
        return;
      }
      if (continuous || inlineGeneration) {
        setPurpose(latest.purpose);
        setGenerationOpen(true);
      } else onRequestAiDraft(latest);
    } catch {
      if (mountedRef.current) setError(t("suggestion_load_failed"));
    } finally {
      if (mountedRef.current) setApplying(false);
    }
  };

  const generate = async () => {
    const flushed = await (controllerRef.current?.flush() ?? Promise.resolve()).then(() => true, () => false);
    const original = controllerRef.current?.read();
    if (!purpose.trim() || generating) return;
    if (!flushed || original?.dirty) {
      if (mountedRef.current) setError(t("suggestion_save_first"));
      return;
    }
    if (!original?.draft || !original.draft.actions.edit) return;
    const controller = new AbortController();
    generationRequest.current = controller;
    generationKey.current ??= randomUuid();
    setGenerating(true);
    setError(null);
    try {
      await generateCanvasDraftSuggestion(original.draft, purpose.trim(), {
        locale, idempotencyKey: generationKey.current, signal: controller.signal,
      });
      if (!mountedRef.current) return;
      generationKey.current = null;
      setGenerationOpen(false);
      await refreshSuggestions();
    } catch (cause) {
      // Say why drafting failed (a timeout needs a faster model, not a refresh); reads keep their own message.
      if (mountedRef.current && !(cause instanceof DOMException && cause.name === "AbortError")) setError(tDraft(draftGenerationFailureKey(cause)));
    } finally {
      if (mountedRef.current) setGenerating(false);
    }
  };

  const model: AgentDraftSuggestionsModel | null = finished ? null : {
    items: suggestions,
    fieldsOf: supportedFields,
    busy: applying,
    error,
    generation: {
      open: generationOpen,
      purpose,
      generating,
      initial: manualDraft.source_mode === "intent" && suggestions.length === 0 && !draft.definition.system_prompt,
      onPurpose: (value) => { setPurpose(value); generationKey.current = null; },
      onOpen: () => void openAiDraft(),
      onGenerate: () => void generate(),
      onStop: () => { generationRequest.current?.abort(); },
      onCancel: () => { generationRequest.current?.abort(); setGenerationOpen(false); },
    },
    onCommit: commitSuggestion,
    onDismiss: (suggestion) => dismissSuggestion(suggestion),
    onPendingDecisions: (count) => { pendingDecisions.current = count; setHasPendingDecisions(count > 0); },
    onAnswerInChat,
  };

  return (<>
    {guardDialog}
    <CreateSubagentCard manualDraft={draft} continuous={continuous} controllerRef={controllerRef} onActivate={onActivate} onAccessDenied={onAccessDenied}
      purposeHint={purposeHint} nameHint={nameHint} displayNameHint={displayNameHint}
      unifiedDraftEnabled embedded presentation="canvas" refreshVersion={Math.max(refreshVersion ?? 0, draft.version)}
      chatDraftEditEnabled={chatDraftEditEnabled} aiSuggestions={model} externalBusy={applying} onCanvasState={setCanvasState}
      onDraftChange={handleDraftChange} onManualDraftSaved={onManualDraftSaved}
      onClose={onClose} onReceiptLabel={setReceiptLabel} onCreated={(name, count) => { setFinished(true); onCreated(name, count); }} />
  </>);
}
