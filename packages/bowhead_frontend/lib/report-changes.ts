/**
 * The report sections a Turn added and revised (chat running state R2.2), read from `lead.final.payload`.
 *
 * The backend writes `report_changes` only when the Turn's sections stay in the report: `{added: [{section_id,
 * kind, title}], updated: [...]}`, each capped at 20 entries with `added_omitted` / `updated_omitted` counting the
 * rest, and `added_kinds` / `updated_kinds` counting every changed section by kind. Live, reloaded and replayed Turns
 * read the same payload. Turns from before the report card have no field and keep their stored pointer sentence.
 *
 * Readers count a report's chapters and charts, not its frame: the title, eyebrow, meta line and sources are not
 * counted ("本轮新增 4 节、2 张图", 116 report Turns write one title and one sources block beside 3–26 chapters).
 */

export interface ReportChange {
  sectionId: string;
  kind: string;
  title: string;
}

export interface ReportChangeCounts {
  /** Chapters and other content sections (narrative, KPI row, table, card…). */
  sections: number;
  /** Charts and images. */
  figures: number;
}

export interface ReportChanges {
  added: ReportChangeCounts;
  updated: ReportChangeCounts;
  /** Changed sections in payload order, added first; titles may be empty. */
  sections: ReportChange[];
  /** Where "打开到这一节" goes: the first changed chapter or chart, else the first change. */
  firstSectionId: string;
}

/** The report's frame: present in every report, never what a Turn "added" for the reader. */
const FRAME_KINDS = new Set(["h1", "eyebrow", "meta", "sources"]);
const FIGURE_KINDS = new Set(["chart", "image"]);

export function reportChangeGroup(kind: string): keyof ReportChangeCounts | null {
  if (FRAME_KINDS.has(kind)) return null;
  return FIGURE_KINDS.has(kind) ? "figures" : "sections";
}

function entries(value: unknown): ReportChange[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const { section_id: sectionId, kind, title } = item as { section_id?: unknown; kind?: unknown; title?: unknown };
    if (typeof sectionId !== "string" || sectionId === "") return [];
    return [{ sectionId, kind: typeof kind === "string" ? kind : "", title: typeof title === "string" ? title.trim() : "" }];
  });
}

function nonNegative(value: unknown): number {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : 0;
}

function counts(kinds: unknown, listed: ReportChange[], omitted: unknown): ReportChangeCounts {
  const out: ReportChangeCounts = { sections: 0, figures: 0 };
  if (kinds && typeof kinds === "object" && !Array.isArray(kinds)) {
    for (const [kind, count] of Object.entries(kinds as Record<string, unknown>)) {
      const group = reportChangeGroup(kind);
      if (group) out[group] += nonNegative(count);
    }
    return out;
  }
  // Without per-kind counts, the entries are all there is; entries past the cap count as sections.
  for (const change of listed) {
    const group = reportChangeGroup(change.kind);
    if (group) out[group] += 1;
  }
  out.sections += nonNegative(omitted);
  return out;
}

export function reportChangesOf(finalPayload: unknown): ReportChanges | null {
  if (!finalPayload || typeof finalPayload !== "object") return null;
  const raw = (finalPayload as { report_changes?: unknown }).report_changes;
  if (!raw || typeof raw !== "object") return null;
  const changes = raw as Record<string, unknown>;
  const added = entries(changes.added);
  const updated = entries(changes.updated);
  const sections = [...added, ...updated];
  if (sections.length === 0) return null;
  const content = sections.find((change) => reportChangeGroup(change.kind) !== null);
  return {
    added: counts(changes.added_kinds, added, changes.added_omitted),
    updated: counts(changes.updated_kinds, updated, changes.updated_omitted),
    sections,
    firstSectionId: (content ?? sections[0]).sectionId,
  };
}
