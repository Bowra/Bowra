"use client";

/**
 * kb-matrix M2/M3 — run banners (FR-21), M1b task summary:
 * - All runs collapse into one disclosure summary (running progress, completion notice, issue count);
 *   expanding it lists every run as a retained history entry.
 * - Each entry: scope-label prefix (scopeColumnId looks up name) + progress/ETA + Cancel (only
 *   run.canCancel, server authority, FR-34) + failed terminal View failures.
 * - Never render single_cell runs (FR-12b inline execution + Toast; server excludes, filter defensively again).
 */

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import type { MatrixColumnVM, MatrixRunVM } from "@/lib/kb-matrix/types";

import { IcCheck, IcSpin, IcWarn } from "./matrix-icons";

export interface RunBannersProps {
  /** Undismissed runs (store-merged snapshot.runs). */
  runs: MatrixRunVM[];
  /** Scope-label column-name lookup (scopeColumnId → name). */
  columns: MatrixColumnVM[];
  cancelling: boolean;
  onCancel: (runId: string) => void;
  onViewFailed: (runId: string) => void;
}

export function RunBanners(props: RunBannersProps) {
  const t = useTranslations("kbMatrix");
  const previous = useRef(new Map(props.runs.map((r) => [r.runId, r.status])));
  const [notice, setNotice] = useState<{ count: number } | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const runs = props.runs.filter((r) => r.scopeKind !== "single_cell");
  const active = runs.filter((r) => r.status === "running" || r.status === "cancelling");
  const issues = runs.filter((r) => r.status === "failed" || r.status === "paused" || r.cellsFailed > 0);
  useEffect(() => {
    let completed = 0;
    let failed = false;
    for (const run of props.runs) {
      const before = previous.current.get(run.runId);
      if (run.scopeKind !== "single_cell" && (before === "running" || before === "cancelling" || before === "paused")) {
        if (run.status === "succeeded" && run.cellsFailed === 0) completed++;
        else if (run.status !== before && (run.status === "failed" || run.status === "paused" || run.cellsFailed > 0)) failed = true;
      }
      previous.current.set(run.runId, run.status);
    }
    if (completed) setNotice({ count: completed });
    if (failed || completed) setAnnouncement(t(failed ? "tasks_need_attention" : "tasks_completed", { n: completed }));
  }, [props.runs, t]);
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => { setNotice(null); setAnnouncement(""); }, 3000);
    return () => clearTimeout(timer);
  }, [notice]);
  if (!runs.length) return null;
  const done = active.reduce((n, r) => n + r.cellsDone + r.cellsFailed, 0);
  const total = active.reduce((n, r) => n + r.cellsTotal, 0);
  return (
    <div data-testid="kb-matrix-runbanners" className="mx-task-summary">
      <span className="sr-only" role="status" aria-live="polite">{announcement}</span>
      <details>
        <summary className="mx-runbanner" data-testid="kb-matrix-task-summary">
          <span>{active.length ? t("banner_multi_running", { n: active.length, x: done, y: total }) : notice ? t("tasks_completed", { n: notice.count }) : t("task_history", { n: runs.length })}</span>
          {issues.length > 0 && <span className="mx-task-issues">{t("tasks_issues", { n: issues.length })}</span>}
          <span className="mx-task-toggle">{t("task_details")}</span>
        </summary>
        {runs.map((run) => <SingleRunBanner key={run.runId} {...props} run={run} />)}
      </details>
    </div>
  );
}

/** Scope → banner prefix (§4.5 mapping); unknown/absent (old backend in image rollback window) → no prefix. */
function scopeLabel(run: MatrixRunVM, columns: MatrixColumnVM[], t: ReturnType<typeof useTranslations>): string | null {
  const name = run.scopeColumnId ? (columns.find((c) => c.columnId === run.scopeColumnId)?.name ?? "") : "";
  switch (run.scopeKind) {
    case "all_pending":
      return t("run_scope_label_all_pending");
    case "column":
      return t("run_scope_label_column", { name });
    case "force_all":
      return t("run_scope_label_force_all");
    case "force_column":
      return t("run_scope_label_force_column", { name });
    case "stale":
      return t("run_scope_label_stale");
    case "stale_column":
      return t("run_scope_label_stale_column", { name });
    default:
      return null;
  }
}

function SingleRunBanner({
  run,
  columns,
  cancelling,
  onCancel,
  onViewFailed,
}: {
  run: MatrixRunVM;
  columns: MatrixColumnVM[];
  cancelling: boolean;
  onCancel: (runId: string) => void;
  onViewFailed: (runId: string) => void;
}) {
  const t = useTranslations("kbMatrix");
  const active = run.status === "running" || run.status === "cancelling";
  // Server owns cancellation permission (FR-34: members cancel only their own runs); no shadow role checks.
  const canCancel = !!run.canCancel;
  const label = scopeLabel(run, columns, t);
  const prefix = label ? `${label} · ` : "";

  // Flag/billing freeze leaves run paused (resumable): must remain visible/cancellable, or user faces
  // queued cells and stalled new runs with no way forward (impl review FE-P1-1).
  if (run.status === "paused") {
    return (
      <div className="mx-runbanner err" data-testid="kb-matrix-runbanner">
        <span className="ic"><IcWarn s={15} /></span>
        <span>{prefix}{t("banner_paused")}</span>
        {canCancel && (
          <button type="button" className="cancel" disabled={cancelling} onClick={() => onCancel(run.runId)}>
            {t("banner_cancel")}
          </button>
        )}
      </div>
    );
  }

  if (!active && run.status !== "succeeded" && run.status !== "failed" && run.status !== "cancelled") return null;

  if (active) {
    const etaMin = Math.max(1, Math.ceil(run.etaS / 60));
    const pct = run.cellsTotal > 0 ? ((run.cellsDone + run.cellsFailed) / run.cellsTotal) * 100 : 0;
    return (
      <div className="mx-runbanner" data-testid="kb-matrix-runbanner">
        <span className="ic"><IcSpin s={14} /></span>
        <span>
          {prefix}
          {run.status === "cancelling"
            ? t("banner_cancelling")
            : t.rich("banner_running", {
                done: run.cellsDone + run.cellsFailed,
                total: run.cellsTotal,
                min: etaMin,
                b: (chunks) => <b>{chunks}</b>,
              })}
        </span>
        {canCancel && run.status === "running" && (
          <button type="button" className="cancel" disabled={cancelling} onClick={() => onCancel(run.runId)}>
            {t("banner_cancel")}
          </button>
        )}
        <span className="bar" style={{ width: `${pct}%` }} />
      </div>
    );
  }

  if (run.status === "failed") {
    return (
      <div className="mx-runbanner err" data-testid="kb-matrix-runbanner">
        <span className="ic"><IcWarn s={15} /></span>
        <span>
          {prefix}
          {run.errorCode === "insufficient_credits"
            ? t("banner_failed_credits")
            : run.errorCode === "quota_exceeded"
              ? t("banner_failed_quota")
              : t("banner_failed_system")}
        </span>
        {run.cellsFailed > 0 && (
          <button type="button" className="link" style={{ marginLeft: "auto" }} onClick={() => onViewFailed(run.runId)}>
            {t("banner_view_failed")}
          </button>
        )}
      </div>
    );
  }

  if (run.status === "cancelled") {
    return (
      <div className="mx-runbanner done" data-testid="kb-matrix-runbanner">
        <span className="ic"><IcCheck s={15} /></span>
        <span>{prefix}{t("banner_cancelled", { done: run.cellsDone })}</span>
      </div>
    );
  }

  return (
    <div className="mx-runbanner done" data-testid="kb-matrix-runbanner">
      <span className="ic"><IcCheck s={15} /></span>
      <span>
        {prefix}
        {t.rich("banner_done", { n: run.cellsDone, b: (chunks) => <b>{chunks}</b> })}
        {run.cellsFailed > 0 && <> · {t("banner_done_failed", { n: run.cellsFailed })}</>}
      </span>
      {run.cellsFailed > 0 && (
        <button type="button" className="link" style={{ marginLeft: "auto" }} onClick={() => onViewFailed(run.runId)}>
          {t("banner_view_failed")}
        </button>
      )}
    </div>
  );
}
