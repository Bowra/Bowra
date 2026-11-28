"use client";

/**
 * Knowledge base sections (2b, interaction design 05 · A/B): Document Matrices lists the Space's matrices as a table
 * (document matrix step 1, S2: name, row source, files × columns, last run, creator); Extraction Results lists only
 * extraction tables with results. MatrixTab fetches the data. Step 1 S8: only-me matrices carry a "仅自己可见" badge.
 */

import { useLocale, useTranslations } from "next-intl";

import { ListTime } from "@/components/ui/list-time";
import { TableFrame } from "@/components/ui/table-frame";
import { appPath } from "@/lib/base-path";
import { formatShortDateTime } from "@/lib/locale";
import { shouldHandleAppLinkClick } from "@/lib/navigation-click";

import type { MatrixListItem } from "@/lib/kb-matrix/snapshot-api";
import type { TemplateViewSummary } from "@/lib/kb-matrix/types";

import { IcCheck, IcSpin, IcWarn } from "./matrix-icons";
import { MatrixVisibilityBadge } from "./visibility-badge";

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


/** The address MatrixTab opens for a matrix (kept in sync with MatrixTab.openView). */
function matrixHref(matrixId: string): string {
  return `/knowledge?tab=matrix&view=${encodeURIComponent(matrixId)}`;
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
  return (
    <div data-testid="kb-matrix-section">
      <div className="viewhd">
        <span className="d">{t("list_subtitle")}</span>
        {canEdit && (
          <span className="a">
            {/* The tab's one primary action; the page header's upload button is secondary here. */}
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
        <TableFrame className="mx-list-frame">
          <table className="ui-table mx-list-table" data-testid="kb-matrix-table">
            <thead>
              <tr>
                <th scope="col">{t("list_col_name")}</th>
                <th scope="col">{t("list_col_source")}</th>
                <th scope="col" className="ui-num">{t("list_col_size")}</th>
                <th scope="col">{t("list_col_last_run")}</th>
                <th scope="col">{t("list_col_creator")}</th>
              </tr>
            </thead>
            <tbody>
              {matrices.map((m) => (
                <tr key={m.matrixId} className="mx-list-row" data-testid={`kb-matrix-mx-${m.matrixId}`} onClick={() => onOpen(m.matrixId)}>
                  <td>
                    <a
                      className="mx-list-name"
                      href={appPath(matrixHref(m.matrixId))}
                      onClick={(e) => {
                        e.stopPropagation();
                        if (!shouldHandleAppLinkClick(e)) return;
                        e.preventDefault();
                        onOpen(m.matrixId);
                      }}
                    >
                      {m.name}
                    </a>
                    <span className="mx-list-sub">
                      <MatrixVisibilityBadge visibility={m.visibility} />
                      {m.columnsCount === 0 ? tn("empty_matrix") : <MatrixStatus m={m} />}
                    </span>
                  </td>
                  <td>{t("list_source_selected")}</td>
                  <td className="ui-num">
                    {m.filesCount} × {m.columnsCount}
                  </td>
                  <td>
                    <ListTime className="mx-list-muted" value={m.lastRunAt} empty={t("list_never_run")} />
                  </td>
                  <td>{m.createdByName ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableFrame>
      )}
    </div>
  );
}

export function TemplateRecordsSection({
  views,
  loading,
  onOpen,
  variant = "section",
}: {
  views: TemplateViewSummary[];
  loading: boolean;
  onOpen: (viewId: string) => void;
  /**
   * "section" (default): today's Extracted Results view with its own header. "group": one group of the
   * Extraction tables tab (knowledge bases switch on), headed like the template groups (name + count + one line).
   */
  variant?: "section" | "group";
}) {
  const t = useTranslations("kbMatrix");
  const tn = useTranslations("knowledge_base.nav_v2");
  const tTabs = useTranslations("knowledge_bases.tabs");
  const locale = useLocale();
  const Root = variant === "group" ? "section" : "div";
  return (
    <Root className={variant === "group" ? "tgrp" : undefined} data-testid="kb-records-section">
      {variant === "group" ? (
        <div className="tgh">
          <span className="t">{tTabs("results_group")}</span>
          {loading ? null : <span className="n">{views.length}</span>}
          <span className="d">{tTabs("results_group_desc")}</span>
        </div>
      ) : (
        <div className="viewhd">
          <span className="d">{tn("records_desc")}</span>
        </div>
      )}
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
    </Root>
  );
}
