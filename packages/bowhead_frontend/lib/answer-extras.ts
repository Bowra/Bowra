/**
 * What the finished answer shows after its files (chat running state R2.2, R2.3): the report card and the
 * work-file line, both read from `lead.final.payload` so live, reloaded and replayed Turns agree.
 */
import { reportChangesOf, type ReportChanges } from "./report-changes";

export interface AnswerExtras {
  report: ReportChanges | null;
  /** Files the Turn saved that the answer does not show (`work_files.count`). */
  workFiles: number;
  /** The Turn, for the work-file line's link to its files in Results. */
  turnId?: string | null;
}

export function answerExtrasOf(finalPayload: unknown, turnId: string | null = null): AnswerExtras | null {
  const report = reportChangesOf(finalPayload);
  const raw = finalPayload && typeof finalPayload === "object"
    ? (finalPayload as { work_files?: { count?: unknown } }).work_files?.count
    : undefined;
  const workFiles = typeof raw === "number" && Number.isInteger(raw) && raw > 0 ? raw : 0;
  return report || workFiles > 0 ? { report, workFiles, turnId } : null;
}
