"use client";

/**
 * Knowledge-base file-details drawer — KB experience remediation design 03 (narrow-screen fullscreen: 06).
 *
 * Header only filename/status and “对话中被引用 N 次” when N ≥1; status area follows five states: processing shows
 * progress, failed/stuck reason+retry, ready “在对话中使用”. Content-deep-search entry pins/highlights matching chunk.
 */

import { useLocale, useTranslations } from "next-intl";
import { Fragment, type ReactNode, useEffect, useMemo, useRef, useState } from "react";

import { DialogShell } from "@/components/ui/dialog";
import { kbFileError } from "@/lib/kb-file-error";
import { kbFailReason, kbFileState, type KbFileState } from "@/lib/kb-file-status";
import {
  type KbChunkSummary,
  type KbFileDTO,
  type KbSearchResultChunk,
  deleteKbFile,
  downloadKbFileBlob,
  fetchKbFileChunks,
  formatBytes,
} from "@/lib/kb-api";
import { useMediaQuery } from "@/lib/use-media-query";
import { formatShortDateTime } from "@/lib/locale";

import { IconChat, IconChevL, IconDownload, IconEye, IconTrash, IconX } from "./kb-icons";
import { KbPreview } from "./kb-preview";
import { KbFileHealth } from "./kb-file-health";
import { KbStatusPill, type ExtractionState } from "./kb-status-pill";

type Translator = (key: string, values?: Record<string, string | number>) => string;

/** Hit-chunk location labels: “第 N 页” → chapter “…” → “第 N 段”; omit if none (design 02·D). */
export function kbChunkLocation(
  t: Translator,
  c: { page?: number | null; section_title?: string | null; chunk_index?: number | null },
): string | null {
  if (c.page != null) return t("files_view.loc_page", { n: c.page });
  if (c.section_title) return t("files_view.loc_section", { title: c.section_title });
  if (c.chunk_index != null) return t("files_view.loc_para", { n: c.chunk_index });
  return null;
}

/** Keyword highlighting (case-insensitive, every occurrence). */
export function kbHighlight(text: string, q: string): ReactNode {
  const needle = q.trim().toLowerCase();
  if (!needle) return text;
  const out: ReactNode[] = [];
  const lower = text.toLowerCase();
  let from = 0;
  for (let i = lower.indexOf(needle); i >= 0; i = lower.indexOf(needle, from)) {
    out.push(<Fragment key={`t${i}`}>{text.slice(from, i)}</Fragment>);
    out.push(<mark key={`m${i}`}>{text.slice(i, i + needle.length)}</mark>);
    from = i + needle.length;
  }
  out.push(<Fragment key="tail">{text.slice(from)}</Fragment>);
  return out;
}

type ShownChunk = Pick<KbChunkSummary, "chunk_id" | "content" | "page" | "section_title"> & {
  chunk_index: number | null;
};

const PILL_STATE: Record<KbFileState, ExtractionState> = {
  uploading: "uploading",
  queued: "queued",
  processing: "parsing",
  ready: "chunks_only",
  failed: "parse_failed",
};

interface Props {
  file: KbFileDTO;
  onClose: () => void;
  /** Postdelete callback; omitted removes delete (read-only, e.g. matrix “查看原文”). */
  onDeleted?: () => void;
  /** Retry failed/stuck file (same list action). */
  onRetry?: () => void;
  /** “可见范围” text (space scope uses space name); defaults to scope name. */
  scopeLabel?: string;
  /** Content-deep-search matching chunk: pin/highlight/scroll into view. */
  hitChunk?: KbSearchResultChunk | null;
  /** Matching keyword for chunk highlighting. */
  hitQuery?: string;
  /** Pinned-chunk origin: content-deep-search (default) or chat-answer KB citation. */
  hitSource?: "search" | "citation" | "matrix";
  /** “在对话中使用”, same action as “资料库 › 文件”; omitted hides entry. */
  onUseInChat?: () => void;
  /** Opened with a pointer: focus the panel instead of the close button, so no focus ring shows until the keyboard is used. */
  openedWithPointer?: boolean;
  /** Change visible scope; omitted (no permission) hides entry. */
  onChangeScope?: () => void;
  /** Bind template; omitted hides entry. */
  onBindTemplate?: () => void;
}

export function KbFileDrawer({
  file,
  onClose,
  onDeleted,
  onRetry,
  scopeLabel,
  hitChunk,
  hitQuery = "",
  hitSource = "search",
  onUseInChat,
  onChangeScope,
  onBindTemplate,
  openedWithPointer = false,
}: Props) {
  const t = useTranslations("knowledge_base.drawer");
  const locale = useLocale();
  const tKb = useTranslations("knowledge_base");
  const tErr = useTranslations("knowledge_base.errors");
  const narrow = useMediaQuery("(max-width: 640px)");
  const state = kbFileState(file);
  const failReason = kbFailReason(file);
  const ready = state === "ready";
  const [deleting, setDeleting] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [chunks, setChunks] = useState<KbChunkSummary[] | null>(null);
  const [chunksLoading, setChunksLoading] = useState(false);
  const [showPreview, setShowPreview] = useState(false);
  const bodyRef = useRef<HTMLDivElement | null>(null);

  // Shell ignores Esc while upper layer (preview/delete confirmation) open (busy below); Esc here closes uppermost only.
  useEffect(() => {
    if (!showPreview && !confirmingDelete) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (showPreview) setShowPreview(false);
      else setConfirmingDelete(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [showPreview, confirmingDelete]);

  useEffect(() => {
    if (file.parse_status !== "done") return;
    let cancelled = false;
    setChunksLoading(true);
    fetchKbFileChunks(file.file_id)
      .then((res) => {
        if (!cancelled) setChunks(Array.isArray(res.chunks) ? res.chunks : []);
      })
      .catch(() => {
        if (!cancelled) setChunks([]);
      })
      .finally(() => {
        if (!cancelled) setChunksLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [file.file_id, file.parse_status]);

  // Pin matching chunk; absent from list (display limit, etc.), prepend search result itself.
  const shownChunks = useMemo((): { c: ShownChunk; hit: boolean }[] | null => {
    if (!chunks) return null;
    const list = chunks.slice(0, 50);
    if (!hitChunk) return list.map((c) => ({ c, hit: false }));
    const found = chunks.find((c) => c.chunk_id === hitChunk.chunk_id);
    // Chunk id without original text (chat-citation deep link), absent from list: never pin empty chunk.
    if (!found && !hitChunk.content) return list.map((c) => ({ c, hit: false }));
    const pinned: ShownChunk = found ?? {
      chunk_id: hitChunk.chunk_id,
      content: hitChunk.content,
      page: hitChunk.page,
      section_title: hitChunk.section_title,
      chunk_index: hitChunk.chunk_index ?? null,
    };
    return [
      { c: pinned, hit: true },
      ...list.filter((c) => c.chunk_id !== hitChunk.chunk_id).map((c) => ({ c, hit: false })),
    ];
  }, [chunks, hitChunk]);

  // Scroll hit into view. Drawer body is scroll container (position: relative), set scrollTop directly,
  // not scrollIntoView, which would also scroll page itself.
  const hitShown = Boolean(shownChunks?.some((x) => x.hit));
  useEffect(() => {
    if (!hitShown) return;
    const body = bodyRef.current;
    const el = body?.querySelector<HTMLElement>('[data-hit="true"]');
    if (body && el) body.scrollTop = Math.max(0, el.offsetTop - 12);
  }, [hitShown]);

  const handleDownload = async () => {
    setDownloading(true);
    setError(null);
    try {
      // fetch+blob ensures space headers sent (browser <a> omits custom headers).
      await downloadKbFileBlob(file.file_id, file.filename);
    } catch (e) {
      setError(tKb(kbFileError(e, "download")));
    } finally {
      setDownloading(false);
    }
  };

  const handleDelete = async () => {
    setDeleting(true);
    setError(null);
    try {
      await deleteKbFile(file.file_id);
      onDeleted?.();
    } catch (e) {
      setError(tErr("delete_failed", { error: tKb(kbFileError(e, "delete")) }));
    } finally {
      setDeleting(false);
    }
  };

  const scopeText =
    scopeLabel ??
    (file.scope === "personal"
      ? t("scope_personal_desc")
      : tKb(`scope_${(file.scope as string) === "workspace" ? "space" : file.scope}`));
  const sizeText = file.chunks_count > 0
    ? t("size_chunks", { size: formatBytes(file.size_bytes), n: file.chunks_count })
    : formatBytes(file.size_bytes);

  const previewBtn = (
    <button
      type="button"
      onClick={(e) => { e.currentTarget.focus(); setShowPreview(true); }}
      disabled={!ready}
      className="kb-btn kb-btn-secondary"
      data-testid="kb-preview-button"
    >
      <IconEye /> {t("preview")}
    </button>
  );
  const downloadBtn = (label: string, cls = "kb-btn kb-btn-secondary") => (
    <button type="button" onClick={handleDownload} disabled={downloading} className={cls} data-testid="kb-download-link">
      <IconDownload /> {downloading ? "…" : label}
    </button>
  );
  const canDelete = Boolean(file.can_delete && onDeleted);
  const deleteBtn = (small = false) =>
    canDelete ? (
      <button
        type="button"
        onClick={() => setConfirmingDelete(true)}
        disabled={deleting}
        className={`kb-btn kb-btn-danger del${small ? " kb-btn-sm" : ""}`}
        data-testid="kb-delete-button"
      >
        <IconTrash /> {t("delete")}
      </button>
    ) : null;

  return (
    // DialogShell owns Esc close/focus return to drawer opener (PRD FR2.5), always invokes latest onClose.
    // Shell ignores Esc/backdrop click while preview/delete confirmation above: Esc closes uppermost only (preview handles itself),
    // never closing entire drawer too.
    <DialogShell
      presentation="center"
      onClose={onClose}
      busy={showPreview || confirmingDelete}
      label={file.filename}
      backdropClassName="kb-drawer-overlay"
      panelClassName="kb-drawer-panel"
      panelProps={{
        "data-testid": "kb-file-drawer",
        "data-dialog-initial-focus": openedWithPointer ? "" : undefined,
        // Delete-confirmation strip inside panel: shell prevents panel-key bubbling to document; Esc collapses it here.
        onKeyDown: (event: React.KeyboardEvent<HTMLDivElement>) => {
          if (event.key !== "Escape") return;
          if (showPreview) setShowPreview(false);
          else if (confirmingDelete) setConfirmingDelete(false);
        },
      } as React.HTMLAttributes<HTMLDivElement>}
    >
        <header className="dr-hd">
          {narrow && (
            <button type="button" className="kb-x" onClick={onClose} aria-label={t("back_aria")}>
              <IconChevL />
            </button>
          )}
          <div className="dr-title">
            <h2 className="dr-name">{file.filename}</h2>
            <div className="dr-state">
              <KbStatusPill
                sourceHealth={file.source_health}
                state={PILL_STATE[state]}
                pct={state === "processing" ? file.parse_progress_pct : null}
              />
              {file.cite_count >= 1 && (
                <span className="dr-cited" data-testid="kb-drawer-cited">{t("cited_n", { n: file.cite_count })}</span>
              )}
            </div>
          </div>
          {!narrow && (
            <button type="button" className="kb-x" onClick={onClose} aria-label={t("close_aria")}>
              <IconX />
            </button>
          )}
        </header>

        <div className="dr-bd" ref={bodyRef}>
          {(state === "processing" || state === "queued" || state === "uploading") && (
            <div className="prog">
              <div className="t">{t("processing_title")}</div>
              <div className="d">{t("processing_sub")}</div>
              {state === "processing" && file.parse_progress_pct != null && (
                <div className="bar"><span style={{ width: `${file.parse_progress_pct}%` }} /></div>
              )}
            </div>
          )}

          {failReason && (
            <div className="reason">
              <div className="t">{t(`fail_title.${failReason}`)}</div>
              <div className="d">{tKb(`fail_reason.${failReason}`)}</div>
              <div className="r">
                {onRetry && (
                  <button type="button" onClick={onRetry} className="kb-btn kb-btn-primary kb-btn-sm" data-testid="kb-drawer-retry">
                    {t("retry")}
                  </button>
                )}
                {/* Previous missing-original failure first guides parsing retry; download offered after success. */}
                {failReason !== "source_missing" && downloadBtn(t("download_original"), "kb-btn kb-btn-ghost kb-btn-sm")}
              </div>
            </div>
          )}

          {ready && (
            <div className="dr-acts">
              {onUseInChat && (
                <button type="button" className="kb-btn kb-btn-primary" onClick={onUseInChat} data-testid="kb-drawer-use-in-chat">
                  <IconChat /> {t("use_in_chat")}
                </button>
              )}
              {previewBtn}
              {downloadBtn(t("download"))}
            </div>
          )}

          <dl className="meta">
            <dt>{t("meta_scope")}</dt>
            <dd>{scopeText}</dd>
            <dt>{t("meta_uploaded_by")}</dt>
            <dd>{file.uploaded_by_name ?? "—"}</dd>
            <dt>{t("meta_uploaded_at")}</dt>
            <dd>{formatShortDateTime(file.uploaded_at, locale)}</dd>
            <dt>{t("meta_size")}</dt>
            <dd>
              {sizeText}
              {file.truncated_rows ? (
                <span className="dr-note" data-testid="kb-truncated-rows">
                  {t("truncated_rows", { indexed: file.truncated_rows.indexed.toLocaleString(), total: file.truncated_rows.total.toLocaleString() })}
                </span>
              ) : null}
            </dd>
          </dl>

          {!ready && (
            <>
              <div className="dr-acts">
                {previewBtn}
                {!failReason && state !== "uploading" && downloadBtn(t("download"))}
                {deleteBtn()}
              </div>
              <div className="why">{failReason ? (failReason === "source_missing" ? t("preview_source_missing") : t("preview_processing_failed")) : t("preview_disabled_until_done")}</div>
            </>
          )}

          <KbFileHealth fileId={file.file_id} version={file.version} allowActions={Boolean(file.can_delete)} />

          {ready && (
            <section className="dr-chunks" data-testid="kb-chunks-section">
              <div className="sub-t">{t("title_chunks")}</div>
              {chunksLoading && <div className="kb-skel" />}
              {!chunksLoading && shownChunks !== null && shownChunks.length === 0 && (
                <div className="why">{t("no_chunks_yet")}</div>
              )}
              {shownChunks && shownChunks.length > 0 && (
                <div className="chunks">
                  {shownChunks.map(({ c, hit }) => {
                    const loc = kbChunkLocation(tKb, c);
                    const locText = hit ? [loc, t(hitSource === "citation" ? "cited_hit" : hitSource === "matrix" ? "matrix_hit" : "search_hit")].filter(Boolean).join(" · ") : loc;
                    const body = c.content.length > 300 ? `${c.content.slice(0, 300)}…` : c.content;
                    return (
                      <div
                        key={c.chunk_id}
                        className={`chunk${hit ? " hit" : ""}`}
                        data-testid="kb-chunk-card"
                        data-chunk-id={c.chunk_id}
                        data-hit={hit ? "true" : undefined}
                      >
                        {locText && <span className="loc">{locText}</span>}
                        <p>{hit ? kbHighlight(body, hitQuery) : body}</p>
                      </div>
                    );
                  })}
                </div>
              )}
            </section>
          )}

          {ready && (onChangeScope || onBindTemplate || canDelete) && (
            <div className="dr-acts">
              {onChangeScope && (
                <button type="button" className="kb-btn kb-btn-ghost kb-btn-sm" onClick={onChangeScope}>
                  {t("change_scope")}
                </button>
              )}
              {onBindTemplate && (
                <button type="button" className="kb-btn kb-btn-ghost kb-btn-sm" onClick={onBindTemplate}>
                  {t("bind_template")}
                </button>
              )}
              {deleteBtn(true)}
            </div>
          )}

          {confirmingDelete && (
            <div className="confirm" data-testid="kb-delete-confirm">
              <div className="t">{t("delete_confirm")}</div>
              <div className="r">
                <button
                  type="button"
                  onClick={() => setConfirmingDelete(false)}
                  disabled={deleting}
                  className="kb-btn kb-btn-secondary kb-btn-sm"
                >
                  {t("cancel")}
                </button>
                <button
                  type="button"
                  onClick={handleDelete}
                  disabled={deleting}
                  className="kb-btn kb-btn-danger-solid kb-btn-sm"
                  data-testid="kb-delete-confirm-button"
                >
                  {deleting ? t("deleting") : t("confirm_delete")}
                </button>
              </div>
            </div>
          )}

          {error && (
            <p role="alert" className="dr-error">
              {error}
            </p>
          )}
        </div>
      {showPreview && <KbPreview file={file} onClose={() => setShowPreview(false)} />}
    </DialogShell>
  );
}
