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
const nonnegativeInteger = (value: unknown) => Number.isSafeInteger(value) && Number(value) >= 0;

function validWordLocation(value: unknown): boolean {
  return record(value) && value.kind === "word_block" && text(value.part) && text(value.block_id) &&
    nonnegativeInteger(value.block_index) && ["body", "header", "footer"].includes(String(value.story)) &&
    arrayOf(value.sections, (section) => nonnegativeInteger(section) && Number(section) > 0) && (value.table_id === null || text(value.table_id)) &&
    ((value.table_id === null && value.row === null && value.column === null) ||
      (text(value.table_id) && nonnegativeInteger(value.row) && nonnegativeInteger(value.column)));
}

function validWordDraft(draft: Record<string, unknown>): boolean {
  const descriptor = draft.descriptor;
  const manifest = draft.manifest;
  const preview = draft.preview;
  const files = draft.files;
  return record(descriptor) && Object.keys(descriptor).length === 4 && descriptor.format === "docx" &&
    descriptor.source_name === "candidate.docx" && descriptor.pdf_name === "candidate.pdf" &&
    Array.isArray(descriptor.published_outputs) &&
    (JSON.stringify(descriptor.published_outputs) === '["source"]' || JSON.stringify(descriptor.published_outputs) === '["source","pdf"]') &&
    draft.kind === "authoring_draft" && draft.semantic_status === "unapproved" && draft.requires_full_review === true &&
    record(manifest) && manifest.schema_version === 3 && digest(manifest.source_sha256) && digest(manifest.candidate_sha256) &&
    nonnegativeInteger(manifest.source_block_count) && manifest.source_block_count === manifest.candidate_block_count &&
    record(files) && record(files["candidate.docx"]) && files["candidate.docx"].sha256 === manifest.candidate_sha256 &&
    record(preview) && preview.profile === "ordinary-docx-preview-v1" && preview.source_sha256 === manifest.candidate_sha256 &&
    preview.native_word_certified === false && preview.text_and_geometry === "visible_page_bounds_and_body_character_coverage" &&
    Array.isArray(preview.pages) && preview.pages.length > 0 && preview.pages.length === manifest.candidate_page_count;
}

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
    (value.superseded_by_job_id === undefined ||
      (id(value.superseded_by_job_id) && value.superseded_by_job_id !== value.id &&
        value.state === "cancelled" && !value.can_confirm && value.can_revise !== true &&
        digest(value.draft_digest) && record(value.draft))) &&
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
    const word = record(draft) && draft.profile === "docx-script-worker-v1";
    valid =
      record(draft) &&
      (draft.requires_full_review === undefined || typeof draft.requires_full_review === "boolean") &&
      (draft.output_stem === undefined || text(draft.output_stem)) &&
      record(draft.files) &&
      Object.values(draft.files).every(
        (file) => record(file) && id(file.artifact_id) && digest(file.sha256),
      ) &&
      record(draft.manifest) &&
      (word ? draft.manifest.comparison === "source_preserving_word_text" :
        draft.manifest.comparison === undefined || draft.manifest.comparison === "observed_object_facts" || draft.manifest.comparison === "unavailable") &&
      (word ? validWordDraft(draft) : draft.descriptor === undefined &&
        ((draft.manifest.schema_version === undefined && draft.profile !== "pptx-script-worker-v1") ||
        (draft.manifest.schema_version === 2 && draft.profile === "pptx-script-worker-v1" &&
          draft.kind === "authoring_draft" && draft.semantic_status === "unapproved" && draft.requires_full_review === true))) &&
      arrayOf(
        draft.manifest.changes,
        (change) =>
          record(change) &&
          (word ? validWordLocation(change.location) && change.slide === undefined : Number.isInteger(change.slide) && Number(change.slide) > 0 && change.location === undefined) &&
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
        record(draft.files[word ? "candidate.docx" : "candidate.pptx"]) &&
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
