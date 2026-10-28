"use client";

/**
 * Extraction-table (kind=manual doc_type) creation/editing wizard — natural-language description → LLM field draft → preview/edit → save.
 * Used in the knowledge base Templates › Extraction Tables group (knowledge base UX cleanup P1).
 */

import { useTranslations } from "next-intl";
import { type HTMLAttributes, type RefObject, useId, useState } from "react";

import { kbDocTypeError } from "@/lib/kb-doc-type-error";
import {
  type ExtractField,
  type FieldType,
  type KbDocTypeDTO,
  createKnowledgeTable,
  draftKnowledgeTableFields,
  updateKnowledgeTable,
} from "@/lib/kb-knowledge-tables-api";
import { useSpaceSelection } from "@/lib/use-space";
import { CharIcon, CharIconText } from "@/components/char-icon";
import { DialogShell } from "@/components/ui/dialog";


// ─── DraftWizard ────────────────────────


export interface DraftWizardProps {
  mode: "create" | "edit";
  existing?: KbDocTypeDTO;
  onClose: () => void;
  onSaved: () => void;
  /** Where focus returns on close (the opener); explicit because WebKit does not focus clicked buttons. */
  returnFocusRef?: RefObject<HTMLElement | null>;
}


function emptyField(): ExtractField {
  return {
    name: "",
    type: "string",
    description: "",
    required: false,
    cross_verify: false,
    extraction_hint: "",
  };
}


export function DraftWizard({ mode, existing, onClose, onSaved, returnFocusRef }: DraftWizardProps) {
  const ws = useSpaceSelection().activeId ?? "";
  const t = useTranslations("kt");
  const [step, setStep] = useState<"describe" | "preview">(
    mode === "edit" ? "preview" : "describe",
  );
  const [name, setName] = useState(existing?.name ?? "");
  const [description, setDescription] = useState(existing?.description ?? "");
  const [userQuery, setUserQuery] = useState("");
  const [crossVerifyEnabled, setCrossVerifyEnabled] = useState(
    existing?.cross_verify_enabled ?? true,
  );
  const [fields, setFields] = useState<ExtractField[]>(
    existing?.fields ?? [],
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const titleId = useId();

  const handleDraft = async () => {
    if (!ws) {
      setError(t("errors.no_ws"));
      return;
    }
    if (!userQuery.trim()) {
      setError(t("wizard.no_query"));
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const result = await draftKnowledgeTableFields({
        workspace_id: ws,
        user_query: userQuery,
      });
      setFields(result.fields);
      setStep("preview");
    } catch (e) {
      setError(t(kbDocTypeError(e, "draft")));
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async () => {
    if (!ws && mode === "create") {
      setError(t("errors.no_ws"));
      return;
    }
    if (!name.trim()) {
      setError(t("wizard.name_required"));
      return;
    }
    if (fields.length === 0) {
      setError(t("wizard.min_one_field"));
      return;
    }
    if (fields.some((f) => !f.name.trim())) {
      setError(t("wizard.all_fields_named"));
      return;
    }
    setLoading(true);
    setError(null);
    try {
      if (mode === "edit" && existing) {
        await updateKnowledgeTable(existing.doc_type_id, {
          name: name.trim(),
          description,
          fields,
          cross_verify_enabled: crossVerifyEnabled,
        });
      } else {
        await createKnowledgeTable({
          workspace_id: ws,
          name: name.trim(),
          description,
          fields,
          cross_verify_enabled: crossVerifyEnabled,
        });
      }
      onSaved();
    } catch (e) {
      setError(t(kbDocTypeError(e, mode)));
    } finally {
      setLoading(false);
    }
  };

  const updateField = (idx: number, patch: Partial<ExtractField>) => {
    setFields((prev) => prev.map((f, i) => (i === idx ? { ...f, ...patch } : f)));
  };

  return (
    // Dialog semantics, focus trap, Esc and focus return (P2 batch 5 accessibility fix, both appearances). Rendered in
    // place as before so page-scoped styles still apply; the former inline styles live in the kt-wizard-* classes
    // (styles/ui-controls-legacy.css, unchanged look), the refreshed look in styles/ui-refresh-kb.css.
    <DialogShell
      onClose={onClose}
      busy={loading}
      returnFocusRef={returnFocusRef}
      labelledBy={titleId}
      mount="inline"
      backdropClassName="kt-wizard-overlay"
      panelClassName="kt-wizard-panel"
      panelProps={{ "data-testid": "kt-wizard" } as HTMLAttributes<HTMLDivElement>}
    >
        <h2 id={titleId} className="kt-wizard-title">
          {mode === "edit" ? t("wizard.title_edit") : t("wizard.title_create")} —{" "}
          {step === "describe" ? t("wizard.step_describe") : t("wizard.step_preview")}
        </h2>

        {error && (
          <div className="kt-wizard-error" role="alert" data-testid="kt-wizard-error">
            {error}
          </div>
        )}

        {step === "describe" && (
          <>
            <label className="kt-wizard-field">
              <span className="kt-wizard-label">{t("wizard.name_label")}</span>
              <input
                className="kt-wizard-input"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={t("wizard.name_placeholder")}
                data-testid="kt-name"
              />
            </label>
            <label className="kt-wizard-field kt-wizard-field--last">
              <span className="kt-wizard-label">{t("wizard.describe_label")}</span>
              <textarea
                className="kt-wizard-input kt-wizard-textarea"
                value={userQuery}
                onChange={(e) => setUserQuery(e.target.value)}
                placeholder={t("wizard.describe_placeholder")}
                rows={5}
                data-testid="kt-describe"
              />
            </label>
            <div className="kt-wizard-actions">
              <button type="button" className="kt-wizard-btn" onClick={onClose}>
                {t("wizard.cancel")}
              </button>
              <button
                type="button"
                className={`kt-wizard-btn-primary${loading ? " is-loading is-dim" : ""}`}
                onClick={handleDraft}
                disabled={loading || !userQuery.trim()}
                data-testid="kt-draft-btn"
              >
                {loading ? t("wizard.drafting") : <CharIconText ch="→" at="end">{t("wizard.draft_btn")}</CharIconText>}
              </button>
            </div>
          </>
        )}

        {step === "preview" && (
          <>
            {mode === "create" && (
              <label className="kt-wizard-field">
                <span className="kt-wizard-label">{t("wizard.name_short_label")}</span>
                <input
                  className="kt-wizard-input"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  data-testid="kt-name"
                />
              </label>
            )}
            <label className="kt-wizard-field">
              <span className="kt-wizard-label">{t("wizard.description_label")}</span>
              <input
                className="kt-wizard-input"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </label>
            <label className="kt-wizard-check">
              <input
                type="checkbox"
                className="kt-wizard-checkbox"
                checked={crossVerifyEnabled}
                onChange={(e) => setCrossVerifyEnabled(e.target.checked)}
                data-testid="kt-cross-verify"
              />
              <span>
                {t("wizard.cross_verify")}
              </span>
            </label>

            <div className="kt-wizard-fields-head">
              <strong className="kt-wizard-fields-count">{t("wizard.fields_count", { count: fields.length })}</strong>
              <button
                type="button"
                className="kt-wizard-add"
                onClick={() => setFields([...fields, emptyField()])}
                disabled={fields.length >= 20}
                data-testid="kt-add-field"
              >
                {t("wizard.add_field")}
              </button>
            </div>

            {fields.map((f, idx) => (
              <div key={idx} className="kt-wizard-row" data-testid={`kt-field-${idx}`}>
                <input
                  className="kt-wizard-cell"
                  value={f.name}
                  onChange={(e) => updateField(idx, { name: e.target.value })}
                  placeholder={t("wizard.field_name_ph")}
                  aria-label={t("wizard.field_name_ph")}
                  data-testid={`kt-field-${idx}-name`}
                />
                <select
                  className="kt-wizard-cell"
                  value={f.type}
                  onChange={(e) =>
                    updateField(idx, { type: e.target.value as FieldType })
                  }
                  aria-label={t("wizard.field_type_label")}
                  data-testid={`kt-field-${idx}-type`}
                >
                  <option value="string">{t("field_type.string")}</option>
                  <option value="number">{t("field_type.number")}</option>
                  <option value="currency">{t("field_type.currency")}</option>
                  <option value="date">{t("field_type.date")}</option>
                </select>
                <input
                  className="kt-wizard-cell"
                  value={f.description}
                  onChange={(e) =>
                    updateField(idx, { description: e.target.value })
                  }
                  placeholder={t("wizard.field_desc_ph")}
                  aria-label={t("wizard.field_desc_ph")}
                />
                <div className="kt-wizard-flags">
                  <label className="kt-wizard-flag" title={t("wizard.flag_required")}>
                    <input
                      type="checkbox"
                      className="kt-wizard-checkbox"
                      checked={f.required}
                      onChange={(e) =>
                        updateField(idx, { required: e.target.checked })
                      }
                    />
                    {t("wizard.flag_required_short")}
                  </label>
                  <label className="kt-wizard-flag" title={t("wizard.flag_cross_verify")}>
                    <input
                      type="checkbox"
                      className="kt-wizard-checkbox"
                      checked={f.cross_verify}
                      onChange={(e) =>
                        updateField(idx, { cross_verify: e.target.checked })
                      }
                    />
                    {t("wizard.flag_cross_verify_short")}
                  </label>
                  <button
                    type="button"
                    className="kt-wizard-del"
                    onClick={() =>
                      setFields((prev) => prev.filter((_, i) => i !== idx))
                    }
                    title={t("wizard.delete_field")}
                    aria-label={t("wizard.delete_field")}
                  >
                    <CharIcon ch="×" />
                  </button>
                </div>
              </div>
            ))}

            <div className="kt-wizard-actions kt-wizard-actions--end">
              {mode === "create" && (
                <button type="button" className="kt-wizard-btn" onClick={() => setStep("describe")}>
                  <CharIconText ch="←">{t("wizard.back_describe")}</CharIconText>
                </button>
              )}
              <button type="button" className="kt-wizard-btn" onClick={onClose}>
                {t("wizard.cancel")}
              </button>
              <button
                type="button"
                className={`kt-wizard-btn-primary${loading ? " is-loading" : ""}`}
                onClick={handleSave}
                disabled={loading}
                data-testid="kt-save-btn"
              >
                {loading
                  ? t("wizard.saving")
                  : mode === "edit"
                    ? t("wizard.save_edit")
                    : t("wizard.create")}
              </button>
            </div>
          </>
        )}
    </DialogShell>
  );
}
