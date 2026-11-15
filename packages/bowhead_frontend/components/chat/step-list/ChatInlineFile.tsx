"use client";

import { useEffect } from "react";
import { useTranslations } from "next-intl";
import { Check, Download, RotateCcw } from "lucide-react";

import { LucideGlyph } from "@/components/icon-refresh";
import { ChatCard, ChatCardButton, ChatCardSpinner } from "@/components/ui/card";
import { requestCanvasDataFocus } from "@/lib/canvas-workspace-focus";
import { recordFileVersion, useNewestFileVersion } from "@/lib/file-version-registry";
import { fileExtensionLabel, fileTypeIcon } from "@/lib/file-type-icon";
import { useFileDownload } from "@/lib/use-file-download";
import { useResultsWorkspaceCapabilitySnapshot } from "@/lib/use-results-workspace-capability";

/**
 * subagent-unification P5a — a produced **deliverable** file inside a chat answer (Chat Cards Spec §6, D10).
 *
 * A ChatCard with a 36px tile holding a 20px line icon for the file type (presentation / spreadsheet /
 * document / image / archive); the extension goes first in the meta line ("PPTX · 12 pages · 3.4 MB").
 * Two sibling controls, never nested: the whole card opens the result in Results, and the 28px download
 * button (44 on touch) downloads it. Without Results the whole card downloads. Downloading shows a 2px
 * bottom bar; done shows a check; a real HTTP failure shows "Download failed" with a Retry button.
 *
 * Download strategy (CR review): small same-origin files use fetch-with-progress; a REAL HTTP error goes to
 * the error state (never a false "Downloaded"); a cross-origin fetch reject or a large file falls back to a
 * native `<a download>` navigation, which streams to disk without buffering the blob in JS heap.
 */

export interface ChatInlineFileData {
  id: string;
  filename: string;
  fileType: string;
  mime?: string;
  downloadUrl: string;
  sizeBytes?: number;
  slideCount?: number;
  /** File versions (R3): the delivered version, when its file has a newer live version. */
  versionNo?: number;
  /** The file's newest version; the card opens and downloads it. */
  latest?: { id: string; versionNo: number; filename: string; downloadUrl: string; sizeBytes?: number };
  /** The delivered version was cleaned up (older versions are kept up to a limit). */
  pruned?: boolean;
  /** The file this card's version belongs to (its first version's id) and that version's number. */
  lineage?: { root: string; versionNo: number };
}

export function fmtSize(bytes?: number): string {
  if (!bytes || bytes <= 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  // .toFixed(1) for KB matches the Data-tab file list (data-tab-files.tsx) so the
  // SAME deliverable shows an identical size in chat and the Assets surface.
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function ChatInlineFile({ data }: { data: ChatInlineFileData }) {
  const tAttach = useTranslations("attachment");
  const resultsT = useTranslations("results_workspace");
  const resultsWorkspaceEnabled = useResultsWorkspaceCapabilitySnapshot();
  const { fileType, mime, pruned, lineage } = data;
  // 116 recheck N6: a newer version delivered while the page is open reaches this card without a reload.
  const shownNewest = useNewestFileVersion(lineage?.root);
  const latest =
    lineage && shownNewest && shownNewest.versionNo > Math.max(lineage.versionNo, data.latest?.versionNo ?? 0)
      ? shownNewest
      : data.latest;
  const lineageRoot = lineage?.root;
  const lineageVersion = lineage?.versionNo;
  const projectedLatest = data.latest;
  useEffect(() => {
    if (!lineageRoot || lineageVersion === undefined) return;
    recordFileVersion(lineageRoot, {
      id: data.id,
      versionNo: lineageVersion,
      filename: data.filename,
      downloadUrl: data.downloadUrl,
      sizeBytes: data.sizeBytes,
    });
    if (projectedLatest) recordFileVersion(lineageRoot, projectedLatest);
  }, [lineageRoot, lineageVersion, data.id, data.filename, data.downloadUrl, data.sizeBytes, projectedLatest]);
  // The card stands for the file: with a newer version it opens and downloads that version (Q5).
  const filename = latest?.filename ?? data.filename;
  const downloadUrl = latest?.downloadUrl ?? data.downloadUrl;
  const sizeBytes = latest ? latest.sizeBytes : data.sizeBytes;
  const slideCount = latest ? undefined : data.slideCount;
  const { status, pct, download: runDownload } = useFileDownload({ downloadUrl, filename, mime, sizeBytes });
  const delivered = useFileDownload({
    downloadUrl: data.downloadUrl,
    filename: data.filename,
    mime,
    sizeBytes: data.sizeBytes,
  });
  const deliveredVersion = data.versionNo ?? lineage?.versionNo ?? 1;

  const sizeStr = fmtSize(sizeBytes);
  const ext = fileExtensionLabel(filename) || fileType.toUpperCase();
  // Meta: at most type · scale (pages) · size (spec §6).
  const facts = [
    latest ? tAttach("file_updated_version", { version: latest.versionNo }) : null,
    ext,
    slideCount ? tAttach("file_pages", { count: slideCount }) : null,
    sizeStr || null,
  ].filter(Boolean).join(" · ");
  const meta =
    status === "downloading"
      ? tAttach("file_downloading", { pct })
      : status === "done"
        ? `${tAttach("file_downloaded")} · ${facts}`
        : status === "error"
          ? tAttach("file_download_failed")
          : facts || tAttach("file_generic");

  const downloadLabel =
    status === "downloading"
      ? tAttach("file_aria_downloading", { filename })
      : status === "done"
        ? tAttach("file_aria_downloaded", { filename })
        : status === "error"
          ? tAttach("file_aria_failed", { filename })
          : tAttach("file_aria_download", { filename });

  const glyph =
    status === "downloading" ? <ChatCardSpinner small /> : status === "done" ? (
      <LucideGlyph icon={Check} size={16} aria-hidden="true" />
    ) : (
      <LucideGlyph icon={Download} size={16} aria-hidden="true" />
    );

  const trailing = status === "error" ? (
    <ChatCardButton variant="secondary" icon={RotateCcw} onClick={runDownload} aria-label={downloadLabel}>
      {tAttach("retry")}
    </ChatCardButton>
  ) : resultsWorkspaceEnabled ? (
    <button
      type="button"
      className="ui-card-dl"
      data-state={status}
      onClick={runDownload}
      disabled={status === "downloading"}
      aria-busy={status === "downloading" || undefined}
      aria-label={downloadLabel}
      title={downloadLabel}
    >
      {glyph}
    </button>
  ) : (
    <span className="ui-card-dl" data-state={status} aria-hidden="true">
      {glyph}
    </span>
  );

  const Icon = fileTypeIcon(filename, mime ?? "");
  return (
    <ChatCard
      intent="info"
      className="chat-deliverable"
      data-testid="chat-inline-file"
      data-state={status}
      media={<LucideGlyph icon={Icon} size={20} aria-hidden="true" />}
      title={filename}
      onActivate={
        resultsWorkspaceEnabled
          ? () => requestCanvasDataFocus(latest?.id ?? data.id, { entryPoint: "chat" })
          : status === "downloading"
            ? () => undefined
            : runDownload
      }
      activateLabel={resultsWorkspaceEnabled ? resultsT("open_object", { filename }) : downloadLabel}
      trailing={trailing}
      progress={status === "downloading" ? pct / 100 : undefined}
      secondary={
        latest && !pruned
          ? [
              {
                label:
                  delivered.status === "error"
                    ? tAttach("file_this_version_failed", { version: deliveredVersion })
                    : tAttach("file_this_version", { version: deliveredVersion }),
                busy: delivered.status === "downloading",
                busyLabel: tAttach("file_downloading", { pct: delivered.pct }),
                onSelect: delivered.download,
                testId: "chat-inline-file-this-version",
              },
            ]
          : undefined
      }
      footerStart={
        latest && pruned ? (
          <span className="ui-card__note">{tAttach("file_this_version_pruned", { version: deliveredVersion })}</span>
        ) : undefined
      }
    >
      {/* role=status so assistive technology hears downloading / done / failed transitions */}
      <span className={status === "error" ? "ui-card__note ui-card__note--error" : "ui-card__note"} role="status" aria-live="polite">
        {meta}
      </span>
    </ChatCard>
  );
}
