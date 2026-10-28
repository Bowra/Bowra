"use client";

/**
 * Knowledge base left sidebar (2b, interaction design 05 · A/B): Document Matrices lists only my matrices; Extraction Results lists only extraction tables with results.
 * MatrixTab fetches the data.
 */

import { useLocale, useTranslations } from "next-intl";

import { formatShortDateTime } from "@/lib/locale";

import type { MatrixListItem } from "@/lib/kb-matrix/snapshot-api";
import type { TemplateViewSummary } from "@/lib/kb-matrix/types";

import { IcCheck, IcSpin, IcWarn } from "./matrix-icons";

function MatrixStatus({ m }: { m: MatrixListItem }) {
  const t = useTranslations("kbMatrix");
  const s = m.statusSummary;
  if (s.running > 0 || s.queued > 0) {
    return (
      <span className="status run">
        <IcSpin s={12} />
        {t("list_status_running", { done: s.done, total: s.done + s.failed + s.running + s.queued + s.needs_review })}
      </span>
    );
  }
  if (s.failed > 0) {
    return (
      <span className="status err">
        <IcWarn s={12} />
        {t("list_status_failed", { n: s.failed })}
      </span>
    );
  }
  return (
    <span className="status ok">
      <IcCheck s={13} />
      {t("list_status_done", { n: s.done })}
    </span>
  );
}

function cardKeys(onOpen: () => void) {
  return {
    role: "button" as const,
    tabIndex: 0,
    onClick: onOpen,
    onKeyDown: (e: React.KeyboardEvent) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onOpen();
      }
    },
  };
}


export function MatrixSection({
  matrices,
  loading,
  canEdit,
  onOpen,
  onNew,
}: {
  matrices: MatrixListItem[];
  loading: boolean;
  canEdit: boolean;
  onOpen: (viewId: string) => void;
  onNew: () => void;
}) {
  const t = useTranslations("kbMatrix");
  const tn = useTranslations("knowledge_base.nav_v2");
  const locale = useLocale();
  return (
    <div data-testid="kb-matrix-section">
      <div className="viewhd">
        <span className="d">{t("list_subtitle")}</span>
        {canEdit && (
          <span className="a">
            <button type="button" className="kb-btn kb-btn-primary" data-testid="kb-matrix-new" onClick={onNew}>
              {t("new_matrix")}
            </button>
          </span>
        )}
      </div>
      {loading ? (
        <div className="emptyline">{t("loading")}</div>
      ) : matrices.length === 0 ? (
        <div className="emptyline">{t("empty_body_m2")}</div>
      ) : (
        <div className="mxlist">
          {matrices.map((m) => (
            <div key={m.matrixId} className="mx" data-testid={`kb-matrix-mx-${m.matrixId}`} {...cardKeys(() => onOpen(m.matrixId))}>
              <div className="nm">{m.name}</div>
              {m.columnsCount === 0 ? (
                <>
                  <div className="mt">{tn("matrix_files", { n: m.filesCount })}</div>
                  <div className="mt2">{tn("empty_matrix")}</div>
                </>
              ) : (
                <>
                  <div className="mt">
                    {t("scale_matrix", { files: m.filesCount, cols: m.columnsCount })}
                    {" · "}
                    <MatrixStatus m={m} />
                  </div>
                  <div className="mt2">{m.lastRunAt ? tn("last_run", { time: formatShortDateTime(m.lastRunAt, locale) }) : null}</div>
                </>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function TemplateRecordsSection({
  views,
  loading,
  onOpen,
}: {
  views: TemplateViewSummary[];
  loading: boolean;
  onOpen: (viewId: string) => void;
}) {
  const t = useTranslations("kbMatrix");
  const tn = useTranslations("knowledge_base.nav_v2");
  const locale = useLocale();
  return (
    <div data-testid="kb-records-section">
      <div className="viewhd">
        <span className="d">{tn("records_desc")}</span>
      </div>
      {loading ? (
        <div className="emptyline">{t("loading")}</div>
      ) : views.length === 0 ? (
        <div className="emptyline" data-testid="kb-records-empty">
          {tn("records_empty")}
        </div>
      ) : (
        <div className="mxlist">
          {views.map((v) => (
            <div key={v.viewId} className="mx" data-testid={`kb-matrix-view-${v.docTypeId}`} {...cardKeys(() => onOpen(v.viewId))}>
              <div className="nm">{v.name}</div>
              <div className="mt">{t("scale", { rows: v.recordCount, cols: v.columnCount })}</div>
              <div className="mt2">{v.lastExtractedAt ? tn("last_extracted", { time: formatShortDateTime(v.lastExtractedAt, locale) }) : null}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
