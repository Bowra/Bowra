"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type {
  DocumentDraftCanvasContext,
  DocumentDraftCanvasUpdate,
} from "@/components/canvas/document-draft-canvas-object";
import { randomUuid } from "./random-id";
import { readDocumentDraft } from "./document-draft-client";
import type { DocumentDraftOpen } from "./document-canvas-events";
import { clearDocumentDismissal, documentDismissalIdentity, persistDocumentDismissals, readDocumentDismissals } from "./document-canvas-dismissal";
import {
  documentCanvasKey,
  documentCanvasMatches,
  documentCanvasScopeKey,
  readDocumentCanvasRoute,
  writeDocumentCanvasRoute,
  type DocumentCanvasMode,
  type DocumentCanvasScope,
} from "./document-canvas-navigation";

type ObjectView = {
  key: string;
  context: Readonly<DocumentDraftCanvasContext>;
  update: DocumentDraftCanvasUpdate | null;
  refreshKey: number;
};
type State = { scope: string | null; objects: ObjectView[]; selectedKey: string | null; rememberedKey: string | null };
const empty = (scope: string | null): State => ({ scope, objects: [], selectedKey: null, rememberedKey: null });
const routeIdentity = (params: URLSearchParams) => JSON.stringify(readDocumentCanvasRoute(params));

/** Retained view instances are independent of selection and canonical document storage. */
export function useDocumentCanvasObjects(
  scope: DocumentCanvasScope | null,
  searchParams: URLSearchParams,
  onRoute: (mode: DocumentCanvasMode | null) => void,
) {
  const scopeKey = documentCanvasScopeKey(scope);
  const [state, setState] = useState(() => empty(scopeKey));
  const current = useRef(state);
  const latest = useRef({ scope, onRoute });
  latest.current = { scope, onRoute };
  const consumed = useRef<string | null>(null);
  const seenToolOpens = useRef(new Set<string>());
  const closedJobs = useRef(new Set<string>());
  const dismissals = useRef<Set<string> | null>(null);
  if (!dismissals.current) dismissals.current = readDocumentDismissals();
  const toolIdentities = useRef(new Map<string, Set<string>>());
  const pendingToolOpens = useRef(new Map<string, AbortController>());
  if (current.current.scope !== scopeKey) {
    for (const controller of pendingToolOpens.current.values()) controller.abort();
    pendingToolOpens.current.clear();
    seenToolOpens.current.clear();
    closedJobs.current.clear();
    toolIdentities.current.clear();
    current.current = empty(scopeKey);
    consumed.current = null;
    setState(current.current);
  }
  useEffect(() => () => {
    for (const controller of pendingToolOpens.current.values()) controller.abort();
    pendingToolOpens.current.clear();
  }, []);
  const commit = useCallback((next: State) => {
    current.current = next;
    setState(next);
  }, []);
  const isCurrent = useCallback((context: Readonly<DocumentDraftCanvasContext>) => {
    const live = latest.current.scope;
    return Boolean(
      live &&
      documentCanvasMatches(context, live) &&
      context.authSessionGeneration === live.authSessionGeneration &&
      current.current.objects.some(
        (object) => object.context.instanceId === context.instanceId && object.key === documentCanvasKey(context),
      ),
    );
  }, []);
  const open = useCallback(
    (jobId: string, mode: DocumentCanvasMode = "editor", history: "push" | "replace" | "none" = "push") => {
      const live = latest.current.scope;
      if (!live || !jobId.trim() || jobId.length > 256) return null;
      const key = documentCanvasKey({ ...live, jobId });
      closedJobs.current.delete(jobId);
      clearDocumentDismissal(dismissals.current!, live, jobId);
      pendingToolOpens.current.get(key)?.abort();
      pendingToolOpens.current.delete(key);
      const existing = current.current.objects.find((object) => object.key === key);
      const object = existing ?? {
        key,
        context: Object.freeze({ ...live, jobId, instanceId: randomUuid() }),
        update: null,
        refreshKey: 0,
      };
      commit({
        ...current.current,
        objects: existing ? current.current.objects : [...current.current.objects, object],
        selectedKey: mode === "editor" ? key : null,
        rememberedKey: key,
      });
      if (history !== "none") {
        writeDocumentCanvasRoute(object.context, mode, history);
        consumed.current = routeIdentity(new URLSearchParams(window.location.search));
      }
      return object;
    },
    [commit],
  );
  const openFromTool = useCallback(async (
    pointer: DocumentDraftOpen,
    admission: { isCurrent(): boolean; maySelect(): boolean; onOpened(selected: boolean): void },
  ) => {
    const live = latest.current.scope;
    if (!live || !admission.isCurrent() || pointer.user_id !== live.userId || pointer.team_id !== live.teamId
      || pointer.workspace_id !== live.workspaceId || pointer.session_id !== live.sessionId) return;
    const identity = documentDismissalIdentity(live, pointer);
    const identities = toolIdentities.current.get(pointer.job_id) ?? new Set<string>();
    identities.add(identity);
    toolIdentities.current.set(pointer.job_id, identities);
    if (closedJobs.current.has(pointer.job_id)) {
      dismissals.current!.add(identity);
      persistDocumentDismissals(dismissals.current!);
      return;
    }
    if (dismissals.current!.has(identity)) return;
    const key = documentCanvasKey({ ...live, jobId: pointer.job_id });
    if (seenToolOpens.current.has(identity) || pendingToolOpens.current.has(key)) return;
    const controller = new AbortController();
    pendingToolOpens.current.set(key, controller);
    const context = Object.freeze({ ...live, jobId: pointer.job_id, instanceId: randomUuid() });
    try {
      const job = await readDocumentDraft(context, controller.signal);
      if (controller.signal.aborted || pendingToolOpens.current.get(key) !== controller
        || documentCanvasScopeKey(latest.current.scope) !== documentCanvasScopeKey(live)
        || !admission.isCurrent() || closedJobs.current.has(pointer.job_id)
        || dismissals.current!.has(identity)) return;
      seenToolOpens.current.add(identity);
      if (job.origin_turn_id !== pointer.turn_id || job.draft_digest !== pointer.draft_digest
        || !["review", "published"].includes(job.state)) return;
      const existing = current.current.objects.find((item) => item.key === key);
      // An existing view owns its confirmation, inputs and selection independently.
      if (existing) return;
      const selected = admission.maySelect();
      const object = { key, context, update: null, refreshKey: 0 };
      commit({
        ...current.current,
        objects: [...current.current.objects, object],
        ...(selected ? { selectedKey: key, rememberedKey: key } : {}),
      });
      if (selected) {
        writeDocumentCanvasRoute(context, "editor", "replace");
        consumed.current = routeIdentity(new URLSearchParams(window.location.search));
      }
      admission.onOpened(selected);
    } catch {
      // The original document menu remains the explicit retry/discovery surface.
      if (!controller.signal.aborted) seenToolOpens.current.add(identity);
    } finally {
      if (pendingToolOpens.current.get(key) === controller) pendingToolOpens.current.delete(key);
    }
  }, [commit]);
  const select = useCallback(
    (key: string) => {
      const object = current.current.objects.find((item) => item.key === key);
      return object ? open(object.context.jobId) : null;
    },
    [open],
  );
  const deselect = useCallback(
    (mode: "background" | "results" = "background") => {
      const previous = current.current;
      const object = previous.objects.find((item) => item.key === previous.rememberedKey);
      commit({ ...previous, selectedKey: null });
      const live = readDocumentCanvasRoute(new URLSearchParams(window.location.search));
      if (object && live && documentCanvasKey(live.target) === object.key) {
        writeDocumentCanvasRoute(object.context, mode, live.mode === "editor" ? "push" : "replace");
        consumed.current = routeIdentity(new URLSearchParams(window.location.search));
      }
    },
    [commit],
  );
  const close = useCallback(
    (context: Readonly<DocumentDraftCanvasContext>) => {
      if (!isCurrent(context)) return;
      const key = documentCanvasKey(context);
      closedJobs.current.add(context.jobId);
      for (const identity of toolIdentities.current.get(context.jobId) ?? []) dismissals.current!.add(identity);
      persistDocumentDismissals(dismissals.current!);
      pendingToolOpens.current.get(key)?.abort();
      pendingToolOpens.current.delete(key);
      const previous = current.current;
      const objects = previous.objects.filter((object) => object.key !== key);
      const replacement = objects.at(-1) ?? null;
      commit({
        ...previous,
        objects,
        selectedKey: previous.selectedKey === key ? (replacement?.key ?? null) : previous.selectedKey,
        rememberedKey: previous.rememberedKey === key ? (replacement?.key ?? null) : previous.rememberedKey,
      });
      const live = readDocumentCanvasRoute(new URLSearchParams(window.location.search));
      if (live && documentCanvasKey(live.target) === key) {
        writeDocumentCanvasRoute(replacement?.context ?? null, live.mode);
        consumed.current = routeIdentity(new URLSearchParams(window.location.search));
      }
      return { selectedKey: current.current.selectedKey };
    },
    [commit, isCurrent],
  );
  const update = useCallback(
    (value: DocumentDraftCanvasUpdate) => {
      if (!isCurrent(value.context)) return;
      commit({
        ...current.current,
        objects: current.current.objects.map((object) =>
          object.context.instanceId === value.context.instanceId ? { ...object, update: value } : object,
        ),
      });
    },
    [commit, isCurrent],
  );
  const invalidate = useCallback(
    (jobIds?: ReadonlySet<string>) => {
      commit({
        ...current.current,
        objects: current.current.objects.map((object) =>
          (!jobIds || jobIds.has(object.context.jobId)) &&
          !["confirming", "confirmation_unknown"].includes(object.update?.phase ?? "")
            ? { ...object, refreshKey: object.refreshKey + 1 }
            : object,
        ),
      });
    },
    [commit],
  );
  const clear = useCallback(() => {
    for (const controller of pendingToolOpens.current.values()) controller.abort();
    pendingToolOpens.current.clear();
    commit(empty(current.current.scope));
  }, [commit]);
  const query = searchParams.toString();
  useEffect(() => {
    const identity = routeIdentity(new URLSearchParams(query));
    if (
      !scope ||
      consumed.current === identity ||
      routeIdentity(new URLSearchParams(window.location.search)) !== identity
    )
      return;
    consumed.current = identity;
    const route = readDocumentCanvasRoute(new URLSearchParams(query));
    if (route && documentCanvasMatches(route.target, scope)) {
      open(route.target.jobId, route.mode, "none");
      latest.current.onRoute(route.mode);
    } else if (current.current.selectedKey) {
      commit({ ...current.current, selectedKey: null });
      latest.current.onRoute(null);
    }
  }, [query, scopeKey, open, commit]);
  const visible = state.scope === scopeKey ? state : empty(scopeKey);
  return {
    objects: visible.objects,
    selectedKey: visible.selectedKey,
    open,
    openFromTool,
    select,
    deselect,
    close,
    update,
    invalidate,
    clear,
    isCurrent,
  };
}
