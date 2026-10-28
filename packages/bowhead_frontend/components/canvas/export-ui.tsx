/**
 * Export UI trio (WP1 complete export flow, design D5 verbatim):
 *   <ExportMenu>    — report export formats and export history
 *   <ExportToast>   — in progress (indeterminate bar) / done / failed (plain explanation+Retry+default-theme escape) / timeout (points to drawer)
 *   <ExportsDrawer> — export history (SQLite persistence, recover downloads after restart/timeout)
 *
 * Design deviations (p1-review mode): D5's three-stage "Preparing→Rendering→Ready" requires
 * backend stage field (job only has queued/running/done/failed) — downgraded to two states + indeterminate bar;
 * remove "kept for 90 days" copy (backend has no retention policy).
 */

"use client";

import { isNetworkFailure } from "@/lib/transport-error";
import { useEffect, useId, useState, type RefObject } from "react";
import { useTranslations } from "next-intl";
import {
  exportJobDownloadUrl,
  listExportJobs,
  type ExportJobDTO,
} from "@/lib/api";
import type { ExportFmt, ExportState } from "@/lib/use-canvas-export";
import { DialogShell } from "@/components/ui/dialog";
import { ErrorDiagnosticsInline } from "@/components/error-notice";
import {
  MENU_DESCRIPTION_ATTR,
  MenuFloating,
  MenuItemReason,
  useMenuItem,
  type MenuApi,
} from "@/components/ui/menu";
import { CharIcon } from "@/components/char-icon";
import { Download } from "lucide-react";

/* ───────────── Format icons (D5 FmtIcon colors verbatim) ───────────── */

const FMT_STYLE: Record<string, { label: string; bg: string; fg: string }> = {
  pdf: { label: "PDF", bg: "var(--c-accent-soft)", fg: "var(--c-accent)" },
  docx: { label: "DOC", bg: "#dfe6f0", fg: "#3c5a86" },
  pptx: { label: "PPT", bg: "#e8e0d2", fg: "#7a5a3f" },
  xlsx: { label: "XLS", bg: "#e3e9e0", fg: "var(--c-pos)" },
};

export function FmtIcon({ fmt, size = 30 }: { fmt: string; size?: number }) {
  const m = FMT_STYLE[fmt] ?? FMT_STYLE.pdf;
  return (
    <span
      className="rc-fmt-ico"
      style={{
        width: size,
        height: size,
        background: m.bg,
        color: m.fg,
        fontSize: size * 0.3,
      }}
      aria-hidden
    >
      {m.label}
    </span>
  );
}

function fmtSize(bytes: number | null): string {
  if (!bytes || bytes <= 0) return "";
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/* ───────────── Export menu (D5Menu) ───────────── */

type ExportPick = (fmt: ExportFmt) => void;

/**
 * Export-format menu overlay. Caller renders trigger and holds `menu = useMenu(...)`:
 * useMenu handles keyboard, Esc, outside clicks, focus return, and positioning. Selecting an item returns focus to trigger
 * before closing menu and performing action (shared component step 4).
 */
export function ExportMenu({
  menu,
  busy,
  onPick,
  onOpenHistory,
}: {
  menu: MenuApi;
  busy: boolean;
  onPick: ExportPick;
  onOpenHistory: () => void;
}) {
  return (
    <MenuFloating menu={menu}>
      <div
        ref={menu.refs.setFloating}
        {...menu.getFloatingProps({ className: "ws-menu rc-export-menu" })}
      >
        <ExportMenuItems busy={busy} onPick={onPick} onOpenHistory={onOpenHistory} />
      </div>
    </MenuFloating>
  );
}

/** The same report-export actions serve the toolbar and its narrow submenu. */
export function ExportMenuItems({
  busy,
  onPick,
  onOpenHistory,
}: {
  busy: boolean;
  onPick: ExportPick;
  onOpenHistory: () => void;
}) {
  const t = useTranslations("export_ui");

  return (
    <>
      <ExportRow fmt="pdf" name={t("pdf_name")} desc={t("pdf_desc")} busy={busy} onSelect={() => onPick("pdf")} />
      <ExportRow fmt="docx" name={t("docx_name")} desc={t("docx_desc")} busy={busy} onSelect={() => onPick("docx")} />
      <ExportRow
        fmt="pptx"
        name={t("pptx_name")}
        desc={t("pptx_desc")}
        busy={busy}
        onSelect={() => onPick("pptx")}
      />
      <ExportRow fmt="xlsx" name={t("xlsx_name")} desc={t("xlsx_desc")} busy={busy} onSelect={() => onPick("xlsx")} />
      <ExportHistoryItem onSelect={onOpenHistory}>{t("past_exports")} <CharIcon ch="›" /></ExportHistoryItem>
    </>
  );
}

function ExportRow({
  fmt,
  name,
  desc,
  busy,
  onSelect,
}: {
  fmt: ExportFmt;
  name: string;
  desc: string;
  busy: boolean;
  onSelect: () => void;
}) {
  const itemProps = useMenuItem({ onSelect, disabled: busy, label: name });
  // Behavior batch A: exporting format rows omit native disabled (still keyboard-highlightable), with reason always on second line;
  // description uses data-menu-description; narrow screens switch to bottom sheet at threshold.
  const t = useTranslations("export_ui");
  const reasonId = useId();
  const reason = busy ? t("busy_reason") : null;
  return (
    <div className="rc-export-row" role="presentation">
      <button
        type="button"
        className="rc-export-row-btn"
        aria-describedby={reason ? reasonId : undefined}
        {...itemProps}
      >
        <FmtIcon fmt={fmt} />
        <span className="rc-export-row-body">
          <span className="rc-export-row-name">{name}</span>
          <span className="rc-export-row-desc" {...{ [MENU_DESCRIPTION_ATTR]: "" }}>
            {desc}
          </span>
        </span>
      </button>
      {reason ? <MenuItemReason id={reasonId}>{reason}</MenuItemReason> : null}
    </div>
  );
}

function ExportHistoryItem({ onSelect, children }: { onSelect: () => void; children: React.ReactNode }) {
  const itemProps = useMenuItem({ onSelect });
  return (
    <button type="button" className="rc-export-history" {...itemProps}>
      {children}
    </button>
  );
}

/* ───────────── Progress toast (D5 Toast, downgraded to two states) ───────────── */

export type ExportStartErrorKey =
  | "start_failed_asset_missing"
  | "start_failed_not_owner"
  | "start_failed_too_large"
  | "start_failed_quarantined"
  | "start_failed_unsupported"
  | "start_failed_changed"
  | "start_failed_capacity"
  | "start_failed_forbidden"
  | "start_failed_too_frequent"
  | "start_failed_network"
  | "start_failed";

const EXPORT_START_CODE_KEYS: Readonly<Record<string, ExportStartErrorKey>> = {
  // Referenced report file deleted, moved, or unreadable to current account — not an export-permission denial.
  asset_missing_or_forbidden: "start_failed_asset_missing",
  canvas_not_owned: "start_failed_not_owner",
  asset_too_large: "start_failed_too_large",
  snapshot_assets_too_large: "start_failed_too_large",
  snapshot_too_large: "start_failed_too_large",
  quarantined_sections: "start_failed_quarantined",
  unsupported_required_capability: "start_failed_unsupported",
  // Pre-export checked version expired: retrying unchanged sends old version again; user must initiate export anew.
  projection_conflict: "start_failed_changed",
  surface_projection_conflict: "start_failed_changed",
  export_capacity_exceeded: "start_failed_capacity",
};

/**
 * User-facing message when export cannot start, and whether to offer Retry.
 * Select by error code, then fall back by status; retryability follows server `detail.retryable`.
 * Do not display raw messages such as "start export: 500".
 */
export function exportStartErrorInfo(error: unknown): { key: ExportStartErrorKey; retryable: boolean } {
  const record = error && typeof error === "object"
    ? (error as { status?: unknown; code?: unknown; body?: unknown })
    : {};
  const status = typeof record.status === "number" ? record.status : undefined;
  const detail = record.body && typeof record.body === "object"
    ? (record.body as { detail?: unknown }).detail
    : undefined;
  const published = detail && typeof detail === "object"
    ? (detail as { retryable?: unknown }).retryable
    : undefined;
  const codeKey = typeof record.code === "string" ? EXPORT_START_CODE_KEYS[record.code] : undefined;
  let key: ExportStartErrorKey;
  if (codeKey) key = codeKey;
  else if (status === 403) key = "start_failed_forbidden";
  else if (status === 409 || status === 412) key = "start_failed_changed";
  else if (status === 429) key = "start_failed_too_frequent";
  else if (status === undefined && isNetworkFailure(error)) key = "start_failed_network";
  else key = "start_failed";
  // Expired-version conflicts cannot retry unchanged (old version sent); without other server conclusions, network loss, rate limits, and 5xx are retryable.
  const retryable = key === "start_failed_changed"
    ? false
    : typeof published === "boolean"
      ? published
      : status === undefined || status === 429 || status >= 500;
  return { key, retryable };
}

export function ExportToast({
  state,
  onDismiss,
  onRetry,
  onRetryDefaultTheme,
  onOpenHistory,
}: {
  state: ExportState;
  onDismiss: () => void;
  onRetry: () => void;
  onRetryDefaultTheme: () => void;
  onOpenHistory: () => void;
}) {
  const t = useTranslations("export_ui");
  const { job, timedOut, startError, lastArgs } = state;
  if (!job && !startError) return null;
  const fmt = lastArgs?.fmt ?? job?.fmt ?? "pdf";
  const running =
    !timedOut && (job?.status === "queued" || job?.status === "running");
  const failed = Boolean(startError) || job?.status === "failed";
  const done = job?.status === "done";
  const usedTemplate = Boolean(lastArgs?.opts?.template_artifact_id);
  const downloadUrl = job ? exportJobDownloadUrl(job) : null;
  const legacy = done && job?.contractVersion !== 3;
  const startInfo = startError ? exportStartErrorInfo(startError) : null;
  // No Retry when export never started and server declares nonretryable (or version expired): unchanged resend must fail again.
  const canRetry = !startInfo || startInfo.retryable;

  return (
    // Announce title and description only; "复制诊断" sits outside announcement region (in action row).
    <div className="rc-export-toast" data-testid="export-toast">
      <div className="rc-export-toast-row">
        {done ? (
          <span className="rc-export-toast-ok"><CharIcon ch="✓" /></span>
        ) : (
          <FmtIcon fmt={fmt} size={26} />
        )}
        <div className="rc-export-toast-body">
          <div className="rc-export-toast-title" role="status">
            {failed && t("failed_title", { fmt: fmt.toUpperCase() })}
            {done && t("ready_title", { fmt: fmt.toUpperCase() })}
            {running && t("running_title", { fmt: fmt.toUpperCase() })}
            {timedOut && !done && !failed && t("timeout_title")}
          </div>
          <div className="rc-export-toast-sub" aria-live="polite">
            {failed && (startInfo
              ? (startInfo.retryable ? `${t(startInfo.key)} ${t("start_retry_hint")}` : t(startInfo.key))
              : job?.error || t("failed_generic"))}
            {done && (
              <>
                <span>{`${job?.filename ?? ""}${job?.size ? ` · ${fmtSize(job.size)}` : ""}`}</span>
                {legacy ? (
                  <span className="rc-export-legacy">{t("legacy_notice")}</span>
                ) : null}
              </>
            )}
            {running && t("running_sub")}
            {timedOut && !done && !failed && t("timeout_sub")}
          </div>
          {(failed || (timedOut && !done)) && (
            <div className="rc-export-toast-actions">
              {failed && canRetry && (
                <button
                  type="button"
                  className="rc-pill-btn accept"
                  onClick={onRetry}
                >
                  {t("retry")}
                </button>
              )}
              {failed && fmt === "pptx" && usedTemplate && (
                <button
                  type="button"
                  className="rc-pill-btn ghost"
                  onClick={onRetryDefaultTheme}
                >
                  {t("retry_default_theme")}
                </button>
              )}
              {timedOut && !failed && (
                <button
                  type="button"
                  className="rc-pill-btn accept"
                  onClick={onOpenHistory}
                >
                  {t("open_history")}
                </button>
              )}
              {failed && startError ? <ErrorDiagnosticsInline error={startError} surface="export" /> : null}
            </div>
          )}
        </div>
        {done && legacy ? (
          <button type="button" className="rc-pill-btn accept" onClick={onRetry}>
            {t("regenerate")}
          </button>
        ) : null}
        {done && downloadUrl ? (
          <a
            className={`rc-pill-btn ${legacy ? "ghost" : "accept"}`}
            href={downloadUrl}
            target="_blank"
            rel="noopener noreferrer"
            onClick={onDismiss}
          >
            {t("download")}
          </a>
        ) : null}
        <button
          type="button"
          className="rc-export-toast-x"
          onClick={onDismiss}
          aria-label={t("dismiss")}
        >
          <CharIcon ch="✕" />
        </button>
      </div>
      {running && (
        <div className="rc-export-bar">
          <span />
        </div>
      )}
    </div>
  );
}

/* ───────────── Export history drawer (D5Drawer) ───────────── */

export function ExportsDrawer({
  canvasId,
  open,
  onClose,
  onRegenerate,
  returnFocusRef,
}: {
  canvasId: string | null;
  open: boolean;
  onClose: () => void;
  onRegenerate?: (fmt: ExportFmt) => void;
  /** Opening from export menu closes that menu; closing drawer returns focus to export button. */
  returnFocusRef?: RefObject<HTMLElement | null>;
}) {
  const t = useTranslations("export_ui");
  const [items, setItems] = useState<ExportJobDTO[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open || !canvasId) return;
    let cancelled = false;
    setLoading(true);
    listExportJobs(canvasId)
      .then((list) => {
        if (!cancelled) setItems(list);
      })
      .catch(() => {
        if (!cancelled) setItems([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, canvasId]);

  if (!open) return null;

  return (
    <DialogShell
      presentation="center"
      onClose={onClose}
      returnFocusRef={returnFocusRef}
      label={t("drawer_title")}
      backdropClassName="rc-drawer-scrim"
      panelClassName="rc-exports-drawer"
      panelProps={{ "data-testid": "exports-drawer" } as Record<string, string>}
    >
      <div className="rc-exports-head">
        <span className="rc-exports-title">{t("drawer_title")}</span>
        <span className="rc-exports-scope">{t("drawer_scope")}</span>
        <button
          type="button"
          className="rc-export-toast-x"
          onClick={onClose}
          aria-label={t("dismiss")}
        >
          <CharIcon ch="✕" />
        </button>
      </div>
      <div className="rc-exports-list bh-scroll">
        {loading && <div className="rc-exports-empty">…</div>}
        {!loading && items.length === 0 && (
          <div className="rc-exports-empty">{t("drawer_empty")}</div>
        )}
        {items.map((j) => {
          const url = exportJobDownloadUrl(j);
          return (
            <div
              key={j.jobId}
              className="rc-exports-item"
              data-status={j.status}
            >
              <FmtIcon fmt={j.fmt} size={28} />
              <div className="rc-exports-item-body">
                <div
                  className="rc-exports-item-name"
                  title={j.filename ?? j.jobId}
                >
                  {j.filename ??
                    `${j.fmt.toUpperCase()} · ${j.jobId.slice(-8)}`}
                </div>
                <div className="rc-exports-item-meta">
                  {j.status === "done" &&
                    `${fmtSize(j.size)}${j.size ? " · " : ""}${new Date(j.createdAt).toLocaleString()}`}
                  {j.status === "failed" && (
                    <span className="rc-exports-item-err">
                      {t("status_failed")}
                      {j.error ? ` · ${j.error}` : ""}
                    </span>
                  )}
                  {j.status === "canceled" && t("status_canceled")}
                  {(j.status === "running" || j.status === "queued") &&
                    t("status_running")}
                  {j.status === "done" && j.contractVersion !== 3 ? (
                    <span className="rc-export-legacy">
                      {t("legacy_notice")}
                    </span>
                  ) : null}
                </div>
              </div>
              {j.status === "done"
              && j.contractVersion !== 3
              && onRegenerate
              && (j.fmt === "pdf" || j.fmt === "pptx" || j.fmt === "xlsx") ? (
                <button
                  type="button"
                  className="rc-pill-btn accept"
                  onClick={() => onRegenerate(j.fmt as ExportFmt)}
                >
                  {t("regenerate")}
                </button>
                ) : null}
              {j.status === "done" && url ? (
                <a
                  className="rc-exports-dl"
                  href={url}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={t("download")}
                >
                  <CharIcon ch="↓" icon={Download} />
                </a>
              ) : null}
            </div>
          );
        })}
      </div>
    </DialogShell>
  );
}
