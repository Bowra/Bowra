"use client";

/**
 * kb-matrix M2 — run-confirmation dialog (FR-14 mandatory cost confirmation; design r2 RunModal verbatim).
 * Breakdown rows (server estimate) + estimated usage + disclaimer + warning (>20% of balance) + force danger state.
 * blocked (estimate > balance) → disabled CTA + guidance to top up/contact admin.
 */

import { useId, useRef } from "react";
import { useTranslations } from "next-intl";

import { DialogShell } from "@/components/ui/dialog";

import type { RunEstimate, RunScope } from "@/lib/kb-matrix/run-api";

import { IcWarn } from "./matrix-icons";

export interface RunConfirmModalProps {
  scope: RunScope;
  columnName?: string;
  estimate: RunEstimate | null; // null = estimate loading
  starting: boolean;
  /** M3 §4.2 post-column-edit confirmation variant: heading/breakdown/CTA/secondary button use stale_confirm_* copy;
   * secondary Only Apply to New Rows action closes without requests (stale is a marker, not a trigger, FR-4). */
  postEdit?: boolean;
  onClose: () => void;
  onConfirm: () => void;
  /** Return focus here after close (opening button, or menu trigger if opened from an item); default is the focused element at opening. */
  returnFocusTo?: HTMLElement | null;
}

export function RunConfirmModal({ scope, columnName, estimate, starting, postEdit, onClose, onConfirm, returnFocusTo }: RunConfirmModalProps) {
  const t = useTranslations("kbMatrix");
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
              : t("run_modal_title_all");
  const blocked = !!estimate?.blocked;

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
        <div className="mx-breakdown">
          {estimate ? (
            postEdit ? (
              <div className={"mx-brow" + (estimate.cells_pending === 0 ? " zero" : "")}>
                <span className="k">{t("stale_confirm_breakdown", { n: estimate.cells_pending, m: estimate.skipped_edited })}</span>
              </div>
            ) : (
              <>
                <div className={"mx-brow" + (estimate.cells_pending === 0 ? " zero" : "")}>
                  <span className="k">{t("run_modal_pending")}</span>
                  <span className="n">{estimate.cells_pending}</span>
                </div>
                <div className={"mx-brow skip" + (estimate.skipped_edited === 0 ? " zero" : "")}>
                  <span className="k">{force ? t("run_modal_skip_edited_force", { n: estimate.skipped_edited }) : t("run_modal_skip_edited", { n: estimate.skipped_edited })}</span>
                </div>
              </>
            )
          ) : (
            <div className="mx-brow skip">
              <span className="k">{t("run_modal_estimating")}</span>
            </div>
          )}
        </div>
        <div className="mx-estimate">
          <span className="big">
            {t.rich("run_modal_estimate", {
              n: estimate?.credits_estimated ?? 0,
              b: (chunks) => <b>{chunks}</b>,
            })}
          </span>
        </div>
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
          className={"kb-btn " + (force ? "kb-btn-danger-solid" : "kb-btn-primary")}
          disabled={!estimate || blocked || starting || estimate.cells_pending === 0}
          data-testid="kb-matrix-run-confirm"
          onClick={onConfirm}
        >
          {postEdit
            ? t("stale_confirm_cta", { n: estimate?.cells_pending ?? 0 })
            : force
              ? t("run_modal_cta_force", { n: estimate?.cells_pending ?? 0 })
              : t("run_modal_cta")}
        </button>
      </div>
    </DialogShell>
  );
}
