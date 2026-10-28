"use client";

import clsx from "clsx";
import { Check, ChevronDown, CircleAlert, Download, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState, type CSSProperties } from "react";

import { LucideGlyph } from "@/components/icon-refresh";
import { ChatCardSpinner } from "@/components/ui/card";
import { MenuFloating, useMenu, useMenuItem } from "@/components/ui/menu";
import { fileExtensionLabel, fileTypeIcon } from "@/lib/file-type-icon";
import { useFileDownload, type FileDownloadStatus } from "@/lib/use-file-download";
import { chartSelectedScopeLabel } from "@/lib/chart/selected-context";
import type { AttachmentRef } from "@/lib/types";

// Attachment chip (Chat Cards Spec §5, D10/D11): 32 high, --r-2, a 16px line icon for the file type (no
// coloured "PPT / XLS" blocks), the name (ellipsised first, max 300), one meta fact per state that never
// wraps, 28px inner buttons (44 on touch), and a 2px progress bar. Bundle contents open in the shared menu
// (bottom sheet on phones), failures first. Styles: styles/ui-refresh-cards.css (.v2-chip*).
//
// Status states tightened: uploading / confirming / indexing / done / error.
// errorKind splits the error state into terminal (red, × only) vs recoverable
// (amber, retry pill).

export type AttachmentStatus =
  | "uploading"
  | "confirming"
  | "indexing"
  | "done"
  | "error";

export type AttachmentErrorKind =
  | "size"          // terminal — file too large
  | "unsupported"   // terminal — bad mime
  | "auth"          // recoverable — presign expired / sign-in
  | "network"      // recoverable — TCP / 5xx
  | "other";

export interface BundleChild {
  filename: string;
  size: number;
  // Phase 3-followup review P1-1: failed status surfaced as 4th dot state.
  // 0.40.4 Bug 10 side branch: "skipped" added — entries the backend rejected at
  // unzip-time (unsupported mime / oversize / etc) BEFORE indexing started.
  // Distinct from "failed" (parse failed mid-pipeline) so the popover dot
  // signals "intentionally not indexed" vs "errored during indexing".
  status: "done" | "busy" | "queued" | "failed" | "skipped";
  /** Why the server skipped this entry (`kb_bundle_entry_*`), shown in the bundle contents. */
  reasonCode?: string;
}

/** Skipped bundle entries: the server's finite reason codes, worded like the knowledge-base upload result. */
const BUNDLE_SKIP_REASON_KEYS: Readonly<Record<string, string>> = {
  kb_bundle_entry_metadata: "bundle_skip_reason.metadata",
  kb_bundle_entry_empty: "bundle_skip_reason.empty",
  kb_bundle_entry_unreadable: "bundle_skip_reason.unreadable",
  kb_bundle_entry_too_large: "bundle_skip_reason.too_large",
  kb_bundle_entry_unsafe: "bundle_skip_reason.unsafe",
  kb_bundle_entry_type_unsupported: "bundle_skip_reason.type_unsupported",
  kb_bundle_entry_ingest_failed: "bundle_skip_reason.ingest_failed",
  kb_bundle_entry_capability_component: "bundle_skip_reason.capability_component",
};

export interface AttachmentItem extends Partial<AttachmentRef> {
  /** Local id (uuid) — track chip until upload settles */
  localId: string;
  filename: string;
  size: number;
  mime: string;
  status: AttachmentStatus;
  /** `File.lastModified` of a local pick: duplicate detection compares it with name and size (D10). */
  lastModified?: number;
  /** Single-file vs zip-bundle marker. Determines badge + indexing semantic. */
  kind?: "file" | "bundle";
  /** User-facing (localized) error message when status === 'error' — never raw XHR/HTTP prose. */
  error?: string;
  /** Raw failure (status, code, request id) — only for「复制诊断」, never rendered as text. */
  errorCause?: unknown;
  /** Sub-classification for visual style + retry affordance */
  errorKind?: AttachmentErrorKind;
  /** Upload progress 0..100 — populated during xhrPostFormWithProgress */
  progressPct?: number;
  /** Bundle indexing — N of M child docs processed */
  indexedCount?: number;
  totalCount?: number;
  /** Bundle child file_ids surfaced to orchestrator (tech-design §7.2 P0-C-4) */
  kbFileIds?: string[];
  /** Per-child status rows shown in disclosure popover */
  bundleChildren?: BundleChild[];
  /**
   * chat-file-download #2 — when set (status="done"), the chip gains a
   * download affordance sharing `ChatInlineFile`'s state machine (downloading
   * rail → done check → error retry). Read-only chips (no `onRemove`, e.g. the
   * sent-message attachment strip) make the WHOLE card the click target; chips
   * that also have `onRemove` (e.g. the pinned-attachments strip) get a
   * dedicated small download button instead — never nesting a real `<button>`
   * inside a `role="button"` card. Caller must pre-wrap with `appPath()` (same
   * convention as `inline-artifacts.ts`) — this component does not know about
   * deployment path prefixing.
   */
  downloadUrl?: string;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

interface Props {
  item: AttachmentItem;
  /** Disable composer-side mutations while keeping safe read/download affordances available. */
  mutationDisabled?: boolean;
  /** Results Workspace: open this exact artifact instead of downloading it. */
  onOpen?: () => void;
  /** Accessible name for the whole-card open target. */
  openLabel?: string;
  /** undefined → read-only (history bubble); else show × dismiss */
  onRemove?: (localId: string) => void;
  /** Cancel in-flight upload (status='uploading'|'confirming'|'indexing') */
  onCancel?: (localId: string) => void;
  /** Re-trigger upload after recoverable error (auth/network) */
  onRetry?: (localId: string) => void;
  /** Override the × button's aria-label/title once upload has settled — e.g.
   * "Unpin" for the pinned-attachments strip vs the composer's default "Dismiss".
   * Ignored while status is uploading/confirming/indexing (always "Cancel"). */
  removeLabel?: string;
  /** The same file was added again: outline the existing chip once (600ms) instead of adding a second chip. */
  flash?: boolean;
}

/** More than this many indexed bundle files collapse into "N more indexed". */
const BUNDLE_INDEXED_VISIBLE_MAX = 6;
const BUNDLE_INDEXED_COLLAPSED_SHOWN = 3;

export function AttachmentChip({
  item,
  mutationDisabled = false,
  onRemove,
  onCancel,
  onRetry,
  onOpen,
  openLabel,
  removeLabel,
  flash = false,
}: Props) {
  const t = useTranslations("attachment");
  const scopeLabel = item.selectedContext ? chartSelectedScopeLabel(item.selectedContext) : null;
  const kind: "file" | "bundle" = item.kind ?? "file";
  const [menuOpen, setMenuOpen] = useState(false);
  const bundleMenu = useMenu({
    open: menuOpen,
    onOpenChange: setMenuOpen,
    placement: "bottom-start",
    offset: 4,
    // D11: bundle contents open as a bottom sheet on phones (the shared menu's narrow breakpoint).
    presentation: "sheet",
  });

  // chat-file-download #2: a finished chip with a downloadUrl is a download trigger, sharing ChatInlineFile's
  // state machine. The hook is always called (rules of hooks) but only listened to when downloadable.
  const downloadable = item.status === "done" && !!item.downloadUrl;
  const dl = useFileDownload({
    downloadUrl: item.downloadUrl ?? "",
    filename: item.filename,
    mime: item.mime,
    sizeBytes: item.size,
  });
  const dlStatus = downloadable ? dl.status : "idle";
  // The whole chip is the click target only when read-only (no onRemove, e.g. the sent-message strip);
  // with onRemove (pinned strip) a dedicated download button sits beside ×, never nested inside a button.
  const wholeCardOpenable = item.status === "done" && !!onOpen && !onRemove;
  const wholeCardDownloadable = downloadable && !onRemove && !wholeCardOpenable;
  const explicitDownloadButton = downloadable && !!onRemove;

  const variant =
    item.status === "uploading" ? "uploading"
    : item.status === "confirming" ? "confirming"
    : item.status === "indexing" ? "indexing"
    : item.status === "error"
      ? (isRecoverableError(item.errorKind) ? "warning" : "error")
      : "done";

  const pct = Math.max(0, Math.min(100, item.progressPct ?? 0));
  const children = item.bundleChildren ?? [];
  const failedChildren = children.filter((c) => c.status === "failed" || c.status === "skipped");
  const indexedChildren = children.filter((c) => c.status === "done");
  const ext = fileExtensionLabel(item.filename, kind);

  // Meta: exactly one fact per state (spec §5) — percentage, indexing progress, type · size, or the failure reason.
  const meta: { text: string; tone?: "warn" | "neg" } | null = (() => {
    if (dlStatus === "downloading") return { text: t("file_downloading", { pct: dl.pct }) };
    if (dlStatus === "done") return { text: t("file_downloaded") };
    if (dlStatus === "error") return { text: t("file_download_failed"), tone: "neg" };
    if (item.status === "uploading") return { text: `${Math.round(pct)}%` };
    if (item.status === "confirming") return null; // spinner replaces
    if (item.status === "indexing") {
      return { text: t("bundle_indexing", { done: item.indexedCount ?? 0, total: item.totalCount ?? 0 }) };
    }
    if (item.status === "error") return { text: errorMetaText(item.errorKind, t), tone: "neg" };
    if (kind === "bundle" && failedChildren.length > 0) {
      return { text: t("bundle_partial", { ok: indexedChildren.length, failed: failedChildren.length }), tone: "warn" };
    }
    if (scopeLabel) return { text: t("chart_selection", { scope: scopeLabel.length > 24 ? scopeLabel.slice(0, 23) + "…" : scopeLabel }) };
    return { text: [ext, formatSize(item.size)].filter(Boolean).join(" · ") };
  })();

  const ariaLabel = downloadable
    ? dlStatus === "downloading"
      ? t("file_aria_downloading", { filename: item.filename })
      : dlStatus === "done"
        ? t("file_aria_downloaded", { filename: item.filename })
        : dlStatus === "error"
          ? t("file_aria_failed", { filename: item.filename })
          : t("file_aria_download", { filename: item.filename })
    : undefined;

  const progress = item.status === "uploading" ? pct : dlStatus === "downloading" ? dl.pct : null;
  const busyUpload = item.status === "uploading" || item.status === "confirming" || item.status === "indexing";
  const showBundleMenu = (item.status === "indexing" || (item.status === "done" && onRemove)) && children.length > 0;
  const TypeIcon = item.status === "error" ? CircleAlert : fileTypeIcon(item.filename, item.mime, kind);

  // Bundle contents: failures first; more than six indexed files collapse to "N more indexed" (spec §5).
  const collapseIndexed = indexedChildren.length > BUNDLE_INDEXED_VISIBLE_MAX;
  const visibleIndexed = collapseIndexed ? indexedChildren.slice(0, BUNDLE_INDEXED_COLLAPSED_SHOWN) : indexedChildren;

  return (
    <span className={clsx("v2-chip-wrap", `v2-chip-wrap--${variant}`, flash && "is-flash")}>
      <span
        className={clsx(
          "v2-chip",
          `v2-chip--${variant}`,
          wholeCardDownloadable && "v2-chip--downloadable",
          wholeCardOpenable && "v2-chip--openable",
          dlStatus !== "idle" && `v2-chip--dl-${dlStatus}`,
        )}
        style={progress === null ? undefined : ({ "--chip-progress": `${progress}%` } as CSSProperties)}
        title={
          item.status === "error"
            ? t("upload_failed_for", { filename: item.filename, err: item.error ?? t("failed") })
            : `${item.filename} · ${formatSize(item.size)}${scopeLabel ? " · " + t("chart_selection_detail", { scope: scopeLabel }) : ""}`
        }
        role={wholeCardDownloadable ? "button" : undefined}
        tabIndex={wholeCardDownloadable ? 0 : undefined}
        aria-label={wholeCardDownloadable ? ariaLabel : undefined}
        aria-busy={dlStatus === "downloading" || undefined}
        onClick={wholeCardDownloadable ? dl.download : undefined}
        onKeyDown={
          wholeCardDownloadable
            ? (e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  dl.download();
                }
              }
            : undefined
        }
      >
        <span className="v2-chip__icon" aria-hidden="true">
          <LucideGlyph icon={TypeIcon} size={16} aria-hidden="true" />
        </span>
        <span className="v2-chip__name">{item.filename}</span>
        {meta ? (
          <span className="v2-chip__meta" data-tone={meta.tone}>
            {meta.text}
          </span>
        ) : null}
        {item.status === "confirming" ? <ChatCardSpinner small /> : null}

        {/* Download: a decorative glyph when the whole chip is the target; a real sibling button with × otherwise. */}
        {wholeCardDownloadable ? (
          <span className="v2-chip__dl" aria-hidden="true">
            {renderDlGlyph(dlStatus)}
          </span>
        ) : explicitDownloadButton ? (
          <button
            type="button"
            className="v2-chip__dl"
            aria-label={ariaLabel}
            aria-busy={dlStatus === "downloading" || undefined}
            disabled={dlStatus === "downloading"}
            onClick={(e) => {
              e.stopPropagation();
              dl.download();
            }}
          >
            {renderDlGlyph(dlStatus)}
          </button>
        ) : null}

        {item.status === "error" && isRecoverableError(item.errorKind) && onRetry ? (
          <button
            type="button"
            className="v2-chip__retry"
            disabled={mutationDisabled}
            onClick={(e) => {
              e.stopPropagation();
              onRetry(item.localId);
            }}
          >
            {item.errorKind === "auth" ? t("retry_signin") : t("retry")}
          </button>
        ) : null}

        {/* Bundle contents: a menu trigger; the list opens in the shared menu (bottom sheet on phones). */}
        {showBundleMenu ? (
          <button
            ref={bundleMenu.refs.setReference}
            type="button"
            className={clsx("v2-chip__chev", menuOpen && "is-open")}
            aria-label={t("show_bundle_children")}
            {...bundleMenu.getReferenceProps({ onClick: (e) => e.stopPropagation() })}
          >
            <LucideGlyph icon={ChevronDown} size={14} aria-hidden="true" />
          </button>
        ) : null}

        {onRemove ? (
          <button
            type="button"
            className="v2-chip__x"
            disabled={mutationDisabled}
            aria-label={busyUpload ? t("cancel_upload") : (removeLabel ?? t("dismiss"))}
            title={busyUpload ? undefined : removeLabel}
            onClick={(e) => {
              e.stopPropagation();
              // Mid-upload cancel uses onCancel so the caller can abort the XHR.
              if (busyUpload && onCancel) onCancel(item.localId);
              onRemove(item.localId);
            }}
          >
            <LucideGlyph icon={X} size={14} aria-hidden="true" />
          </button>
        ) : null}

        {progress !== null ? <span className="v2-chip__bar" aria-hidden="true" /> : null}
      </span>

      {/* Results Workspace: open and download are flat sibling buttons; the transparent open hit area covers
          the chip except the download slot, so no real button is nested inside a role=button chip. */}
      {wholeCardOpenable ? (
        <button
          type="button"
          className="v2-chip__open-hit"
          aria-label={openLabel ?? item.filename}
          onClick={onOpen}
        />
      ) : null}
      {wholeCardOpenable && downloadable ? (
        <button
          type="button"
          className="v2-chip__dl v2-chip__dl--overlay"
          aria-label={ariaLabel}
          aria-busy={dlStatus === "downloading" || undefined}
          disabled={dlStatus === "downloading"}
          onClick={dl.download}
        >
          {renderDlGlyph(dlStatus)}
        </button>
      ) : null}

      {showBundleMenu ? (
        <MenuFloating menu={bundleMenu}>
          <div
            ref={bundleMenu.refs.setFloating}
            {...bundleMenu.getFloatingProps({
              className: "app-floating-menu ui-card-bundle-menu is-portal",
              "aria-label": t("bundle_contents"),
            })}
          >
            <div className="ui-card-bundle-menu__head" aria-hidden="true">
              <b>{t("bundle_contents")}</b>
              <span>{t("bundle_file_count", { count: item.totalCount ?? children.length })}</span>
            </div>
            {/* Spec §5: failures first, then indexed files; files still indexing are counted on the chip only. */}
            {[...failedChildren, ...visibleIndexed].map((child, i) => (
              <BundleChildItem key={`${child.filename}-${i}`} child={child} />
            ))}
            {collapseIndexed ? (
              <div className="ui-card-bundle-menu__more" aria-hidden="true">
                {t("bundle_more_indexed", { count: indexedChildren.length - visibleIndexed.length })}
              </div>
            ) : null}
            {bundleMenu.presentation === "sheet" ? <BundleDoneItem label={t("bundle_done")} /> : null}
          </div>
        </MenuFloating>
      ) : null}
    </span>
  );
}

/** One read-only row of the bundle contents menu: type icon (or error icon), file name, state. */
function BundleChildItem({ child }: { child: BundleChild }) {
  const t = useTranslations("attachment");
  const tKb = useTranslations("knowledge_base.upload_modal");
  const skipKey = child.status === "skipped" && child.reasonCode ? BUNDLE_SKIP_REASON_KEYS[child.reasonCode] : undefined;
  const failed = child.status === "failed" || child.status === "skipped";
  const itemProps = useMenuItem({ label: child.filename });
  const Icon = failed ? CircleAlert : fileTypeIcon(child.filename);
  return (
    <button
      type="button"
      className="app-floating-menu-item ui-card-bundle-menu__item"
      data-tone={failed ? "neg" : undefined}
      {...itemProps}
    >
      <LucideGlyph icon={Icon} size={14} aria-hidden="true" />
      <span className="ui-card-bundle-menu__name">{child.filename}</span>
      <span className="ui-card-bundle-menu__state">{skipKey ? tKb(skipKey) : t(`bundle_child_${child.status}`)}</span>
    </button>
  );
}

/** Bottom sheet only: "Done" closes the bundle contents (spec §5 phone frame). */
function BundleDoneItem({ label }: { label: string }) {
  const itemProps = useMenuItem({ label });
  return (
    <button type="button" className="app-floating-menu-item ui-card-bundle-menu__done" {...itemProps}>
      {label}
    </button>
  );
}

// ── helpers ─────────────────────────────────────────────────────

/** Download glyph: spinner while downloading, check when done, a download arrow otherwise (idle and error
 *  both retry via click). */
function renderDlGlyph(status: FileDownloadStatus) {
  if (status === "downloading") return <ChatCardSpinner small />;
  if (status === "done") return <LucideGlyph icon={Check} size={14} aria-hidden="true" />;
  return <LucideGlyph icon={Download} size={14} aria-hidden="true" />;
}

function isRecoverableError(kind?: AttachmentErrorKind): boolean {
  return kind === "auth" || kind === "network";
}

function errorMetaText(
  kind: AttachmentErrorKind | undefined,
  t: (key: string) => string,
): string {
  switch (kind) {
    case "size": return t("err_size");
    case "unsupported": return t("err_unsupported");
    case "auth": return t("err_auth");
    case "network": return t("err_network");
    // The full sentence would push the name out of a 32px chip: it lives in the title and the composer notice.
    default: return t("failed");
  }
}
