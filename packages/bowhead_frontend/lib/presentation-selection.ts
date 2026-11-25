/**
 * An element of a deck referenced in chat (docs/sprints/pptx-inline-edit-2026-11-24/tech-design.md §6.9).
 * The same closed wire is used by drafts, Send, history and recovery; the server checks it again
 * and builds the model's fact from the deck itself, not from these labels.
 */

export type PresentationSelectionKind = "text" | "table" | "chart" | "picture" | "smartart" | "ole" | "other";

export interface PresentationSelection {
  version: 1;
  artifactId: string;
  slideId: number;
  slideIndex: number;
  elementKey: string;
  elementName: string;
  elementKind: PresentationSelectionKind;
}

const KEYS = ["version", "artifactId", "slideId", "slideIndex", "elementKey", "elementName", "elementKind"];
const KINDS = new Set<string>(["text", "table", "chart", "picture", "smartart", "ole", "other"]);
export const MAX_PRESENTATION_SELECTION_BYTES = 1024;

export function canonicalizePresentationSelection(value: unknown): PresentationSelection | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const v = value as Record<string, unknown>;
  if (Object.keys(v).some((key) => !KEYS.includes(key)) || v.version !== 1) return null;
  if (typeof v.artifactId !== "string" || v.artifactId.length < 1 || v.artifactId.length > 128) return null;
  if (!Number.isSafeInteger(v.slideId) || (v.slideId as number) < 1 || (v.slideId as number) > 2 ** 31 - 1) return null;
  if (!Number.isSafeInteger(v.slideIndex) || (v.slideIndex as number) < 1 || (v.slideIndex as number) > 64) return null;
  if (typeof v.elementKey !== "string" || !/^[0-9]{1,10}:[0-9]{1,10}$/.test(v.elementKey) || v.elementKey.split(":")[0] !== String(v.slideId)) return null;
  if (typeof v.elementName !== "string" || v.elementName.length > 200) return null;
  if (typeof v.elementKind !== "string" || !KINDS.has(v.elementKind)) return null;
  const wire: PresentationSelection = {
    version: 1,
    artifactId: v.artifactId,
    slideId: v.slideId as number,
    slideIndex: v.slideIndex as number,
    elementKey: v.elementKey,
    elementName: v.elementName,
    elementKind: v.elementKind as PresentationSelectionKind,
  };
  if (new TextEncoder().encode(JSON.stringify(wire)).length > MAX_PRESENTATION_SELECTION_BYTES) return null;
  return wire;
}
