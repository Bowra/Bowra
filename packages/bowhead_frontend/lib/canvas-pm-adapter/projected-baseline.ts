/**
 * Projected baseline: stored sections after one trip through the editor.
 *
 * The editor never holds the stored Markdown itself; it holds `canvasToPMDoc(sections)` normalized by its
 * schema, and the write path serializes that back with `pmNodeToSection`. That trip is not the identity
 * (`  \n` hard breaks come back as `\\\n`, a heading followed by a hard break becomes two blocks, a wrapper
 * without `body` gains `body: []`, …). Diffing the editor output against the **stored** sections therefore
 * marks untouched sections as changed, and any edit anywhere in the report PATCHes that normalization
 * over content the user never edited.
 *
 * Diffing against this projection instead compares like with like: an untouched section reproduces its
 * projection exactly, so only sections the user actually changed are written, whatever the serializer does.
 */

import type { Schema } from "@tiptap/pm/model";
import type { ReportSection } from "@/lib/types";

import { canvasToPMDoc } from "./canvas-to-pm";
import { normalizeDocForEditor } from "./editor-doc-normalize";
import { reconstructSectionsById } from "./pm-to-canvas";

const cache = new WeakMap<readonly ReportSection[], WeakMap<Schema, Map<string, ReportSection>>>();

/**
 * Project `sections` through canonical → PM (normalized by `schema`, as `setContent` does) → canonical.
 * Memoized per sections array and schema: the sync baseline array is replaced, not mutated, when it changes.
 */
export function projectSectionsThroughEditor(
  sections: readonly ReportSection[],
  schema: Schema,
): Map<string, ReportSection> {
  let bySchema = cache.get(sections);
  const cached = bySchema?.get(schema);
  if (cached) return cached;
  const doc = normalizeDocForEditor(schema, canvasToPMDoc(sections as ReportSection[]));
  const projected = reconstructSectionsById(doc);
  if (!bySchema) {
    bySchema = new WeakMap();
    cache.set(sections, bySchema);
  }
  bySchema.set(schema, projected);
  return projected;
}
