import { validAnalyticalViews, type AnalyticalInput, type ProjectedAnalyticalView } from "./analytical";
import { validAnnotationEvidence } from "./contextual-evidence";
import { validTableTrends, type TableTrendInput, type ProjectedTableTrend } from "./table-trends";
import type { FrameBinding } from "./prepared-frames";
import { validAnnotations, type AnnotationInput, type ProjectedAnnotation } from "./annotations";
import type { SourceRef } from "../canvas-pm-adapter/citation-types";
import { validProjectedFocus, type FocusBinding, type NativeInteraction } from "./linked-focus";
import { validSharedScales, type SharedScale, type SharedScaleInput } from "./shared-scales";
import { validGuides, type GuideInteraction, type GuideStep } from "./guided-reading";

export interface EvidenceNoteInput {
  id: string;
  measure_keys: string[];
  ref_id: string;
  excerpt: string;
  locator?: string;
}
export interface EvidencePlanInputV1 {
  version: 1 | 2 | 3;
  evidence_notes?: EvidenceNoteInput[];
  view_bindings?: Record<string, string>;
  interactions?: Array<NativeInteraction | GuideInteraction>;
  guide_steps?: GuideStep[];
  scale_groups?: SharedScaleInput[];
  annotations?: AnnotationInput[];
  table_trends?: TableTrendInput[];
  analytical_views?: AnalyticalInput[];
}
export interface EvidenceNote {
  id: string;
  measure_keys: string[];
  source_ref: SourceRef;
  availability: "available" | "unavailable";
  excerpt?: string;
  locator?: string;
  verified_content_digest?: string;
}
export interface EvidencePlanV1 {
  version: 1 | 2 | 3;
  dataset: {
    artifact_id: string;
    availability: "available" | "unavailable";
    content_digest?: string;
    schema_digest?: string;
  };
  evidence_notes: EvidenceNote[];
  text_dependencies: Record<string, { note_ids: string[]; dataset: boolean }>;
  bindings?: Record<string, FocusBinding | FrameBinding | { kind: "row" | "entity" | "time" | "frame"; availability: "unavailable" }>;
  view_bindings?: Record<string, string>;
  interactions?: Array<NativeInteraction | GuideInteraction>;
  guide_steps?: GuideStep[];
  scale_groups?: SharedScale[];
  annotations?: ProjectedAnnotation[];
  table_trends?: ProjectedTableTrend[];
  analytical_views?: ProjectedAnalyticalView[];
}

const digest = /^[a-f0-9]{64}$/;
const id = /^[a-z][a-z0-9_]{0,31}$/;
const chars = (value: string) => Array.from(value).length;
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);

/** The viewer accepts unavailable stubs; it never accepts authoring/canonical refs as a read grant. */
export function evidencePlanForViewer(value: unknown): EvidencePlanV1 | null {
  if (!object(value) || ![1, 2, 3].includes(Number(value.version)) || typeof value.version !== "number" || Object.keys(value).some((key) => !["version", "dataset", "evidence_notes", "text_dependencies", "bindings", "view_bindings", "interactions", "scale_groups", "guide_steps", "annotations", "table_trends", "analytical_views"].includes(key))) return null;
  if ("annotations" in value && !validAnnotations(value.annotations, value.version as number)) return null;
  if ("table_trends" in value && (Number(value.version) < 2 || !validTableTrends(value.table_trends))) return null;
  if ("analytical_views" in value && (value.version !== 3 || !validAnalyticalViews(value.analytical_views))) return null;
  if (!validGuides(value)) return null;
  if ("scale_groups" in value && !validSharedScales(value.scale_groups)) return null;
  const interactions = Array.isArray(value.interactions) ? value.interactions.filter((item) => !object(item) || item.preset !== "guided_steps") : value.interactions;
  const hasFocus = ["bindings", "view_bindings"].some((key) => key in value) || (Array.isArray(interactions) ? interactions.length > 0 : interactions !== undefined);
  if (hasFocus && !validProjectedFocus({ ...value, interactions })) return null;
  try { if (new TextEncoder().encode(JSON.stringify(value)).length > 20_000) return null; } catch { return null; }
  const dataset = value.dataset;
  if (!object(dataset) || Object.keys(dataset).some((key) => !["artifact_id", "availability", "content_digest", "schema_digest"].includes(key)) || typeof dataset.artifact_id !== "string" || !["available", "unavailable"].includes(String(dataset.availability))) return null;
  if (dataset.availability === "available" && (typeof dataset.content_digest !== "string" || !digest.test(dataset.content_digest) || typeof dataset.schema_digest !== "string" || !digest.test(dataset.schema_digest))) return null;
  if (dataset.availability === "unavailable" && (dataset.content_digest !== undefined || dataset.schema_digest !== undefined)) return null;
  const notes = value.evidence_notes;
  if (!Array.isArray(notes) || (!hasFocus && !("scale_groups" in value) && !("guide_steps" in value) && !("annotations" in value) && !("table_trends" in value) && !("analytical_views" in value) && notes.length < 1) || notes.length > 3) return null;
  const ids = new Set<string>();
  for (const note of notes) {
    if (!object(note) || Object.keys(note).some((key) => !["id", "measure_keys", "source_ref", "availability", "excerpt", "locator", "verified_content_digest"].includes(key))) return null;
    if (typeof note.id !== "string" || !id.test(note.id) || ids.has(note.id)) return null;
    ids.add(note.id);
    if (!Array.isArray(note.measure_keys) || !note.measure_keys.length || note.measure_keys.length > 12 || note.measure_keys.some((key) => typeof key !== "string" || !key) || new Set(note.measure_keys).size !== note.measure_keys.length) return null;
    if (!object(note.source_ref) || typeof note.source_ref.id !== "string" || !["available", "unavailable"].includes(String(note.availability))) return null;
    if (note.availability === "available") {
      if (typeof note.excerpt !== "string" || !note.excerpt.trim() || chars(note.excerpt) > 300 || typeof note.verified_content_digest !== "string" || !digest.test(note.verified_content_digest)) return null;
      if (note.locator !== undefined && (typeof note.locator !== "string" || !note.locator.trim() || chars(note.locator) > 160)) return null;
    } else if (note.excerpt !== undefined || note.locator !== undefined || note.verified_content_digest !== undefined) return null;
  }
  if (!object(value.text_dependencies)) return null;
  for (const [pointer, dependency] of Object.entries(value.text_dependencies)) {
    if (!/^\/(?:title|subtitle|views\/[0-5]\/spec\/(?:title|subtitle)|presentation_plan\/guide_steps\/[0-3]\/title|presentation_plan\/annotations\/[0-5]\/label)$/.test(pointer) || !object(dependency) || Object.keys(dependency).some((key) => !["note_ids", "dataset"].includes(key))) return null;
    if (dependency.dataset !== true || !Array.isArray(dependency.note_ids) || dependency.note_ids.length !== ids.size || dependency.note_ids.some((nid) => typeof nid !== "string" || !ids.has(nid)) || new Set(dependency.note_ids).size !== ids.size) return null;
  }
  if (!validAnnotationEvidence(value as unknown as EvidencePlanV1)) return null;
  return value as unknown as EvidencePlanV1;
}

/** A missing plan retains legacy completion. Unknown/unavailable seals cannot fetch more bytes. */
export function chartDatasetAvailable(spec: { presentation_plan?: unknown }): boolean {
  return spec.presentation_plan === undefined || evidencePlanForViewer(spec.presentation_plan)?.dataset.availability === "available";
}
