"use client";

/**
 * Knowledge-base UX cleanup P1 (interaction design 05 · C)—Extraction Templates group and New Template purpose selection in the Templates view.
 *
 * Extraction template = kind=manual doc_type (bound during upload, automatically extracts fields), managed in the Templates view.
 * Edit/delete reuse the server's admin-or-creator gate
 * (canManageTemplate); cards without permission show neither action.
 */

import { useTranslations } from "next-intl";

import { DialogShell } from "@/components/ui/dialog";
import { RowActionsMenu } from "@/components/ui/row-actions-menu";
import type { KbDocTypeDTO } from "@/lib/kb-knowledge-tables-api";
import { canManageTemplate } from "@/lib/kb-matrix/template-view";

export interface ExtractionTemplateGroupProps {
  items: KbDocTypeDTO[];
  recordCounts: Record<string, number>;
  uid: string | null;
  isAdmin: boolean;
  busy: boolean;
  /** trigger: the control that opened the wizard (its focus-return target; WebKit does not focus clicked buttons). */
  onEdit: (dt: KbDocTypeDTO, trigger: HTMLElement | null) => void;
  onDelete: (dt: KbDocTypeDTO) => void;
  onViewRecords: (dt: KbDocTypeDTO) => void;
}

export function ExtractionTemplateGroup({
  items,
  recordCounts,
  uid,
  isAdmin,
  busy,
  onEdit,
  onDelete,
  onViewRecords,
}: ExtractionTemplateGroupProps) {
  const t = useTranslations("knowledge_base.nav_v2");
  return (
    <section className="tgrp" data-testid="kb-tpl-group-extraction">
      <div className="tgh">
        <span className="t">{t("group_extraction")}</span>
        <span className="n">{items.length}</span>
        <span className="d">{t("group_extraction_desc")}</span>
      </div>
      {items.length === 0 ? (
        <div className="emptyline">{t("group_extraction_empty")}</div>
      ) : (
        <div className="tcards">
          {items.map((dt) => {
            const canManage = canManageTemplate(dt.created_by, uid, isAdmin);
            const records = recordCounts[dt.doc_type_id] ?? dt.record_count ?? 0;
            return (
              <div className="tcard" key={dt.doc_type_id} data-testid={`kb-tpl-extraction-${dt.doc_type_id}`}>
                <div className="nm">
                  {dt.name}
                  <span className="tag x">{t("tag_extraction")}</span>
                </div>
                {dt.fields.length > 0 && (
                  <div className="cols">{dt.fields.map((f) => `${f.name}${f.required ? "*" : ""}`).join(" · ")}</div>
                )}
                <div className="ft">
                  {t("extraction_meta", { fields: dt.fields.length, records })}
                  <span className="a">
                    <RowActionsMenu
                      label={t("more_actions_for", { name: dt.name })}
                      pending={busy}
                      actions={[
                        { id: "records", label: t("view_records"), onSelect: () => onViewRecords(dt) },
                        ...(canManage
                          ? [
                              { id: "edit", label: t("edit"), disabled: busy, onSelect: () => onEdit(dt, document.activeElement instanceof HTMLElement ? document.activeElement : null) },
                              { id: "delete", label: t("delete"), danger: true, disabled: busy, onSelect: () => onDelete(dt) },
                            ]
                          : []),
                      ]}
                    />
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

export type TemplatePurpose = "matrix" | "extraction";

/** New Template first asks the purpose: matrix column group → new-matrix flow (Save as Template in a matrix); automatic upload extraction → extraction-template wizard. */
export function TemplatePurposeDialog({
  onPick,
  onClose,
}: {
  onPick: (purpose: TemplatePurpose) => void;
  onClose: () => void;
}) {
  const t = useTranslations("knowledge_base.nav_v2");
  return (
    <DialogShell
      onClose={onClose}
      labelledBy="kb-tpl-purpose-title"
      backdropClassName="kb-overlay"
      panelClassName="kb-modal"
      panelProps={{ "data-testid": "kb-tpl-purpose" } as Record<string, string>}
    >
      <div className="kb-modal-head">
        <h3 id="kb-tpl-purpose-title">{t("new_template")}</h3>
        <div className="sub">{t("purpose_sub")}</div>
      </div>
      <div className="kb-modal-body">
        <button type="button" className="kb-scope-card kb-purpose" data-testid="kb-tpl-purpose-matrix" onClick={() => onPick("matrix")}>
          <span>
            <span className="title">{t("purpose_matrix_title")}</span>
            <span className="desc">{t("purpose_matrix_desc")}</span>
          </span>
        </button>
        <button
          type="button"
          className="kb-scope-card kb-purpose"
          data-testid="kb-tpl-purpose-extraction"
          onClick={() => onPick("extraction")}
        >
          <span>
            <span className="title">{t("purpose_extraction_title")}</span>
            <span className="desc">{t("purpose_extraction_desc")}</span>
          </span>
        </button>
      </div>
      <div className="kb-modal-foot">
        <button type="button" className="kb-btn kb-btn-ghost" onClick={onClose}>
          {t("cancel")}
        </button>
      </div>
    </DialogShell>
  );
}
