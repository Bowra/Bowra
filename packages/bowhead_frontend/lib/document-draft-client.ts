import { apiJson } from "./api-client";
import type { DocumentJob } from "./document-drafts";
import { sha256Hex } from "./sha256-fallback";
import { spaceHeader } from "./space-storage";

export interface DocumentDraftCanvasContext {
  userId: string;
  teamId: string;
  workspaceId: string;
  authSessionGeneration: string;
  sessionId: string;
  jobId: string;
  instanceId: string;
}

export function documentCanvasIdentity(
  context: Readonly<DocumentDraftCanvasContext>,
): string {
  return JSON.stringify([
    context.userId,
    context.teamId,
    context.workspaceId,
    context.authSessionGeneration,
    context.sessionId,
    context.jobId,
    context.instanceId,
  ]);
}

export function completeDocumentCanvasContext(
  context: Readonly<DocumentDraftCanvasContext>,
): boolean {
  return [
    context.userId,
    context.teamId,
    context.workspaceId,
    context.authSessionGeneration,
    context.sessionId,
    context.jobId,
    context.instanceId,
  ].every(
    (value) =>
      typeof value === "string" &&
      value.trim().length > 0 &&
      value.length <= 256,
  );
}

const states = new Set([
  "queued",
  "preparing",
  "planning",
  "rendering",
  "review",
  "question",
  "published",
  "failed",
  "cancelled",
]);
const digest = (value: unknown): value is string =>
  typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === "string";
const id = (value: unknown): value is string =>
  text(value) && value.trim().length > 0 && value.length <= 256;
const arrayOf = (value: unknown, valid: (item: unknown) => boolean): boolean =>
  Array.isArray(value) && value.every(valid);

/** Validate the actual delivery projection, not a model-authored Canvas pointer. */
export function parseDocumentJob(value: unknown, jobId: string): DocumentJob {
  let valid =
    record(value) &&
    value.id === jobId &&
    id(value.origin_turn_id) &&
    text(value.state) &&
    states.has(value.state) &&
    text(value.request_text) &&
    typeof value.can_confirm === "boolean" &&
    (value.can_revise === undefined || typeof value.can_revise === "boolean") &&
    (value.can_revise !== true ||
      (value.state === "review" && value.can_confirm && digest(value.draft_digest))) &&
    (value.draft_digest == null || digest(value.draft_digest)) &&
    (value.clarification_evidence_sources === undefined || arrayOf(value.clarification_evidence_sources,
      (source) => record(source) && text(source.source_name) && digest(source.source_sha256))) &&
    (value.draft_version === undefined || (Number.isSafeInteger(value.draft_version) && Number(value.draft_version) >= 1)) &&
    (value.error_code == null || text(value.error_code)) &&
    (value.question == null || text(value.question));
  if (valid && record(value) && value.draft !== undefined) {
    const draft = value.draft;
    valid =
      record(draft) &&
      (draft.output_stem === undefined || text(draft.output_stem)) &&
      record(draft.files) &&
      Object.values(draft.files).every(
        (file) => record(file) && id(file.artifact_id) && digest(file.sha256),
      ) &&
      record(draft.manifest) &&
      arrayOf(
        draft.manifest.changes,
        (change) =>
          record(change) &&
          Number.isInteger(change.slide) &&
          Number(change.slide) > 0 &&
          text(change.before) &&
          text(change.after) &&
          arrayOf(
            change.evidence,
            (evidence) =>
              record(evidence) &&
              (evidence.source_name === null || text(evidence.source_name)) &&
              (evidence.source_sha256 == null || digest(evidence.source_sha256)) &&
              (evidence.pointer === null || text(evidence.pointer)) &&
              text(evidence.value),
          ),
      ) &&
      arrayOf(
        draft.manifest.retained_unsupported_text,
        (item) =>
          record(item) &&
          Number.isInteger(item.slide) &&
          text(item.text) &&
          text(item.reason),
      ) &&
      record(draft.preview) &&
      arrayOf(draft.preview.pages, (page) => record(page) && id(page.name)) &&
      arrayOf(
        draft.preview.declared_font_fallbacks,
        (font) =>
          record(font) &&
          text(font.declared_font) &&
          text(font.environment_fallback),
      );
    if (
      valid &&
      record(draft) &&
      record(draft.files) &&
      (value.state === "review" || value.state === "published")
    ) {
      valid =
        digest(value.draft_digest) &&
        record(draft.files["candidate.pptx"]) &&
        record(draft.files["candidate.pdf"]);
    }
  } else if (
    record(value) &&
    (value.state === "review" || value.state === "published")
  )
    valid = false;
  if (valid && record(value) && value.usage !== undefined) {
    const usage = value.usage;
    valid =
      record(usage) &&
      typeof usage.settled_credits === "number" &&
      Number.isFinite(usage.settled_credits) &&
      typeof usage.pending === "boolean" &&
      (usage.settlement_pending === undefined ||
        typeof usage.settlement_pending === "boolean");
  }
  if (!valid) throw new Error("Invalid document delivery projection");
  return value as DocumentJob;
}

export async function readDocumentDraft(
  context: Readonly<DocumentDraftCanvasContext>,
  signal: AbortSignal,
): Promise<DocumentJob> {
  const value = await apiJson<unknown>(
    `/api/v1/sessions/${encodeURIComponent(context.sessionId)}/documents/${encodeURIComponent(context.jobId)}`,
    {
      signal,
      headers: spaceHeader(context),
      cache: "no-store",
    },
  );
  return parseDocumentJob(value, context.jobId);
}

/** Public idempotency identity, not authorization; stable across view disposal. */
export async function documentConfirmationId(
  context: Readonly<DocumentDraftCanvasContext>,
  draftDigest: string,
): Promise<string> {
  const bytes = new TextEncoder().encode(
    JSON.stringify([
      "document-canvas-confirm.v1",
      context.userId,
      context.teamId,
      context.workspaceId,
      context.sessionId,
      context.jobId,
      draftDigest,
    ]),
  );
  return `document-confirm:${await sha256Hex(bytes)}`;
}

export async function confirmDocumentDraft(
  context: Readonly<DocumentDraftCanvasContext>,
  draftDigest: string,
  commandId: string,
  signal: AbortSignal,
): Promise<DocumentJob> {
  const value = await apiJson<unknown>(
    `/api/v1/documents/${encodeURIComponent(context.jobId)}/confirm`,
    {
      method: "POST",
      signal,
      headers: { ...spaceHeader(context), "Content-Type": "application/json" },
      body: JSON.stringify({
        command_id: commandId,
        draft_digest: draftDigest,
      }),
    },
  );
  const job = parseDocumentJob(value, context.jobId);
  if (job.state !== "published" || job.draft_digest !== draftDigest)
    throw new Error("Unconfirmed document publication");
  return job;
}
