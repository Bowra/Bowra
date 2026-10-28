"use client";

/**
 * SkillUploadModal—drag/select .zip or .md to upload one skill (IA redesign visuals, 2026-07-10).
 *
 * Visuals verbatim from Claude Design handoff (docs/sprints/skills-page-redesign/design-handoff):
 * dropzone → filecard (selected) → uploading (progress bar) → failed (inline error + retry).
 * Upload/overwrite unchanged: uploadSkill(file, {overwrite}); backend E_SKILL_NAME_EXISTS
 * triggers overwrite confirmation (error-code contract: gateway error_handler.py + config/i18n/errors/).
 * Must render within .sset scope (styles/skills-settings.css).
 */

import { useCallback, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";

import { uploadSkill, type SkillDetailDTO } from "@/lib/api";
import { SkillImportFindings, skillImportSummary } from "@/components/skill-import-findings";
import { ApiError } from "@/lib/api-client";
import type { ErrorPresentation } from "@/lib/error-presentation";
import { skillWorkflowErrorPresentation } from "@/lib/skill-workflow-error";
import { ErrorNotice } from "@/components/error-notice";
import { DialogShell } from "@/components/ui/dialog";
import { Package, Upload, X } from "@/components/icons";

interface Props {
  open: boolean;
  onClose: () => void;
  onUploaded: (skill: SkillDetailDTO) => void;
  /** skill-import batch 3: receipt-state Edit opens this skill's drawer via parent (without callback, only Done appears). */
  onEdit?: (skill: SkillDetailDTO) => void;
}

const ACCEPTED = ".zip,.md,.markdown";
const MAX_SIZE = 16 * 1024 * 1024; // Aligned with backend MAX_SKILL_SIZE (largest actual Anthropic skill: 5.55 MB).

export function SkillUploadModal({ open, onClose, onUploaded, onEdit }: Props) {
  const t = useTranslations("skill_upload");
  const te = useTranslations("errors");
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState<ErrorPresentation | null>(null);
  const [busy, setBusy] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const [askOverwrite, setAskOverwrite] = useState(false);
  // skill-import batch 3: red inventory result ⇒ uploaded but **not effective**; retain modal receipt state rather than close—
  // otherwise users cannot see why it does not work. Retain only for red; hints alone close normally, preserving the successful path.
  const [pending, setPending] = useState<SkillDetailDTO | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const ti = useTranslations("skill_import");
  const locale = useLocale();

  const reset = () => {
    setFile(null);
    setError(null);
    setBusy(false);
    setAskOverwrite(false);
    setPending(null);
  };

  const close = () => {
    reset();
    onClose();
  };

  const validateFile = (f: File): string | null => {
    if (f.size === 0) return t("empty_file");
    if (f.size > MAX_SIZE) return t("too_large", { size: (f.size / 1024 / 1024).toFixed(1) });
    const lower = f.name.toLowerCase();
    if (!lower.endsWith(".zip") && !lower.endsWith(".md") && !lower.endsWith(".markdown")) {
      return t("bad_ext");
    }
    return null;
  };

  const onPickFile = (f: File | null | undefined) => {
    if (!f) return;
    const err = validateFile(f);
    if (err) {
      setError({ message: err });
      setFile(null);
      return;
    }
    setFile(f);
    setError(null);
    setAskOverwrite(false);
  };

  const doUpload = useCallback(
    async (overwrite: boolean) => {
      if (!file) return;
      setBusy(true);
      setError(null);
      try {
        const result = await uploadSkill(file, { overwrite });
        onUploaded(result); // Refresh list first: effective or not, this skill already belongs to the user.
        if (result.import_status === "pending") {
          setBusy(false);
          setPending(result);
          return;
        }
        reset();
        onClose();
      } catch (e) {
        if (e instanceof ApiError && e.code === "E_SKILL_NAME_EXISTS") {
          setAskOverwrite(true);
        }
        setError(
          skillWorkflowErrorPresentation(e, {
            surface: "upload",
            copy: {
              generic: t("failed"),
              transport: {
                requestTimedOut: te("transport.requestTimedOut"),
                serviceUnreachable: te("transport.serviceUnreachable"),
                retry: te("transport.retry"),
              },
            },
          }),
        );
      } finally {
        setBusy(false);
      }
    },
    [file, onUploaded, onClose, t, te],
  );

  if (!open) return null;

  const kb = (n: number) => (n >= 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)}MB` : `${(n / 1024).toFixed(0)}KB`);

  return (
    <DialogShell
      onClose={close}
      busy={busy}
      label={t("title")}
      mount="inline"
      backdropClassName="scrim"
      panelClassName="modal"
    >
      <span className={`modal-ico ${pending ? "danger" : "plat"}`}>
        <Upload size={18} />
      </span>
      <h3 className="modal-title">{pending ? ti("pending_title") : t("title")}</h3>
      <p className="modal-body">{pending ? ti("pending_sub") : t("format_hint")}</p>

      {pending ? (
        <div className="imp-result">
          <div className="filecard">
            <div className="fc-ico">
              <Package size={16} />
            </div>
            <div className="fc-main">
              <div className="fc-n">{pending.name}</div>
              <div className="fc-z">{kb(pending.size_bytes)}</div>
            </div>
            <span className="sbadge skeleton">{ti("pending_badge")}</span>
          </div>
          {pending.import_report ? (
            <>
              <p className="imp-verdict">{skillImportSummary(pending.import_report, ti)}</p>
              <SkillImportFindings report={pending.import_report} locale={locale} />
            </>
          ) : null}
          <p className="imp-note">{ti("not_effective_note")}</p>
        </div>
      ) : busy ? (
        <div className="filecard">
          <div className="fc-ico">
            <Package size={16} />
          </div>
          <div className="fc-main">
            <div className="fc-n">{file?.name}</div>
            <div className="fc-z">{t("upload_busy")}</div>
          </div>
          <div className="fc-prog">
            <div className="p" />
          </div>
        </div>
      ) : (
        <>
          <button
            type="button"
            className={`dropzone${dragActive ? " hot" : ""}`}
            onDragOver={(e) => {
              e.preventDefault();
              setDragActive(true);
            }}
            onDragLeave={() => setDragActive(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragActive(false);
              onPickFile(e.dataTransfer.files?.[0]);
            }}
            onClick={() => inputRef.current?.click()}
          >
            <span className="dz-ico">
              <Upload size={20} />
            </span>
            <span className="dz-t">{t("drop_hint")}</span>
            <span className="dz-s">{t("limit_hint")}</span>
          </button>
          <input
            ref={inputRef}
            type="file"
            accept={ACCEPTED}
            onChange={(e) => onPickFile(e.target.files?.[0])}
            style={{ display: "none" }}
          />
          {file ? (
            <div className="filecard">
              <div className="fc-ico">
                <Package size={16} />
              </div>
              <div className="fc-main">
                <div className="fc-n">{file.name}</div>
                <div className="fc-z">{kb(file.size)}</div>
              </div>
              <button type="button" className="fc-x" aria-label={t("cancel")} onClick={() => setFile(null)}>
                <X size={14} />
              </button>
            </div>
          ) : null}
          {error ? (
            <div className="upload-err">
              <ErrorNotice
                message={error.message}
                diagnostics={error.diagnostics}
                surface="upload"
                diagnosticsEnabled
              />
            </div>
          ) : null}
        </>
      )}

      <div className="modal-foot">
        {pending ? (
          <>
            <button type="button" className="btn ghost sm" onClick={close}>
              {ti("done")}
            </button>
            {onEdit ? (
              <button
                type="button"
                className="btn pri sm"
                onClick={() => {
                  const uploaded = pending;
                  reset();
                  onClose();
                  onEdit(uploaded);
                }}
              >
                {ti("go_edit")}
              </button>
            ) : null}
          </>
        ) : (
          <>
            <button type="button" className="btn ghost sm" disabled={busy} onClick={close}>
              {t("cancel")}
            </button>
        {askOverwrite ? (
          <button
            type="button"
            className={`btn pri sm${busy ? " loading" : ""}`}
            disabled={busy}
            onClick={() => void doUpload(true)}
          >
            {t("overwrite")}
          </button>
        ) : (
          <button
            type="button"
            className={`btn pri sm${busy ? " loading" : ""}`}
            disabled={busy || !file}
            onClick={() => void doUpload(false)}
          >
            {error && file ? t("retry") : t("upload")}
          </button>
            )}
          </>
        )}
      </div>
    </DialogShell>
  );
}
