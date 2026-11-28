/**
 * A paragraph, a table cell or rendered pages of a Word file referenced in chat
 * (docs/sprints/docx-inline-edit-2026-11-26/tech-design.md §7.4). The same closed wire is used by
 * drafts, Send, history and recovery; the server checks it again and builds the model's facts from
 * the file (the inventory for a paragraph, the version's kept render for pages), never from these
 * labels. Version 1 names one paragraph or cell; version 2 names 1–10 rendered pages of one version,
 * which is what 「引用这一页」 produces. One reference per file: a paragraph replaces the page set and
 * the other way round, as for decks.
 */

export interface DocumentParagraphSelection {
  version: 1;
  artifactId: string;
  paragraphKey: string;
  /** Display only, for the chips (「一、总体情况 · 第 2 段」); the server builds facts from the file. */
  paragraphName?: string;
}

export interface DocumentPagesSelection {
  version: 2;
  artifactId: string;
  pages: Array<{ page: number }>;
}

export type DocumentSelection = DocumentParagraphSelection | DocumentPagesSelection;

export const MAX_REFERENCED_PAGES = 10;
export const MAX_DOCUMENT_SELECTION_BYTES = 1024;
const PARAGRAPH_KEY = /^document:[a-z0-9.]{1,120}$/;

const isPage = (value: unknown) => Number.isSafeInteger(value) && (value as number) >= 1 && (value as number) <= 300;
const isArtifactId = (value: unknown) => typeof value === "string" && value.length >= 1 && value.length <= 128;
const fits = (wire: DocumentSelection) => new TextEncoder().encode(JSON.stringify(wire)).length <= MAX_DOCUMENT_SELECTION_BYTES;

export function canonicalizeDocumentSelection(value: unknown): DocumentSelection | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const v = value as Record<string, unknown>;
  if (v.version === 1) {
    if (Object.keys(v).some((key) => !["version", "artifactId", "paragraphKey", "paragraphName"].includes(key))) return null;
    if (!isArtifactId(v.artifactId) || typeof v.paragraphKey !== "string" || !PARAGRAPH_KEY.test(v.paragraphKey)) return null;
    if (v.paragraphName !== undefined && (typeof v.paragraphName !== "string" || v.paragraphName.length > 200)) return null;
    const wire: DocumentParagraphSelection = {
      version: 1,
      artifactId: v.artifactId as string,
      paragraphKey: v.paragraphKey,
      ...(v.paragraphName !== undefined ? { paragraphName: v.paragraphName as string } : {}),
    };
    return fits(wire) ? wire : null;
  }
  if (v.version !== 2 || Object.keys(v).some((key) => !["version", "artifactId", "pages"].includes(key))) return null;
  if (!isArtifactId(v.artifactId) || !Array.isArray(v.pages) || v.pages.length < 1 || v.pages.length > MAX_REFERENCED_PAGES) return null;
  const pages: DocumentPagesSelection["pages"] = [];
  for (const item of v.pages as unknown[]) {
    if (!item || typeof item !== "object" || Array.isArray(item)) return null;
    const entry = item as Record<string, unknown>;
    if (Object.keys(entry).some((key) => key !== "page") || !isPage(entry.page)) return null;
    // Unique and ascending, as the server requires.
    if (pages.length && (entry.page as number) <= pages[pages.length - 1].page) return null;
    pages.push({ page: entry.page as number });
  }
  const wire: DocumentPagesSelection = { version: 2, artifactId: v.artifactId as string, pages };
  return fits(wire) ? wire : null;
}

/** 「引用这一段」: one paragraph or table cell of one version. */
export function documentParagraphSelection(artifactId: string, paragraphKey: string, paragraphName?: string): DocumentSelection | null {
  return canonicalizeDocumentSelection({ version: 1, artifactId, paragraphKey, ...(paragraphName ? { paragraphName: paragraphName.slice(0, 200) } : {}) });
}

/** The rendered pages a selection names, ascending; none for a paragraph. */
export function referencedDocumentPages(selection: DocumentSelection | null | undefined): number[] {
  return selection?.version === 2 ? selection.pages.map((entry) => entry.page) : [];
}

export type PageToggleOutcome = "added" | "removed" | "limit";

/**
 * 「引用这一页」 on a rendered page: adds it to the file's page set, or removes it when it is there.
 * A paragraph reference of the file is replaced by the page. `selection` null means no pages are
 * left, so the file's chip goes.
 */
export function toggleDocumentPage(
  current: DocumentSelection | null | undefined,
  artifactId: string,
  page: number,
): { selection: DocumentPagesSelection | null; outcome: PageToggleOutcome } {
  const pages = current && current.artifactId === artifactId && current.version === 2 ? current.pages : [];
  if (pages.some((entry) => entry.page === page)) {
    const rest = pages.filter((entry) => entry.page !== page);
    return { selection: rest.length ? { version: 2, artifactId, pages: rest } : null, outcome: "removed" };
  }
  if (pages.length >= MAX_REFERENCED_PAGES) return { selection: { version: 2, artifactId, pages }, outcome: "limit" };
  const next = [...pages, { page }].sort((a, b) => a.page - b.page);
  return { selection: { version: 2, artifactId, pages: next }, outcome: "added" };
}
