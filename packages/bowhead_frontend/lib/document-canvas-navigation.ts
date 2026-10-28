import type { DocumentDraftCanvasContext } from "./document-draft-client";
import { withoutNextRouterHistoryMarkers } from "./url-session-nav";

export type DocumentCanvasScope = Omit<DocumentDraftCanvasContext, "jobId" | "instanceId">;
export type DocumentCanvasTarget = Omit<DocumentDraftCanvasContext, "authSessionGeneration" | "instanceId">;
export type DocumentCanvasMode = "editor" | "background" | "results";
const fields = {
  jobId: "canvas_document",
  sessionId: "canvas_doc_session",
  userId: "canvas_doc_user",
  teamId: "canvas_doc_team",
  workspaceId: "canvas_doc_space",
} as const;
const modeParam = "canvas_doc_view";

export function documentCanvasScopeKey(scope: DocumentCanvasScope | null): string | null {
  return scope
    ? JSON.stringify([scope.userId, scope.teamId, scope.workspaceId, scope.authSessionGeneration, scope.sessionId])
    : null;
}

export function documentCanvasKey(target: DocumentCanvasTarget): string {
  return JSON.stringify([
    "document_draft",
    target.userId,
    target.teamId,
    target.workspaceId,
    target.sessionId,
    target.jobId,
  ]);
}

export function documentCanvasMatches(target: DocumentCanvasTarget, scope: DocumentCanvasScope): boolean {
  return (
    target.userId === scope.userId &&
    target.teamId === scope.teamId &&
    target.workspaceId === scope.workspaceId &&
    target.sessionId === scope.sessionId
  );
}

export function readDocumentCanvasRoute(
  params: URLSearchParams,
): { target: DocumentCanvasTarget; mode: DocumentCanvasMode } | null {
  const entries = Object.entries(fields).map(([field, param]) => [field, params.get(param)] as const);
  if (!entries.every(([, value]) => value && value.trim() && value.length <= 256)) return null;
  const target = Object.fromEntries(entries) as DocumentCanvasTarget;
  if (params.get("session") !== target.sessionId) return null;
  const mode = params.get(modeParam);
  if (mode !== null && mode !== "editor" && mode !== "background" && mode !== "results") return null;
  return { target, mode: mode ?? "editor" };
}

/** View history contains only a scoped pointer, never document content or write authority. */
export function writeDocumentCanvasRoute(
  target: DocumentCanvasTarget | null,
  mode: DocumentCanvasMode,
  history: "push" | "replace" = "replace",
): void {
  const url = new URL(window.location.href);
  if (target && url.searchParams.get("session") !== target.sessionId) return;
  for (const [field, param] of Object.entries(fields)) {
    if (target) url.searchParams.set(param, target[field as keyof DocumentCanvasTarget]);
    else url.searchParams.delete(param);
  }
  if (target && mode !== "editor") url.searchParams.set(modeParam, mode);
  else url.searchParams.delete(modeParam);
  if (url.href === window.location.href) return;
  const next = `${url.pathname}${url.search}${url.hash}`;
  if (history === "push") window.history.pushState(withoutNextRouterHistoryMarkers(window.history.state), "", next);
  else window.history.replaceState(withoutNextRouterHistoryMarkers(window.history.state), "", next);
}
