import type { CanvasOpenDocument } from "@/components/canvas/object";
import { documentCanvasStateKey, documentCanvasTitle, type DocumentJob } from "@/lib/document-drafts";

type Translate = (key: string, values?: Record<string, string | number>) => string;

/**
 * Document drafts in the C1 “Open” menu: two lines (name; status word · scale). Same-name drafts get a
 * readable ordinal instead of a task ID.
 */
export function documentOpenItems(
  jobs: readonly DocumentJob[],
  t: { documents: Translate; settingsCanvas: Translate },
): CanvasOpenDocument[] {
  const titles = jobs.map((job) => documentCanvasTitle(job, t.documents("title")));
  return jobs.map((job, index) => {
    const title = titles[index];
    const duplicates = titles.filter((value) => value === title).length;
    const ordinal = titles.slice(0, index + 1).filter((value) => value === title).length;
    // Same lifecycle words as the Results document list.
    const detail = [
      t.documents(documentCanvasStateKey(job)),
      job.draft ? t.documents("canvas_pages", { pages: job.draft.preview.pages.length }) : null,
    ].filter(Boolean).join(" · ");
    return {
      id: job.id,
      title: duplicates > 1 ? `${title} · ${t.settingsCanvas("duplicate_ordinal", { number: ordinal })}` : title,
      detail: detail || undefined,
    };
  });
}
