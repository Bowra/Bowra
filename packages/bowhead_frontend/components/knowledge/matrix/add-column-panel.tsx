"use client";

/**
 * kb-matrix M2 — Add-column panel (FR-2/FR-3; design r2 AddColumnPanel verbatim).
 * Question textarea → [让 AI 建议] (draft endpoint, failure 200+fallback never blocks manual input) →
 * name + type segments + collapsed extraction hint → [创建并运行]/[仅创建].
 */

import { useState } from "react";
import { useTranslations } from "next-intl";

import { type ColumnDraft, type NewColumnIn, draftColumn } from "@/lib/kb-matrix/run-api";

import { IcChevR, IcSpark, IcSpin, IcX } from "./matrix-icons";
import { OptionsEditor } from "./options-editor";
import { TypePicker8 } from "./type-picker";

/** Options only for select/multi_select (hide options editor for other types). */
function needsOptions(type: string): boolean {
  return type === "select" || type === "multi_select";
}

export interface AddColumnPanelProps {
  matrixId: string;
  onClose: () => void;
  onCreate: (col: NewColumnIn, run: boolean) => void;
  creating: boolean;
}

type Stage = "empty" | "drafting" | "ready" | "failed";

export function AddColumnPanel({ matrixId, onClose, onCreate, creating }: AddColumnPanelProps) {
  const t = useTranslations("kbMatrix");
  const [q, setQ] = useState("");
  const [stage, setStage] = useState<Stage>("empty");
  const [name, setName] = useState("");
  const [type, setType] = useState<string>("question");
  const [hint, setHint] = useState("");
  const [adv, setAdv] = useState(false);
  const [cv, setCv] = useState(false);
  // M3 wave-2: select/multi_select options + subset prefilled by AI suggestion (trailing marker).
  const [options, setOptions] = useState<string[]>([]);
  const [aiOptions, setAiOptions] = useState<string[]>([]);

  const draft = async () => {
    setStage("drafting");
    try {
      const d: ColumnDraft = await draftColumn(matrixId, q.trim());
      setName(d.name);
      // wave-2: 8-type picker is live; accept AI-suggested select/multi_select/boolean directly
      // (no longer clamp back to question); returned options prefill editor with “AI 建议” trailing marker.
      setType(d.type);
      if (d.options && d.options.length) {
        setOptions(d.options);
        setAiOptions(d.options);
      }
      if (d.extraction_hint) setHint(d.extraction_hint);
      setStage(d.fallback ? "failed" : "ready");
    } catch {
      // FR-3: draft failure never blocks manual input.
      if (!name) setName(q.trim().slice(0, 20));
      setStage("failed");
    }
  };

  const ready = stage === "ready" || stage === "failed";
  const showOpts = needsOptions(type);
  // Mirror server options_required: select/multi_select without options cannot submit.
  const optionsMissing = showOpts && options.length === 0;
  const submit = (run: boolean) =>
    onCreate(
      {
        name: name.trim(),
        type,
        question: q.trim(),
        extraction_hint: hint.trim() || undefined,
        // Send options only for select/multi_select (server ignores/rejects other types with 422).
        ...(showOpts ? { options } : {}),
        // question columns forbid cross-verification (FR-39 server 422); silently omit when switching back to question.
        ...(cv && type !== "question" ? { cross_verify: true } : {}),
      },
      run,
    );

  return (
    <div className="mx-panel addcol" data-testid="kb-matrix-addcol" onClick={(e) => e.stopPropagation()}>
      <div className="mx-panel-head">
        <div style={{ flex: 1 }}>
          <div className="col">{t("addcol_title")}</div>
        </div>
        <button type="button" className="mx-back" aria-label={t("close")} onClick={onClose}>
          <IcX s={16} />
        </button>
      </div>
      <div className="mx-panel-body">
        <textarea
          className="mx-ta"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={t("addcol_placeholder")}
        />
        {stage === "empty" && (
          <button
            type="button"
            className="kb-btn kb-btn-secondary"
            style={{ marginTop: 10 }}
            disabled={!q.trim()}
            onClick={draft}
          >
            <IcSpark s={14} />
            {t("addcol_ai_suggest")}
          </button>
        )}
        {stage === "drafting" && (
          <div className="mx-spinline">
            <IcSpin s={14} />
            {t("addcol_drafting")}
          </div>
        )}
        {stage === "failed" && <div className="mx-inlineerr">{t("addcol_draft_failed")}</div>}
        {ready && (
          <div className="mx-suggest">
            <div className="lab">
              <IcSpark s={12} style={{ color: "var(--c-accent)" }} />
              {t("addcol_ai_label")}
            </div>
            <input className="mx-inp" value={name} onChange={(e) => setName(e.target.value)} placeholder={t("addcol_name_placeholder")} />
            <div className="mx-seclabel" style={{ marginTop: 12 }}>{t("type_section")}</div>
            <TypePicker8 value={type} onChange={setType} />
            <div className="hint" style={{ marginTop: 10 }}>
              {stage === "ready" ? t("addcol_hint_ready") : ""}
              {type === "question" && (
                <>
                  <br />
                  {t("addcol_hint_question")}
                </>
              )}
            </div>
          </div>
        )}
        {ready && showOpts && (
          <OptionsEditor value={options} onChange={setOptions} aiValues={aiOptions} />
        )}
        {stage !== "empty" && (
          <div className={"mx-adv" + (adv ? " open" : "")}>
            <div
              className="mx-adv-head"
              role="button"
              tabIndex={0}
              onClick={() => setAdv((v) => !v)}
              onKeyDown={(e) => e.key === "Enter" && setAdv((v) => !v)}
            >
              <span className="chev"><IcChevR s={13} /></span>
              {t("addcol_hint_label")}
            </div>
            <div className="mx-adv-body">
              <textarea
                className="mx-ta"
                style={{ minHeight: 64 }}
                value={hint}
                onChange={(e) => setHint(e.target.value)}
                placeholder={t("addcol_hint_placeholder")}
              />
              {/* M3 §4.7: cross-verification checkbox (disabled for question columns: FR-39 server 422). */}
              {type !== "question" && (
                <label
                  style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 8, fontSize: "0.75rem", cursor: "pointer" }}
                  data-testid="kb-matrix-cv-toggle"
                >
                  <input type="checkbox" checked={cv} onChange={(e) => setCv(e.target.checked)} />
                  {t("cv_toggle")}
                </label>
              )}
            </div>
          </div>
        )}
      </div>
      <div className="mx-panel-foot">
        <button
          type="button"
          className="kb-btn kb-btn-primary"
          disabled={!ready || !name.trim() || optionsMissing || creating}
          onClick={() => submit(true)}
        >
          {t("addcol_create_run")}
        </button>
        <button
          type="button"
          className="kb-btn kb-btn-secondary"
          disabled={!ready || !name.trim() || optionsMissing || creating}
          onClick={() => submit(false)}
        >
          {t("addcol_create_only")}
        </button>
        <button type="button" className="kb-btn kb-btn-ghost" onClick={onClose}>
          {t("cancel")}
        </button>
      </div>
    </div>
  );
}
