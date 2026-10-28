export type DocumentJobState =
  | "queued"
  | "preparing"
  | "planning"
  | "rendering"
  | "review"
  | "question"
  | "published"
  | "failed"
  | "cancelled";
interface Change {
  slide: number;
  /** PowerPoint shape name and table cell (0-based row, column) when the change is inside a table. */
  shape_name?: string;
  cell?: [number, number] | null;
  before: string;
  after: string;
  evidence: {
    source_name: string | null;
    source_sha256?: string | null;
    pointer: string | null;
    value: string;
  }[];
}
export interface DocumentJob {
  /** Last execution change (ISO); for a review draft, when it was produced. Older servers omit it. */
  updated_at?: string | null;
  id: string;
  state: DocumentJobState;
  origin_turn_id: string;
  request_text: string;
  can_confirm: boolean;
  can_revise?: boolean;
  /** "Draft N": revisions in this job's lineage plus one (recovery continuations keep N). Older servers omit it. */
  draft_version?: number;
  draft_digest?: string | null;
  /** Authorized read-time provenance; not part of the immutable draft digest. */
  clarification_evidence_sources?: { source_name: string; source_sha256: string }[];
  error_code?: string | null;
  question?: string | null;
  usage?: {
    settled_credits: number;
    pending: boolean;
    settlement_pending?: boolean;
  };
  draft?: {
    output_stem?: string;
    files: Record<string, { artifact_id: string; sha256: string }>;
    manifest: {
      changes: Change[];
      retained_unsupported_text: {
        slide: number;
        text: string;
        reason: string;
      }[];
    };
    preview: {
      pages: { name: string }[];
      declared_font_fallbacks: {
        declared_font: string;
        environment_fallback: string;
        /** Slides whose own content declares the face (newer drafts only). */
        slides?: number[];
      }[];
    };
  };
}

export interface DocumentPreviewAccess {
  headers: Record<string, string>;
  isCurrent: () => boolean;
  onAccessDenied: () => void;
}

export function isDocumentClarificationEvidence(
  evidence: Change["evidence"][number],
  sources: DocumentJob["clarification_evidence_sources"],
): boolean {
  return /^\/answers\/\d+\/answer$/.test(evidence.pointer ?? "") && !!sources?.some(
    (source) => source.source_name === evidence.source_name && source.source_sha256 === evidence.source_sha256,
  );
}

/**
 * Names come from the authorized job; without an output name the generic title is used. Same-name
 * objects are told apart by the Canvas tab strip (“· No. 2”), never by task IDs (Canvas Objects Spec).
 */
export function documentCanvasTitle(job: DocumentJob, fallback: string): string {
  const stem = job.draft?.output_stem?.trim();
  if (!stem) return fallback;
  const extension = documentSourceExtension(job);
  return extension && !stem.toLowerCase().endsWith(`.${extension}`) ? `${stem}.${extension}` : stem;
}

/** Extension of the editable source file (pptx / docx), from the draft's file names. */
export function documentSourceExtension(job: DocumentJob): string | null {
  const names = Object.keys(job.draft?.files ?? {});
  const source = names.find((name) => !name.toLowerCase().endsWith(".pdf"));
  const match = source ? /\.([a-z0-9]+)$/i.exec(source) : null;
  return match ? match[1].toLowerCase() : null;
}

export function documentCanvasDisambiguator(job: DocumentJob): string {
  return job.id.replace(/^djob_/, "").slice(-8);
}

/** Lifecycle is authoritative even when a repaired task retains an earlier error. */
export function documentCanvasStateKey(job: DocumentJob): string {
  return job.state === "failed" && job.error_code === "document_information_incomplete"
    ? "state_incomplete"
    : `state_${job.state}`;
}

/** Discovery shows context separately from the object's name; the editor retains the full request. */
export function documentCanvasRequestExcerpt(job: DocumentJob): string {
  const characters = Array.from(job.request_text.trim().replace(/\s+/g, " "));
  return characters.length > 120 ? `${characters.slice(0, 120).join("")}…` : characters.join("");
}

/**
 * Human-readable location of a data fact (Canvas Objects Spec C7): JSON pointers are never shown.
 * XLSX facts are `/{sheet}/{row}/{column}` and CSV facts `/{row}/{column}` (0-based, see
 * bowhead_core.documents.evidence); other sources only name the file.
 */
export function documentEvidenceLocation(sourceName: string | null, pointer: string | null): { sheet: string | null; row: number | null } {
  if (!sourceName || !pointer?.startsWith("/")) return { sheet: null, row: null };
  const parts = pointer.slice(1).split("/").map((part) => part.replace(/~1/g, "/").replace(/~0/g, "~"));
  const suffix = sourceName.toLowerCase().split(".").pop();
  const index = (value: string | undefined) => (value !== undefined && /^\d+$/.test(value) ? Number(value) + 1 : null);
  if (suffix === "xlsx" && parts.length === 3) return { sheet: parts[0] || null, row: index(parts[1]) };
  if (suffix === "csv" && parts.length === 2) return { sheet: null, row: index(parts[0]) };
  return { sheet: null, row: null };
}
