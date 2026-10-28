"use client";

/**
 * kb-matrix M4 — Graduate to template confirmation dialog (design-m4 GraduateDialog verbatim).
 *
 * Eight mockup variants → data-driven: over=columns>20 (preblocked); warn/danger=unfinished/
 * failed/expired cell proportion (>30% danger); graduated=existing graduated-template hint (reverse lineage lookup);
 * conflict=server 409 suggested name; submitting=entire dialog noninteractive (review disposition #4).
 * Readiness derives purely from type (graduate-api.readinessOf); glyph follows matrix-grid
 * TYPE_GLYPH (date=☷, review disposition #1).
 */

import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { DialogShell } from "@/components/ui/dialog";
import { Radio, RadioGroup } from "@/components/ui/radio-group";
import { listDocTypesWithStats } from "@/lib/kb-knowledge-tables-api";
import {
  GRADUATE_MAX_COLUMNS,
  type GraduateMode,
  type GraduateResult,
  MatrixApiError,
  graduateMatrix,
  readinessOf,
} from "@/lib/kb-matrix/graduate-api";
import { kbMatrixMutationError } from "@/lib/kb-matrix/mutation-error";
import type { MatrixColumnVM } from "@/lib/kb-matrix/types";
import { useSpaceSelection } from "@/lib/use-space";

import { TYPE_GLYPH } from "./matrix-grid";
import { IcClock, IcGrid, IcSpin, IcWarn, IcX } from "./matrix-icons";

export interface GraduateDialogProps {
  matrixId: string;
  /** Default name = matrix name. */
  defaultName: string;
  columns: MatrixColumnVM[];
  filesCount: number;
  /** Unfinished cells (no value/queued/running), graduated as empty values. */
  emptyCells: number;
  failedCells: number;
  staleCells: number;
  onClose: () => void;
  /** Success (including idempotent reuse): parent shows Toast + closes. */
  onDone: (r: GraduateResult) => void;
}

interface PriorInfo {
  count: number;
  latestName: string;
  latestDate: string;
}

export function GraduateDialog({
  matrixId,
  defaultName,
  columns,
  filesCount,
  emptyCells,
  failedCells,
  staleCells,
  onClose,
  onDone,
}: GraduateDialogProps) {
  const t = useTranslations("kbMatrix");
  const ws = useSpaceSelection().activeId ?? "";
  const [mode, setMode] = useState<GraduateMode>("snapshot");
  const [name, setName] = useState(defaultName);
  const [submitting, setSubmitting] = useState(false);
  const [conflictSuggestion, setConflictSuggestion] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [prior, setPrior] = useState<PriorInfo | null>(null);
  const nameRef = useRef<HTMLInputElement | null>(null);

  const over = columns.length > GRADUATE_MAX_COLUMNS;
  const totalCells = filesCount * columns.length;
  const incomplete = emptyCells + failedCells + staleCells;
  const danger = totalCells > 0 && incomplete / totalCells > 0.3;
  const warn = !danger && incomplete > 0;

  // Readiness group subtotals (mockup sum).
  const sum = useMemo(() => {
    const acc = { agg: 0, cat: 0, list: 0 };
    for (const c of columns) acc[readinessOf(c.type).k]++;
    return acc;
  }, [columns]);

  // Hint for existing graduated-template count (reverse lineage lookup, once on opening, best-effort).
  useEffect(() => {
    if (!ws) return;
    let cancelled = false;
    listDocTypesWithStats(ws)
      .then(({ items }) => {
        if (cancelled) return;
        const priors = items
          .filter((d) => d.graduated_from_matrix_id === matrixId && d.kind === "graduated")
          .sort((a, b) => (b.updated_at ?? "").localeCompare(a.updated_at ?? ""));
        if (priors.length) {
          setPrior({
            count: priors.length,
            latestName: priors[0].name,
            latestDate: (priors[0].updated_at ?? "").slice(0, 10),
          });
        }
      })
      .catch(() => undefined); // Hint is purely informational; failure silent.
    return () => {
      cancelled = true;
    };
  }, [matrixId, ws]);

  // Conflict appears → focus name input (mockup autoFocus).
  useEffect(() => {
    if (conflictSuggestion) nameRef.current?.focus();
  }, [conflictSuggestion]);

  const warnParts = [
    emptyCells > 0 ? t("grad_warn_empty", { n: emptyCells }) : null,
    failedCells > 0 ? t("grad_warn_failed", { n: failedCells }) : null,
    staleCells > 0 ? t("grad_warn_stale", { n: staleCells }) : null,
  ].filter((x): x is string => !!x);

  const submit = async () => {
    if (over || submitting || !name.trim()) return;
    setSubmitting(true);
    setErr(null);
    setConflictSuggestion(null);
    try {
      const res = await graduateMatrix(matrixId, { name: name.trim(), mode });
      onDone(res);
    } catch (e) {
      if (e instanceof MatrixApiError && e.code === "doc_type_name_taken") {
        setConflictSuggestion((e.payload.suggestion as string | undefined) ?? "");
      } else {
        const message = kbMatrixMutationError(e);
        setErr(message.values ? t(message.key, message.values) : t(message.key));
      }
      setSubmitting(false);
    }
  };

  const bold = { b: (chunks: ReactNode) => <b>{chunks}</b> };

  return (
    <DialogShell
      onClose={onClose}
      busy={submitting}
      label={t("grad_title")}
      backdropClassName="kb-overlay"
      panelClassName="kb-modal mx-grad-modal"
      panelProps={{ "data-testid": "kb-matrix-graduate-modal" } as Record<string, string>}
    >
      <div className="kb-modal-head mx-grad-head">
        <h3>{t("grad_title")}</h3>
        <button type="button" className="kb-x" aria-label={t("cancel")} disabled={submitting} onClick={onClose}>
          <IcX s={16} />
        </button>
      </div>
      {/* Review disposition #4: submitting disables entire dialog, not just CTA. */}
      <div className={`kb-modal-body${submitting ? " mx-grad-busy" : ""}`}>
        {over && (
          <div className="mx-blocked">
            <span className="ic">
              <IcWarn s={15} />
            </span>
            {t("grad_over", { max: GRADUATE_MAX_COLUMNS, got: columns.length })}
          </div>
        )}
        {err && (
          <div className="mx-blocked" data-testid="kb-matrix-graduate-err">
            <span className="ic">
              <IcWarn s={15} />
            </span>
            {err}
          </div>
        )}

        <div className="mx-flabel mx-flabel--first">
          {t("grad_name_label")}
        </div>
        <input
          ref={nameRef}
          className={`mx-inp${conflictSuggestion !== null ? " is-invalid" : ""}`}
          data-testid="kb-matrix-graduate-name"
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            setConflictSuggestion(null);
          }}
          placeholder={t("grad_name_ph")}
        />
        {conflictSuggestion !== null && (
          <div className="mx-fielderr mx-fielderr--6">
            {t("grad_conflict", { suggestion: conflictSuggestion || `${name.trim()} · 2` })}
          </div>
        )}

        {/* Use busy, not disabled, during submission: retain focus on current card while ignoring arrow selection. */}
        <RadioGroup<GraduateMode>
          appearance="none"
          className="mx-moderow mx-moderow--spaced"
          orientation="both"
          value={mode}
          onValueChange={setMode}
          busy={submitting}
        >
          <Radio
            value="snapshot"
            className={"mx-mode" + (mode === "snapshot" ? " on" : "")}
            data-testid="kb-matrix-graduate-mode-snapshot"
          >
            <span className="radio" />
            <span>
              <span className="mt">{t("grad_mode_promote_t")}</span>
              <span className="md">{t("grad_mode_promote_d")}</span>
            </span>
          </Radio>
          <Radio
            value="template"
            className={"mx-mode" + (mode === "template" ? " on" : "")}
            data-testid="kb-matrix-graduate-mode-template"
          >
            <span className="radio" />
            <span>
              <span className="mt">{t("grad_mode_save_t")}</span>
              <span className="md">{t("grad_mode_save_d")}</span>
            </span>
          </Radio>
        </RadioGroup>

        <div className="mx-grad-scale">
          {mode === "snapshot" ? (
            <>
              {t.rich("grad_scale_files", { n: filesCount, ...bold })}
              <span className="arrow">×</span>
              {t.rich("grad_scale_cols", { n: columns.length, ...bold })}
              <span className="arrow">→</span>
              {t("grad_scale_result")}
            </>
          ) : (
            // Review disposition #5: use design wording “N 列的列定义”.
            t.rich("grad_scale_save", { n: columns.length, ...bold })
          )}
        </div>

        {prior && (
          <div className="mx-grad-info">
            <span className="ic">
              <IcGrid s={14} />
            </span>
            {t("grad_prior_info", { n: prior.count, name: prior.latestName, date: prior.latestDate })}
          </div>
        )}

        {warn && (
          <div className="mx-warnbar mx-warnbar--grad">
            <span className="ic">
              <IcWarn s={15} />
            </span>
            {warnParts.join(" · ") + t("grad_warn_suffix")}
          </div>
        )}
        {danger && (
          <div className="mx-warnbar danger mx-warnbar--grad">
            <span className="ic">
              <IcWarn s={15} />
            </span>
            {t("grad_danger")}
          </div>
        )}

        {mode === "snapshot" && (
          <>
            <div className="mx-readsum">
              <span className="pill agg">{t("grad_sum_agg", { n: sum.agg })}</span>
              <span className="pill cat">{t("grad_sum_cat", { n: sum.cat })}</span>
              <span className="pill list">{t("grad_sum_list", { n: sum.list })}</span>
            </div>
            <div className="mx-readlist">
              {columns.map((c) => {
                const r = readinessOf(c.type);
                return (
                  <div className="mx-readrow" key={c.columnId}>
                    <span className="g">{TYPE_GLYPH[c.type] ?? "Aa"}</span>
                    <span className="nm">{c.name}</span>
                    <span className={"rd " + r.k}>
                      <span className="dotm" />
                      {t(`grad_ready_${r.k}` as never)}
                      {r.unit && (
                        <span className="unit" title={t("grad_unit_tip")}>
                          {t("grad_unit_undeclared")} <IcWarn s={11} />
                        </span>
                      )}
                    </span>
                  </div>
                );
              })}
            </div>
          </>
        )}

        <div className="mx-freeze-note">
          <span className="ic">
            <IcClock s={13} />
          </span>
          {t("grad_freeze_note")}
        </div>
      </div>
      <div className="kb-modal-foot">
        <button type="button" className="kb-btn kb-btn-ghost" disabled={submitting} onClick={onClose}>
          {t("cancel")}
        </button>
        <button
          type="button"
          className="kb-btn kb-btn-primary"
          data-testid="kb-matrix-graduate-submit"
          disabled={over || submitting || !name.trim() || (mode === "snapshot" && filesCount === 0)}
          onClick={() => void submit()}
        >
          {submitting ? (
            <>
              <IcSpin s={14} />
              {t("grad_submitting")}
            </>
          ) : mode === "snapshot" ? (
            t("grad_cta_promote", { n: filesCount })
          ) : (
            t("grad_cta_save")
          )}
        </button>
      </div>
    </DialogShell>
  );
}
