"use client";

import { captureSubagentDraftContext } from "@/lib/subagent-draft-context";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { useAppRouter } from "@/lib/use-app-router";
import { AlertCircle, Bot, Lock } from "@/components/icons";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import {
  CanvasObjectHeader,
  CanvasObjectSurface,
  CanvasObjectToolbar,
  ObjectStatusBadge,
  StateBlock,
  type CanvasObjectMarker,
} from "@/components/canvas/object";
import { lookupSubagentDraft, type SubagentDraftLookup } from "@/lib/subagent-draft-lookup";
import type { AgentCanvasBinding, AgentCanvasObject } from "@/lib/use-agent-canvas-objects";
import type { ManualSubagentDraftDetail } from "@/lib/subagent-drafts-api";
import { SubagentCanvasDraftEditor } from "./subagent-canvas-draft-editor";
import { SubagentDraftReview } from "./subagent-draft-review";

/** One mounted editor per exact draft/incarnation, independent of the selected Tab. */
export function AgentDraftCanvasObject({
  object,
  workspaceId,
  teamId,
  unified,
  continuous,
  connected,
  onBinding,
  onClose,
  onActivate,
  onTitle,
  onMarker,
  onAnswerInChat,
}: {
  object: AgentCanvasObject;
  workspaceId: string;
  teamId?: string;
  unified: boolean;
  continuous: boolean;
  connected: boolean;
  onBinding: (key: string, incarnation: string, binding: AgentCanvasBinding | null) => void;
  onClose: (key: string, incarnation: string) => void;
  onActivate: (key: string) => void;
  onTitle: (key: string, incarnation: string, title: string, subtitle?: string) => void;
  /** C1 tab marker reported by the editor (pending suggestions, failed save, creation in progress). */
  onMarker?: (marker: { marker: CanvasObjectMarker; statusLabel: string | null } | null) => void;
  /** Reveals the chat composer to answer an AI clarification in this draft. */
  onAnswerInChat?: () => void;
}) {
  const t = useTranslations("create_subagent");
  const router = useAppRouter();
  const context = useRef(Object.freeze({ ...captureSubagentDraftContext(workspaceId), ...(teamId ? { teamId } : {}) }));
  const [lookup, setLookup] = useState<SubagentDraftLookup | null>(() =>
    object.initialDraft ? { kind: "canvas", draft: object.initialDraft } : null,
  );
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const draftId = object.payload.draft_id;
  const close = useCallback(() => onClose(object.key, object.incarnation), [onClose, object.key, object.incarnation]);
  const activate = useCallback(() => onActivate(object.key), [onActivate, object.key]);
  const bind = useCallback(
    (binding: AgentCanvasBinding | null) => onBinding(object.key, object.incarnation, binding),
    [onBinding, object.key, object.incarnation],
  );
  const title = useCallback((label: string, name: string) => onTitle(object.key, object.incarnation, label, name),
    [onTitle, object.key, object.incarnation]);
  const accessDenied = useCallback(() => {
    setLookup(null);
    setFailed(true);
    bind(null);
    title(t("draft_load_failed"), "");
  }, [bind, title, t]);
  useEffect(() => {
    if (lookup?.kind === "legacy" || (lookup?.kind === "canvas" && unified)) return;
    const view = { requestClose: close };
    object.editorRef.current = view;
    return () => {
      if (object.editorRef.current === view) object.editorRef.current = null;
    };
  }, [object.editorRef, lookup, unified, close]);
  useEffect(() => {
    if (object.initialDraft && attempt === 0) return;
    let disposed = false;
    setFailed(false);
    void lookupSubagentDraft(draftId, context.current)
      .then((result) => {
        if (!disposed) {
          setLookup(result);
          if (result.kind === "not_found") { bind(null); title(t("draft_not_found_title"), ""); }
        }
      })
      .catch(() => {
        if (!disposed) accessDenied();
      });
    return () => {
      disposed = true;
    };
  }, [draftId, object.initialDraft, attempt, accessDenied, bind, title, t]);
  const converted = useCallback((_purpose: string | null, draft?: ManualSubagentDraftDetail) => {
    if (draft) setLookup({ kind: "canvas", draft });
  }, []);
  useEffect(() => {
    if (lookup?.kind === "canvas" && lookup.draft.workspace_id === workspaceId)
      onTitle(
        object.key,
        object.incarnation,
        lookup.draft.definition.display_name || t("entry_untitled"),
        lookup.draft.definition.name,
      );
  }, [lookup, workspaceId, object.key, object.incarnation, onTitle, t]);
  // The tab × is the only close (D1); these states never render their own close buttons.
  if (failed)
    return (
      <CanvasObjectSurface label={t("draft_load_failed")}>
        <StateBlock tone="error" icon={<AlertCircle size={20} />} title={t("draft_open_failed_title")} body={t("draft_open_failed_body")}
          secondaryActions={[{ label: t("entry_retry"), onClick: () => setAttempt((value) => value + 1) }]} />
      </CanvasObjectSurface>
    );
  if (!lookup)
    return (
      <CanvasObjectSurface label={t("draft_loading")}>
        <LoadingSkeleton variant="form" rows={4} label={t("draft_loading")} />
      </CanvasObjectSurface>
    );
  if (lookup.kind === "not_found")
    return (
      <CanvasObjectSurface label={t("draft_not_found_title")}>
        <StateBlock icon={<Lock size={20} />} title={t("draft_not_found_title")} body={t("draft_not_found_description")}
          secondaryActions={[{ label: t("draft_find_existing"), onClick: () => router.push("/agents/manage/created/") }]} />
      </CanvasObjectSurface>
    );
  if (lookup.kind === "legacy")
    return (
      <SubagentDraftReview
        draftId={draftId}
        embedded
        refreshVersion={object.payload.draft_version}
        onActivate={activate}
        viewRef={object.editorRef}
        onDraftChange={bind}
        chatDraftEditEnabled={connected}
        onClose={close}
        onCreated={() => bind(null)}
        unifiedDraftEnabled={unified}
        onManual={converted}
      />
    );
  if (lookup.draft.workspace_id !== workspaceId)
    return (
      <CanvasObjectSurface label={t("draft_load_failed")}>
        <StateBlock icon={<Lock size={20} />} title={t("draft_open_failed_title")} body={t("draft_load_failed")} />
      </CanvasObjectSurface>
    );
  if (!unified)
    return (
      <CanvasObjectSurface label={t("draft_readonly")} toolbar={<CanvasObjectToolbar status={<ObjectStatusBadge status="read_only" />} />}>
        <CanvasObjectHeader icon={<Bot size={14} aria-hidden />} eyebrow={t("canvas_eyebrow")}
          title={lookup.draft.definition.display_name || lookup.draft.purpose || t("entry_untitled")}
          subtitle={t("draft_readonly")} />
        {lookup.draft.definition.description ? <p>{lookup.draft.definition.description}</p> : null}
      </CanvasObjectSurface>
    );
  return (
    <SubagentCanvasDraftEditor
      manualDraft={lookup.draft}
      inlineGeneration
      continuous={continuous}
      onActivate={activate}
      onAccessDenied={accessDenied}
      onMetadataChange={title}
      viewRef={object.editorRef}
      purposeHint={object.payload.purpose_hint}
      nameHint={object.payload.name_hint}
      displayNameHint={object.payload.display_name_hint}
      refreshVersion={object.payload.draft_version}
      onDraftChange={bind}
      chatDraftEditEnabled={connected}
      onRequestAiDraft={() => {}}
      onClose={close}
      onCreated={() => bind(null)}
      onMarker={onMarker}
      onAnswerInChat={onAnswerInChat}
    />
  );
}
