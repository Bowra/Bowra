"use client";

import { staticTranslator } from "@/lib/locale-messages";
import { appPath } from "@/lib/base-path";
import { copyShareLink } from "@/lib/share-link";

// Internal <Link>/router paths omit the public-entry prefix (browser history/fetch patches outside Next
// add it; prefixed paths passed to Link would fail route matching).
export function reportHrefForSession(sessionId: string): string {
  return `/?session=${encodeURIComponent(sessionId)}&report=1`;
}

/** The report library and its one-time export intent share the same format list. Word uses the workbench's existing DOCX job. */
export const AUTO_EXPORT_FMTS = ["pdf", "docx", "pptx", "xlsx"] as const;
export type AutoExportFmt = (typeof AUTO_EXPORT_FMTS)[number];

/** Decode ?export=. **Keep encoding/decoding in one file** to avoid independent drift. */
export function parseReportExportParam(search: string | URLSearchParams): AutoExportFmt | null {
  const params = typeof search === "string" ? new URLSearchParams(search) : search;
  const raw = (params.get("export") ?? "").trim().toLowerCase();
  return (AUTO_EXPORT_FMTS as readonly string[]).includes(raw) ? (raw as AutoExportFmt) : null;
}

/**
 * Consume ?export=: return format and remove the URL parameter (mutating url in place).
 *
 * ★ Remove it to avoid repeated export on refresh/back, but **order is easy to reverse**:
 * delete("export") before parseReportExportParam(url.searchParams)
 * parses an already-cleared object → always null → export **never triggers**,
 * without any page notice (user clicked "下载", nothing happened).
 * Bind both steps in one function so callers cannot reverse them.
 */
export function consumeReportExportParam(url: URL): AutoExportFmt | null {
  const fmt = parseReportExportParam(url.searchParams);
  url.searchParams.delete("export");
  return fmt;
}

export type ReportAutoExportReadiness = "none" | "waiting" | "empty" | "ready";

/**
 * Distinguish an unresolved Canvas request from a resolved session with no
 * visible report content. For the report-library route, callers pass the
 * session identity retained from the authoritative by-session hydration.
 */
export function reportAutoExportReadiness(
  url: URL,
  canvasId: string | null,
  hydratedSessionId: string | null,
): ReportAutoExportReadiness {
  if (!url.searchParams.has("export")) return "none";
  const targetSessionId = url.searchParams.get("session");
  if (!targetSessionId || hydratedSessionId !== targetSessionId) return "waiting";
  return canvasId ? "ready" : "empty";
}

/** Opens the workbench to run a one-time export after the report can supply the rendered surface receipt. Sharing uses reportHrefForSession. */
export function reportExportHrefForSession(sessionId: string, fmt: AutoExportFmt): string {
  return `${reportHrefForSession(sessionId)}&export=${encodeURIComponent(fmt)}`;
}

// External sharing: copied links leave this page, recipients load a fresh page, requiring
// the public-entry prefix for reverse proxy forwarding to gateway.
export function reportShareUrl(sessionId: string, origin?: string): string {
  const base = origin ?? (typeof window !== "undefined" ? window.location.origin : "");
  return new URL(appPath(reportHrefForSession(sessionId)), base || "http://localhost").toString();
}

export async function copyPermissionedReportLink(sessionId: string): Promise<boolean> {
  if (typeof window === "undefined") return false;
  const href = reportShareUrl(sessionId, window.location.origin);
  const t = staticTranslator("report_share_link");
  return copyShareLink(href, t("copied_toast"));
}
