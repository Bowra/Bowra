/**
 * Batch 2 §3.4: "追问这张图" brings the chart back to chat, instead of changing the question in inspection.
 *
 * Product contract (PRD §2): inspection supports only **cheap BI actions** (pure frontend derived views). Changing
 * the question itself (dimensions, definitions, filters) is new analysis: return to chat and ask the
 * agent to fetch data again. This event supplies that exit.
 *
 * Transport uses CustomEvent (precedent: canvas-workspace-focus.ts): inspection and composer
 * have no shared React ancestor; events cost less than threading context and do not expose composer shape
 * to chart components.
 */

import { canonicalizeChartSelectedContext } from "./selected-context";
import type { AttachmentRef } from "../types";
import { readRepresentation, representationPath } from "../use-artifact-representation";
import { ResultsWorkspaceError } from "../results-workspace-errors";

export const CHART_FOLLOWUP_EVENT = "bowhead:chart-followup";

/** Reuse authorized file details; display titles never define attachment metadata. */
export async function chartFollowupReference(sessionId: string, artifactId: string, signal: AbortSignal): Promise<AttachmentRef> {
  const response = await readRepresentation(representationPath(sessionId, artifactId, "session", "details"), "details", signal);
  const details = response.details;
  if (!details || details.id !== artifactId || typeof details.filename !== "string" || !details.filename || details.filename.length > 512 || typeof details.mime !== "string" || !details.mime || details.mime.length > 200 || !Number.isSafeInteger(details.size) || details.size < 0) throw new ResultsWorkspaceError("unavailable");
  return { fileId: details.id, filename: details.filename, mime: details.mime, size: details.size, referenceKind: "result" };
}

export interface ChartFollowupDetail {
  selectedContext?: import("./selected-context").ChartSelectedContext;
  /** Chart title used to identify this chart in prefilled copy. */
  title: string;
  /** Underlying data artifact; composer uses the existing reference-in-chat attachment pipeline. */
  dataArtifactId?: string | null;
  /** Optional kind, allowing composer to provide different prompts. */
  kind?: string;
  /** Attached caption, telling the model which conclusion the user is viewing. */
  captionText?: string | null;
}

/** Emit event; return whether it was sent (silently false for SSR / no window). */
export function requestChartFollowup(detail: ChartFollowupDetail): boolean {
  if (typeof window === "undefined") return false;
  return window.dispatchEvent(
    new CustomEvent<ChartFollowupDetail>(CHART_FOLLOWUP_EVENT, {
      detail,
      bubbles: false,
      cancelable: false,
    }),
  );
}

/** Composer subscription; return unsubscribe function. */
export function onChartFollowup(handler: (detail: ChartFollowupDetail) => void): () => void {
  if (typeof window === "undefined") return () => {};
  const listener = (event: Event) => {
    const detail = (event as CustomEvent<ChartFollowupDetail>).detail;
    if (!detail || typeof detail.title !== "string") return;
    if (detail.selectedContext !== undefined) {
      const scope = canonicalizeChartSelectedContext(detail.selectedContext);
      if (!scope || typeof detail.dataArtifactId !== "string" || !detail.dataArtifactId) return;
      handler({ ...detail, selectedContext: scope });
    } else handler(detail);
  };
  window.addEventListener(CHART_FOLLOWUP_EVENT, listener);
  return () => window.removeEventListener(CHART_FOLLOWUP_EVENT, listener);
}
