/**
 * Citation data model — maintained internally by the frontend.
 *
 * Existing backend fields:
 *   - narrative.markdown contains `[num:V:U]` placeholders (no src id, P3.3 backward compatibility)
 *   - insight_card.source_refs: list[str] (id array only)
 *
 * Browser contract:
 *   - all linkage identities are report-scoped opaque aliases;
 *   - only `safe_*` fields may be presented;
 *   - access is rechecked before an interactive preview or action opens.
 */

export interface SourceRef {
  /** Report-scoped opaque alias; never a persisted resource identity. */
  id: string;
  source_alias?: string;
  /** Batch 2 six states (tech-design §4.1). `linked_unavailable` is a legacy value, removed in batch 3. */
  access_state?:
    | "linked_accessible"
    | "linked_labeled"
    | "linked_restricted"
    | "linked_missing"
    | "unsupported"
    | "linked_unavailable";
  generic_type?: "query_result" | "document" | "web_source" | "generated_artifact" | "other_source";
  ordinal?: number;
  safe_label?: string;
  safe_query?: string;
  safe_preview?: string;
  safe_preview_columns?: string[];
  safe_preview_rows?: Record<string, string>[];
  safe_total_rows?: number;
  safe_url?: string;
  safe_preview_url?: string;
  /** Legacy fixture compatibility only. Browser source UI must not consume these. */
  tool?: string;
  query?: string;
  /** Human-readable title (sources entry.label, often "tool: query"), used for the panel title,
   *  not the query slot (which would duplicate the "query: bi_query: …" prefix). */
  label?: string;
  /** SQL produced by NL2SQL (display if present). */
  sql?: string;
  /** CSV download link. */
  artifact_id?: string;
  /** bi_query batch: per-sub-query data sources (each sub-query has its own artifact);
   *  popover lists all N to avoid showing only 1-of-N in multi-query reports. artifact_id == sub_sources[0]. */
  sub_sources?: { artifact_id: string; query: string }[];
  // ─── citation-hover original data: inline preview ───────────────
  // A bi_query source's `artifact_id` is a datarc `tl_…` ledger ref (404s on
  // /files), so the backend folds the tool result's `sample_rows` into these so
  // the hover popover can show original rows inline instead of a dead download link.
  // (Fetchable `art_` artifacts have no preview here — the popover lazy-fetches
  // /preview for those. See artifact-preview-cache.ts.)
  /** Column order for the inline preview table (≤6). */
  preview_columns?: string[];
  /** Row dicts for the inline preview table (≤5, cells stringified). */
  preview_rows?: Record<string, string>[];
  /** True total row count behind the query (preview_rows may be a subset). */
  total_rows?: number;
  /** Originating turn (for navigation). */
  turn_id?: string;
  /** Originating tool call. */
  tool_call_id?: string;
  // ─── V7 KB sprint (P1-F + Day 14) ───────────────
  /** KB chunk id (for kb.search hits; fetch chunk content in popover + navigate to KB page). */
  kb_chunk_id?: string;
  /** KB file id (containing the chunk, for KB drawer navigation). */
  kb_file_id?: string;
  /** KB filename (displayed in popover, no backend lookup required). */
  kb_filename?: string;
  /** KB file page (displayed in popover, navigate to details). */
  kb_page?: number;
  /** KB section_title (displayed in popover). */
  kb_section_title?: string;
  /** Knowledge-base answer citations batch 3: file scope (personal / space / team), for deep links and location rows. */
  kb_scope?: string;
  /** Space containing a space file; use as `X-Space-Id` when fetching a chunk. */
  kb_space_id?: string;
}

/** Backend marks viewer access and supplies KB location → KB location; others (including restricted / missing) → null. */
export function kbRefOfSource(
  source: SourceRef | undefined,
): { fileId: string; chunkId: string; scope?: string; spaceId?: string } | null {
  if (!source || source.access_state !== "linked_accessible") return null;
  if (!source.kb_file_id || !source.kb_chunk_id) return null;
  return {
    fileId: source.kb_file_id,
    chunkId: source.kb_chunk_id,
    scope: source.kb_scope,
    spaceId: source.kb_space_id,
  };
}

/** Report KB sources (accessible with location, or restricted/missing document sources) use KB cards, not artifact probing. */
export function isKbReportSource(source: SourceRef | undefined): boolean {
  if (!source) return false;
  if (kbRefOfSource(source)) return true;
  return source.generic_type === "document"
    && (source.access_state === "linked_restricted" || source.access_state === "linked_missing");
}

/** Copy KB fields from a viewer-safe source entry (only when accessible; backend already omits them for restricted/missing sources). */
export function copyKbFields(entry: Record<string, unknown>, target: SourceRef): void {
  if (entry.access_state !== "linked_accessible") return;
  for (const key of ["kb_file_id", "kb_chunk_id", "kb_scope", "kb_space_id"] as const) {
    const value = entry[key];
    if (typeof value === "string" && value) target[key] = value;
  }
}

const VIEWER_SOURCE_ALIAS_RE = /^source-ref-v1:[A-Za-z0-9_-]{43}$/;

export function isViewerSourceAlias(value: unknown): value is string {
  return typeof value === "string" && VIEWER_SOURCE_ALIAS_RE.test(value);
}

const VIEWER_SOURCE_GENERIC_TYPES = new Set([
  "query_result",
  "document",
  "web_source",
  "generated_artifact",
  "other_source",
]);

/**
 * Deep-copy a viewer presentation while removing business detail for aliases
 * whose current authorization probe failed. Canonical source identities never
 * belong in this value; opaque aliases and neutral shape metadata remain so
 * citations keep their stable slots.
 */
export function redactViewerSourceIdentitiesInValue<T>(
  value: T,
  aliases: ReadonlySet<string>,
): T {
  // Return the same reference unchanged when nothing needs clearing: canvas store and useMemo detect changes by reference;
  // deep-copying every time makes even idempotent revocation rerender the whole page (see source-access-registry loop explanation).
  if (Array.isArray(value)) {
    const next = value.map((child) => redactViewerSourceIdentitiesInValue(child, aliases));
    return (next.every((child, index) => child === value[index]) ? value : next) as T;
  }
  if (value === null || typeof value !== "object") return value;
  const record = value as Record<string, unknown>;
  const alias = typeof record.source_alias === "string"
    ? record.source_alias
    : record.access_state && typeof record.id === "string" ? record.id : undefined;
  if (alias && aliases.has(alias)) {
    const redacted: Record<string, unknown> = {
      id: alias,
      source_alias: alias,
      access_state: "linked_unavailable",
      generic_type: VIEWER_SOURCE_GENERIC_TYPES.has(String(record.generic_type))
        ? record.generic_type
        : "other_source",
    };
    if (typeof record.ordinal === "number" && Number.isSafeInteger(record.ordinal) && record.ordinal > 0) {
      redacted.ordinal = record.ordinal;
    }
    const keys = Object.keys(record);
    const alreadyRedacted = keys.length === Object.keys(redacted).length
      && keys.every((key) => record[key] === redacted[key]);
    return (alreadyRedacted ? value : redacted) as T;
  }
  let changed = false;
  const entries = Object.entries(record).map(([key, child]) => {
    const next = redactViewerSourceIdentitiesInValue(child, aliases);
    if (next !== child) changed = true;
    return [key, next] as const;
  });
  const next = (changed ? Object.fromEntries(entries) : record) as Record<string, unknown>;
  return redactChartEvidenceInValue(next, aliases) as T;
}

/** Source revocation also removes verified quotes and every declared dependent chart caption. */
function redactChartEvidenceInValue(record: Record<string, unknown>, aliases: ReadonlySet<string>): Record<string, unknown> {
  const plan = record.presentation_plan as Record<string, unknown> | undefined;
  if (!plan || ![1, 2].includes(Number(plan.version)) || !Array.isArray(plan.evidence_notes)) return record;
  let changed = false;
  const notes = plan.evidence_notes.map((note: Record<string, unknown>) => {
    if (!note || typeof note !== "object") return note;
    const source = note.source_ref as SourceRef | undefined;
    const alias = source?.source_alias || source?.id;
    if (!alias || !aliases.has(alias) || note.availability === "unavailable") return note;
    const { excerpt: _excerpt, locator: _locator, verified_content_digest: _digest, ...safe } = note;
    changed = true;
    return { ...safe, availability: "unavailable" };
  });
  const dataset = plan.dataset as Record<string, unknown> | undefined;
  let safeDataset = dataset;
  if (dataset && typeof dataset.artifact_id === "string" && aliases.has(dataset.artifact_id) && dataset.availability !== "unavailable") {
    const { content_digest: _content, schema_digest: _schema, ...safe } = dataset;
    safeDataset = { ...safe, availability: "unavailable" };
    changed = true;
  }
  if (!changed) return record;
  const safeBindings = plan.bindings && typeof plan.bindings === "object" && safeDataset?.availability !== "available"
    ? Object.fromEntries(Object.entries(plan.bindings).map(([id, descriptor]) => [id, { kind: descriptor.kind, availability: "unavailable" }])) : plan.bindings;
  const result: Record<string, unknown> = { ...record, presentation_plan: { ...plan, evidence_notes: notes, dataset: safeDataset, ...(safeBindings ? { bindings: safeBindings } : {}) } };
  const unavailable = new Set(notes.filter((note) => note?.availability === "unavailable").map((note) => note.id));
  const safePlan = result.presentation_plan as Record<string, unknown>;
  if (Array.isArray(plan.annotations) && (unavailable.size > 0 || safeDataset?.availability !== "available")) safePlan.annotations = plan.annotations.map((item) => ({ id: item.id, view_id: item.view_id, availability: "unavailable" }));
  if (Array.isArray(plan.guide_steps) && (unavailable.size > 0 || safeDataset?.availability !== "available")) safePlan.guide_steps = plan.guide_steps.map((item) => ({ ...item, title: "" }));
  if (Array.isArray(plan.table_trends) && safeDataset?.availability !== "available") safePlan.table_trends = plan.table_trends.map((item) => ({ view_id: item.view_id, availability: "unavailable" }));
  if (Array.isArray(plan.analytical_views) && (unavailable.size > 0 || safeDataset?.availability !== "available")) safePlan.analytical_views = plan.analytical_views.map((item) => ({ view_id: item.view_id, availability: "unavailable" }));
  const dependencies = plan.text_dependencies;
  if (dependencies && typeof dependencies === "object") for (const [pointer, dependency] of Object.entries(dependencies)) {
    if (!dependency || typeof dependency !== "object" || !Array.isArray(dependency.note_ids)) continue;
    if (!dependency.note_ids.some((nid: string) => unavailable.has(nid)) && safeDataset?.availability === "available") continue;
    const match = /^\/(title|subtitle)$|^\/views\/([0-5])\/spec\/(title|subtitle)$/.exec(pointer);
    if (!match) continue;
    const field = match[1] || match[3]!;
    if (match[1]) {
      if (field === "title") result.title = "";
      else delete result.subtitle;
    } else if (Array.isArray(result.views)) {
      result.views = result.views.map((view, index) => {
        if (index !== Number(match[2]) || !view || typeof view !== "object" || !view.spec) return view;
        const spec = { ...view.spec };
        if (field === "title") spec.title = "";
        else delete spec.subtitle;
        return { ...view, spec };
      });
    }
  }
  return result;
}

/** Placeholder type dispatched after a Citation Plugin regex match. */
export type CitationKind = "num" | "ref" | "chart";

/** Decoration data (data-attrs written to DOM, read by popover). */
export interface CitationDecorationData {
  kind: CitationKind;
  /** Numeric string for num (e.g. "347100"). */
  value?: string;
  /** Unit for num (e.g. "CNY"). */
  unit?: string;
  /** Source id reference (nullable for backward-compatible cases without src). */
  sourceId?: string;
  /** Referenced chart section_id for chart. */
  chartSectionId?: string;
}

/** source-accessibility §1.6 consumer half c2: one row in the canvas detail `turnCitations` sidecar.
 *  Backend contract (tech-design §4.1 six states + single `citation_presentation` mapping):
 *  only the OWNED family (linked_accessible / linked_labeled) carries safe_label / safe_anchor_id;
 *  other states have only the triple, so frontend **does not need** another permission check; render only with safe_label. */
export interface TurnCitationRow {
  /** Equals N in body `[cite:N]` (1-based within a turn). */
  seq: number;
  kind: string;
  access_state: string;
  safe_label?: string;
  /** Only chart_section and OWNED: canvas DOM anchor (data-section-id value). */
  safe_anchor_id?: string;
}

/** turn_id → citation rows for that turn, supplied with `CanvasDetailDTO.turnCitations`. */
export type TurnCitationsMap = Record<string, TurnCitationRow[]>;
