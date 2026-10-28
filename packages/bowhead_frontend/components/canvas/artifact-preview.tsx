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
import type { ResultsWorkspaceErrorClass } from "@/lib/results-workspace-errors";
import type { ResultsViewerMode } from "@/lib/results-workspace-route";
import type {
  ArtifactResultRole,
  ResultCapability,
} from "@/lib/results-workspace-model";

import {
  ArtifactImagePreview,
  ArtifactPresentationPreview,
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
import { ResultObjectActions } from "./result-object-actions";

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
  onOpenFailed,
  onClose,
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
  onReference?: () => void;
  onDownload?: () => void;
  capabilities?: ReadonlySet<ResultCapability>;
  resultRole?: ArtifactResultRole;
  downloadLabel?: string;
  feedbackRecorded?: boolean;
  onViewerModeChange?: (mode: ResultsViewerMode) => void;
  onOpenHistory?: () => void;
  onFeedbackUnsupported?: () => void;
  onRetry?: () => void;
  onOpenFailed?: (errorClass: ResultsWorkspaceErrorClass) => void;
  onClose?: () => void;
}) {
  const t = useTranslations("artifact_viewer");
  const href = downloadHref ?? appPath(`/api/v1/files/${encodeURIComponent(fileId)}`);
  const rendererHref = contentHref ?? href;
  const profile = profileProp ?? deriveArtifactViewerProfile(mime, filename);
  const [expanded, setExpanded] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);
  const [imageView, setImageView] = useState<ImageViewState>(DEFAULT_IMAGE_VIEW_STATE);
  const [textView, setTextView] = useState<TextViewState>(DEFAULT_TEXT_VIEW_STATE);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const documentPreviewCacheRef = useRef<DocumentPreviewCache>({});
  const videoPlaybackRef = useRef<VideoPlaybackState>({
    currentTime: 0,
    volume: 1,
    muted: false,
    playbackRate: 1,
    started: false,
  });
  const safeHtmlActive = mode === "preview" && isSafeHtmlMime(mime) && !loading && !error;
  const videoActive = mode === "preview" && isVideoPreviewMime(mime) && !loading && !error;
  const inlineHref = withInlineDisposition(href);
  const safeHtml = useSafeHtmlDocument({
    enabled: safeHtmlActive,
    href: rendererHref,
    declaredSize: sizeBytes,
    reloadToken,
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

  useEffect(() => {
    if (!expanded) return;
    // Return to the Fullscreen View trigger: Safari does not focus clicked buttons, so DialogShell's recorded pre-open focus may be elsewhere.
    return () => returnFocusRef.current?.focus();
  }, [expanded]);

  const meta = [
    mime,
    sizeLabel,
    safeHtmlActive
      ? t("capability_safe_html")
      : videoActive
        ? t("capability_video")
        : t(`capability_${profile}`),
  ].filter(Boolean).join(" · ");
  const safeHtmlBody = safeHtmlActive
    ? {
        ...safeHtml,
        trust,
        onReload: () => setReloadToken((value) => value + 1),
      }
    : null;
  const bodyProps = {
    profile,
    mode,
    preview,
    loading,
    error,
    onRetry,
    recoveryActions: onViewerModeChange ? (
      <ArtifactRecoveryActions mode={mode} profile={profile} capabilities={capabilities}
        downloadHref={href} onDownload={onDownload} onModeChange={onViewerModeChange} />
    ) : null,
    href: rendererHref,
    inlineHref,
    alt: filename,
    sizeBytes,
    safeHtml: safeHtmlBody,
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

  return (
    <>
      <div className="artifact-viewer">
        <ArtifactPreviewHeader
          artifactId={fileId}
          filename={filename}
          meta={meta}
          href={href}
          onExpand={(trigger) => {
            returnFocusRef.current = trigger;
            setExpanded(true);
          }}
          onReference={onReference}
          onDownload={onDownload}
          capabilities={capabilities}
          resultRole={resultRole}
          mode={mode}
          downloadLabel={downloadLabel}
          feedbackRecorded={feedbackRecorded}
          onViewerModeChange={onViewerModeChange}
          onOpenHistory={onOpenHistory}
          onFeedbackUnsupported={onFeedbackUnsupported}
          onClose={onClose}
          showTitle={showHeaderTitle}
        />
        {!expanded ? <ArtifactPreviewBody {...bodyProps} /> : null}
      </div>
      {expanded ? (
        <DialogShell
          presentation="center"
          onClose={() => setExpanded(false)}
          label={filename}
          closeOnBackdrop={false}
          backdropStyle={{ zIndex: 240 }}
          panelClassName="artifact-viewer-modal"
        >
          <div className="artifact-viewer-modal-panel" tabIndex={-1}>
            <ArtifactPreviewHeader
              artifactId={fileId}
              filename={filename}
              meta={meta}
              href={href}
              onReference={onReference
                ? () => {
                    setExpanded(false);
                    onReference();
                  }
                : undefined}
              onDownload={onDownload}
              capabilities={capabilities}
              resultRole={resultRole}
              mode={mode}
              downloadLabel={downloadLabel}
              feedbackRecorded={feedbackRecorded}
              onViewerModeChange={onViewerModeChange}
              onOpenHistory={onOpenHistory}
              onFeedbackUnsupported={onFeedbackUnsupported}
              onClose={() => setExpanded(false)}
              modalClose
            />
            <div className="artifact-viewer-modal-body">
              <ArtifactPreviewBody {...bodyProps} expanded />
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
  feedbackRecorded,
  onViewerModeChange,
  onOpenHistory,
  onFeedbackUnsupported,
  onClose,
  showTitle = true,
  modalClose = false,
}: {
  artifactId: string;
  filename: string;
  meta: string;
  href: string;
  onExpand?: (trigger: HTMLButtonElement) => void;
  onReference?: () => void;
  onDownload?: () => void;
  capabilities?: ReadonlySet<ResultCapability>;
  resultRole?: ArtifactResultRole;
  mode?: ResultsViewerMode;
  downloadLabel?: string;
  feedbackRecorded?: boolean;
  onViewerModeChange?: (mode: ResultsViewerMode) => void;
  onOpenHistory?: () => void;
  onFeedbackUnsupported?: () => void;
  onClose?: () => void;
  showTitle?: boolean;
  modalClose?: boolean;
}) {
  const t = useTranslations("artifact_viewer");
  return (
    <div className="artifact-viewer-head">
      <div className="artifact-viewer-title">
        {showTitle ? <strong title={filename}>{filename}</strong> : null}
        {meta ? <span>{meta}</span> : null}
      </div>
      <div className="artifact-viewer-actions">
        <div className="artifact-object-actions">
          {capabilities && resultRole ? (
            <ResultObjectActions
              artifactId={artifactId}
              capabilities={capabilities}
              role={resultRole}
              mode={mode}
              downloadHref={href}
              downloadLabel={downloadLabel}
              feedbackRecorded={feedbackRecorded}
              onReference={onReference}
              onDownload={onDownload}
              onModeChange={onViewerModeChange}
              onOpenHistory={onOpenHistory}
              onFeedbackUnsupported={onFeedbackUnsupported}
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
        {onExpand || onClose ? (
          <div className="artifact-viewer-tools">
            {onExpand ? (
              <button type="button" onClick={(event) => onExpand(event.currentTarget)}>
                <Maximize size={13} aria-hidden="true" />
                {t("open_full")}
              </button>
            ) : null}
            {onClose ? (
              <button type="button" onClick={onClose} data-dialog-initial-focus={modalClose || undefined}>
                <X size={13} aria-hidden="true" />
                {t("close")}
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function ArtifactPreviewBody({
  profile,
  mode,
  preview,
  loading,
  error,
  onRetry,
  recoveryActions,
  href,
  inlineHref,
  alt,
  sizeBytes,
  expanded = false,
  safeHtml,
  video,
  imageView,
  onImageViewChange,
  textView,
  onTextViewChange,
  onOpenFailed,
  documentPreviewCacheRef,
}: {
  profile: ArtifactViewerProfile;
  mode: ResultsViewerMode;
  preview: PreviewLike | null;
  loading: boolean;
  error: string | null;
  onRetry?: () => void;
  recoveryActions?: React.ReactNode;
  href: string;
  inlineHref: string;
  alt: string;
  sizeBytes?: number | null;
  expanded?: boolean;
  safeHtml: {
    document: string | null;
    error: SafeHtmlErrorCode | null;
    loading: boolean;
    trust?: string | null;
    onReload: () => void;
  } | null;
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
}) {
  const t = useTranslations("artifact_viewer");
  if (loading && !error) return <div className="artifact-viewer-note loading">{t("loading")}</div>;
  if (safeHtml) return <><SafeHtmlPreviewBody state={safeHtml} alt={alt} expanded={expanded} />{safeHtml.error ? recoveryActions : null}</>;
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
  if (mode === "preview" && (profile === "pdf" || profile === "docx")) {
    if (!preview && loading) {
      return <div className="artifact-viewer-note loading">{t("loading")}</div>;
    }
    return (
      <div className={bodyClass}>
        <ArtifactDocumentPreview
          profile={profile}
          href={href}
          title={alt}
          sizeBytes={sizeBytes}
          onError={onOpenFailed}
          cacheRef={documentPreviewCacheRef}
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
      ) : mode === "preview" && profile === "pptx" && preview.text ? (
        <ArtifactPresentationPreview text={preview.text} />
      ) : mode === "preview" && profile === "xlsx" && preview.text ? (
        <ArtifactWorkbookPreview text={preview.text} />
      ) : preview.text != null ? (
        <ArtifactTextPreview
          text={preview.text}
          profile={profile}
          raw={mode === "raw"}
          truncated={preview.truncated}
          view={textView}
          onViewChange={onTextViewChange}
        />
      ) : (
        <div className="artifact-viewer-note">{t("no_preview")}</div>
      )}
      {profile === "pptx" ? <div className="artifact-viewer-footnote">{t("presentation_fidelity_hint")}</div> : null}
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
}: {
  state: {
    document: string | null;
    error: SafeHtmlErrorCode | null;
    loading: boolean;
    trust?: string | null;
    onReload: () => void;
  };
  alt: string;
  expanded: boolean;
}) {
  const t = useTranslations("artifact_viewer");
  const message = state.error ? t(`safe_html_${state.error}`) : null;
  return (
    <div className={expanded ? "safe-html-preview expanded" : "safe-html-preview"}>
      <div className="safe-html-preview-status">
        <span className="safe-html-preview-badge">{t("safe_html_badge")}</span>
        <span>{t("safe_html_network_off")}</span>
        {state.trust === "unverified" ? (
          <span className="safe-html-preview-unverified">{t("safe_html_unverified")}</span>
        ) : null}
        <button type="button" onClick={state.onReload}>
          <RefreshCw size={13} aria-hidden="true" />
          {t("safe_html_reload")}
        </button>
      </div>
      {message ? (
        <div className="artifact-viewer-note error" role="alert">{message}</div>
      ) : state.loading || state.document === null ? (
        <div className="artifact-viewer-note loading">{t("safe_html_loading")}</div>
      ) : (
        <SafeHtmlFrame document={state.document} title={alt} className="safe-html-preview-frame" />
      )}
      <div className="safe-html-preview-hint">{t("safe_html_hint")}</div>
    </div>
  );
}

function withInlineDisposition(href: string): string {
  return `${href}${href.includes("?") ? "&" : "?"}disposition=inline`;
}
