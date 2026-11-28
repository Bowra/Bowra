"use client";

/**
 * kb-matrix M2 — new-matrix flow (design r2 NewMatrixStep verbatim skeleton):
 * Step 1 template cards → step 2 files (skip with Open as matrix preselected file_ids).
 *
 * M4 (decision 14): template cards fetch doc_types (built-in seed + user template/graduated,
 * retired FE constants); prefill columns from dt.fields (fieldsToColumns); `preset` (Templates-page
 * Apply as matrix handoff) fixes columns → skip step 1, choose files directly.
 *
 * Step 1 S8 (`privateOption`, FEATURE_KB_MATRIX_PRIVATE): "谁能看到" in the step with the create button — Space
 * members (default) or only me. Only me also lists the viewer's personal files; switching back unchecks them.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { LucideGlyph } from "@/components/icon-refresh";
import { fileTypeIcon } from "@/lib/file-type-icon";
import { fetchKbFiles } from "@/lib/kb-api";
import type { KbDocTypeDTO } from "@/lib/kb-knowledge-tables-api";
import { fieldsToColumns, isColumnGroupKind } from "@/lib/kb-matrix/graduate-api";
import { kbMatrixMutationError } from "@/lib/kb-matrix/mutation-error";
import { type NewColumnIn, createMatrix } from "@/lib/kb-matrix/run-api";
import type { MatrixVisibility } from "@/lib/kb-matrix/types";

import { IcPlus } from "./matrix-icons";

interface FileOption {
  fileId: string;
  filename: string;
  mime: string;
}

export interface NewMatrixFlowProps {
  workspaceId: string;
  /** Open as matrix prefill (PRD §7 path 1): skip file selection. */
  preselectedFileIds?: string[];
  /** M4 Apply as matrix handoff: fixed columns → skip template selection. */
  preset?: { name: string; columns: NewColumnIn[] };
  /** Template-card source (matrix-tab loaded space doc_types; only column-group template kinds become cards). */
  docTypes?: KbDocTypeDTO[];
  /** Step 1 S8: only-me matrices are enabled (`/me` `capabilities.kb_matrix_private`); offers "谁能看到". */
  privateOption?: boolean;
  onCancel: () => void;
  onCreated: (matrixId: string) => void;
  onError: (message: string) => void;
}

export function NewMatrixFlow({
  workspaceId,
  preselectedFileIds,
  preset,
  docTypes,
  privateOption = false,
  onCancel,
  onCreated,
  onError,
}: NewMatrixFlowProps) {
  const t = useTranslations("kbMatrix");
  const skipFiles = !!preselectedFileIds?.length;
  const [step, setStep] = useState<1 | 2>(1);
  // review P1 second safeguard: preset may arrive late (matrix-tab already gates loading, but SPA
  // stale docTypes paths may transition undefined→defined); derive effective step: preset
  // always means step 2, never dead state step=1 with showStep1=false.
  const effStep: 1 | 2 = preset ? 2 : step;
  const [sel, setSel] = useState<string>("blank"); // doc_type_id | "blank"
  const [name, setName] = useState("");
  const [files, setFiles] = useState<FileOption[]>([]);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [loadingFiles, setLoadingFiles] = useState(false);
  const [creating, setCreating] = useState(false);
  const [visibility, setVisibility] = useState<MatrixVisibility>("space");
  // The viewer's personal files: loaded once "仅自己" is chosen (null until then).
  const [personalFiles, setPersonalFiles] = useState<FileOption[] | null>(null);
  const [personalFailed, setPersonalFailed] = useState(false);
  const isPrivate = privateOption && visibility === "private";

  // M4: column-group cards (seeds first in seed order; user templates by updated_at descending).
  const templates = useMemo(() => {
    const all = (docTypes ?? []).filter((d) => isColumnGroupKind(d.kind));
    const seeds = all.filter((d) => d.kind === "seed");
    const mine = all
      .filter((d) => d.kind !== "seed")
      .sort((a, b) => (b.updated_at ?? "").localeCompare(a.updated_at ?? ""));
    return [...seeds, ...mine];
  }, [docTypes]);

  const selDt = sel === "blank" ? null : (templates.find((d) => d.doc_type_id === sel) ?? null);
  const columns: NewColumnIn[] = preset ? preset.columns : selDt ? fieldsToColumns(selDt.fields) : [];
  const defaultName = preset ? preset.name : (selDt?.name ?? t("blank_matrix_name"));

  // review P2: hold onError in ref; matrix-tab passes inline closure (new identity each parent render),
  // so dependency inclusion loops fetch failure→Toast→parent render→effect rerun, endless refetch/Toast storm.
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;
  const [filesFailed, setFilesFailed] = useState(false);

  useEffect(() => {
    if (effStep !== 2 || skipFiles || files.length > 0 || filesFailed) return;
    setLoadingFiles(true);
    (async () => {
      try {
        const out: FileOption[] = [];
        let offset = 0;
        for (;;) {
          const res = await fetchKbFiles({ scope: "space", offset, limit: 200 });
          for (const f of res.files) if (f.parse_status === "done") out.push({ fileId: f.file_id, filename: f.filename, mime: f.mime_type });
          offset += res.files.length;
          if (!res.has_more || res.files.length === 0) break;
        }
        setFiles(out);
      } catch (e) {
        setFilesFailed(true); // Mark failure → manual retry, never dependency-driven self-healing.
        onErrorRef.current(e instanceof Error ? e.message : String(e));
      } finally {
        setLoadingFiles(false);
      }
    })();
  }, [effStep, skipFiles, files.length, filesFailed]);

  useEffect(() => {
    if (!isPrivate || effStep !== 2 || skipFiles || personalFiles !== null || personalFailed) return;
    let cancelled = false;
    (async () => {
      try {
        const out: FileOption[] = [];
        let offset = 0;
        for (;;) {
          const res = await fetchKbFiles({ scope: "personal", offset, limit: 200 });
          for (const f of res.files) if (f.parse_status === "done") out.push({ fileId: f.file_id, filename: f.filename, mime: f.mime_type });
          offset += res.files.length;
          if (!res.has_more || res.files.length === 0) break;
        }
        if (!cancelled) setPersonalFiles(out);
      } catch (e) {
        if (cancelled) return;
        setPersonalFailed(true);
        onErrorRef.current(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isPrivate, effStep, skipFiles, personalFiles, personalFailed]);

  const chooseVisibility = (next: MatrixVisibility) => {
    setVisibility(next);
    // Personal files only go into only-me matrices: switching back unchecks them.
    if (next === "space" && personalFiles?.length) {
      const personal = new Set(personalFiles.map((f) => f.fileId));
      setChecked((cur) => new Set([...cur].filter((id) => !personal.has(id))));
    }
  };

  const fileIds = useMemo(
    () => (skipFiles ? (preselectedFileIds ?? []) : Array.from(checked)),
    [skipFiles, preselectedFileIds, checked],
  );

  const create = async () => {
    setCreating(true);
    try {
      const res = await createMatrix(workspaceId, name.trim() || defaultName, fileIds, columns, privateOption ? visibility : undefined);
      onCreated(res.matrix_id);
    } catch (e) {
      const message = kbMatrixMutationError(e);
      onError(message.values ? t(message.key, message.values) : t(message.key));
      setCreating(false);
    }
  };

  // Preset handoff hides step 1 (columns fixed); with skipFiles too, show create footer directly.
  const showStep1 = !preset;

  // S8: shown once, in the step that holds the create button (the file step when files are chosen here).
  const visibilityControl = privateOption ? (
    <fieldset className="mx-visibility" data-testid="kb-matrix-newflow-visibility">
      <legend className="mx-visibility-legend">{t("newflow_visibility")}</legend>
      {(["space", "private"] as const).map((value) => (
        <label key={value} className={"mx-visopt" + (visibility === value ? " on" : "")}>
          <input type="radio" name="kb-matrix-visibility" value={value} checked={visibility === value} onChange={() => chooseVisibility(value)} />
          <span className="mx-visopt-copy">
            <span className="t">{t(value === "space" ? "newflow_visibility_space" : "newflow_visibility_private")}</span>
            <span className="d">{t(value === "space" ? "newflow_visibility_space_desc" : "newflow_visibility_private_desc")}</span>
          </span>
        </label>
      ))}
    </fieldset>
  ) : null;

  const fileRow = (f: FileOption) => (
    <label key={f.fileId} className="mx-filepick-row">
      <input
        type="checkbox"
        checked={checked.has(f.fileId)}
        onChange={(e) => {
          const next = new Set(checked);
          if (e.target.checked) next.add(f.fileId);
          else next.delete(f.fileId);
          setChecked(next);
        }}
      />
      {/* The icon follows the file type (a PDF used to show the table icon). */}
      <LucideGlyph icon={fileTypeIcon(f.filename, f.mime)} size={14} aria-hidden="true" style={{ color: "var(--c-ink-3)" }} />
      <span className="fn">{f.filename}</span>
    </label>
  );

  return (
    <div data-testid="kb-matrix-newflow" style={{ background: "var(--c-bg)" }}>
      {(showStep1 || !skipFiles) && (
        <div className="mx-stepper">
          {showStep1 && (
            <span className={"mx-step" + (effStep === 1 ? " on" : "")}>
              <span className="n">1</span>
              {t("newflow_step_template")}
            </span>
          )}
          {!skipFiles && (
            <>
              {showStep1 && <span className="line" />}
              <span className={"mx-step" + (effStep === 2 ? " on" : "")}>
                <span className="n">{showStep1 ? 2 : 1}</span>
                {t("newflow_step_files")}
              </span>
            </>
          )}
        </div>
      )}

      {effStep === 1 && showStep1 && (
        <>
          <div className="mx-tplgrid">
            {templates.map((dt) => {
              const k = dt.kind ?? "manual";
              return (
                <div
                  key={dt.doc_type_id}
                  className={"mx-tplcard" + (sel === dt.doc_type_id ? " on" : "")}
                  role="button"
                  tabIndex={0}
                  data-testid={`kb-matrix-newflow-tpl-${dt.doc_type_id}`}
                  onClick={() => setSel(dt.doc_type_id)}
                  onKeyDown={(e) => e.key === "Enter" && setSel(dt.doc_type_id)}
                >
                  <div className="tophead">
                    <span className="t">{dt.name}</span>
                    <span className={"kb-kind " + k}>{t(`kind_${k}` as never)}</span>
                  </div>
                  <div className="d">{dt.description}</div>
                  <div className="mx-chips">
                    {dt.fields.map((f) => (
                      <span className="chip" key={f.name}>
                        {f.name}
                      </span>
                    ))}
                  </div>
                </div>
              );
            })}
            <div
              className={"mx-tplcard blank" + (sel === "blank" ? " on" : "")}
              role="button"
              tabIndex={0}
              onClick={() => setSel("blank")}
              onKeyDown={(e) => e.key === "Enter" && setSel("blank")}
            >
              <div className="ic"><IcPlus s={16} /></div>
              <div className="t" style={{ marginBottom: 5 }}>{t("blank_matrix")}</div>
              <div className="d" style={{ minHeight: 0, textAlign: "center" }}>{t("blank_matrix_desc")}</div>
            </div>
          </div>
          {skipFiles && visibilityControl}
          <div className="mx-newfoot">
            <input
              className="mx-inp"
              style={{ maxWidth: 320 }}
              value={name}
              placeholder={defaultName}
              onChange={(e) => setName(e.target.value)}
            />
            <span className="grow" />
            <button type="button" className="kb-btn kb-btn-ghost" onClick={onCancel}>
              {t("cancel")}
            </button>
            {skipFiles ? (
              <button type="button" className="kb-btn kb-btn-primary" disabled={creating} onClick={create}>
                {t("newflow_create", { n: fileIds.length })}
              </button>
            ) : (
              <button type="button" className="kb-btn kb-btn-primary" onClick={() => setStep(2)}>
                {t("newflow_next")}
              </button>
            )}
          </div>
        </>
      )}

      {effStep === 2 && !skipFiles && (
        <>
          {visibilityControl}
          <div className="mx-filepick">
            {loadingFiles ? (
              <div style={{ padding: 24, textAlign: "center", color: "var(--c-ink-3)" }}>{t("loading")}</div>
            ) : filesFailed ? (
              <div style={{ padding: 24, textAlign: "center", color: "var(--c-ink-3)" }}>
                <button type="button" className="kb-btn kb-btn-secondary" onClick={() => setFilesFailed(false)}>
                  {t("matrix_unavailable_retry")}
                </button>
              </div>
            ) : isPrivate ? (
              // Only me: this Space's files, then the viewer's personal files.
              <>
                <div className="mx-filepick-group">{t("add_files_group_space")}</div>
                {files.length === 0 ? <div className="mx-filepick-empty">{t("newflow_no_files")}</div> : files.map(fileRow)}
                <div className="mx-filepick-group">{t("add_files_group_personal")}</div>
                {personalFiles === null && !personalFailed ? (
                  <div className="mx-filepick-empty">{t("loading")}</div>
                ) : personalFailed ? (
                  <div className="mx-filepick-empty">
                    <button type="button" className="kb-btn kb-btn-secondary" onClick={() => setPersonalFailed(false)}>
                      {t("matrix_unavailable_retry")}
                    </button>
                  </div>
                ) : personalFiles?.length ? (
                  personalFiles.map(fileRow)
                ) : (
                  <div className="mx-filepick-empty">{t("newflow_no_files")}</div>
                )}
              </>
            ) : files.length === 0 ? (
              <div style={{ padding: 24, textAlign: "center", color: "var(--c-ink-3)" }}>{t("newflow_no_files")}</div>
            ) : (
              files.map(fileRow)
            )}
          </div>
          <div className="mx-newfoot">
            {/* Preset lacks step 1; name input belongs here. */}
            {preset && (
              <input
                className="mx-inp"
                style={{ maxWidth: 320 }}
                value={name}
                placeholder={defaultName}
                onChange={(e) => setName(e.target.value)}
              />
            )}
            <span style={{ fontSize: "0.78125rem", color: "var(--c-ink-3)" }}>{t("newflow_selected", { n: checked.size })}</span>
            <span className="grow" />
            {preset ? (
              <button type="button" className="kb-btn kb-btn-ghost" onClick={onCancel}>
                {t("cancel")}
              </button>
            ) : (
              <button type="button" className="kb-btn kb-btn-ghost" onClick={() => setStep(1)}>
                {t("back_step")}
              </button>
            )}
            <button type="button" className="kb-btn kb-btn-primary" disabled={creating || checked.size === 0} onClick={create}>
              {t("newflow_create", { n: checked.size })}
            </button>
          </div>
        </>
      )}

      {/* Preset + Open as matrix dual prefill: zero steps, create directly. */}
      {preset && skipFiles && visibilityControl}
      {preset && skipFiles && (
        <div className="mx-newfoot">
          <input
            className="mx-inp"
            style={{ maxWidth: 320 }}
            value={name}
            placeholder={defaultName}
            onChange={(e) => setName(e.target.value)}
          />
          <span className="grow" />
          <button type="button" className="kb-btn kb-btn-ghost" onClick={onCancel}>
            {t("cancel")}
          </button>
          <button type="button" className="kb-btn kb-btn-primary" disabled={creating} onClick={create}>
            {t("newflow_create", { n: fileIds.length })}
          </button>
        </div>
      )}
    </div>
  );
}
