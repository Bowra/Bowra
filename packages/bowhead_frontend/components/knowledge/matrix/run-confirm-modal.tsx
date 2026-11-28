"use client";

/**
 * kb-matrix M2 — run-confirmation dialog (FR-14 mandatory cost confirmation).
 * Document matrix step 1 (S3, mockup ⑥): files × columns and the cells kept, one cost line per column (a cross-checked
 * column carries its extra reading and is called out when it drives the cost), the shared reading, total and balance,
 * the ETA, the longest file's share, and a "try on N files first" option. Warning (>20% of balance) and blocked
 * (estimate > balance → disabled CTA + top-up/contact-admin guidance) stay.
 */

import { useId, useRef } from "react";
import { useFormatter, useTranslations } from "next-intl";

import { DialogShell } from "@/components/ui/dialog";

import type { RunEstimate, RunEstimateColumn, RunScope } from "@/lib/kb-matrix/run-api";
import type { MatrixColumnVM } from "@/lib/kb-matrix/types";

import { IcWarn } from "./matrix-icons";

export interface RunConfirmModalProps {
  scope: RunScope;
  columnName?: string;
  /** Column names for the per-column lines. */
  columns?: MatrixColumnVM[];
  /** File name for the longest-file note. */
  fileName?: (fileId: string) => string | undefined;
  /** The estimate shown (the trial's while the trial option is on); null = loading. */
  estimate: RunEstimate | null;
  /** Rows a trial would run; 0 or absent hides the trial option. */
  trialFiles?: number;
  trial?: boolean;
  onTrialChange?: (on: boolean) => void;
  starting: boolean;
  /** M3 §4.2 post-column-edit confirmation variant: heading/breakdown/CTA/secondary button use stale_confirm_* copy;
   * secondary Only Apply to New Rows action closes without requests (stale is a marker, not a trigger, FR-4). */
  postEdit?: boolean;
  onClose: () => void;
  onConfirm: () => void;
  /** Return focus here after close (opening button, or menu trigger if opened from an item); default is the focused element at opening. */
  returnFocusTo?: HTMLElement | null;
}

/** The cross-checked column that is the single largest cost line (shared reading included), if any. */
export function crossCheckCostDriver(estimate: RunEstimate | null): RunEstimateColumn | null {
  const lines = estimate?.columns ?? [];
  const top = lines.reduce<RunEstimateColumn | null>((best, line) => (!best || line.credits > best.credits ? line : best), null);
  if (!top?.cross_verify || top.credits <= (estimate?.reading_credits ?? 0)) return null;
  return top;
}

export function RunConfirmModal({
  scope,
  columnName,
  columns = [],
  fileName,
  estimate,
  trialFiles = 0,
  trial = false,
  onTrialChange,
  starting,
  postEdit,
  onClose,
  onConfirm,
  returnFocusTo,
}: RunConfirmModalProps) {
  const t = useTranslations("kbMatrix");
  const format = useFormatter();
  const titleId = useId();
  const bodyId = useId();
  const returnFocusRef = useRef<HTMLElement | null>(returnFocusTo ?? null);
  const force = scope.kind === "force_all" || scope.kind === "force_column";
  const title = postEdit
    ? t("stale_confirm_title")
    : scope.kind === "column"
      ? t("run_modal_title_column", { name: columnName ?? "" })
      : scope.kind === "force_column"
        ? t("run_modal_title_force_column", { name: columnName ?? "" })
        : scope.kind === "force_all"
          ? t("run_modal_title_force_all")
          : scope.kind === "stale"
            ? t("run_modal_title_stale")
            : scope.kind === "stale_column"
              ? t("run_modal_title_stale_column", { name: columnName ?? "" })
              : scope.kind === "failed"
                ? t("run_modal_title_failed")
                : t("run_modal_title_all");
  const blocked = !!estimate?.blocked;
  const nameOf = (columnId: string) => columns.find((c) => c.columnId === columnId)?.name ?? columnId;
  const lines = estimate?.columns ?? [];
  const total = estimate?.credits_estimated ?? 0;
  const driver = crossCheckCostDriver(estimate);
  const longest = estimate?.longest_file && (estimate.files ?? 0) > 1 && total > 0 ? estimate.longest_file : null;
  const keptVerified = estimate?.skipped_verified ?? 0;
  const keptEdited = estimate?.skipped_edited ?? 0;
  const showTrial = !postEdit && trialFiles > 0 && !!onTrialChange;
  const cta = postEdit
    ? t("stale_confirm_cta", { n: estimate?.cells_pending ?? 0 })
    : trial
      ? t("run_modal_cta_trial", { n: trialFiles })
      : force
        ? t("run_modal_cta_force", { n: estimate?.cells_pending ?? 0 })
        : t("run_modal_cta");

  return (
    <DialogShell
      onClose={onClose}
      busy={starting}
      mount="inline"
      labelledBy={titleId}
      describedBy={bodyId}
      returnFocusRef={returnFocusTo ? returnFocusRef : undefined}
      backdropClassName="kb-overlay"
      panelClassName="kb-modal"
      panelProps={{ "data-testid": "kb-matrix-run-modal" } as Record<string, string>}
    >
      <div className="kb-modal-head">
        <h3 id={titleId}>{title}</h3>
      </div>
      <div className="kb-modal-body" id={bodyId}>
        {estimate?.warn && !blocked && (
          <div className="mx-warnbar">
            <span className="ic"><IcWarn s={15} /></span>
            {t("run_modal_warn")}
          </div>
        )}
        {blocked && (
          <div className="mx-warnbar danger">
            <span className="ic"><IcWarn s={15} /></span>
            {estimate.team_pool_tighter ? t("run_modal_blocked_team") : t("run_modal_blocked_user")}
          </div>
        )}
        {!estimate ? (
          <div className="mx-breakdown">
            <div className="mx-brow skip">
              <span className="k">{t("run_modal_estimating")}</span>
            </div>
          </div>
        ) : postEdit ? (
          <>
            <div className="mx-breakdown">
              <div className={"mx-brow" + (estimate.cells_pending === 0 ? " zero" : "")}>
                <span className="k">{t("stale_confirm_breakdown", { n: estimate.cells_pending, m: estimate.skipped_edited })}</span>
              </div>
            </div>
            <div className="mx-estimate">
              <span className="big">{t.rich("run_modal_estimate", { n: total, b: (chunks) => <b>{chunks}</b> })}</span>
            </div>
          </>
        ) : (
          <>
            <p className="mx-run-summary" data-testid="kb-matrix-run-summary">
              {t("run_modal_summary", { files: estimate.files ?? 0, cols: lines.length, cells: estimate.cells_pending })}
              {(keptVerified > 0 || keptEdited > 0) && <> {t("run_modal_kept", { verified: keptVerified, edited: keptEdited })}</>}
            </p>
            <div className="mx-breakdown">
              <table className="mx-costtable" data-testid="kb-matrix-run-costs">
                <thead>
                  <tr>
                    <th scope="col">{t("run_modal_col_header")}</th>
                    <th scope="col" className="num">{t("run_modal_cells_header")}</th>
                    <th scope="col" className="num">{t("run_modal_credits_header")}</th>
                  </tr>
                </thead>
                <tbody>
                  {(estimate.reading_credits ?? 0) > 0 && (
                    <tr className="shared">
                      <th scope="row">{t("run_modal_reading")}</th>
                      <td className="num">—</td>
                      <td className="num">{format.number(estimate.reading_credits ?? 0)}</td>
                    </tr>
                  )}
                  {lines.map((line) => (
                    <tr key={line.column_id}>
                      <th scope="row">
                        {nameOf(line.column_id)}
                        {line.cross_verify && <span className="tag"> · {t("run_modal_cv_tag")}</span>}
                      </th>
                      <td className="num">{format.number(line.cells)}</td>
                      <td className="num">{format.number(line.credits)}</td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr>
                    <th scope="row">{t("run_modal_total")}</th>
                    <td className="num">{format.number(estimate.cells_pending)}</td>
                    <td className="num">{format.number(total)}</td>
                  </tr>
                </tfoot>
              </table>
            </div>
            {driver && <p className="mx-run-note" data-testid="kb-matrix-run-cv-note">{t("run_modal_cv_main", { name: nameOf(driver.column_id) })}</p>}
            <dl className="mx-runfacts">
              <dt>{t("run_modal_balance_label")}</dt>
              <dd>{t("run_modal_balance", { n: estimate.available_credits })}</dd>
              <dt>{t("run_modal_eta_label")}</dt>
              <dd>{t("run_modal_eta", { min: Math.max(1, Math.ceil(estimate.eta_s / 60)) })}</dd>
              {longest && (
                <>
                  <dt>{t("run_modal_longest_label")}</dt>
                  <dd data-testid="kb-matrix-run-longest">
                    {t("run_modal_longest", {
                      name: fileName?.(longest.file_id) || t("file_missing"),
                      chars: longest.chars,
                      pct: Math.round((longest.credits / total) * 100),
                    })}
                  </dd>
                </>
              )}
            </dl>
            {showTrial && (
              <label className="mx-trialopt" data-testid="kb-matrix-run-trial">
                <input type="checkbox" checked={trial} disabled={starting} onChange={(e) => onTrialChange?.(e.target.checked)} />
                {t("run_modal_trial", { n: trialFiles })}
              </label>
            )}
          </>
        )}
        <div className="mx-estimate mx-estimate--note">
          <span className="sub">{t("run_modal_disclaimer")}</span>
        </div>
      </div>
      <div className="kb-modal-foot">
        <button type="button" className="kb-btn kb-btn-ghost" disabled={starting} onClick={onClose}>
          {postEdit ? t("stale_confirm_secondary") : t("cancel")}
        </button>
        <button
          type="button"
          className={"kb-btn " + (force && !trial ? "kb-btn-danger-solid" : "kb-btn-primary")}
          disabled={!estimate || blocked || starting || estimate.cells_pending === 0}
          data-testid="kb-matrix-run-confirm"
          onClick={onConfirm}
        >
          {cta}
        </button>
      </div>
    </DialogShell>
  );
}
