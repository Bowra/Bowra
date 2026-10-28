import type { ComposerPersistenceScope } from "./composer-persistence-scope";

/** Exact saved draft identity. Possessing it grants no document access. */
export interface DocumentRevisionTarget {
  kind: "document_revision";
  version: 1;
  session_id: string;
  job_id: string;
  draft_digest: string;
}

/** What the revision target bar shows. Display only: never sent, never part of the target's identity. */
export interface DocumentRevisionDisplay {
  /** The draft's file name as the Canvas shows it (no task ID). */
  title: string;
  /** "Draft N" of the targeted draft, when the server reported it. */
  version?: number;
}

export interface ComposerDocumentRevision {
  handoffId: string;
  target: DocumentRevisionTarget;
  /** Older drafts and handoffs omit it; the bar then uses the generic "Document draft". */
  display?: DocumentRevisionDisplay;
}

export interface DocumentRevisionStageRequest extends ComposerDocumentRevision {
  scope: ComposerPersistenceScope;
  instruction: string;
}

export type DocumentRevisionStageResult =
  | { status: "applied"; draftRevision: string }
  | { status: "stale" }
  | { status: "blocked"; reason: "draft_not_ready" | "draft_not_empty" | "pending_submission" | "persistence_unavailable" | "unsupported_mode" };

export function parseDocumentRevisionTarget(value: unknown): DocumentRevisionTarget {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid document revision target");
  const v = value as Record<string, unknown>;
  if (
    Object.keys(v).length !== 5 ||
    Object.keys(v).some((key) => !["kind", "version", "session_id", "job_id", "draft_digest"].includes(key)) ||
    v.kind !== "document_revision" || v.version !== 1 ||
    typeof v.session_id !== "string" || !/^s_[A-Za-z0-9_]{1,126}$/.test(v.session_id) ||
    typeof v.job_id !== "string" || !/^djob_[A-Za-z0-9_]{1,123}$/.test(v.job_id) ||
    typeof v.draft_digest !== "string" || !/^[0-9a-f]{64}$/.test(v.draft_digest)
  ) throw new Error("Invalid document revision target");
  return { kind: "document_revision", version: 1, session_id: v.session_id, job_id: v.job_id, draft_digest: v.draft_digest };
}

function parseDocumentRevisionDisplay(value: unknown): DocumentRevisionDisplay {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid document revision display");
  const v = value as Record<string, unknown>;
  if (Object.keys(v).some((key) => !["title", "version"].includes(key)) ||
      typeof v.title !== "string" || !v.title.trim() || v.title.length > 200 ||
      (v.version !== undefined && (!Number.isSafeInteger(v.version) || (v.version as number) < 1)))
    throw new Error("Invalid document revision display");
  return { title: v.title, ...(v.version !== undefined ? { version: v.version as number } : {}) };
}

export function copyComposerDocumentRevision(value: unknown, sessionId?: string | null): ComposerDocumentRevision {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid document revision handoff");
  const v = value as Record<string, unknown>;
  const keys = Object.keys(v);
  if (keys.length < 2 || keys.some((key) => !["handoffId", "target", "display"].includes(key)) ||
      !keys.includes("handoffId") || !keys.includes("target") ||
      typeof v.handoffId !== "string" || !v.handoffId.trim() || v.handoffId.length > 128) throw new Error("Invalid document revision handoff");
  const target = parseDocumentRevisionTarget(v.target);
  if (sessionId !== undefined && target.session_id !== sessionId) throw new Error("Document revision scope mismatch");
  return { handoffId: v.handoffId, target, ...(v.display !== undefined ? { display: parseDocumentRevisionDisplay(v.display) } : {}) };
}

export function sameDocumentRevisionTarget(a: unknown, b: unknown): boolean {
  try { return JSON.stringify(parseDocumentRevisionTarget(a)) === JSON.stringify(parseDocumentRevisionTarget(b)); }
  catch { return false; }
}
