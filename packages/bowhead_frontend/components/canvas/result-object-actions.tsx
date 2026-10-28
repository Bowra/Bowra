"use client";

import { useId, useState, type MouseEvent, type ReactNode } from "react";
import { useTranslations } from "next-intl";

import { Chat, FileDown, Folder, MoreH } from "@/components/icons";
import { MenuFloating, MenuItemReason, useMenu, useMenuItem } from "@/components/ui/menu";
import { appPath } from "@/lib/base-path";
import type {
  ArtifactResultRole,
  ResultCapability,
} from "@/lib/results-workspace-model";
import type { ResultsViewerMode } from "@/lib/results-workspace-route";

export function ResultObjectActions({
  artifactId,
  capabilities,
  role,
  mode,
  downloadHref,
  downloadLabel,
  feedbackRecorded = false,
  onReference,
  onDownload,
  onModeChange,
  onOpenHistory,
  onFeedbackUnsupported,
}: {
  artifactId: string;
  capabilities: ReadonlySet<ResultCapability>;
  role: ArtifactResultRole;
  mode: ResultsViewerMode;
  downloadHref: string;
  downloadLabel?: string;
  feedbackRecorded?: boolean;
  onReference?: () => void;
  onDownload?: () => void;
  onModeChange?: (mode: ResultsViewerMode) => void;
  onOpenHistory?: () => void;
  onFeedbackUnsupported?: () => void;
}) {
  const t = useTranslations("artifact_viewer");
  const canReference = capabilities.has("reference_in_chat") && Boolean(onReference);
  const canDownload = capabilities.has("download");
  const downloadIsPrimary = (
    role === "deliverable"
    || role === "unsupported"
    || !canReference
  );
  const menuActions = [
    !downloadIsPrimary && canDownload ? "download" : null,
    capabilities.has("manage_in_files") ? "files" : null,
    capabilities.has("view_raw") && mode !== "raw" && onModeChange ? "raw" : null,
    capabilities.has("view_details") && mode !== "details" && onModeChange ? "details" : null,
    capabilities.has("view_history") && onOpenHistory ? "history" : null,
    capabilities.has("feedback_unsupported") && onFeedbackUnsupported ? "feedback" : null,
  ].filter(Boolean);
  const [menuOpen, setMenuOpen] = useState(false);
  // useMenu handles keyboard, Esc, outside clicks and focus return (component consolidation step 4); mount menu on body,
  // right-aligned below ⋯; shared positioning clamps it within the visible region on narrow screens.
  const menu = useMenu({ open: menuOpen, onOpenChange: setMenuOpen, placement: "bottom-end", offset: 5 });

  return (
    <>
      {canReference ? (
        <button type="button" className="result-object-action-primary" onClick={onReference}>
          <Chat size={13} aria-hidden="true" />
          {t("reference_in_chat")}
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
          {downloadLabel ?? t("download")}
        </a>
      ) : null}
      {menuActions.length > 0 ? (
        <div className="result-object-actions-menu">
          <button
            type="button"
            ref={menu.refs.setReference}
            className="result-object-actions-trigger"
            aria-label={t("more_actions")}
            title={t("more_actions")}
            {...menu.getReferenceProps()}
          >
            <MoreH size={15} aria-hidden="true" />
            <span>{t("more_actions")}</span>
          </button>
          <MenuFloating menu={menu}>
            <div
              ref={menu.refs.setFloating}
              {...menu.getFloatingProps({ className: "result-object-actions-pop", "aria-label": t("more_actions") })}
            >
              {!downloadIsPrimary && canDownload ? (
                <ResultMenuLink href={downloadHref} onSelect={onDownload}>
                  <FileDown size={13} aria-hidden="true" />
                  {downloadLabel ?? t("download")}
                </ResultMenuLink>
              ) : null}
              {capabilities.has("manage_in_files") ? (
                <ResultMenuLink href={appPath(`/files/?focus=${encodeURIComponent(artifactId)}`)}>
                  <Folder size={13} aria-hidden="true" />
                  {t("manage_in_files")}
                </ResultMenuLink>
              ) : null}
              {capabilities.has("view_raw") && mode !== "raw" && onModeChange ? (
                <ResultMenuButton onSelect={() => onModeChange("raw")}>{t("view_raw")}</ResultMenuButton>
              ) : null}
              {capabilities.has("view_details") && mode !== "details" && onModeChange ? (
                <ResultMenuButton onSelect={() => onModeChange("details")}>{t("view_details")}</ResultMenuButton>
              ) : null}
              {capabilities.has("view_history") && onOpenHistory ? (
                <ResultMenuButton onSelect={onOpenHistory}>{t("version_history")}</ResultMenuButton>
              ) : null}
              {capabilities.has("feedback_unsupported") && onFeedbackUnsupported ? (
                <ResultMenuButton
                  disabled={feedbackRecorded}
                  // Behavior batch A: disabled items retain action names with reasons on line two.
                  reason={feedbackRecorded ? t("feedback_recorded_reason") : undefined}
                  onSelect={onFeedbackUnsupported}
                >
                  {t("feedback_unsupported")}
                </ResultMenuButton>
              ) : null}
            </div>
          </MenuFloating>
        </div>
      ) : null}
    </>
  );
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
  onSelect,
  children,
}: {
  disabled?: boolean;
  /** Disabled reason: always on line two, read via aria-describedby. */
  reason?: string;
  onSelect: (event: MouseEvent<HTMLElement>) => void;
  children: ReactNode;
}) {
  const itemProps = useMenuItem({ onSelect, disabled });
  // Disabled items omit native disabled, remaining keyboard-highlightable so their reasons can be read.
  const reasonId = useId();
  return (
    <>
      <button
        type="button"
        aria-describedby={reason ? reasonId : undefined}
        {...itemProps}
      >
        {children}
      </button>
      {reason ? <MenuItemReason id={reasonId}>{reason}</MenuItemReason> : null}
    </>
  );
}
