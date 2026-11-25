/**
 * Slides, or an element on one, of a deck referenced in chat
 * (docs/sprints/pptx-inline-edit-2026-11-24/tech-design.md §6.9, §13.2, §13.6). The same closed wire
 * is used by drafts, Send, history and recovery; the server checks it again and builds the model's
 * facts from the deck itself, not from these labels. Version 1 names one slide, or an element on it
 * (with the element fields); version 2 names whole slides of one version in page order, which is
 * what 「引用这一页」 produces. A reference names one version, so the slide number identifies a slide.
 */

export type PresentationSelectionKind = "text" | "table" | "chart" | "picture" | "smartart" | "ole" | "other";

export interface PresentationElementSelection {
  version: 1;
  artifactId: string;
  slideId?: number;
  slideIndex: number;
  elementKey?: string;
  elementName?: string;
  elementKind?: PresentationSelectionKind;
}

export interface PresentationSlidesSelection {
  version: 2;
  artifactId: string;
  slides: Array<{ slideIndex: number; slideId?: number }>;
}

export type PresentationSelection = PresentationElementSelection | PresentationSlidesSelection;

/** At most this many pages ride in one reference (design §13.6, D1). */
export const MAX_REFERENCED_SLIDES = 10;

const KEYS = ["version", "artifactId", "slideId", "slideIndex", "elementKey", "elementName", "elementKind"];
const KINDS = new Set<string>(["text", "table", "chart", "picture", "smartart", "ole", "other"]);
export const MAX_PRESENTATION_SELECTION_BYTES = 1024;

const isSlideId = (value: unknown) => Number.isSafeInteger(value) && (value as number) >= 1 && (value as number) <= 2 ** 31 - 1;
const isSlideIndex = (value: unknown) => Number.isSafeInteger(value) && (value as number) >= 1 && (value as number) <= 64;

function canonicalizeSlides(v: Record<string, unknown>): PresentationSlidesSelection | null {
  if (Object.keys(v).some((key) => !["version", "artifactId", "slides"].includes(key))) return null;
  if (typeof v.artifactId !== "string" || v.artifactId.length < 1 || v.artifactId.length > 128) return null;
  if (!Array.isArray(v.slides) || v.slides.length < 1 || v.slides.length > MAX_REFERENCED_SLIDES) return null;
  const slides: PresentationSlidesSelection["slides"] = [];
  for (const item of v.slides as unknown[]) {
    if (!item || typeof item !== "object" || Array.isArray(item)) return null;
    const slide = item as Record<string, unknown>;
    if (Object.keys(slide).some((key) => key !== "slideIndex" && key !== "slideId")) return null;
    if (!isSlideIndex(slide.slideIndex) || (slide.slideId !== undefined && !isSlideId(slide.slideId))) return null;
    // Unique and in page order, as the server requires.
    if (slides.length && (slide.slideIndex as number) <= slides[slides.length - 1].slideIndex) return null;
    slides.push({ slideIndex: slide.slideIndex as number, ...(slide.slideId !== undefined ? { slideId: slide.slideId as number } : {}) });
  }
  const wire: PresentationSlidesSelection = { version: 2, artifactId: v.artifactId, slides };
  if (new TextEncoder().encode(JSON.stringify(wire)).length > MAX_PRESENTATION_SELECTION_BYTES) return null;
  return wire;
}

export function canonicalizePresentationSelection(value: unknown): PresentationSelection | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const v = value as Record<string, unknown>;
  if (v.version === 2) return canonicalizeSlides(v);
  if (Object.keys(v).some((key) => !KEYS.includes(key)) || v.version !== 1) return null;
  if (typeof v.artifactId !== "string" || v.artifactId.length < 1 || v.artifactId.length > 128) return null;
  const hasSlideId = v.slideId !== undefined;
  if (hasSlideId && (!Number.isSafeInteger(v.slideId) || (v.slideId as number) < 1 || (v.slideId as number) > 2 ** 31 - 1)) return null;
  if (!Number.isSafeInteger(v.slideIndex) || (v.slideIndex as number) < 1 || (v.slideIndex as number) > 64) return null;
  const elementFields = [v.elementKey, v.elementName, v.elementKind].filter((field) => field !== undefined).length;
  if (elementFields !== 0 && elementFields !== 3) return null;
  const wire: PresentationElementSelection = {
    version: 1,
    artifactId: v.artifactId,
    ...(hasSlideId ? { slideId: v.slideId as number } : {}),
    slideIndex: v.slideIndex as number,
  };
  if (elementFields === 3) {
    if (!hasSlideId) return null;
    if (typeof v.elementKey !== "string" || !/^[0-9]{1,10}:[0-9]{1,10}$/.test(v.elementKey) || v.elementKey.split(":")[0] !== String(v.slideId)) return null;
    if (typeof v.elementName !== "string" || v.elementName.length > 200) return null;
    if (typeof v.elementKind !== "string" || !KINDS.has(v.elementKind)) return null;
    wire.elementKey = v.elementKey;
    wire.elementName = v.elementName;
    wire.elementKind = v.elementKind as PresentationSelectionKind;
  }
  if (new TextEncoder().encode(JSON.stringify(wire)).length > MAX_PRESENTATION_SELECTION_BYTES) return null;
  return wire;
}

/** A reference to one whole slide of one version of a deck (version 2, one page). */
export function presentationSlideSelection(artifactId: string, slideIndex: number, slideId?: number): PresentationSelection | null {
  return canonicalizePresentationSelection({ version: 2, artifactId, slides: [{ slideIndex, ...(slideId ? { slideId } : {}) }] });
}

/** The whole slides a selection names, in page order; none for an element. */
export function referencedSlides(selection: PresentationSelection | null | undefined): number[] {
  if (!selection) return [];
  if (selection.version === 2) return selection.slides.map((slide) => slide.slideIndex);
  return selection.elementKey === undefined ? [selection.slideIndex] : [];
}

export type SlideToggleOutcome = "added" | "removed" | "limit";

/**
 * 「引用这一页」 on a page: adds the page to the file's page set, or removes it when it is there.
 * A reference to an element of the file is replaced by the page (one reference per file, design
 * §13.6 D2). `selection` null means the file has no pages left, so its chip goes.
 */
export function toggleSlide(
  current: PresentationSelection | null | undefined,
  artifactId: string,
  slideIndex: number,
  slideId?: number,
): { selection: PresentationSlidesSelection | null; outcome: SlideToggleOutcome } {
  const slides = current && current.artifactId === artifactId
    ? current.version === 2
      ? current.slides
      : current.elementKey === undefined ? [{ slideIndex: current.slideIndex, ...(current.slideId ? { slideId: current.slideId } : {}) }] : []
    : [];
  if (slides.some((slide) => slide.slideIndex === slideIndex)) {
    const rest = slides.filter((slide) => slide.slideIndex !== slideIndex);
    return { selection: rest.length ? { version: 2, artifactId, slides: rest } : null, outcome: "removed" };
  }
  if (slides.length >= MAX_REFERENCED_SLIDES) {
    return { selection: { version: 2, artifactId, slides }, outcome: "limit" };
  }
  const next = [...slides, { slideIndex, ...(slideId ? { slideId } : {}) }].sort((a, b) => a.slideIndex - b.slideIndex);
  return { selection: { version: 2, artifactId, slides: next }, outcome: "added" };
}
