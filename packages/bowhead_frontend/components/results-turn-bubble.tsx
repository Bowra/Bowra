"use client";

/**
 * The one-time reminder beside the collapsed Results strip (chat running state R2.6).
 *
 * When a Turn ends with new results and Results stays closed (Lead named no file to open, or the reader closed it),
 * a bubble says what is new: "本轮新增：1 份演示文稿 · 报告改了 4 节 · 2 个工作文件 [查看]". It reads the Turn's
 * final payload: file counts worded as the finished line words them, the report sections the Turn added or revised,
 * and the work files. It fades after 8 s or on any other interaction; 查看 opens Results. Desktop only: on phones the
 * Results tab keeps its new-content dot.
 */
import { useEffect, useRef } from "react";
import { useTranslations } from "next-intl";

import { reportChangesOf } from "@/lib/report-changes";
import { extractTurnOutputs } from "@/lib/turn-outputs";

/** How long the reminder stays when the reader does nothing. */
export const RESULTS_BUBBLE_MS = 8_000;

type Translate = (key: string, values?: Record<string, number | string>) => string;

function omitted(value: unknown): number {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : 0;
}

/** What the Turn added to Results, worded for the reminder; empty when nothing is new. */
export function resultsTurnItems(finalPayload: unknown, tStep: Translate, tPane: Translate): string[] {
  const outputs = extractTurnOutputs(finalPayload);
  const items = outputs
    .filter((output) => output.kind !== "report" && output.kind !== "chart")
    .map((output) => tStep(`summary_output.${output.kind}`, { count: output.count }));
  const payload = (finalPayload && typeof finalPayload === "object" ? finalPayload : {}) as { work_files?: { count?: unknown } };
  // Chapters and charts the Turn added or revised, as the report card counts them.
  const changes = reportChangesOf(finalPayload);
  if (changes) {
    const sections = changes.added.sections + changes.updated.sections;
    const figures = changes.added.figures + changes.updated.figures;
    if (sections > 0) items.push(tPane("turn_bubble_report", { count: sections }));
    if (figures > 0) items.push(tPane("turn_bubble_report_figures", { count: figures }));
    if (sections === 0 && figures === 0) items.push(tPane("turn_bubble_report_changed"));
  } else if (outputs.some((output) => output.kind === "report")) items.push(tStep("summary_output.report", { count: 1 }));
  const work = omitted(payload.work_files?.count);
  if (work > 0) items.push(tPane("turn_bubble_work_files", { count: work }));
  return items;
}

export function ResultsTurnBubble({
  finalPayload,
  onView,
  onDismiss,
}: {
  finalPayload: unknown;
  onView: () => void;
  onDismiss: () => void;
}) {
  const tStep = useTranslations("step_list");
  const tPane = useTranslations("split_pane");
  const ref = useRef<HTMLDivElement | null>(null);
  const items = resultsTurnItems(finalPayload, tStep as unknown as Translate, tPane as unknown as Translate);
  const empty = items.length === 0;

  useEffect(() => {
    if (empty) {
      onDismiss();
      return;
    }
    const timer = window.setTimeout(onDismiss, RESULTS_BUBBLE_MS);
    // Any other interaction dismisses it; the bubble's own button acts first.
    const elsewhere = (event: Event) => {
      if (!(event.target instanceof Node) || !ref.current?.contains(event.target)) onDismiss();
    };
    document.addEventListener("pointerdown", elsewhere, true);
    document.addEventListener("keydown", elsewhere, true);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("pointerdown", elsewhere, true);
      document.removeEventListener("keydown", elsewhere, true);
    };
  }, [empty, onDismiss]);

  if (empty) return null;
  return (
    <div className="v2-results-bubble" ref={ref} role="status" data-testid="results-turn-bubble">
      <span className="v2-results-bubble__text">{tPane("turn_bubble", { items: items.join(" · ") })}</span>
      <button type="button" className="v2-results-bubble__view" onClick={onView}>
        {tPane("turn_bubble_view")}
      </button>
    </div>
  );
}
