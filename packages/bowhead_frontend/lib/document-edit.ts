/**
 * In-place editing of a Word result: the client of the owner Results routes (docx inline edit
 * design §7). Mirrors `presentation-edit.ts`: the inventory read, the save, refusals with their
 * stable codes and facts, the pending edits and their re-application on a newer version.
 */

import { appFetch } from "@/lib/base-path";
import type { ErrorDiagnostics } from "@/lib/error-presentation";
import type { EditedCopy, LatestVersionFacts, PresentationEditMode } from "@/lib/presentation-edit";

export type DocumentEditMode = PresentationEditMode;

/** Why a paragraph cannot be edited here (engine reasons, design §9). */
export type DocumentReadOnlyReason =
  | "header_footer"
  | "note"
  | "tracked_change"
  | "field_span"
  | "content_control"
  | "text_box"
  | "merged_continuation";

/** Why the whole document refuses edits (design §9). */
export type DocumentRefusal = "protection_enforced" | "digitally_signed" | "track_changes_on";

export interface DocumentTableCell {
  key: string;
  row: number;
  col: number;
  row_span: number;
  col_span: number;
}

export interface DocumentParagraph {
  /** The paragraph's path, such as `document:b4.r2.c1.b0`. */
  key: string;
  kind: "body" | "cell";
  table: DocumentTableCell | null;
  /** Flattened text: characters, tabs, soft breaks (`\n`) and one field token per atomic object. */
  text: string;
  heading_level: number | null;
  editable: boolean;
  reason: DocumentReadOnlyReason | string | null;
}

export interface DocumentReadOnlyParagraph {
  key: string;
  part: "header" | "footer" | "footnote" | "endnote";
  text: string;
  reason: DocumentReadOnlyReason | string;
}

export interface DocumentInventory {
  artifact_id: string;
  version_no: number;
  edit_mode: DocumentEditMode;
  /** The first section's page size, in twips. */
  page: { width: number; height: number };
  field_token: string;
  document: { refusal: DocumentRefusal | string | null };
  paragraphs: DocumentParagraph[];
  /** Read-only parts: headers and footers with text, notes, comments. */
  read_only_parts: { header_footer: number; note: number; comment: number };
  /** The text of headers, footers and notes: found on the pages and explained, never edited (canvas W7). */
  read_only_paragraphs: DocumentReadOnlyParagraph[];
  file: { filename: string; mime: string; size: number } | null;
  new_filename: string | null;
  edited_copies: EditedCopy[];
  drops_on_save?: number[];
}

export interface DocumentWireEdit {
  key: string;
  expect: string;
  text: string;
}

export interface DocumentSaveChange {
  index: number;
  key: string;
  before: string;
  after: string;
}

export interface DocumentSaveResult {
  artifact_id: string;
  version_no: number;
  filename: string;
  new_file: boolean;
  changes: DocumentSaveChange[];
}

export interface RefusedDocumentEdit {
  index: number | null;
  key: string | null;
  code: string;
  reason: string | null;
}

/** A refused request, with the server's stable code and the facts the editor shows. */
export class DocumentEditError extends Error {
  readonly status: number | null;
  readonly code: string;
  readonly latest: LatestVersionFacts | null;
  readonly refused: RefusedDocumentEdit[];
  readonly reason: string | null;
  readonly diagnosticId: string | null;
  readonly requestId: string | null;

  constructor(code: string, options: {
    status?: number | null;
    latest?: LatestVersionFacts | null;
    refused?: RefusedDocumentEdit[];
    reason?: string | null;
    diagnosticId?: string | null;
    requestId?: string | null;
  } = {}) {
    super(code);
    this.name = "DocumentEditError";
    this.code = code;
    this.status = options.status ?? null;
    this.latest = options.latest ?? null;
    this.refused = options.refused ?? [];
    this.reason = options.reason ?? null;
    this.diagnosticId = options.diagnosticId ?? null;
    this.requestId = options.requestId ?? null;
  }

  /** Diagnostics for `ErrorNotice`: status, code and the server's `de_…` reference, never the cause. */
  get diagnostics(): ErrorDiagnostics | undefined {
    if (this.status === null && !this.diagnosticId) return undefined;
    return {
      schemaVersion: 1,
      disclosure: "authenticated",
      ...(this.status !== null ? { status: this.status } : {}),
      code: this.code,
      ...(this.diagnosticId && /^[a-z][a-z0-9_]{0,63}$/.test(this.diagnosticId) ? { diagnosticCode: this.diagnosticId } : {}),
      ...(this.requestId && /^[0-9a-f]{32}$/.test(this.requestId) ? { requestId: this.requestId } : {}),
      ...(this.status === null ? { serverRequestIdUnavailable: true as const } : {}),
      observedAt: new Date().toISOString(),
    };
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseLatest(value: unknown): LatestVersionFacts | null {
  if (!isRecord(value) || typeof value.artifact_id !== "string" || typeof value.version_no !== "number") return null;
  return {
    artifact_id: value.artifact_id,
    version_no: value.version_no,
    created_at: typeof value.created_at === "string" ? value.created_at : "",
    produced_by: value.produced_by === "user" ? "user" : "agent",
    request_excerpt: typeof value.request_excerpt === "string" ? value.request_excerpt : null,
  };
}

async function errorFor(response: Response): Promise<DocumentEditError> {
  const requestId = response.headers.get("x-request-id");
  let error: Record<string, unknown> = {};
  try {
    const body = await response.json() as unknown;
    const detail = isRecord(body) ? body.detail : null;
    if (isRecord(detail) && isRecord(detail.error)) error = detail.error;
  } catch { /* A non-JSON refusal keeps only its status. */ }
  const code = typeof error.code === "string" && error.code ? error.code : `http_${response.status}`;
  const refused = Array.isArray(error.edits)
    ? error.edits.flatMap((item): RefusedDocumentEdit[] => isRecord(item) && typeof item.code === "string"
      ? [{
        index: typeof item.index === "number" ? item.index : null,
        key: typeof item.key === "string" ? item.key : null,
        code: item.code,
        reason: typeof item.reason === "string" ? item.reason : null,
      }]
      : [])
    : [];
  return new DocumentEditError(code, {
    status: response.status,
    latest: parseLatest(error.latest),
    refused,
    reason: typeof error.reason === "string" ? error.reason : null,
    diagnosticId: typeof error.diagnostic_id === "string" ? error.diagnostic_id : null,
    requestId,
  });
}

async function post(path: string, body: unknown, signal?: AbortSignal): Promise<unknown> {
  let response: Response;
  try {
    response = await appFetch(path, {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new DocumentEditError("network");
  }
  if (!response.ok) throw await errorFor(response);
  try {
    return await response.json() as unknown;
  } catch {
    throw new DocumentEditError("response_invalid", { status: response.status });
  }
}

function sessionPath(sessionId: string, tail: string): string {
  return `/api/v1/sessions/${encodeURIComponent(sessionId)}/results/${tail}`;
}

function parseTableCell(value: unknown): DocumentTableCell | null {
  if (!isRecord(value) || typeof value.key !== "string" || typeof value.row !== "number" || typeof value.col !== "number") return null;
  return {
    key: value.key,
    row: value.row,
    col: value.col,
    row_span: typeof value.row_span === "number" ? value.row_span : 1,
    col_span: typeof value.col_span === "number" ? value.col_span : 1,
  };
}

function count(value: unknown): number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : 0;
}

export function parseDocumentInventory(value: unknown): DocumentInventory {
  if (
    !isRecord(value)
    || typeof value.artifact_id !== "string"
    || typeof value.version_no !== "number"
    || (value.edit_mode !== "version" && value.edit_mode !== "new_file")
    || !Array.isArray(value.paragraphs)
  ) throw new DocumentEditError("response_invalid");
  const page = isRecord(value.page) && typeof value.page.width === "number" && typeof value.page.height === "number"
    ? { width: value.page.width, height: value.page.height }
    : { width: 12240, height: 15840 };
  const document = isRecord(value.document) ? value.document : {};
  const parts = isRecord(value.read_only_parts) ? value.read_only_parts : {};
  return {
    artifact_id: value.artifact_id,
    version_no: value.version_no,
    edit_mode: value.edit_mode,
    page,
    field_token: typeof value.field_token === "string" ? value.field_token : "￼",
    document: { refusal: typeof document.refusal === "string" ? document.refusal : null },
    paragraphs: value.paragraphs.flatMap((item): DocumentParagraph[] => {
      if (!isRecord(item) || typeof item.key !== "string" || typeof item.text !== "string") return [];
      return [{
        key: item.key,
        kind: item.kind === "cell" ? "cell" : "body",
        table: parseTableCell(item.table),
        text: item.text,
        heading_level: typeof item.heading_level === "number" ? item.heading_level : null,
        editable: item.editable === true,
        reason: typeof item.reason === "string" ? item.reason : null,
      }];
    }),
    read_only_parts: { header_footer: count(parts.header_footer), note: count(parts.note), comment: count(parts.comment) },
    read_only_paragraphs: Array.isArray(value.read_only_paragraphs)
      ? value.read_only_paragraphs.flatMap((item): DocumentReadOnlyParagraph[] => isRecord(item) && typeof item.key === "string" && typeof item.text === "string"
        && (item.part === "header" || item.part === "footer" || item.part === "footnote" || item.part === "endnote")
        ? [{ key: item.key, part: item.part, text: item.text, reason: typeof item.reason === "string" ? item.reason : "header_footer" }]
        : [])
      : [],
    file: isRecord(value.file) && typeof value.file.filename === "string" && typeof value.file.mime === "string" && typeof value.file.size === "number"
      ? { filename: value.file.filename, mime: value.file.mime, size: value.file.size }
      : null,
    new_filename: typeof value.new_filename === "string" ? value.new_filename : null,
    edited_copies: Array.isArray(value.edited_copies)
      ? value.edited_copies.flatMap((item): EditedCopy[] => isRecord(item) && typeof item.artifact_id === "string" && typeof item.filename === "string"
        ? [{
          artifact_id: item.artifact_id,
          filename: item.filename,
          version_no: typeof item.version_no === "number" ? item.version_no : 1,
          made_by: item.made_by === "ai" ? "ai" : "you",
        }]
        : [])
      : [],
    ...(Array.isArray(value.drops_on_save)
      ? { drops_on_save: value.drops_on_save.filter((item): item is number => typeof item === "number" && Number.isInteger(item) && item > 0) }
      : {}),
  };
}

export async function fetchDocumentInventory({
  sessionId, artifactId, signal,
}: { sessionId: string; artifactId: string; signal?: AbortSignal }): Promise<DocumentInventory> {
  return parseDocumentInventory(await post(sessionPath(sessionId, "document-edit/inventory"), { artifact_id: artifactId }, signal));
}

export async function saveDocumentEdits({
  sessionId, artifactId, edits, signal,
}: { sessionId: string; artifactId: string; edits: DocumentWireEdit[]; signal?: AbortSignal }): Promise<DocumentSaveResult> {
  const value = await post(sessionPath(sessionId, "document-edits"), { artifact_id: artifactId, edits }, signal);
  if (!isRecord(value) || typeof value.artifact_id !== "string" || typeof value.version_no !== "number" || typeof value.filename !== "string") {
    throw new DocumentEditError("response_invalid");
  }
  return {
    artifact_id: value.artifact_id,
    version_no: value.version_no,
    filename: value.filename,
    new_file: value.new_file === true,
    changes: Array.isArray(value.changes)
      ? value.changes.flatMap((item): DocumentSaveChange[] => isRecord(item) && typeof item.key === "string"
        ? [{
          index: typeof item.index === "number" ? item.index : 0,
          key: item.key,
          before: typeof item.before === "string" ? item.before : "",
          after: typeof item.after === "string" ? item.after : "",
        }]
        : [])
      : [],
  };
}

// ── Pending edits ───────────────────────────────────────────────────────────────────

/** One paragraph's unsaved text: `expect` is the text the save will find there. */
export interface DocumentPendingEdit {
  key: string;
  expect: string;
  text: string;
}

/** Sets a paragraph's text, dropping the edit when the text is back to what it was. */
export function upsertDocumentEdit(edits: ReadonlyArray<DocumentPendingEdit>, edit: DocumentPendingEdit): DocumentPendingEdit[] {
  const index = edits.findIndex((item) => item.key === edit.key);
  if (edit.text === edit.expect) return index < 0 ? [...edits] : edits.filter((item) => item.key !== edit.key);
  if (index < 0) return [...edits, edit];
  const next = [...edits];
  next[index] = { ...next[index], text: edit.text };
  return next;
}

export function removeDocumentEdit(edits: ReadonlyArray<DocumentPendingEdit>, key: string): DocumentPendingEdit[] {
  return edits.filter((item) => item.key !== key);
}

export function toDocumentWireEdits(edits: ReadonlyArray<DocumentPendingEdit>): DocumentWireEdit[] {
  return edits.map(({ key, expect, text }) => ({ key, expect, text }));
}

/** Each changed paragraph's number, in the order it was first changed: the page marks and 修改记录 share it. */
export function changeNumbers(edits: ReadonlyArray<DocumentPendingEdit>): Map<string, number> {
  const numbers = new Map<string, number>();
  for (const edit of edits) if (!numbers.has(edit.key)) numbers.set(edit.key, numbers.size + 1);
  return numbers;
}

export function countFieldTokens(text: string, token: string): number {
  return token ? text.split(token).length - 1 : 0;
}

/**
 * What the save would refuse before it is sent: a field token removed or added (fields are kept
 * whole), or a new paragraph (only tabs and soft breaks are allowed inside one).
 */
export function editProblem(edit: DocumentPendingEdit, fieldToken: string): "field_changed" | "new_paragraph" | null {
  if (countFieldTokens(edit.text, fieldToken) !== countFieldTokens(edit.expect, fieldToken)) return "field_changed";
  if (/[\r\u0000-\u0008\u000b\u000c\u000e-\u001f\u2028\u2029]/.test(edit.text)) return "new_paragraph";
  return null;
}

// ── Re-applying after a refused save (design §7.2) ──────────────────────────────────

export interface DocumentReapplyRow {
  edit: DocumentPendingEdit;
  /** The paragraph is unchanged in the newer version. */
  applicable: boolean;
  /** What the newer version holds when it differs. */
  latestText: string | null;
  /** 「用我的」: the edit made against the newer version, when the paragraph is still there and editable. */
  rebased: DocumentPendingEdit | null;
}

export function matchDocumentEdits(edits: ReadonlyArray<DocumentPendingEdit>, latest: DocumentInventory): DocumentReapplyRow[] {
  const byKey = new Map(latest.paragraphs.map((paragraph) => [paragraph.key, paragraph]));
  return edits.map((edit): DocumentReapplyRow => {
    const paragraph = byKey.get(edit.key);
    if (!paragraph || !paragraph.editable || latest.document.refusal) return { edit, applicable: false, latestText: paragraph?.text ?? null, rebased: null };
    const same = paragraph.text === edit.expect;
    return { edit, applicable: same, latestText: same ? null : paragraph.text, rebased: same ? null : { ...edit, expect: paragraph.text } };
  });
}
