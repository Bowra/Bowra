"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { Chat, FileDown, Maximize, RefreshCw, X } from "@/components/icons";
import { DialogShell } from "@/components/ui/dialog";
import {
  deriveArtifactViewerProfile,
  viewerModesForProfile,
  type ArtifactViewerProfile,
} from "@/lib/artifact-viewer-profile";
import { appPath } from "@/lib/base-path";
import { codeLanguageForFile } from "@/lib/code-highlight";
import { formatByteSize } from "@/lib/format";
import { fileExtensionLabel } from "@/lib/file-type-icon";
import { itemsOnOneLine, noOverflow, writeFitTokens, type FitStep } from "@/lib/toolbar-fit";
import { useDocxInlineEditCapability } from "@/lib/use-docx-inline-edit-capability";
import { DocumentEditor, type DocumentEditorHandle } from "./document-edit/document-editor";
import { useToolbarFit } from "@/lib/use-toolbar-fit";
import type { AttachmentRef } from "@/lib/types";
import type { ResultsWorkspaceErrorClass } from "@/lib/results-workspace-errors";
import type { ResultsViewerMode } from "@/lib/results-workspace-route";
import type {
  ArtifactResultRole,
  ResultCapability,
} from "@/lib/results-workspace-model";

import {
  ArtifactImagePreview,
  ArtifactRowsPreview,
  ArtifactTextPreview,
  ArtifactWorkbookPreview,
  DEFAULT_IMAGE_VIEW_STATE,
  DEFAULT_TEXT_VIEW_STATE,
  type ImageViewState,
  type PreviewLike,
  type TextViewState,
} from "./artifact-content-previews";
import {
  ArtifactDocumentPreview,
  type DocumentPreviewCache,
} from "./artifact-document-preview";
import {
  isSafeHtmlMime,
  SafeHtmlFrame,
  type SafeHtmlErrorCode,
  useSafeHtmlDocument,
} from "./safe-html-preview";
import {
  isVideoPreviewMime,
  VideoArtifactPreview,
  type VideoPlaybackState,
} from "./video-artifact-preview";
import { HtmlPreviewModeControl, useHtmlPreviewMode, type HtmlPreviewMode } from "./html-preview-mode";
import { InPlaceFullView } from "./in-place-full-view";
import type { HtmlPreviewIssue, InteractiveHtmlDocument } from "@/lib/html-preview-contract";
import { ResultObjectActions, type CompactResultToolbar } from "./result-object-actions";
import {
  clearPresentationHandoff,
  markPresentationEditing,
  readPresentationHandoff,
  stashPresentationHandoff,
} from "./presentation-edit/handoff";
import {
  PresentationEditor,
  type PresentationEditorExit,
  type PresentationEditorHandle,
} from "./presentation-edit/presentation-editor";

/** What the Results viewer gives a deck preview so it can be edited in place (pptx-inline-edit design §7). */
export interface PresentationEditHost {
  sessionId: string;
  /** A Turn of this conversation is running and may save a newer version. */
  agentRunning?: boolean;
  /** A save or a move to another version: show `artifactId` next (the viewer reloads its list first), then run `then`. */
  onOpenVersion: (artifactId: string, then?: () => void) => void;
  /** A save produced this version (the editor is still open): list it before the person leaves. */
  onSaved?: (artifactId: string) => void;
  /** Add a reference to one deck element to the Composer (design §6.9). */
  /** `quiet` when the control that asked confirms on its own (「已引用」), so no toast covers the header. */
  onReferenceElement?: (reference: import("@/lib/types").AttachmentRef, options?: { quiet?: boolean; stay?: boolean }) => void;
  /**
   * 「引用这一页」: add a page of `file` to its page set in the Composer, or remove it (design §13.6).
   * `stay` when the full view or the editor asked: they stay open instead of showing the Composer.
   */
  onToggleSlide?: (file: import("@/lib/types").AttachmentRef, page: number, options?: { stay?: boolean }) => import("@/lib/presentation-selection").SlideToggleOutcome;
  /** The pages of each deck file the Composer references, by file ID. */
  referencedSlides?: Readonly<Record<string, ReadonlyArray<number>>>;
}

export function ArtifactPreview({
  fileId,
  filename,
  mime,
  sizeLabel,
  preview,
  profile: profileProp,
  mode = "preview",
  loading = false,
  error = null,
  downloadHref,
  contentHref,
  sizeBytes,
  trust,
  showHeaderTitle = true,
  compactToolbar,
  allowFileShare = false,
  onReference,
  onDownload,
  capabilities,
  resultRole,
  downloadLabel,
  feedbackRecorded = false,
  onViewerModeChange,
  onOpenHistory,
  onFeedbackUnsupported,
  onRetry,
  onContentVersionChanged,
  onOpenFailed,
  onClose,
  presentationEdit,
}: {
  fileId: string;
  filename: string;
  mime?: string | null;
  sizeLabel?: string | null;
  preview: PreviewLike | null;
  profile?: ArtifactViewerProfile;
  mode?: ResultsViewerMode;
  loading?: boolean;
  error?: string | null;
  downloadHref?: string | null;
  contentHref?: string | null;
  sizeBytes?: number | null;
  trust?: string | null;
  showHeaderTitle?: boolean;
  compactToolbar?: CompactResultToolbar;
  allowFileShare?: boolean;
  onReference?: () => void;
  onDownload?: () => void;
  capabilities?: ReadonlySet<ResultCapability>;
  resultRole?: ArtifactResultRole;
  downloadLabel?: string;
  feedbackRecorded?: boolean;
  onViewerModeChange?: (mode: ResultsViewerMode) => void;
  onOpenHistory?: (trigger?: HTMLElement) => void;
  onFeedbackUnsupported?: () => void;
  onRetry?: () => void;
  onContentVersionChanged?: () => void;
  onOpenFailed?: (errorClass: ResultsWorkspaceErrorClass) => void;
  onClose?: () => void;
  presentationEdit?: PresentationEditHost;
}) {
  const t = useTranslations("artifact_viewer");
  const href = downloadHref ?? appPath(`/api/v1/files/${encodeURIComponent(fileId)}`);
  const rendererHref = contentHref ?? href;
  const profile = profileProp ?? deriveArtifactViewerProfile(mime, filename);
  // A viewer opened right after a deck edit takes the editor's render and full view (handoff.ts).
  const [handoff] = useState(() => readPresentationHandoff(fileId));
  const [expanded, setExpanded] = useState(Boolean(handoff?.expanded));
  const [editing, setEditing] = useState(Boolean(handoff?.edit));
  // Where editing started, so leaving it returns there (design §13.1).
  const editOriginRef = useRef<"panel" | "full">(handoff?.origin ?? (handoff?.expanded ? "full" : "panel"));
  const [editStartPage, setEditStartPage] = useState<number | undefined>(handoff?.edit ? undefined : handoff?.page);
  const editorRef = useRef<PresentationEditorHandle>(null);
  const documentEditorRef = useRef<DocumentEditorHandle>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [imageView, setImageView] = useState<ImageViewState>(DEFAULT_IMAGE_VIEW_STATE);
  const [textView, setTextView] = useState<TextViewState>(DEFAULT_TEXT_VIEW_STATE);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const documentPreviewCacheRef = useRef<DocumentPreviewCache>({
    ...(handoff?.pdfData ? { pdfData: handoff.pdfData } : {}),
    ...(handoff?.page ? { focusPage: handoff.page } : {}),
  });
  const mountedFileRef = useRef(fileId);
  const videoPlaybackRef = useRef<VideoPlaybackState>({
    currentTime: 0,
    volume: 1,
    muted: false,
    playbackRate: 1,
    started: false,
  });
  const safeHtmlActive = mode === "preview" && isSafeHtmlMime(mime) && !loading && !error;
  const pptxPreview = mode === "preview" && profile === "pptx";
  // Word files show their rendered pages while the switch is on (docx inline edit design §5.1).
  const docxPages = useDocxInlineEditCapability() && mode === "preview" && profile === "docx";
  // Word pages open in the same in-place full view as slides, where the Word editor also shows.
  const inPlaceFullView = safeHtmlActive || pptxPreview || docxPages;
  const videoActive = mode === "preview" && isVideoPreviewMime(mime) && !loading && !error;
  const inlineHref = withInlineDisposition(href);
  const htmlMode = useHtmlPreviewMode(fileId);
  const safeHtml = useSafeHtmlDocument({
    presentation: htmlMode.mode,
    enabled: safeHtmlActive && !htmlMode.paused,
    href: rendererHref,
    declaredSize: sizeBytes,
    reloadToken,
    onVersionChanged: onContentVersionChanged,
  });

  useEffect(() => {
    if (!safeHtmlActive || !safeHtml.error) return;
    const errorClass: ResultsWorkspaceErrorClass = safeHtml.error === "too_large"
      ? "too_large"
      : safeHtml.error === "unsupported"
        ? "unsupported"
        : safeHtml.error === "unavailable"
          ? "unavailable"
          : "unknown";
    onOpenFailed?.(errorClass);
  }, [onOpenFailed, safeHtml.error, safeHtmlActive]);

  useEffect(() => {
    clearPresentationHandoff(fileId);
    if (mountedFileRef.current === fileId) return;
    mountedFileRef.current = fileId;
    setEditing(false);
    setImageView(DEFAULT_IMAGE_VIEW_STATE);
    setTextView(DEFAULT_TEXT_VIEW_STATE);
    documentPreviewCacheRef.current = {};
    videoPlaybackRef.current = {
      currentTime: 0,
      volume: 1,
      muted: false,
      playbackRate: 1,
      started: false,
    };
    setExpanded(false);
  }, [fileId]);

  // Every file shows its type label ("PPTX", "XLSX"), not its MIME type, which crowds the header's actions
  // (critique 2026-11-25; narrow desktop layout design §5). Details keeps the MIME type.
  const meta = [
    fileExtensionLabel(filename) || (profile === "pptx" ? "PPTX" : null),
    sizeLabel,
    safeHtmlActive
      ? t(htmlMode.mode === "interactive" ? "html_interactive" : "capability_safe_html")
      : videoActive
        ? t("capability_video")
        : t(`capability_${profile}`),
  ].filter(Boolean).join(" · ");
  const safeHtmlBody = safeHtmlActive
    ? {
        ...safeHtml,
        htmlMode: htmlMode.available ? htmlMode.mode : undefined,
        paused: htmlMode.paused,
        onPause: htmlMode.togglePaused,
        onModeChange: htmlMode.select,
        onEscape: () => setExpanded(false),
        trust,
        onReload: onContentVersionChanged ?? (() => setReloadToken((value) => value + 1)),
      }
    : null;
  const bodyProps = {
    profile,
    codeLanguage: codeLanguageForFile(filename, mime),
    mode,
    preview,
    loading,
    error,
    onRetry,
    recoveryActions: onViewerModeChange ? (
      <ArtifactRecoveryActions mode={mode} profile={profile} capabilities={capabilities}
        downloadHref={href} onDownload={onDownload} onModeChange={onViewerModeChange} />
    ) : null,
    href: pptxPreview ? `${href.split(/[?#]/, 1)[0]}/representations/pptx-pdf` : rendererHref,
    pagesHref: docxPages ? `${href.split(/[?#]/, 1)[0]}/representations/docx-pdf` : undefined,
    inlineHref,
    alt: filename,
    sizeBytes,
    safeHtml: safeHtmlBody,
    compactHtml: Boolean(compactToolbar),
    video: videoActive
      ? {
          src: inlineHref,
          mime: mime ?? "video/mp4",
          onError: onOpenFailed,
          resumeState: videoPlaybackRef.current,
          onPlaybackStateChange: (state: VideoPlaybackState) => {
            videoPlaybackRef.current = state;
          },
        }
      : null,
    imageView,
    onImageViewChange: setImageView,
    textView,
    onTextViewChange: setTextView,
    onOpenFailed,
    documentPreviewCacheRef,
  };

  const leaveFullViewThen = <Args extends unknown[]>(action: (...args: Args) => void) => (...args: Args) => {
    if (expanded) setExpanded(false);
    action(...args);
  };
  const compactActions = compactToolbar && {
    modes: viewerModesForProfile(profile),
    returns: compactToolbar.returnActions.map((action) => ({
      ...action,
      onSelect: leaveFullViewThen(action.onSelect),
    })),
    html: safeHtmlBody && {
      mode: safeHtmlBody.htmlMode,
      paused: safeHtmlBody.paused,
      onPause: safeHtmlBody.onPause,
      onModeChange: safeHtmlBody.onModeChange,
      onReload: safeHtmlBody.onReload,
    },
  };
  // The host stays the one editing started with: a list refresh can drop it (or the capability) for a moment.
  const editHostRef = useRef(presentationEdit);
  if (presentationEdit) editHostRef.current = presentationEdit;
  const editHost = presentationEdit ?? editHostRef.current;
  const canEditPresentation = pptxPreview && !loading && !error
    && Boolean(presentationEdit) && Boolean(capabilities?.has("edit_presentation"));
  // The capability only decides whether Edit is offered; once open, the editor stays until it is left
  // (the server checks every save again).
  const editingPresentation = pptxPreview && Boolean(editHost) && editing && expanded;
  // Word files: the same host, the edit_document capability (docx inline edit design §7.1, §8.4).
  const canEditDocument = docxPages && !loading && !error && Boolean(presentationEdit) && Boolean(capabilities?.has("edit_document"));
  const editingDocument = docxPages && Boolean(editHost) && editing && expanded;
  const startEditing = (trigger?: HTMLElement) => {
    if (!expanded && trigger) returnFocusRef.current = trigger;
    editOriginRef.current = expanded ? "full" : "panel";
    setEditStartPage(documentPreviewCacheRef.current.visiblePage);
    setExpanded(true);
    setEditing(true);
  };
  const finishEditing = (exit: PresentationEditorExit | null) => {
    if (!exit) {
      setEditing(false);
      return;
    }
    // Leaving returns to where editing started: the Results panel or the full view, on the same page.
    const toPanel = exit.close || editOriginRef.current === "panel";
    if (exit.artifactId === fileId) {
      if (exit.page) documentPreviewCacheRef.current.focusPage = exit.page;
      setEditing(false);
      if (toPanel) setExpanded(false);
      exit.then?.();
      return;
    }
    // The next file opens in a fresh viewer (Results keys viewers by file): hand it the render and the view.
    // Staying in edit mode until then keeps this file's full view from showing in between.
    stashPresentationHandoff(exit.artifactId, {
      pdfData: exit.pdfData,
      expanded: exit.edit ? true : !toPanel,
      edit: exit.edit,
      origin: editOriginRef.current,
      ...(exit.page ? { page: exit.page } : {}),
    });
    if (exit.close) {
      setEditing(false);
      setExpanded(false);
    }
    editHost?.onOpenVersion(exit.artifactId, exit.then);
  };
  // 「引用这一页」 under each page of a deck the person can reference in chat: a toggle whose pages
  // gather in one Composer chip (design §13.2, §13.6). The full view stays open, so several pages
  // can be picked there.
  const deckFile: AttachmentRef = {
    fileId, filename, mime: mime ?? "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    size: sizeBytes ?? 0, referenceKind: "result",
  };
  const toggleSlide = editHost?.onToggleSlide && capabilities?.has("reference_in_chat") ? editHost.onToggleSlide : undefined;
  // Word pages too, while their pages are shown (docx inline edit design §7.4).
  const referencePage = (pptxPreview || docxPages) && toggleSlide && !editingPresentation && !editingDocument
    ? (page: number) => toggleSlide(deckFile, page, { stay: expanded })
    : undefined;
  const referencedPages = editHost?.referencedSlides?.[fileId];
  useEffect(() => {
    if (!editingPresentation && !editingDocument) return;
    markPresentationEditing(fileId, true);
    return () => markPresentationEditing(fileId, false);
  }, [editingDocument, editingPresentation, fileId]);
  // The full view says how many pages of this file wait in the Composer (design §13.7).
  const headerMeta = expanded && referencedPages?.length ? `${meta} · ${t("pages_referenced", { count: referencedPages.length })}` : meta;
  const sharedHeaderProps = {
    artifactId: fileId, filename, meta: headerMeta, href, onDownload, capabilities, resultRole, mode,
    downloadLabel, feedbackRecorded, onFeedbackUnsupported,
    compactActions,
    onEditPresentation: canEditPresentation || canEditDocument ? startEditing : undefined,
    onReference: onReference && leaveFullViewThen(onReference),
    referenceLabel: pptxPreview ? t("reference_whole_file") : undefined,
    onViewerModeChange: compactToolbar && onViewerModeChange ? leaveFullViewThen(onViewerModeChange) : onViewerModeChange,
    onOpenHistory: compactToolbar && onOpenHistory ? leaveFullViewThen(onOpenHistory) : onOpenHistory,
  };
  return (
    <>
      <InPlaceFullView active={inPlaceFullView && expanded} label={filename} closeOnEscape={!editingPresentation && !editingDocument}
        onClose={() => {
          if (editingPresentation) editorRef.current?.requestExit(true);
          else if (editingDocument) documentEditorRef.current?.requestExit(true);
          else setExpanded(false);
        }} returnFocusRef={returnFocusRef}>
        {editingDocument && editHost ? (
          <DocumentEditor
            ref={documentEditorRef}
            sessionId={editHost.sessionId}
            artifactId={fileId}
            filename={filename}
            downloadHref={href}
            initialPdfData={documentPreviewCacheRef.current.pdfData}
            initialPage={editStartPage}
            agentRunning={editHost.agentRunning}
            onReference={onReference && capabilities?.has("reference_in_chat") ? onReference : undefined}
            onReferenceElement={editHost.onReferenceElement && capabilities?.has("reference_in_chat") ? editHost.onReferenceElement : undefined}
            onTogglePage={toggleSlide ? (file, page) => toggleSlide(file, page, { stay: true }) : undefined}
            referencedPages={referencedPages}
            fromFullView={editOriginRef.current === "full"}
            onSaved={editHost.onSaved}
            onExit={finishEditing}
          />
        ) : editingPresentation && editHost ? (
          <PresentationEditor
            ref={editorRef}
            sessionId={editHost.sessionId}
            artifactId={fileId}
            filename={filename}
            downloadHref={href}
            initialPdfData={documentPreviewCacheRef.current.pdfData}
            initialPage={editStartPage}
            agentRunning={editHost.agentRunning}
            onReference={onReference && capabilities?.has("reference_in_chat") ? onReference : undefined}
            onReferenceElement={editHost.onReferenceElement && capabilities?.has("reference_in_chat")
              ? editHost.onReferenceElement
              : undefined}
            onToggleSlide={toggleSlide ? (file, page) => toggleSlide(file, page, { stay: true }) : undefined}
            referencedSlides={editHost.referencedSlides}
            // The editor always shows in the full view; only editing started there has two ways out.
            fromFullView={editOriginRef.current === "full"}
            onSaved={editHost.onSaved}
            onExit={finishEditing}
          />
        ) : <>
        <ArtifactPreviewHeader
          {...sharedHeaderProps}
          compactTitle={inPlaceFullView && expanded ? undefined : compactToolbar?.title}
          onExpand={(trigger) => {
            returnFocusRef.current = trigger;
            setExpanded((value) => !value);
          }}
          fullViewActive={inPlaceFullView && expanded}
          allowFileShare={allowFileShare && isSafeHtmlMime(mime)}
          onClose={inPlaceFullView && expanded ? undefined : onClose}
          showTitle={showHeaderTitle}
        />
        {!expanded || inPlaceFullView ? <ArtifactPreviewBody {...bodyProps} onReferencePage={referencePage} referencedPages={referencedPages} expanded={inPlaceFullView && expanded} /> : null}
        </>}
      </InPlaceFullView>
      {expanded && !inPlaceFullView ? (
        <DialogShell
          presentation="center"
          onClose={() => setExpanded(false)}
          label={filename}
          closeOnBackdrop={false}
          returnFocusRef={returnFocusRef}
          backdropStyle={{ zIndex: 240 }}
          panelClassName="artifact-viewer-modal"
        >
          <div className="artifact-viewer-modal-panel" tabIndex={-1}>
            <ArtifactPreviewHeader
              {...sharedHeaderProps}
              onClose={() => setExpanded(false)}
              modalClose
            />
            <div className="artifact-viewer-modal-body">
              <ArtifactPreviewBody {...bodyProps} onReferencePage={referencePage} referencedPages={referencedPages} expanded />
            </div>
          </div>
          <button
            type="button"
            tabIndex={-1}
            className="artifact-viewer-modal-scrim"
            aria-label={t("close")}
            onClick={() => setExpanded(false)}
          />
        </DialogShell>
      ) : null}
    </>
  );
}

/** Narrow desktop layout (design §5): action labels become icons in this order before the title gets too narrow. */
const FILE_HEADER_FIT_STEPS: readonly FitStep[] = ["more-icon", "full-icon", "reference-icon", "edit-icon"];
const FILE_HEADER_MIN_TITLE_PX = 120;

function fileHeaderInColumn(head: HTMLElement): boolean {
  return head.ownerDocument.defaultView?.getComputedStyle(head).flexDirection === "column";
}

function fileHeaderFits(head: HTMLElement): boolean {
  // In a column (the `stacked` token, or the ≤640 px layout) the title has its own row; the actions still keep one.
  const column = head.matches('[data-fit~="stacked"]') || fileHeaderInColumn(head);
  const title = head.querySelector(".artifact-viewer-title");
  const titleWide = column || !title || title.getBoundingClientRect().width >= FILE_HEADER_MIN_TITLE_PX;
  return titleWide
    && itemsOnOneLine(head.querySelector(".artifact-viewer-actions"))
    && itemsOnOneLine(head.querySelector(".artifact-object-actions"))
    && noOverflow(head);
}

/**
 * Stack title over actions only when even icon-only actions leave the title too narrow; then labels return where they
 * fit. The ≤640 px layout is already a column: there the labels give way in the same order until the actions fit.
 */
function fileHeaderStacked(head: HTMLElement): FitStep[] {
  writeFitTokens(head, []);
  if (fileHeaderInColumn(head)) return [];
  writeFitTokens(head, FILE_HEADER_FIT_STEPS);
  return fileHeaderFits(head) ? [] : ["stacked"];
}

function ArtifactPreviewHeader({
  artifactId,
  filename,
  meta,
  href,
  onExpand,
  onReference,
  onDownload,
  capabilities,
  resultRole,
  mode = "preview",
  downloadLabel,
  allowFileShare,
  feedbackRecorded,
  onViewerModeChange,
  onOpenHistory,
  onFeedbackUnsupported,
  onClose,
  showTitle = true,
  modalClose = false,
  fullViewActive = false,
  compactTitle,
  compactActions,
  onEditPresentation,
  referenceLabel,
}: {
  artifactId: string;
  filename: string;
  meta: string;
  href: string;
  onExpand?: (trigger: HTMLButtonElement) => void;
  onReference?: () => void;
  /** The reference action's label where 「在对话中引用」 would be ambiguous (a deck's whole file). */
  referenceLabel?: string;
  onDownload?: () => void;
  capabilities?: ReadonlySet<ResultCapability>;
  resultRole?: ArtifactResultRole;
  mode?: ResultsViewerMode;
  downloadLabel?: string;
  allowFileShare?: boolean;
  feedbackRecorded?: boolean;
  onViewerModeChange?: (mode: ResultsViewerMode) => void;
  onOpenHistory?: (trigger?: HTMLElement) => void;
  onFeedbackUnsupported?: () => void;
  onClose?: () => void;
  showTitle?: boolean;
  modalClose?: boolean;
  fullViewActive?: boolean;
  compactTitle?: React.ReactNode;
  compactActions?: React.ComponentProps<typeof ResultObjectActions>["compact"];
  onEditPresentation?: (trigger?: HTMLElement) => void;
}) {
  const t = useTranslations("artifact_viewer");
  const expandLabel = t(fullViewActive ? "close" : "open_full");
  const headRef = useRef<HTMLDivElement | null>(null);
  useToolbarFit(headRef, {
    steps: FILE_HEADER_FIT_STEPS,
    first: fileHeaderStacked,
    fits: fileHeaderFits,
    enabled: !compactActions,
  });
  const tools = onExpand || onClose ? (
    <div className="artifact-viewer-tools">
      {onExpand ? (
        <button type="button" aria-label={expandLabel} title={expandLabel} data-fit-key="full"
          onClick={(event) => onExpand(event.currentTarget)}>
          {fullViewActive ? <X size={13} aria-hidden="true" /> : <Maximize size={13} aria-hidden="true" />}
          {compactActions ? null : <span className="result-action-label">{expandLabel}</span>}
        </button>
      ) : null}
      {onClose ? (
        <button type="button" aria-label={t("close")} title={compactActions ? t("close") : undefined}
          onClick={onClose} data-dialog-initial-focus={modalClose || undefined}>
          <X size={13} aria-hidden="true" />
          {compactActions ? null : t("close")}
        </button>
      ) : null}
    </div>
  ) : null;
  return (
    <div ref={headRef} className={compactActions ? "artifact-viewer-head result-file-toolbar" : "artifact-viewer-head"}>
      {compactTitle ?? (
        <div className="artifact-viewer-title">
          {showTitle ? <strong title={filename}>{filename}</strong> : null}
          {!compactActions && meta ? <span>{meta}</span> : null}
        </div>
      )}
      <div className="artifact-viewer-actions">
        {compactActions ? tools : null}
        <div className="artifact-object-actions">
          {capabilities && resultRole ? (
            <ResultObjectActions
              artifactId={artifactId}
              capabilities={capabilities}
              role={resultRole}
              mode={mode}
              downloadHref={href}
              downloadLabel={downloadLabel}
              shareFilename={allowFileShare ? filename : undefined}
              feedbackRecorded={feedbackRecorded}
              compact={compactActions}
              onReference={onReference}
              referenceLabel={referenceLabel}
              onDownload={onDownload}
              onModeChange={onViewerModeChange}
              onOpenHistory={onOpenHistory}
              onFeedbackUnsupported={onFeedbackUnsupported}
              onEditPresentation={onEditPresentation}
            />
          ) : (
            <>
              {onReference ? (
                <button type="button" onClick={onReference}>
                  <Chat size={13} aria-hidden="true" />
                  {t("reference_in_chat")}
                </button>
              ) : null}
              <a href={href} target="_blank" rel="noreferrer" onClick={onDownload}>
                <FileDown size={13} aria-hidden="true" />
                {t("download")}
              </a>
            </>
          )}
        </div>
        {compactActions ? null : tools}
      </div>
    </div>
  );
}

function ArtifactPreviewBody({
  profile,
  codeLanguage,
  mode,
  preview,
  loading,
  error,
  onRetry,
  recoveryActions,
  href,
  pagesHref,
  inlineHref,
  alt,
  sizeBytes,
  expanded = false,
  safeHtml,
  compactHtml,
  video,
  imageView,
  onImageViewChange,
  textView,
  onTextViewChange,
  onOpenFailed,
  documentPreviewCacheRef,
  onReferencePage,
  referencedPages,
}: {
  profile: ArtifactViewerProfile;
  /** Syntax grammar derived from the file name/MIME for text previews. */
  codeLanguage: string | null;
  mode: ResultsViewerMode;
  preview: PreviewLike | null;
  loading: boolean;
  error: string | null;
  onRetry?: () => void;
  recoveryActions?: React.ReactNode;
  href: string;
  pagesHref?: string;
  inlineHref: string;
  alt: string;
  sizeBytes?: number | null;
  expanded?: boolean;
  safeHtml: {
    document: string | null;
    error: SafeHtmlErrorCode | null;
    loading: boolean;
    maxBytes: number | null;
    trust?: string | null;
    onReload: () => void;
    interactive?: InteractiveHtmlDocument;
    htmlMode?: HtmlPreviewMode;
    paused?: boolean;
    onPause: () => void;
    onModeChange: (mode: HtmlPreviewMode) => void;
    onEscape: () => void;
  } | null;
  compactHtml: boolean;
  video: {
    src: string;
    mime: string;
    onError?: (errorClass: ResultsWorkspaceErrorClass) => void;
    resumeState: VideoPlaybackState;
    onPlaybackStateChange: (state: VideoPlaybackState) => void;
  } | null;
  imageView: ImageViewState;
  onImageViewChange: React.Dispatch<React.SetStateAction<ImageViewState>>;
  textView: TextViewState;
  onTextViewChange: React.Dispatch<React.SetStateAction<TextViewState>>;
  onOpenFailed?: (errorClass: ResultsWorkspaceErrorClass) => void;
  documentPreviewCacheRef: React.MutableRefObject<DocumentPreviewCache>;
  onReferencePage?: (page: number) => import("@/lib/presentation-selection").SlideToggleOutcome;
  referencedPages?: ReadonlyArray<number>;
}) {
  const t = useTranslations("artifact_viewer");
  if (loading && !error) return <div className="artifact-viewer-note loading">{t("loading")}</div>;
  if (safeHtml) return <><SafeHtmlPreviewBody state={safeHtml} alt={alt} expanded={expanded} compact={compactHtml} />{safeHtml.error ? recoveryActions : null}</>;
  if (video) {
    return (
      <VideoArtifactPreview
        src={video.src}
        title={alt}
        mime={video.mime}
        expanded={expanded}
        onError={() => video.onError?.("corrupt")}
        resumeState={video.resumeState}
        onPlaybackStateChange={video.onPlaybackStateChange}
      />
    );
  }
  if (error) {
    return (
      <div className="artifact-viewer-note error" role="alert">
        <span>{error}</span>
        {onRetry ? (
          <button type="button" onClick={onRetry}>
            <RefreshCw size={13} aria-hidden="true" />
            {t("retry")}
          </button>
        ) : null}
        {recoveryActions}
      </div>
    );
  }

  const bodyClass = expanded ? "artifact-viewer-body expanded" : "artifact-viewer-body";
  if (mode === "preview" && profile === "image") {
    return (
      <div className={bodyClass}>
        <ArtifactImagePreview src={inlineHref} alt={alt} view={imageView} onViewChange={onImageViewChange} />
      </div>
    );
  }
  if (mode === "preview" && (profile === "pdf" || profile === "docx" || profile === "pptx")) {
    if (!preview && loading) {
      return <div className="artifact-viewer-note loading">{t("loading")}</div>;
    }
    return (
      <div className={bodyClass}>
        <ArtifactDocumentPreview
          profile={profile}
          href={href}
          pagesHref={pagesHref}
          title={alt}
          sizeBytes={profile === "pptx" ? undefined : sizeBytes}
          onError={onOpenFailed}
          cacheRef={documentPreviewCacheRef}
          thumbnails={profile === "pptx" && !expanded}
          onReferencePage={onReferencePage}
          referencedPages={referencedPages}
        />
      </div>
    );
  }
  if (!preview && loading) return <div className="artifact-viewer-note loading">{t("loading")}</div>;
  if (!preview) return <div className="artifact-viewer-note">{t("no_preview")}</div>;

  return (
    <div className={bodyClass}>
      {mode === "preview" && profile === "table" && preview.columns && preview.rows ? (
        <ArtifactRowsPreview preview={preview} />
      ) : mode === "preview" && profile === "xlsx" && preview.text ? (
        <ArtifactWorkbookPreview text={preview.text} />
      ) : preview.text != null ? (
        <ArtifactTextPreview
          text={preview.text}
          profile={profile}
          language={codeLanguage}
          raw={mode === "raw"}
          truncated={preview.truncated}
          view={textView}
          onViewChange={onTextViewChange}
        />
      ) : (
        <div className="artifact-viewer-note">{t("no_preview")}</div>
      )}
      {profile === "xlsx" ? <div className="artifact-viewer-footnote">{t("workbook_fidelity_hint")}</div> : null}
    </div>
  );
}

function ArtifactRecoveryActions({ mode, profile, capabilities, downloadHref, onDownload, onModeChange }: {
  mode: ResultsViewerMode; profile: ArtifactViewerProfile; capabilities?: ReadonlySet<ResultCapability>;
  downloadHref: string; onDownload?: () => void; onModeChange: (mode: ResultsViewerMode) => void;
}) {
  const t = useTranslations("results_workspace");
  const viewerT = useTranslations("artifact_viewer");
  const modes = viewerModesForProfile(profile);
  return <div className="artifact-recovery-actions">
    {mode === "preview" && modes.includes("raw") && (!capabilities || capabilities.has("view_raw"))
      ? <button type="button" onClick={() => onModeChange("raw")}>{t("mode_raw")}</button> : null}
    {(!capabilities || capabilities.has("download"))
      ? <a href={downloadHref} target="_blank" rel="noreferrer" onClick={onDownload}>{viewerT("download")}</a> : null}
    {mode !== "details" ? <button type="button" onClick={() => onModeChange("details")}>{t("mode_details")}</button> : null}
  </div>;
}

function SafeHtmlPreviewBody({
  state,
  alt,
  expanded,
  compact,
}: {
  state: {
    document: string | null;
    error: SafeHtmlErrorCode | null;
    loading: boolean;
    maxBytes: number | null;
    trust?: string | null;
    onReload: () => void;
    interactive?: InteractiveHtmlDocument;
    htmlMode?: HtmlPreviewMode;
    paused?: boolean;
    onPause: () => void;
    onModeChange: (mode: HtmlPreviewMode) => void;
    onEscape: () => void;
  };
  alt: string;
  expanded: boolean;
  compact: boolean;
}) {
  const t = useTranslations("artifact_viewer");
  const [issue, setIssue] = useState<HtmlPreviewIssue | null>(null);
  useEffect(() => setIssue(null), [state.document]);
  const message = state.error ? t(`safe_html_${state.error}`, { limit: formatByteSize(state.maxBytes ?? 0) }) : null;
  return (
    <div className={expanded ? "safe-html-preview expanded" : "safe-html-preview"}>
      {compact ? null : <div className="safe-html-preview-status">
        {state.htmlMode ? <HtmlPreviewModeControl value={state.htmlMode} onChange={state.onModeChange} />
          : <span className="safe-html-preview-badge">{t("safe_html_badge")}</span>}
        <span>{t(state.htmlMode === "interactive" ? "html_offline" : "safe_html_network_off")}</span>
        {state.htmlMode === "interactive" ? <button type="button" onClick={state.onPause}>{t(state.paused ? "html_restart" : "html_stop")}</button> : null}
        {state.trust === "unverified" ? (
          <span className="safe-html-preview-unverified">{t("safe_html_unverified")}</span>
        ) : null}
        <button type="button" onClick={state.onReload}>
          <RefreshCw size={13} aria-hidden="true" />
          {t("safe_html_reload")}
        </button>
      </div>}
      {state.paused ? <div role="status" className="artifact-viewer-note">{t("html_paused")}</div> : message ? (
        <div className="artifact-viewer-note error" role="alert">{message}</div>
      ) : state.loading || state.document === null ? (
        <div className="artifact-viewer-note loading">{t("safe_html_loading")}</div>
      ) : (
        <SafeHtmlFrame document={state.document} interactive={state.interactive} onEscape={state.onEscape} onIssue={setIssue} title={alt} className="safe-html-preview-frame" />
      )}
      <div className="safe-html-preview-hint">
        {compact ? (
          <>
            {!state.htmlMode ? <span className="safe-html-preview-badge">{t("safe_html_badge")}</span> : null}{" "}
            {t(state.htmlMode === "interactive" ? "html_offline" : "safe_html_network_off")}{" "}
            {state.trust === "unverified" ? (
              <span className="safe-html-preview-unverified">{t("safe_html_unverified")}</span>
            ) : null}{" "}
          </>
        ) : null}
        {t(state.htmlMode === "interactive" ? "html_interactive_hint" : "safe_html_hint")}
        {state.interactive?.externalResources ? ` ${t("html_external_blocked")}` : null}</div>
      {issue ? <div role="status" className="safe-html-preview-hint">{t(`html_issue_${issue}`)}</div> : null}
    </div>
  );
}

function withInlineDisposition(href: string): string {
  return `${href}${href.includes("?") ? "&" : "?"}disposition=inline`;
}
