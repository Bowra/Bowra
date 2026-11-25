"use client";

import { useId, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { useTranslations } from "next-intl";

import { Chat, Edit, FileDown, Folder, MoreH, Pin, RefreshCw } from "@/components/icons";
import { FileShareAction } from "@/components/share/file-share-action";
import { MenuFloating, MenuItemReason, useMenu, useMenuItem } from "@/components/ui/menu";
import { appPath } from "@/lib/base-path";
import type {
  ArtifactResultRole,
  ResultCapability,
} from "@/lib/results-workspace-model";
import type { ResultsViewerMode } from "@/lib/results-workspace-route";
import { useResultPinActions } from "@/lib/use-result-pin-actions";
import type { HtmlPreviewMode } from "./html-preview-mode";

export type ResultReturnAction = {
  label: string;
  ariaLabel?: string;
  onSelect: () => void;
};

export type CompactResultToolbar = {
  title: ReactNode;
  returnActions: readonly ResultReturnAction[];
};

type CompactResultActions = {
  modes: readonly ResultsViewerMode[];
  returns: readonly ResultReturnAction[];
  html?: {
    mode?: HtmlPreviewMode;
    paused?: boolean;
    onPause: () => void;
    onModeChange: (mode: HtmlPreviewMode) => void;
    onReload: () => void;
  } | null;
};

export function ResultObjectActions({
  artifactId,
  capabilities,
  role,
  mode,
  downloadHref,
  downloadLabel,
  shareFilename,
  feedbackRecorded = false,
  compact,
  onReference,
  referenceLabel,
  onDownload,
  onModeChange,
  onOpenHistory,
  onFeedbackUnsupported,
  onEditPresentation,
}: {
  artifactId: string;
  capabilities: ReadonlySet<ResultCapability>;
  role: ArtifactResultRole;
  mode: ResultsViewerMode;
  downloadHref: string;
  downloadLabel?: string;
  shareFilename?: string;
  feedbackRecorded?: boolean;
  compact?: CompactResultActions;
  onReference?: () => void;
  /** Replaces 「在对话中引用」 where it would be ambiguous, such as a deck's whole file. */
  referenceLabel?: string;
  onDownload?: () => void;
  onModeChange?: (mode: ResultsViewerMode) => void;
  onOpenHistory?: (trigger?: HTMLElement) => void;
  onFeedbackUnsupported?: () => void;
  /** Open the deck editor (pptx-inline-edit design §7.1); on phones an icon button beside More actions. */
  onEditPresentation?: (trigger?: HTMLElement) => void;
}) {
  const t = useTranslations("artifact_viewer");
  const shareT = useTranslations("share");
  const resultsT = useTranslations("results_workspace");
  const canReference = capabilities.has("reference_in_chat") && Boolean(onReference);
  const canDownload = capabilities.has("download");
  const downloadIsPrimary = !compact && (
    role === "deliverable" || role === "unsupported" || !canReference
  );
  const downloadInMenu = !downloadIsPrimary && canDownload;
  const canManage = capabilities.has("manage_in_files");
  const canViewRaw = capabilities.has("view_raw") && Boolean(onModeChange);
  const canViewDetails = capabilities.has("view_details") && Boolean(onModeChange);
  const canViewHistory = capabilities.has("view_history") && Boolean(onOpenHistory);
  const canSendFeedback = capabilities.has("feedback_unsupported") && Boolean(onFeedbackUnsupported);
  const canEditPresentation = capabilities.has("edit_presentation") && Boolean(onEditPresentation);
  const canPin = capabilities.has("pin_result");
  const pinActions = useResultPinActions("results", { enabled: canPin });
  const showRawAction = !compact && canViewRaw && mode !== "raw";
  const showDetailsAction = !compact && canViewDetails && mode !== "details";
  const [menuOpen, setMenuOpen] = useState(false);
  const menu = useMenu({
    open: menuOpen,
    onOpenChange: setMenuOpen,
    placement: "bottom-end",
    offset: 5,
    presentation: compact ? "sheet" : undefined,
    sheetTitle: compact ? t("more_actions") : undefined,
  });

  const shareTrigger = useRef<HTMLElement | null>(null);
  const getCompactTrigger = () => {
    const trigger = menu.refs.domReference.current;
    return compact && trigger instanceof HTMLElement ? trigger : undefined;
  };
  const modes = compact?.modes.filter((value) => (
    value === "raw" ? canViewRaw : value === "details" ? canViewDetails : Boolean(onModeChange)
  )) ?? [];
  const html = compact?.html;
  const returns = compact?.returns ?? [];
  const menuLabel = t("more_actions");
  const download = downloadLabel ?? t("download");

  const renderActions = (openShare?: () => void) => {
    const hasFileActions = downloadInMenu || canManage || canPin || canViewHistory || canSendFeedback || Boolean(openShare);
    const hasMenuActions = hasFileActions || modes.length > 0 || returns.length > 0 || html || showRawAction || showDetailsAction;
    return (
      <>
        {canEditPresentation && compact ? (
          <button type="button" className="result-object-actions-trigger" aria-label={t("edit_presentation")} title={t("edit_presentation")}
            onClick={(event) => onEditPresentation?.(event.currentTarget)}>
            <Edit size={15} aria-hidden="true" />
          </button>
        ) : null}
        {canEditPresentation && !compact ? (
          <button type="button" className="result-object-action-primary" data-fit-key="edit" title={t("edit_presentation")}
            onClick={(event) => onEditPresentation?.(event.currentTarget)}>
            <Edit size={13} aria-hidden="true" />
            <span className="result-action-label">{t("edit_presentation")}</span>
          </button>
        ) : null}
        {canReference && !compact ? (
          <button type="button" className="result-object-action-primary" data-fit-key="reference"
            title={referenceLabel ?? t("reference_in_chat")} onClick={onReference}>
            <Chat size={13} aria-hidden="true" />
            <span className="result-action-label">{referenceLabel ?? t("reference_in_chat")}</span>
          </button>
        ) : null}
        {downloadIsPrimary && canDownload ? (
          <a
            className="result-object-action-primary"
            href={downloadHref}
            target="_blank"
            rel="noreferrer"
            onClick={onDownload}
          >
            <FileDown size={13} aria-hidden="true" />
            {download}
          </a>
        ) : null}
        {!compact && shareFilename ? <FileShareAction artifactId={artifactId} filename={shareFilename} /> : null}
        {hasMenuActions || (compact && canReference) ? (
          <div className="result-object-actions-menu">
            <button
              type="button"
              ref={menu.refs.setReference}
              className="result-object-actions-trigger"
              aria-label={menuLabel}
              title={menuLabel}
              data-fit-key="more"
              {...menu.getReferenceProps()}
            >
              <MoreH size={15} aria-hidden="true" />
              {compact ? null : <span className="result-action-label">{menuLabel}</span>}
            </button>
            <MenuFloating menu={menu}>
              <div
                ref={menu.refs.setFloating}
                {...menu.getFloatingProps({
                  className: compact ? "result-object-actions-pop result-object-actions-pop--compact" : "result-object-actions-pop",
                  "aria-label": menuLabel,
                })}
              >
                {returns.map((action) => (
                  <ResultMenuButton key={action.label} ariaLabel={action.ariaLabel} onSelect={action.onSelect}>
                    {action.label}
                  </ResultMenuButton>
                ))}
                {returns.length && (hasFileActions || canReference || modes.length || html)
                  ? <div role="separator" className="ui-row-menu-sep" /> : null}
                {compact && canReference && onReference ? (
                  <ResultMenuButton onSelect={onReference}>
                    <Chat size={13} aria-hidden="true" />
                    {referenceLabel ?? t("reference_in_chat")}
                  </ResultMenuButton>
                ) : null}
                {downloadInMenu ? (
                  <ResultMenuLink href={downloadHref} onSelect={onDownload}>
                    <FileDown size={13} aria-hidden="true" />
                    {download}
                  </ResultMenuLink>
                ) : null}
                {openShare ? (
                  <ResultMenuButton onSelect={() => {
                    shareTrigger.current = getCompactTrigger() ?? null;
                    openShare();
                  }}>{shareT("header_eyebrow")}</ResultMenuButton>
                ) : null}
                {canManage ? (
                  <ResultMenuLink href={appPath(`/files/?focus=${encodeURIComponent(artifactId)}`)}>
                    <Folder size={13} aria-hidden="true" />
                    {t("manage_in_files")}
                  </ResultMenuLink>
                ) : null}
                {canPin ? (
                  <ResultMenuButton
                    disabled={pinActions.isBusy("file", artifactId)}
                    onSelect={() => void pinActions.toggle("file", artifactId)}
                  >
                    <Pin size={13} aria-hidden="true" fill={pinActions.isPinned("file", artifactId) ? "currentColor" : "none"} />
                    {pinActions.label("file", artifactId)}
                  </ResultMenuButton>
                ) : null}
                {showRawAction ? (
                  <ResultMenuButton onSelect={() => onModeChange?.("raw")}>{t("view_raw")}</ResultMenuButton>
                ) : null}
                {showDetailsAction ? (
                  <ResultMenuButton onSelect={() => onModeChange?.("details")}>{t("view_details")}</ResultMenuButton>
                ) : null}
                {canViewHistory && onOpenHistory ? (
                  <ResultMenuButton onSelect={() => onOpenHistory(getCompactTrigger())}>{t("version_history")}</ResultMenuButton>
                ) : null}
                {canSendFeedback && onFeedbackUnsupported ? (
                  <ResultMenuButton
                    disabled={feedbackRecorded}
                    reason={feedbackRecorded ? t("feedback_recorded_reason") : undefined}
                    onSelect={onFeedbackUnsupported}
                  >
                    {t("feedback_unsupported")}
                  </ResultMenuButton>
                ) : null}
                {modes.length > 0 && (hasFileActions || canReference)
                  ? <div role="separator" className="ui-row-menu-sep" /> : null}
                {modes.map((value) => (
                  <ResultMenuButton key={value} checked={mode === value} onSelect={() => {
                    if (value !== mode) onModeChange?.(value);
                  }}>
                    {resultsT(`mode_${value}`)}
                  </ResultMenuButton>
                ))}
                {html ? (
                  <>
                    {hasFileActions || canReference || modes.length > 0
                      ? <div role="separator" className="ui-row-menu-sep" /> : null}
                    {html.mode ? (
                      <>
                        <ResultMenuButton checked={html.mode === "interactive"} onSelect={() => {
                          if (html.mode !== "interactive") html.onModeChange("interactive");
                        }}>{t("html_interactive")}</ResultMenuButton>
                        <ResultMenuButton checked={html.mode === "static"} onSelect={() => {
                          if (html.mode !== "static") html.onModeChange("static");
                        }}>{t("html_reading")}</ResultMenuButton>
                      </>
                    ) : null}
                    {html.mode === "interactive" ? (
                      <ResultMenuButton onSelect={html.onPause}>{t(html.paused ? "html_restart" : "html_stop")}</ResultMenuButton>
                    ) : null}
                    <ResultMenuButton onSelect={html.onReload}>
                      <RefreshCw size={13} aria-hidden="true" />
                      {t("safe_html_reload")}
                    </ResultMenuButton>
                  </>
                ) : null}
              </div>
            </MenuFloating>
          </div>
        ) : null}
      </>
    );
  };

  return compact && shareFilename
    ? <FileShareAction artifactId={artifactId} filename={shareFilename} renderTrigger={renderActions} returnFocusRef={shareTrigger} />
    : renderActions();
}

function ResultMenuLink({
  href,
  onSelect,
  children,
}: {
  href: string;
  onSelect?: () => void;
  children: ReactNode;
}) {
  const itemProps = useMenuItem({ onSelect: onSelect ? () => onSelect() : undefined });
  return (
    <a href={href} target="_blank" rel="noreferrer" {...itemProps}>
      {children}
    </a>
  );
}

function ResultMenuButton({
  disabled = false,
  reason,
  ariaLabel,
  checked,
  onSelect,
  children,
}: {
  disabled?: boolean;
  /** Disabled reason: always on line two, read via aria-describedby. */
  reason?: string;
  ariaLabel?: string;
  checked?: boolean;
  onSelect: (event: MouseEvent<HTMLElement>) => void;
  children: ReactNode;
}) {
  const itemProps = useMenuItem({
    onSelect, disabled, role: checked === undefined ? undefined : "menuitemradio", checked,
    label: typeof children === "string" ? children : undefined,
  });
  // Disabled items omit native disabled, remaining keyboard-highlightable so their reasons can be read.
  const reasonId = useId();
  return (
    <>
      <button
        type="button"
        aria-label={ariaLabel}
        aria-describedby={reason ? reasonId : undefined}
        {...itemProps}
      >
        {children}
      </button>
      {reason ? <MenuItemReason id={reasonId}>{reason}</MenuItemReason> : null}
    </>
  );
}
