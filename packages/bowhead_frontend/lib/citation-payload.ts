import type { Citation } from "./types";

const KINDS: ReadonlySet<Citation["kind"]> = new Set([
  "data_ref",
  "chart_section",
  "tool_output",
  "kb_chunk",
  "kb_row",
  "web_clip",
]);

const ACCESS_STATES: ReadonlySet<NonNullable<Citation["accessState"]>> = new Set([
  "linked_accessible",
  "linked_labeled",
  "linked_restricted",
  "linked_missing",
  "unsupported",
]);

function nonEmpty(value: unknown): string | undefined {
  return typeof value === "string" && value ? value : undefined;
}

/** `turn.citation_registered` payload → Citation. Live (use-turn-runner) and refresh replay
 *  (page.tsx hydrate) share this implementation, keeping fields and the kind allowlist consistent (live == replay).
 *
 *  - Read `citation_seq`, not `seq` (the bus envelope overwrites `seq` with its incrementing counter);
 *  - Unknown kinds fall back to `data_ref` (old-client compatibility);
 *  - kb location fields are sent only when the backend allows viewing; drop the entire group if any critical field is missing. */
export function citationFromPayload(payload: Record<string, unknown>): Citation | null {
  const seq = Number(payload.citation_seq ?? payload.seq);
  if (!Number.isInteger(seq) || seq < 1) return null;
  const artifactId = String(payload.artifact_id ?? payload.artifactId ?? "");
  if (!artifactId) return null;
  const kindRaw = String(payload.kind ?? "data_ref") as Citation["kind"];
  const kind = KINDS.has(kindRaw) ? kindRaw : "data_ref";
  const stateRaw = payload.access_state;
  const accessState = typeof stateRaw === "string" && ACCESS_STATES.has(stateRaw as NonNullable<Citation["accessState"]>)
    ? (stateRaw as Citation["accessState"])
    : undefined;
  const citation: Citation = {
    seq,
    artifactId,
    label: payload.label != null ? String(payload.label) : null,
    kind,
    accessState,
    safeAnchorId: nonEmpty(payload.safe_anchor_id),
  };
  const fileId = nonEmpty(payload.kb_file_id);
  const chunkId = nonEmpty(payload.kb_chunk_id);
  if (kind === "kb_chunk" && fileId && chunkId) {
    citation.kb = { fileId, chunkId, scope: nonEmpty(payload.kb_scope), spaceId: nonEmpty(payload.kb_space_id) };
    if (nonEmpty(payload.kb_generation_id)) citation.kb.generationId = nonEmpty(payload.kb_generation_id);
    if (nonEmpty(payload.kb_source_check_state)) citation.kb.sourceCheckState = nonEmpty(payload.kb_source_check_state);
  }
  return citation;
}

/** A repeated registration can refine a source check, never remap its identity. */
export function mergeCitation(previous: Citation | undefined, incoming: Citation): Citation {
  if (!previous) return incoming;
  if (previous.artifactId !== incoming.artifactId || !incoming.kb?.generationId) return previous;
  if (previous.kb?.generationId && previous.kb.generationId !== incoming.kb.generationId) return previous;
  if (previous.kb?.generationId === incoming.kb.generationId && previous.kb?.sourceCheckState === incoming.kb.sourceCheckState) return previous;
  return { ...previous, kb: incoming.kb };
}
