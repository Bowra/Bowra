"use client";

import { appPath } from "@/lib/base-path";

/**
 * V7 KB · Upload Modal — scope cards + drag/drop or pick file + use_llm toggle.
 *
 * KB experience remediation P1 (design “04 上传弹窗”, “06 窄屏 390”, FR5.1/5.2/5.4):
 * - Scope cards: “空间” gives space name, “团队” locked for nonadmins with “仅团队管理员”;
 *   before identity arrives, team card placeholder and submission disabled (unknown never shown as denied).
 * - Sole supported-format source: lib/kb-upload-formats.ts (accept/filter/hints/errors share it).
 * - Three failure classes: unsupported type (blocked on selection, filename shown), transfer interrupted (by transfer progress,
 *   see upload-xhr.watchUploadTransfer), ordinary network failure.
 * - ≤640px bottom sheet (styles/kb-upload-modal.css).
 */

import { type KeyboardEvent, type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import "@/styles/kb-upload-modal.css";

import { DialogShell } from "@/components/ui/dialog";
import { KbIngestionNotice } from "@/components/knowledge/kb-ingestion-notice";

import { ApiError } from "@/lib/api-client";
import { useOptionalAuthSession } from "@/lib/auth/auth-session-context";
import { googlePickerErrorKey, pickDriveFiles, prepareGooglePicker } from "@/lib/google-picker";
import {
  type KbBundleUploadResponse,
  type KbFileDTO,
  type KbGoogleImportResult,
  type KbParseStatus,
  type KbScope,
  formatBytes,
  getKbFile,
  importKbFromGoogleDrive,
  isParseInProgress,
  reparseKbFile,
  uploadKbBundle,
  uploadKbFile,
} from "@/lib/kb-api";
import { fetchKbCapabilities, type KbCapabilityState } from "@/lib/kb-capabilities-api";
import { useKbIngestionReadiness } from "@/lib/kb-ingestion-readiness";
import {
  type KbDocTypeDTO as DocTypeDTO,
  listKnowledgeTables as listDocTypes,
} from "@/lib/kb-knowledge-tables-api";
import { type KbFileErrorKey, kbFileError } from "@/lib/kb-file-error";
import {
  KB_ACCEPTED_EXT,
  KB_MAX_UPLOAD_BYTES,
  KB_UPLOAD_FORMATS,
  isKbAcceptedFileName,
} from "@/lib/kb-upload-formats";
import { useSpaceSelection } from "@/lib/use-space";

import { typeMeta } from "./kb-file-types";
import { IconCheckBig, IconRetry, IconSpin, IconX } from "./kb-icons";
import { useKbMeState } from "./use-kb-me";


interface Props {
  defaultScope: KbScope;
  onClose: () => void;
  onUploaded: (file: KbFileDTO) => void;
}

type RejectReason = "type" | "empty" | "too_large";

const MAX_MB = (KB_MAX_UPLOAD_BYTES / 1024 / 1024).toFixed(0);


/** Files one Drive import accepts (matches the backend's per-request limit). */
const DRIVE_IMPORT_LIMIT = 20;

export function KbUploadModal({ defaultScope, onClose, onUploaded }: Props) {
  const t = useTranslations("knowledge_base.upload_modal");
  const tKb = useTranslations("knowledge_base");
  const tErr = useTranslations("knowledge_base.errors");
  const tDrive = useTranslations("google_drive");
  const googleDriveAvailable = useOptionalAuthSession()?.me?.capabilities?.google_workspace === true;
  useEffect(() => {
    // Load Google's scripts before the click so the token popup is not blocked.
    if (googleDriveAvailable) void prepareGooglePicker().catch(() => undefined);
  }, [googleDriveAvailable]);
  // Google Drive import: per-file results once the import request returns (no re-import later).
  const [driveResults, setDriveResults] = useState<KbGoogleImportResult[] | null>(null);
  const [driveBusy, setDriveBusy] = useState(false);
  // More files were picked than one import accepts; only the first DRIVE_IMPORT_LIMIT are imported.
  const [driveSkipped, setDriveSkipped] = useState(0);

  const [file, setFile] = useState<File | null>(null);
  // File rejected at selection/drop: preserve filename for error (FR5.4), keep dialog open.
  const [rejected, setRejected] = useState<{ file: File; reason: RejectReason } | null>(null);
  const [scope, setScope] = useState<KbScope>(defaultScope);
  const [useLlm, setUseLlm] = useState(false);
  // credential-readiness R9: upload entry warns if visual parsing ineffective (null = unknown/unavailable, no hint).
  const [kbVision, setKbVision] = useState<KbCapabilityState | null>(null);
  const readiness = useKbIngestionReadiness();
  const { me, status: identityStatus, retry: retryIdentity } = useKbMeState();
  const { items: spaces, loading: spacesLoading } = useSpaceSelection();
  // Upload failure after sending starts: preserve selected scope/file, “重试” resends unchanged.
  const [uploadError, setUploadError] = useState<KbFileErrorKey | null>(null);
  // Presend blocking (missing identity, etc.) is not individual file failure.
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  // #17 (dingsiyan 2026-07-07): single-file upload progress; null = idle, 0..100 = up.
  const [uploadPct, setUploadPct] = useState<number | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  // V3 document-type selection (xlsx files may bind doc_type for custom splitting strategy).
  const [docTypes, setDocTypes] = useState<DocTypeDTO[]>([]);
  const [docTypeId, setDocTypeId] = useState<string>("");

  useEffect(() => {
    let cancelled = false;
    fetchKbCapabilities().then((caps) => {
      if (!cancelled && caps) setKbVision(caps.kb_vision);
    });
    return () => { cancelled = true; };
  }, []);

  // Closing/unmounting dialog aborts in-flight upload.
  useEffect(() => () => abortRef.current?.abort(), []);

  // Fetch existing space doc_types (silent failure; unconfigured also fine).
  useEffect(() => {
    if (!me?.workspace_id) return;
    listDocTypes(me.workspace_id)
      .then(setDocTypes)
      .catch(() => setDocTypes([]));
  }, [me?.workspace_id]);

  // Identity not returned (me === null) differs from nonadmin: no permission decision yet, submission temporarily unavailable.
  const identityLoading = identityStatus === "loading";
  const teamLocked = me !== null && !me.is_team_admin;
  // Never conclude no space while space list still loading.
  const noSpace = me !== null && !me.workspace_id && !spacesLoading;
  const spaceName = me?.workspace_id
    ? spaces.find((w) => w.id === me.workspace_id)?.name ?? ""
    : "";

  // Unavailable default scope (team for nonadmin, space with none) falls back to “个人”.
  useEffect(() => {
    if ((scope === "team" && teamLocked) || (scope === "space" && noSpace)) {
      setScope("personal");
    }
  }, [scope, teamLocked, noSpace]);

  const formatList = KB_UPLOAD_FORMATS.map((f) => t(`format.${f.id}`)).join(t("format_sep"));

  const scopeLabel = (s: KbScope): string => {
    if (s === "personal") return t("scope_card_personal");
    if (s === "team") return t("scope_card_team");
    return spaceName ? t("scope_card_space_named", { name: spaceName }) : t("scope_card_space");
  };

  const rejectReason = (f: File): RejectReason | null => {
    if (!isKbAcceptedFileName(f.name)) return "type";
    if (f.size === 0) return "empty";
    if (f.size > KB_MAX_UPLOAD_BYTES) return "too_large";
    return null;
  };
  const isZip = (f?: File | null) =>
    !!f && (f.name.toLowerCase().endsWith(".zip"));

  const onPickFile = (f: File | null | undefined) => {
    if (!f) return;
    const reason = rejectReason(f);
    setUploadError(null);
    setError(null);
    if (reason) {
      setRejected({ file: f, reason });
      setFile(null);
      return;
    }
    setRejected(null);
    setFile(f);
  };

  const isPdf = file?.name.toLowerCase().endsWith(".pdf") ?? false;
  const isXlsx = (file?.name.toLowerCase().endsWith(".xlsx") ||
    file?.name.toLowerCase().endsWith(".xls")) ?? false;

  const computeScopeId = useCallback((): string => {
    if (!me) return "";
    if (scope === "personal") return me.user_id;
    if (scope === "space") return me.workspace_id;
    return me.team_id;
  }, [me, scope]);

  const [bundleSummary, setBundleSummary] = useState<KbBundleUploadResponse | null>(null);
  // M3: per-file parse status for the bundle result list (polled until terminal).
  const [bundleStatus, setBundleStatus] = useState<Record<string, KbParseStatus>>({});

  // M3: poll uploaded bundle files whose parse hasn't settled (done/failed).
  useEffect(() => {
    if (!bundleSummary) return;
    const pending = bundleSummary.uploaded
      .map((f) => f.file_id)
      .filter((id) => { const s = bundleStatus[id]; return s !== "done" && s !== "failed"; });
    if (pending.length === 0) return;
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      const updates = await Promise.all(
        pending.map((id) => getKbFile(id).then((d) => [id, d.parse_status] as const).catch(() => null)),
      );
      if (cancelled) return;
      setBundleStatus((prev) => {
        const next = { ...prev };
        for (const u of updates) if (u) next[u[0]] = u[1];
        return next;
      });
    }, 3000);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [bundleSummary, bundleStatus]);

  const retryBundleFile = useCallback(async (fileId: string) => {
    setBundleStatus((p) => ({ ...p, [fileId]: "queued" }));
    try {
      const dto = await reparseKbFile(fileId);
      setBundleStatus((p) => ({ ...p, [fileId]: dto.parse_status }));
    } catch {
      setBundleStatus((p) => ({ ...p, [fileId]: "failed" }));
    }
  }, []);

  const doUpload = useCallback(async () => {
    if (!file || readiness.state !== "ready") return;
    if (!me) {
      setError(tErr("auth_missing"));
      return;
    }
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setBusy(true);
    setError(null);
    setUploadError(null);
    setUploadPct(0);
    try {
      // 0.24.4: .zip → bundle endpoint (server unzip + per-file ingest)
      // 0.24.5 fix: never immediately invoke onUploaded; parent closes dialog ×N times, preventing
      // user from seeing summary. Show summary, dispatch only after Done clicked.
      if (isZip(file)) {
        const summary = await uploadKbBundle(file, {
          scope,
          scope_id: computeScopeId(),
          use_llm: useLlm,
          onProgress: (pct) => setUploadPct(Math.round(pct)),
          signal: ctrl.signal,
        });
        setBundleSummary(summary);
        setBundleStatus(Object.fromEntries(summary.uploaded.map((f) => [f.file_id, f.parse_status])));
        return;
      }
      const dto = await uploadKbFile(file, {
        scope,
        scope_id: computeScopeId(),
        use_llm: useLlm,
        // Send doc_type_id only for xlsx/xls (no binding for other types).
        doc_type_id: isXlsx && docTypeId ? docTypeId : null,
        onProgress: (pct) => setUploadPct(Math.round(pct)),
        signal: ctrl.signal,
      });
      onUploaded(dto);
      onClose();
    } catch (e) {
      // User clicked “取消上传”: return to selection, not failure.
      if (!ctrl.signal.aborted) setUploadError(kbFileError(e, "upload"));
    } finally {
      if (abortRef.current === ctrl) abortRef.current = null;
      setBusy(false);
      setUploadPct(null);
    }
  }, [file, me, scope, useLlm, computeScopeId, onUploaded, onClose, tErr, isXlsx, docTypeId, readiness.state]);

  const cancelUpload = () => abortRef.current?.abort();

  const scopeBlocked =
    busy || driveBusy || me === null || readiness.state !== "ready" ||
    (scope === "team" && teamLocked) ||
    (scope === "space" && !me?.workspace_id);
  const submitBlocked = !file || scopeBlocked;

  const importFromDrive = useCallback(async () => {
    if (readiness.state !== "ready") return;
    if (!me) {
      setError(tErr("auth_missing"));
      return;
    }
    setError(null);
    let picked: Awaited<ReturnType<typeof pickDriveFiles>>;
    try {
      picked = await pickDriveFiles();
    } catch (e) {
      setError(tDrive(googlePickerErrorKey(e)));
      return;
    }
    if (picked.length === 0) return;
    setDriveSkipped(Math.max(0, picked.length - DRIVE_IMPORT_LIMIT));
    setDriveBusy(true);
    try {
      setDriveResults(await importKbFromGoogleDrive(picked.slice(0, DRIVE_IMPORT_LIMIT).map((f) => f.id), { scope, scope_id: computeScopeId() }));
    } catch (e) {
      const code = e instanceof ApiError ? e.code : undefined;
      setError(code === "google_not_connected" || code === "google_reauth_required" ? tDrive("not_connected") : tDrive("load_failed"));
    } finally {
      setDriveBusy(false);
    }
  }, [me, scope, computeScopeId, tErr, tDrive, readiness.state]);

  const finishDriveImport = useCallback(async () => {
    const imported = (driveResults ?? []).flatMap((r) => (r.ok && r.kb_file_id ? [r.kb_file_id] : []));
    const dtos = await Promise.all(imported.map((id) => getKbFile(id).catch(() => null)));
    dtos.forEach((dto) => {
      if (dto) onUploaded(dto);
    });
    onClose();
  }, [driveResults, onUploaded, onClose]);

  // Views: Drive result / zip result / uploading / upload failed / rejected type / selection.
  const view: "drive" | "bundle" | "uploading" | "failed" | "rejected" | "select" =
    driveResults ? "drive"
      : bundleSummary ? "bundle"
      : busy ? "uploading"
        : uploadError && file ? "failed"
          : rejected ? "rejected"
            : "select";
  const bundleProcessing = bundleSummary?.uploaded.some((f) => {
    const status = bundleStatus[f.file_id] ?? f.parse_status;
    return status !== "done" && status !== "failed";
  }) ?? false;


  const openPicker = () => inputRef.current?.click();

  const dropZone = (repick: boolean) => (
    <div
      role="button"
      tabIndex={0}
      className={`drop${dragActive ? " active" : ""}`}
      onDragOver={(e) => {
        e.preventDefault();
        setDragActive(true);
      }}
      onDragLeave={() => setDragActive(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragActive(false);
        if (e.dataTransfer.files.length > 1) {
          setRejected(null);
          setFile(null);
          setError(t("drop_multiple", { n: e.dataTransfer.files.length }));
          return;
        }
        onPickFile(e.dataTransfer.files?.[0]);
      }}
      onClick={openPicker}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openPicker();
        }
      }}
      data-testid="kb-drop-zone"
    >
      <b>{repick ? t("drop_repick") : t("drop_title")}</b>
      {!repick && (
        <div className="fmt">{t("drop_formats", { formats: formatList, mb: MAX_MB })}</div>
      )}
    </div>
  );

  const fileCard = (f: File, opts: { bad?: boolean; state?: string }) => {
    const meta = typeMeta(f.name);
    return (
      <div className={`filecard${opts.bad ? " bad" : ""}`} data-testid="kb-upload-filecard">
        <span className="kb-ftype" aria-hidden style={{ background: meta.color }}>{meta.tag}</span>
        <div className="kb-up-filecard-text">
          <div className="nm">{f.name}</div>
          <div className="mt">{formatBytes(f.size)}</div>
        </div>
        {opts.state && <span className="st">{opts.state}</span>}
      </div>
    );
  };

  return (
    // Esc/backdrop closes; busy upload ignores both to prevent accidental interruption of long upload (cancel via “取消上传”).
    <DialogShell
      presentation="center"
      onClose={onClose}
      busy={busy}
      labelledBy="kb-upload-title"
      backdropClassName="kb-upload-overlay"
      panelClassName="md kb-upload"
      panelProps={{ "data-testid": "kb-upload-modal" } as Record<string, string>
}
    >
      <div className="grab" aria-hidden="true" />
      <div className="md-hd">
        <div className="kb-up-head-text">
          <h3 id="kb-upload-title">{t("title")}</h3>
          {view !== "select" && view !== "bundle" && <div className="sub" data-testid="kb-upload-subtitle">{t("uploading_to", { scope: scopeLabel(scope) })}</div>}
        </div>
        <button type="button" className="kb-x" onClick={onClose} disabled={busy} aria-label={t("close_aria")}>
          <IconX size={12} />
        </button>
      </div>

      <div className="md-bd">
      {identityStatus === "failed" ? <div role="alert" className="alert err" data-testid="kb-identity-error">
        {t("identity_failed")}
        <button type="button" onClick={retryIdentity}>{t("identity_retry")}</button>
      </div> : null}
        {view === "select" && (
          <>
            <KbIngestionNotice state={readiness.state} canConfigure={me?.is_team_admin === true} onRetry={readiness.retry} />
            <div className="scopehd" id="kb-upload-scope-title">{t("scope_title")}</div>
            <div role="radiogroup" aria-labelledby="kb-upload-scope-title" onKeyDown={selectEdgeScope} className="kb-up-scope-list">
              <ScopeCard
                value="personal" current={scope} onChange={setScope}
                title={t("scope_card_personal")}
                desc={t("scope_desc_personal")}
              />
              <ScopeCard
                value="space" current={scope} onChange={setScope}
                title={scopeLabel("space")}
                desc={noSpace ? (
                  <>
                    {t("no_space")}
                    <a href={appPath("/empty-workspace/")} data-testid="kb-upload-join-space">{t("no_space_link")}</a>
                  </>
                ) : t("scope_desc_space")}
                disabled={noSpace}
              />
              <ScopeCard
                value="team" current={scope} onChange={setScope}
                title={t("scope_card_team")}
                desc={identityLoading ? t("scope_desc_team_loading") : t("scope_desc_team")}
                disabled={me === null || teamLocked}
                loading={identityLoading}
                trailing={identityLoading ? (
                  <span
                    className="lockskel"
                    role="status"
                    aria-label={t("scope_identity_loading")}
                    data-testid="kb-scope-team-skeleton"
                  />
                ) : teamLocked ? (
                  <span className="lock" data-testid="kb-scope-team-lock">{t("scope_team_lock")}</span>
                ) : null}
              />
            </div>

            {file && fileCard(file, {})}
            {dropZone(file !== null)}
            {googleDriveAvailable && !file ? (
              <div className="kb-up-drive" data-testid="kb-google-drive-import">
                <span>{tDrive("kb_import_or")}</span>
                <button type="button" className="kb-btn kb-btn-secondary" onClick={() => void importFromDrive()} disabled={scopeBlocked}>
                  {driveBusy ? tDrive("importing") : tDrive("kb_import_entry")}
                </button>
              </div>
            ) : null}

            {/* credential-readiness R9 (PRD §6.4, highest-priority contextual hint):
                ineffective visual parsing silently skips scans/image PDFs; disclose clearly at upload entry. */}
            {kbVision !== null && kbVision !== "enabled" && (
              <div data-testid="kb-vision-degraded-hint" className="kb-up-vision-hint">
                {kbVision === "invalid" ? t("vision_hint_invalid") : t("vision_hint_disabled")}
              </div>
            )}

            {/* Document-type selection (xlsx/xls only): V3 doc_type → chunk_strategy. */}
            {isXlsx && docTypes.length > 0 && (
              <label className="kb-up-template">
                <div className="kb-up-template-label">
                  {t("template_label")} <span className="kb-up-optional">{t("optional")}</span>
                </div>
                <select
                  value={docTypeId}
                  onChange={(e) => setDocTypeId(e.target.value)}
                  data-testid="kb-doc-type-select"
                  className="ui-input legacy-input-kb-upload-template"
                >
                  <option value="">{t("no_doctype")}</option>
                  {docTypes.map((dt) => (
                    <option key={dt.doc_type_id} value={dt.doc_type_id}>
                      {t("doctype_option", { name: dt.name, n: dt.fields.length })}
                    </option>
                  ))}
                </select>
                <small className="kb-up-template-hint">
                  {t("template_hint_prefix")}<a href={appPath("/knowledge/?tab=templates")} className="kb-up-link">{t("manage_link")}</a>
                </small>
              </label>
            )}

            {/* use_llm toggle (PDF only) */}
            {isPdf && (
              <label className="kb-up-llm">
                <input
                  type="checkbox"
                  checked={useLlm}
                  onChange={(e) => setUseLlm(e.target.checked)}
                  data-testid="kb-use-llm"
                />
                <span>
                  {t("use_llm_label")}
                  <small className="kb-up-llm-hint">
                    {t("use_llm_hint")}
                  </small>
                </span>
              </label>
            )}

            {error && (
              <div role="alert" className="alert err" data-testid="kb-upload-error">{error}</div>
            )}
          </>
        )}

        {view === "rejected" && rejected && (
          <>
            {fileCard(rejected.file, {
              bad: true,
              state: rejected.reason === "type"
                ? t("file_unsupported")
                : rejected.reason === "empty"
                  ? tErr("empty_file")
                  : tErr("too_large", { mb: MAX_MB }),
            })}
            <div role="alert" className="alert err" data-testid="kb-upload-error">
              {rejected.reason === "type" ? (
                <><b>{t("unsupported_title", { name: rejected.file.name })}</b> {t("unsupported_body", { formats: formatList })}</>
              ) : rejected.reason === "empty" ? (
                <><b>{t("empty_title")}</b> {t("empty_body")}</>
              ) : (
                <><b>{t("too_large_title", { mb: MAX_MB })}</b> {t("too_large_body")}</>
              )}
            </div>
            {dropZone(true)}
          </>
        )}

        {view === "uploading" && file && (
          <>
            {fileCard(file, { state: `${uploadPct ?? 0}%` })}
            <div
              className="bar t"
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={uploadPct ?? 0}
              data-testid="kb-upload-progress"
            >
              <span style={{ width: `${uploadPct ?? 0}%` }} />
            </div>
            <div className="alert info">{t("uploading_note")}</div>
          </>
        )}

        {view === "failed" && file && uploadError && (
          <>
            {fileCard(file, { bad: true, state: t("file_not_uploaded") })}
            <div role="alert" className="alert err" data-testid="kb-upload-error">
              {uploadError === "errors.upload_cut_off" ? (
                <><b>{t("cut_off_title")}</b> {t("cut_off_body")}</>
              ) : tKb(uploadError)}
            </div>
          </>
        )}

        {view === "drive" && driveResults && (
          <div data-testid="kb-google-drive-results">
            <div className="scopehd">{tDrive("import_title")}</div>
            {driveSkipped > 0 ? (
              <div role="status" className="alert">{tDrive("import_limit", { limit: DRIVE_IMPORT_LIMIT, skipped: driveSkipped })}</div>
            ) : null}
            {driveResults.every((r) => !r.ok) ? (
              <div role="alert" className="alert err">{tDrive("import_failed_all")}</div>
            ) : null}
            <ul className="kb-up-drive-results">
              {driveResults.map((r) => (
                <li key={r.file_id} className={r.ok ? "ok" : "bad"}>
                  <span className="nm">{r.name ?? r.file_id}</span>
                  <span className="st">
                    {r.ok ? tDrive("import_ok") : tDrive(driveErrorKey(r.error))}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {view === "bundle" && bundleSummary && (
          <BundleResult
            summary={bundleSummary}
            status={bundleStatus}
            onRetry={retryBundleFile}
          />
        )}
      </div>

      <div className="md-ft">
        {view === "drive" ? (
          <button type="button" onClick={() => void finishDriveImport()} className="kb-btn kb-btn-primary" data-testid="kb-google-drive-done">
            {tDrive("import_done")}
          </button>
        ) : view === "bundle" && bundleSummary ? (
          <>
            {bundleProcessing && <span className="kb-bundle-leave-note">{t("bundle_continue_in_list")}</span>}
            <button
              type="button"
              onClick={() => {
                bundleSummary.uploaded.forEach(onUploaded);
                onClose();
              }}
              className="kb-btn kb-btn-primary"
              data-testid="kb-bundle-done"
            >
              {bundleProcessing ? t("bundle_view_list") : t("bundle_done", { n: bundleSummary.uploaded.length })}
            </button>
          </>
        ) : view === "uploading" ? (
          <>
            <button type="button" onClick={cancelUpload} className="kb-btn kb-btn-secondary" data-testid="kb-upload-cancel">
              {t("cancel_upload")}
            </button>
            <button type="button" disabled className="kb-btn kb-btn-primary" data-testid="kb-upload-submit">
              {t("submitting")}
            </button>
          </>
        ) : view === "failed" ? (
          <>
            <button type="button" onClick={onClose} className="kb-btn kb-btn-secondary">
              {t("cancel")}
            </button>
            <button type="button" onClick={doUpload} className="kb-btn kb-btn-primary" data-testid="kb-upload-retry">
              <IconRetry /> {t("retry")}
            </button>
          </>
        ) : (
          <>
            <button type="button" onClick={onClose} className="kb-btn kb-btn-secondary">
              {t("cancel")}
            </button>
            <button
              type="button"
              onClick={doUpload}
              disabled={submitBlocked}
              className="kb-btn kb-btn-primary"
              data-testid="kb-upload-submit"
            >
              {t("submit")}
            </button>
          </>
        )}
      </div>
      <input
        ref={inputRef}
        type="file"
        accept={KB_ACCEPTED_EXT}
        style={{ display: "none" }}
        // Excluded from DialogShell Tab loop: final input in panel, but unfocusable.
        aria-hidden="true"
        onChange={(e) => {
          onPickFile(e.target.files?.[0]);
          // Allow selecting same file again (rename/export after rejection).
          e.target.value = "";
        }}
        data-testid="kb-file-input"
      />
    </DialogShell>
  );
}


const DRIVE_ERROR_KEYS = new Set([
  "google_export_too_large",
  "google_file_not_granted",
  "google_file_not_found",
  "google_reauth_required",
  "google_unreachable",
  "kb_file_too_large",
  "kb_file_type_unsupported",
]);

/** Message key under `google_drive` for one file's import failure. */
function driveErrorKey(code: string | undefined): `error.${string}` {
  return code && DRIVE_ERROR_KEYS.has(code) ? `error.${code}` : "error.other";
}

/** M3 — bundle result list: per-file status (ingested/parsing/complete/failed) + skip + retry. */
function BundleResult({
  summary, status, onRetry,
}: {
  summary: KbBundleUploadResponse;
  status: Record<string, KbParseStatus>;
  onRetry: (fileId: string) => void;
}) {
  const t = useTranslations("knowledge_base.upload_modal");
  const skippedReasonKey: Readonly<Record<string, string>> = {
    kb_bundle_entry_metadata: "bundle_skip_reason.metadata",
    kb_bundle_entry_empty: "bundle_skip_reason.empty",
    kb_bundle_entry_unreadable: "bundle_skip_reason.unreadable",
    kb_bundle_entry_too_large: "bundle_skip_reason.too_large",
    kb_bundle_entry_unsafe: "bundle_skip_reason.unsafe",
    kb_bundle_entry_type_unsupported: "bundle_skip_reason.type_unsupported",
    kb_bundle_entry_ingest_failed: "bundle_skip_reason.ingest_failed",
    kb_bundle_entry_capability_component: "bundle_skip_reason.capability_component",
  };
  const isTerminal = (s: KbParseStatus) => s === "done" || s === "failed";
  const settled =
    summary.uploaded.filter((f) => isTerminal(status[f.file_id] ?? f.parse_status)).length +
    summary.skipped.length;
  const total = summary.total_entries || summary.uploaded.length + summary.skipped.length || 1;
  const pct = Math.round((settled / total) * 100);
  const doneN = summary.uploaded.filter((f) => (status[f.file_id] ?? f.parse_status) === "done").length;
  const failedN = summary.uploaded.filter((f) => (status[f.file_id] ?? f.parse_status) === "failed").length;
  const allSettled = settled >= total;
  const barColor = failedN ? "var(--c-warn)" : allSettled ? "var(--c-pos)" : "var(--c-accent)";

  return (
    <div data-testid="kb-bundle-summary" className="kb-bundle">
      <div className="kb-bundle-head">
        <span>
          {allSettled
            ? failedN || summary.skipped.length
              ? <><b className="kb-bundle-count">{t("bundle_completed", { n: doneN })}</b>{t("bundle_summary_partial", { skipped: summary.skipped.length, failed: failedN })}</>
              : <b className="kb-bundle-count is-ok">{t("bundle_all_done", { n: doneN })}</b>
            : t("bundle_processing", { settled, total })}
        </span>
        <span className="kb-bundle-spacer" />
        <span className="kb-bundle-pct">{pct}%</span>
      </div>
      <div className="kb-up-progress"><span style={{ width: `${pct}%`, background: barColor }} /></div>

      <div className="kb-bundle-list">
        {summary.uploaded.map((f) => {
          const s = status[f.file_id] ?? f.parse_status;
          const inProg = isParseInProgress(s) || s === "queued" || s === "awaiting_upload" || s === "pending";
          const meta = typeMeta(f.filename);
          return (
            <div className="kb-up-row" key={f.file_id}>
              <span className="kb-ftype kb-bundle-ftype" style={{ background: meta.color }}>{meta.tag}</span>
              <span className="nm" title={f.filename}>{f.filename}</span>
              {s === "failed" ? (
                <>
                  <span className="kb-up-state failed"><IconX /> {t("bundle_parse_failed")}</span>
                  <button type="button" className="kb-retry-inline kb-bundle-retry" onClick={() => onRetry(f.file_id)}><IconRetry /> {t("bundle_retry")}</button>
                </>
              ) : s === "done" ? (
                <span className="kb-up-state ok"><IconCheckBig /> {t("bundle_done_state")}</span>
              ) : (
                <span className="kb-up-state parsing"><IconSpin /> {inProg ? t("bundle_parsing") : t("bundle_in_progress")}</span>
              )}
            </div>
          );
        })}
        {summary.skipped.map((sk, i) => {
          const meta = typeMeta(sk.filename);
          const reasonKey =
            skippedReasonKey[sk.reason_code] ?? "bundle_skip_reason.unknown";
          return (
            <div className="kb-up-row" key={`sk-${i}`}>
              <span className="kb-ftype kb-bundle-ftype" style={{ background: meta.color }}>{meta.tag}</span>
              <span className="nm" title={sk.filename}>
                {sk.filename} <span className="reason">· {t(reasonKey)}</span>
              </span>
              <span className="kb-up-state skip">{t("bundle_skipped")}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}



/**
 * Scope radios Home/End. Native same-name radio owns arrows moving/selecting, disabled skipping, Tab only selected;
 * browsers omit Home/End, added here. Never replace with RadioGroup: disabled “空间” card description includes “加入空间” link,
 * inside button would nest interactive elements, disabled button would swallow link clicks.
 */
function selectEdgeScope(event: KeyboardEvent<HTMLDivElement>) {
  if (event.key !== "Home" && event.key !== "End") return;
  if (!(event.target instanceof HTMLInputElement) || event.target.type !== "radio") return;
  const radios = Array.from(
    event.currentTarget.querySelectorAll<HTMLInputElement>('input[type="radio"]:not(:disabled)'),
  );
  const target = event.key === "Home" ? radios[0] : radios[radios.length - 1];
  if (!target) return;
  event.preventDefault();
  target.focus();
  if (!target.checked) target.click();
}

/** Scope card (KB management M5 design .kb-scope-card): native radio within card, keyboard/screen readers unchanged. */
function ScopeCard({
  value, current, onChange, title, desc, disabled, loading, trailing,
}: {
  value: KbScope;
  current: KbScope;
  onChange: (v: KbScope) => void;
  title: string;
  desc: ReactNode;
  disabled?: boolean;
  loading?: boolean;
  trailing?: ReactNode;
}) {
  const on = current === value && (!disabled || loading);
  const cls = ["kb-scope-card", on && "on", loading ? "loading" : disabled && "disabled"]
    .filter(Boolean)
    .join(" ");
  return (
    <label className={cls} data-testid={`kb-scope-card-${value}`}>
      <input
        type="radio"
        name="kb-scope"
        value={value}
        checked={current === value}
        onChange={() => onChange(value)}
        disabled={disabled}
        data-testid={`kb-scope-${value}`}
      />
      <span className="radio" aria-hidden="true" />
      <div className="kb-up-scope-text">
        <div className="title">{title}</div>
        <div className="desc">{desc}</div>
      </div>
      {trailing}
    </label>
  );
}
