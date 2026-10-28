/**
 * Draft input for report AI rewriting (Report Canvas Chrome Spec §4).
 *
 * Reopening rewrite input after closing it (×, outside click, collapsed selection) retains the entered instruction. Key
 * by report (canvas ID, session if absent) + section:
 * switching section or report uses a different draft; clear it after sending. Store only in this page's memory, without
 * persistence or cross-tab sharing—
 * this is unsent UI input, not report content, and does not survive refresh.
 */

const drafts = new Map<string, string>();
const MAX_DRAFTS = 100;

export function aiRewriteDraftKey(scope: string | null | undefined, sectionId: string | null | undefined): string | null {
  if (!sectionId) return null;
  return `${scope || "live"}\u0000${sectionId}`;
}

export function readAiRewriteDraft(key: string | null): string {
  return key ? drafts.get(key) ?? "" : "";
}

export function writeAiRewriteDraft(key: string | null, value: string): void {
  if (!key) return;
  drafts.delete(key);
  if (!value) return;
  drafts.set(key, value);
  while (drafts.size > MAX_DRAFTS) {
    const oldest = drafts.keys().next().value;
    if (oldest === undefined) break;
    drafts.delete(oldest);
  }
}

/** For tests: clear every draft. */
export function resetAiRewriteDrafts(): void {
  drafts.clear();
}

/** Find a section ID by walking up from the editor selection (for selection-level rewrite input). */
export function sectionIdAtResolvedPos(
  $pos: { depth: number; node: (depth: number) => { attrs: unknown; type: { name: string } } } | null | undefined,
): string | null {
  if (!$pos) return null;
  for (let depth = $pos.depth; depth >= 0; depth -= 1) {
    const node = $pos.node(depth);
    const sid = (node.attrs as { sectionId?: unknown } | null)?.sectionId;
    if (typeof sid === "string" && sid && node.type.name.startsWith("section_")) return sid;
  }
  return null;
}
