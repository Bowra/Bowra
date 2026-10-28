"use client";

import { useTranslations } from "next-intl";
import { Check, Download, RotateCcw } from "lucide-react";

import { LucideGlyph } from "@/components/icon-refresh";
import { ChatCard, ChatCardButton, ChatCardSpinner } from "@/components/ui/card";
import { requestCanvasDataFocus } from "@/lib/canvas-workspace-focus";
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
  const { filename, fileType, mime, downloadUrl, sizeBytes, slideCount } = data;
  const { status, pct, download: runDownload } = useFileDownload({ downloadUrl, filename, mime, sizeBytes });

  const sizeStr = fmtSize(sizeBytes);
  const ext = fileExtensionLabel(filename) || fileType.toUpperCase();
  // Meta: at most type · scale (pages) · size (spec §6).
  const facts = [ext, slideCount ? tAttach("file_pages", { count: slideCount }) : null, sizeStr || null].filter(Boolean).join(" · ");
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
          ? () => requestCanvasDataFocus(data.id, { entryPoint: "chat" })
          : status === "downloading"
            ? () => undefined
            : runDownload
      }
      activateLabel={resultsWorkspaceEnabled ? resultsT("open_object", { filename }) : downloadLabel}
      trailing={trailing}
      progress={status === "downloading" ? pct / 100 : undefined}
    >
      {/* role=status so assistive technology hears downloading / done / failed transitions */}
      <span className={status === "error" ? "ui-card__note ui-card__note--error" : "ui-card__note"} role="status" aria-live="polite">
        {meta}
      </span>
    </ChatCard>
  );
}
