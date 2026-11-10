/**
 * Current revisions of charts shown inline in chat answers.
 *
 * A chart delivered in chat is edited in place: a later Turn's
 * `canvas.section_patched` carries the saved revision, and the earlier answer
 * must show it at once. The history/replay projection already returns the
 * current revision (with `rev`) in `lead.final.payload.inline_artifacts`, so
 * the figure always renders the newest revision known from either source.
 * Section ids are globally unique, so entries never collide across sessions.
 */
import { useSyncExternalStore } from "react";

import { getCanvasSnapshot, subscribeCanvas } from "./canvas-store";
import type { ChartSpec } from "./chart/types";

interface ChartRevision {
  spec: ChartSpec;
  rev: number;
}

const revisions = new Map<string, ChartRevision>();
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Record a saved chart revision; older or duplicate revisions are ignored. */
export function recordChatFigureRevision(sectionId: string, spec: unknown, rev: unknown): void {
  if (!sectionId || !spec || typeof spec !== "object" || typeof rev !== "number") return;
  const known = revisions.get(sectionId);
  if (known && known.rev >= rev) return;
  revisions.set(sectionId, { spec: spec as ChartSpec, rev });
  listeners.forEach((listener) => listener());
}

/** Test-only reset. */
export function resetChatFigureRevisions(): void {
  revisions.clear();
  listeners.forEach((listener) => listener());
}

type CanvasChartSection = { id?: string; kind?: string; spec?: unknown; rev?: unknown; superseded?: boolean };

/** Stable reference for useSyncExternalStore: the loaded Canvas section itself. */
function canvasSection(sectionId: string): CanvasChartSection | undefined {
  return getCanvasSnapshot().sections.find(
    (item) => (item as unknown as CanvasChartSection).id === sectionId,
  ) as unknown as CanvasChartSection | undefined;
}

function canvasChartRevision(section: CanvasChartSection | undefined): ChartRevision | undefined {
  if (!section || section.kind !== "chart" || section.superseded) return undefined;
  if (!section.spec || typeof section.spec !== "object" || typeof section.rev !== "number") return undefined;
  return { spec: section.spec as ChartSpec, rev: section.rev };
}

/** Pick the newest revision among the delivered figure and the saved updates. */
export function newestChartSpec(
  delivered: ChartSpec,
  deliveredRev: number | undefined,
  candidates: Array<ChartRevision | undefined>,
): ChartSpec {
  let spec = delivered;
  let rev = typeof deliveredRev === "number" ? deliveredRev : 0;
  for (const candidate of candidates) {
    if (candidate && candidate.rev > rev) {
      spec = candidate.spec;
      rev = candidate.rev;
    }
  }
  return spec;
}

/** The spec an inline chat chart should render right now. */
export function useChatFigureSpec(
  sectionId: string | undefined,
  delivered: ChartSpec,
  deliveredRev?: number,
): ChartSpec {
  const patched = useSyncExternalStore(
    subscribe,
    () => (sectionId ? revisions.get(sectionId) : undefined),
    () => undefined,
  );
  // Report charts that are also visible in Canvas follow its loaded revision.
  const inCanvas = useSyncExternalStore(
    subscribeCanvas,
    () => (sectionId ? canvasSection(sectionId) : undefined),
    () => undefined,
  );
  return newestChartSpec(delivered, deliveredRev, [patched, canvasChartRevision(inCanvas)]);
}
