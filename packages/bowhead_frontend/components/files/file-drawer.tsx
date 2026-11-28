"use client";

/**
 * file-management-v2 P1 — file-details drawer (extracted from the old page.tsx inline component and rebuilt to the design).
 *
 * Three product changes from the old version:
 *   1. Source uses readable copy, never raw internal enums such as `bi_fetch_rows` (PRD §4.2.2 hard constraint)
 *   2. Category uses badges, distinguishing automatic/manual knowledge base additions
 *   3. Bottom actions vary by tab + in_kb; knowledge base files **have no delete button**, only navigation (D2)
 */
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import { ArtifactPreview } from "@/components/canvas/artifact-preview";
import { KbColumn } from "@/components/files/kb-column";
import { keepLibraryVersion, libraryRead, type LibraryFile } from "@/lib/file-library";
import { CategoryBadge } from "@/components/files/category-badge";
import { deriveArtifactViewerProfile, previewUsesTextDto } from "@/lib/artifact-viewer-profile";
import { appPath } from "@/lib/base-path";
import { formatDateTime, getCurrentLocale } from "@/lib/locale";
import { assetClassOf, fmtSize, humanSource, type FilesTab } from "@/lib/files-taxonomy";
import {
  FILE_CATEGORIES,
  type FileCategory,
  type FileDTO,
  type FilePreview,
  type FileUsage,
  fetchFilePreview,
  fetchFileUsages,
  fileCategoryLabel,
  isFileCategory,
  setFileCategory,
} from "@/lib/api";
import { fileError, fileErrorFromCode } from "@/lib/file-error";
import { CharIcon } from "@/components/char-icon";

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  return formatDateTime(iso, getCurrentLocale()) || iso;
}

export function FileDrawer({
  file,
  tab,
  onClose,
  onDelete,
  onRestore,
  onGotoKb,
  onUseInChat,
  onAddToKb,
  onRemoveFromKb,
  onFileUpdated,
  pin, libraryFile, onRefresh,
}: {
  libraryFile?: LibraryFile;
  onRefresh?: () => void;
  file: FileDTO;
  tab: FilesTab;
  onClose: () => void;
  onDelete: () => void;
  onRestore: () => void;
  onGotoKb: () => void;
  onUseInChat: () => void;
  onAddToKb: () => void;
  onRemoveFromKb: () => void;
  /** Pin / Unpin to the sidebar (result pins); omitted in the trash. */
  pin?: { label: string; busy: boolean; onToggle: () => void };
  onFileUpdated: (f: FileDTO) => void;
}) {
  const t = useTranslations("files_page");
  const lt = useTranslations("file_library");
  const [details, setDetails] = useState(!libraryFile);
  const [versions,setVersions] = useState<LibraryFile[]>([]);
  const td = useTranslations("files_page.drawer");
  const [preview, setPreview] = useState<FilePreview | null>(null);
  const [usages, setUsages] = useState<FileUsage[] | null>(null);
  const [usagesError, setUsagesError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [savingCat, setSavingCat] = useState(false);
  const profile = deriveArtifactViewerProfile(file.mime, file.filename);
  const rendererLoadsContent = libraryFile ? !previewUsesTextDto(profile) : profile === "pptx";

  useEffect(() => {
    setPreview(null);
    if (rendererLoadsContent || file.deleted_at) return;
    const controller = new AbortController();
    // Start only for a committed selection, including React's development effect replay.
    queueMicrotask(async () => {
      if (controller.signal.aborted) return;
      try {
        const next = await fetchFilePreview(file.file_id, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setPreview({
          ...next,
          error: next.error_code || next.error ? t(fileErrorFromCode(next.error_code, "preview")) : null,
        });
      } catch (error) {
        if (controller.signal.aborted) return;
        setPreview({ family: file.mime_family, text: null, error: t(fileError(error, "preview")) });
      }
    });
    return () => controller.abort();
  }, [file.file_id, file.mime_family, file.deleted_at, rendererLoadsContent, t]);

  useEffect(() => {
    if (!details || file.deleted_at) return;
    let current=true;
    setUsages(null);
    setUsagesError(null);
    void (async () => {
      try {
        const r = libraryFile
          ? await libraryRead<{ usages: FileUsage[] }>("/items/" + encodeURIComponent(file.file_id) + "/usages")
          : await fetchFileUsages(file.file_id);
        if(current)setUsages(r.usages);
      } catch (e) {
        if (!current) return;
        setUsages([]);
        setUsagesError(t(fileError(e, "usages")));
      }
    })();
    return () => {current=false;};
  }, [details, file.deleted_at, file.file_id, libraryFile, t]);
  useEffect(()=>{
    if (!details || !libraryFile || (file.version_count ?? 1)<2) return;
    const ctrl=new AbortController();
    libraryRead<LibraryFile[]>("/items/"+encodeURIComponent(file.file_id)+"/versions",ctrl.signal)
      .then(v=>{if(!ctrl.signal.aborted)setVersions(v);}).catch(()=>{if(!ctrl.signal.aborted)setActionError(lt("unavailable"));});
    return ()=>ctrl.abort();
  }, [details,libraryFile,file.file_id,file.version_count,lt]);

  const onChangeCat = async (cat: FileCategory) => {
    if (cat === file.category) return;
    setSavingCat(true);
    setActionError(null);
    try {
      onFileUpdated(await setFileCategory(file.file_id, cat));
    } catch (e) {
      setActionError(t(fileError(e, "category")));
    } finally {
      setSavingCat(false);
    }
  };

  const cls = assetClassOf(file);
  const previewSection = (<div className="dr-sec">
          <h4>{td("preview")}</h4>
          <ArtifactPreview
            fileId={file.file_id}
            filename={file.filename}
            mime={file.mime}
            sizeLabel={fmtSize(file.size)}
            sizeBytes={file.size}
            trust={file.trust}
            preview={rendererLoadsContent ? null : preview}
            loading={!rendererLoadsContent && preview === null}
            error={rendererLoadsContent ? null : preview?.error ?? null}
          />
        </div>);

  return (
    <div className="dr" role="complementary">
      <div className="dr-head">
        <div className="dr-title">{file.filename}</div>
        <button type="button" className="iact" onClick={onClose} aria-label={td("close")}><CharIcon ch="✕" aria-hidden /></button>
      </div>

      <div className="dr-scroll">
        {libraryFile && <>
          {file.deleted_at ? <p>{libraryFile.recover_until && lt("recover",{date:fmtTime(libraryFile.recover_until)})}</p> : previewSection}
          {!file.deleted_at && <div className="dr-sec">{libraryFile.kb_known === false ? <p>{lt("reason.unknown_reference")}</p> : <KbColumn file={file} onAdd={onAddToKb}/>}
            {file.kb && <button type="button" onClick={onRemoveFromKb}>{t("kb.remove")}</button>}
          </div>}
          <button className="btn sec" aria-expanded={details} onClick={()=>setDetails(v=>!v)}>{lt("details")}</button>
        </>}
        {libraryFile && actionError && <p role="alert">{actionError}</p>}
        {details && <>
        {file.trust.startsWith("export_copy_legacy_") ? (
          <div className="dr-sec" role="note">
            <p className="muted">{t(`export_state.${file.trust}`)}</p>
            {file.trust === "export_copy_legacy_regenerable" && file.session_id ? (
              <a className="btn pri" href={appPath(`/?session=${encodeURIComponent(file.session_id)}`)}>
                {t("export_state.open_report")}
              </a>
            ) : null}
          </div>
        ) : null}
        <div className="dr-sec">
          <dl className="dl">
            <dt>{td("type")}</dt>
            <dd>{t(`family.${file.mime_family}`)}</dd>
            <dt>{td("size")}</dt>
            <dd className="mono">{fmtSize(file.size)}</dd>
            {!libraryFile && tab !== "run" && (
              <>
                <dt>{t("filter.category")}</dt>
                <dd>
                  <CategoryBadge
                    cls={cls}
                    onRemove={file.in_kb ? onRemoveFromKb : undefined}
                  />
                </dd>
              </>
            )}
            <dt>{td("source")}</dt>
            {/* Human-readable source — never expose internal source enums to users */}
            <dd>{t(`human_source.${humanSource(file)}`)}</dd>
            {tab !== "run" && (
              <>
                <dt>{td("category")}</dt>
                <dd>
                  <select
                    className="bizsel"
                    value={file.category ?? ""}
                    disabled={savingCat}
                    onChange={(e) => void onChangeCat(e.target.value as FileCategory)}
                    aria-label={td("category")}
                  >
                    <option value="" disabled>{td("uncategorized")}</option>
                    {/* A code this build does not know is shown verbatim instead of silently looking uncategorized. */}
                    {file.category && !isFileCategory(file.category) && (
                      <option value={file.category} disabled>{fileCategoryLabel(file.category)}</option>
                    )}
                    {FILE_CATEGORIES.map((c) => <option key={c} value={c}>{fileCategoryLabel(c)}</option>)}
                  </select>
                </dd>
              </>
            )}
            <dt>{td("uploaded")}</dt>
            <dd className="mono">{fmtTime(file.created_at)}</dd>
            <dt>{td("last_used")}</dt>
            <dd className="mono">{fmtTime(file.last_used_at)}</dd>
            <dt>{td("id")}</dt>
            <dd className="mono">{file.file_id}</dd>
          </dl>
          {!libraryFile && actionError ? (
            <p className="muted" role="alert">{actionError}</p>
          ) : null}
        </div>

        {!libraryFile && previewSection}

        <div className="dr-sec">
          <h4>{td("used_in")}</h4>
          {usagesError ? (
            <p className="muted" role="alert">{usagesError}</p>
          ) : usages === null ? (
            <p className="muted">{td("loading_preview")}</p>
          ) : usages.length === 0 ? (
            <p className="muted">{td("no_usages")}</p>
          ) : (
            <div className="uses">
              {usages.map((u) => (
                <div className="use" key={u.turn_id}>
                  <i className="ud" aria-hidden />
                  <div>
                    <a className="ut" href={appPath(`/?session=${encodeURIComponent(u.session_id)}`)}>
                      {u.session_title || u.session_id}
                    </a>
                    <div className="um">#{u.turn_seq} · {fmtTime(u.used_at)}</div>
                    {u.user_text ? <div className="uq">{u.user_text.slice(0, 80)}</div> : null}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
        {versions.length>0 && <section className="dr-sec"><h4>{lt("versions")}</h4>{versions.map(v=><p key={v.file_id}><a href={appPath("/api/v1/files/"+encodeURIComponent(v.file_id))}>{lt("version",{n:v.version_no??1})} · {v.filename}</a></p>)}</section>}
        </>}
      </div>

      <div className="dr-foot">
        {!file.deleted_at && <a className="btn sec" href={appPath(`/api/v1/files/${encodeURIComponent(file.file_id)}`)} target="_blank" rel="noreferrer">
          {td("download_original")}
        </a>}
        {tab === "trash" ? (
          <button type="button" className="btn pri" onClick={onRestore}>{t("ops.restore")}</button>
        ) : null}
        {tab !== "trash" && (
          <button type="button" className="btn sec" onClick={onUseInChat}>{t("use_in_chat")}</button>
        )}
        {pin ? (
          <button type="button" className="btn sec" disabled={pin.busy} onClick={pin.onToggle}>{pin.label}</button>
        ) : null}
        {libraryFile && !file.deleted_at && libraryFile.views.includes("run") && <button className="btn sec"
          onClick={()=>void keepLibraryVersion(libraryFile).then(()=>onRefresh?.()).catch(()=>setActionError(lt("unavailable")))}>{lt(libraryFile.kept?"release":"keep")}</button>}
        {tab === "trash" ? null : libraryFile ? (libraryFile.kb_known !== false && !file.in_kb && <button className="btn ghost" onClick={onDelete}>{t("ops.delete")}</button>) : file.in_kb ? (
          <>
            <button type="button" className="btn pri" onClick={onGotoKb}>{t("kb.manage")} <CharIcon ch="→" /></button>
            <button type="button" className="btn ghost" onClick={onRemoveFromKb}>{t("kb.remove")}</button>
            <span className="kbnote">{t("dialog.kb_hint")}</span>
          </>
        ) : (
          <>
            {tab === "assets" && file.kb_promotable && (
              <button type="button" className="btn sec" onClick={onAddToKb}>{t("kb.kb_add")}</button>
            )}
            <button type="button" className="btn ghost" onClick={onDelete}>{t("ops.delete")}</button>
          </>
        )}
      </div>
    </div>
  );
}
