"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { SettingsWorkspaceI18n, type WorkspaceI18nAgentInterface } from "@/components/settings/workspace-i18n";
import { SpaceGeneral } from "@/components/settings/space-general";
import type { WorkspaceSettingsLifecycle } from "@/lib/workspace-settings-agent";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { Cog, Lock, RefreshCw } from "@/components/icons";
import {
  CanvasObjectHeader,
  CanvasObjectSurface,
  CanvasObjectToolbar,
  StateBlock,
  ToolbarAction,
  canvasCloseGuardOptions,
  type CanvasSaveReport,
} from "@/components/canvas/object";
import { getWorkspaceMetadata, type WorkspaceMetadata } from "@/lib/api";
import type { SettingsCanvasTarget } from "@/lib/settings-canvas-target";
import { randomUuid } from "@/lib/random-id";
import { useWorkspaceSettingsCanvasBridge } from "./workspace-settings-canvas-context";
import { WorkspaceSettingsCanvasSuggestions } from "./workspace-settings-suggestion";
import { registerSubagentDraftNavigationGuard } from "@/lib/canvas-navigation-guard";
import { requestConfirm } from "@/lib/confirm-dialog-store";
import styles from "./settings-page-canvas.module.css";

type DisposePurpose = "close" | "refresh";

/** A remount creates a new port; an old getter never resolves to the new page. */
function MountedSettingsPage({ target, name, readOnly, onAccessDenied, onActivate, onSettled, disposalRef, pendingRef }: {
  target: SettingsCanvasTarget; name: string; readOnly: boolean; onAccessDenied: () => void;
  onActivate?: () => void;
  /** Called whenever the page lifecycle changes (a pending close retries once writes settle). */
  onSettled: () => void;
  disposalRef: React.RefObject<((purpose?: DisposePurpose) => Promise<boolean>) | null>;
  /** True while a write is in flight (closing waits for it rather than asking). */
  pendingRef: React.RefObject<(() => boolean) | null>;
}) {
  const tObject = useTranslations("canvas_object");
  const bridge = useWorkspaceSettingsCanvasBridge();
  const [controllerId] = useState(randomUuid);
  const lifecycle = useRef<WorkspaceSettingsLifecycle | null>(null);
  const [ready, setReady] = useState(false);
  const api = useRef<WorkspaceI18nAgentInterface | null>(null);
  const latest = useRef({ readOnly, onAccessDenied, onActivate, onSettled, name });
  latest.current = { readOnly, onAccessDenied, onActivate, onSettled, name };
  const onLifecycleChange = useCallback((state: WorkspaceSettingsLifecycle) => {
    lifecycle.current = state;
    setReady(state.savedValues !== null);
    bridge?.pageChanged(controllerId);
    latest.current.onSettled();
  }, [bridge, controllerId]);
  const onAgentInterfaceChange = useCallback((value: WorkspaceI18nAgentInterface | null) => {
    api.current = value;
    if (!value) lifecycle.current = null;
  }, []);
  const approvedGeneration = useRef<number | null>(null);
  useEffect(() => {
    let disposed = false;
    let confirmation: AbortController | null = null;
    const hasUnsaved = () => Boolean(bridge?.hasPendingAction(controllerId) || lifecycle.current && (lifecycle.current.pendingWrites > 0
      || lifecycle.current.unconfirmedFields.length > 0 && approvedGeneration.current !== lifecycle.current.editGeneration));
    const check = async (purpose: DisposePurpose = "close") => {
      const original = lifecycle.current;
      if (!hasUnsaved()) return true;
      latest.current.onActivate?.();
      // A save in flight blocks closing; the wrapper closes once the write settles.
      if (bridge?.hasPendingAction(controllerId) || original && original.pendingWrites > 0) return false;
      if (!original) return true;
      confirmation?.abort();
      confirmation = new AbortController();
      // C14: one close-guard copy for every Canvas object.
      // Closing uses the shared C14 copy; refreshing says what refreshing does to unsaved input.
      const options = purpose === "refresh" ? {
        title: tObject("refresh_guard_title"),
        description: tObject("refresh_guard_body", { name: latest.current.name }),
        confirmLabel: tObject("refresh_guard_discard"),
        cancelLabel: tObject("guard_keep"),
        tone: "discard" as const,
      } : canvasCloseGuardOptions(tObject, latest.current.name);
      const allowed = await requestConfirm(options, confirmation.signal);
      if (!allowed || disposed || bridge?.hasPendingAction(controllerId) || lifecycle.current?.pendingWrites || lifecycle.current?.editGeneration !== original.editGeneration) return false;
      approvedGeneration.current = original.editGeneration;
      return true;
    };
    disposalRef.current = check;
    pendingRef.current = () => Boolean(bridge?.hasPendingAction(controllerId) || lifecycle.current && lifecycle.current.pendingWrites > 0);
    const unregister = registerSubagentDraftNavigationGuard({ hasUnsaved, confirmLeave: check, cancelLeave: () => { confirmation?.abort(); approvedGeneration.current = null; } });
    const warn = (event: BeforeUnloadEvent) => { if (hasUnsaved()) { event.preventDefault(); event.returnValue = ""; } };
    window.addEventListener("beforeunload", warn);
    return () => { disposed = true; unregister(); window.removeEventListener("beforeunload", warn); if (disposalRef.current === check) { disposalRef.current = null; pendingRef.current = null; } };
  }, [disposalRef, pendingRef, tObject, bridge, controllerId]);
  useEffect(() => bridge?.register({
    controllerId, target: { ...target },
    readLifecycle: () => api.current && !latest.current.readOnly ? lifecycle.current : null,
    onAccessDenied: () => latest.current.onAccessDenied(),
  }), [bridge, controllerId, target.page, target.userId, target.teamId, target.workspaceId, readOnly]);
  return <>
    <WorkspaceSettingsCanvasSuggestions controllerId={controllerId} />
    <fieldset disabled={!ready || readOnly} className={styles.fieldset}>
    {target.page === "space_general" ? <SpaceGeneral workspaceId={target.workspaceId} teamId={target.teamId} userId={target.userId} hosted readOnly={readOnly}
      onLifecycleChange={onLifecycleChange} onAgentInterfaceChange={onAgentInterfaceChange} onAccessDenied={onAccessDenied} /> : <SettingsWorkspaceI18n workspaceId={target.workspaceId} workspaceName={name} hosted
      onLifecycleChange={onLifecycleChange} onAgentInterfaceChange={onAgentInterfaceChange} onAccessDenied={onAccessDenied} />}
    </fieldset>
  </>;
}

/** Minimal page wrapper: the existing metadata read verifies the original Space-admin boundary. */
export function SettingsPageCanvas({
  target,
  readOnly,
  onClose,
  onActivate,
  onSaveStateChange,
  onRegisterClose,
}: {
  target: SettingsCanvasTarget;
  readOnly: boolean;
  onClose: () => void;
  onActivate?: () => void;
  /** SaveBar state for the C1 tab marker. */
  onSaveStateChange?: (state: CanvasSaveReport) => void;
  /** The host's tab × calls this close request (D1: Space settings close only from the tab). */
  onRegisterClose?: (close: (() => void) | null) => void;
}) {
  const t = useTranslations("settings_canvas");
  const [metadata, setMetadata] = useState<WorkspaceMetadata | null>(null);
  const [failed, setFailed] = useState(false);
  const [readRevision, setReadRevision] = useState(0);
  const bridge = useWorkspaceSettingsCanvasBridge();
  const disposalRef = useRef<((purpose?: DisposePurpose) => Promise<boolean>) | null>(null);
  const pendingRef = useRef<(() => boolean) | null>(null);
  const [closing, setClosing] = useState(false);
  const closingRef = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const latestClose = useRef(onClose);
  latestClose.current = onClose;
  const dispose = useCallback(async (action: () => void, waitForSave = false, purpose: DisposePurpose = "close") => {
    const original = disposalRef.current;
    const allowed = !original || await original(purpose);
    if (!mounted.current || original !== disposalRef.current) return;
    if (!allowed) {
      // A save in flight: the tab shows progress and the object closes once the write settles.
      // Keeping the edit in the guard dialog cancels the pending close.
      const waiting = waitForSave && Boolean(pendingRef.current?.());
      closingRef.current = waiting;
      setClosing(waiting);
      return;
    }
    closingRef.current = false;
    setClosing(false);
    action();
  }, []);
  const requestClose = useCallback(() => { void dispose(() => latestClose.current(), true, "close"); }, [dispose]);
  const registerRef = useRef(onRegisterClose);
  registerRef.current = onRegisterClose;
  useEffect(() => {
    registerRef.current?.(requestClose);
    return () => registerRef.current?.(null);
  }, [requestClose]);
  const onAccessDenied = useCallback(() => { setMetadata(null); setFailed(true); }, []);
  const onSettled = useCallback(() => { if (closingRef.current) requestClose(); }, [requestClose]);

  useEffect(() => {
    let cancelled = false;
    setMetadata(null);
    setFailed(false);
    void getWorkspaceMetadata(target.workspaceId)
      .then((value) => {
        if (!cancelled) setMetadata(value);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [target.workspaceId, readRevision]);

  const general = target.page === "space_general";
  const titleId = `settings-canvas-${target.page}-title`;
  return (
    <CanvasObjectSurface
      labelledBy={failed ? undefined : titleId}
      label={failed ? t(general ? "general_title" : "title") : undefined}
      onSaveStateChange={onSaveStateChange}
      toolbar={(
        <CanvasObjectToolbar
          note={closing ? t("save_before_close") : undefined}
          meta={closing ? undefined : metadata?.name}
          actions={!failed ? (
            <ToolbarAction icon={<RefreshCw size={14} aria-hidden />} label={t("refresh")}
              onClick={() => void dispose(() => setReadRevision((revision) => revision + 1), false, "refresh")} />
          ) : undefined}
        />
      )}
    >
      {failed ? (
        <StateBlock icon={<Lock size={20} />} title={t("unavailable_title")} body={t("unavailable")} />
      ) : (
        <>
          <CanvasObjectHeader
            titleId={titleId}
            icon={<Cog aria-hidden />}
            eyebrow={t("eyebrow")}
            title={t(general ? "general_short_title" : "preferences_short_title")}
            subtitle={t(bridge && !readOnly ? "chat_scope_short" : "manual_only_short")}
          />
          {metadata ? (
            <fieldset disabled={readOnly} className={styles.fieldset}>
              <MountedSettingsPage
                key={readRevision}
                target={target}
                name={metadata.name}
                readOnly={readOnly}
                onAccessDenied={onAccessDenied}
                onActivate={onActivate}
                onSettled={onSettled}
                disposalRef={disposalRef}
                pendingRef={pendingRef}
              />
            </fieldset>
          ) : (
            <LoadingSkeleton variant="form" rows={4} label={t("loading")} />
          )}
        </>
      )}
    </CanvasObjectSurface>
  );
}
