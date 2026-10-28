"use client";

import { toast } from "@/lib/use-toast";
import { staticTranslator } from "@/lib/locale-messages";
import { copyTextWithFallback } from "@/lib/clipboard";
import { appPath } from "@/lib/base-path";

// Internal <Link>/router paths omit the public-entry prefix (browser history/fetch patches outside Next
// add it; prefixed paths passed to Link would fail route matching).
export function reportHrefForSession(sessionId: string): string {
  return `/?session=${encodeURIComponent(sessionId)}&report=1`;
}

/** Address for report-library "下载": open report surface + automatically export once after arrival.
 *
 * Why this round trip: V2 export requires a **client receipt** contract, with backend enforcing
 * expected_projection_token / expected_surface_token / client_surface_receipt_sha256
 * (routes/canvas.py, 400 if any missing), produced only by a **mounted, fully rendered report**
 * (validateAndBuildRenderedSurfaceReceipt uses renderStateOf to inspect each section's DOM markers).
 * Library has only cards, no rendering, so previously used legacy; same measured content yielded
 * xlsx three sheets/29 rows versus V2 nine sheets/426 rows, pdf one page/no figures/3.8KB versus six pages/143KB with figures.
 * Send it to the already-rendering workspace to export, rather than add another renderer in library.
 *
 * ⚠ **Do not** use for sharing (use reportHrefForSession), or recipients download automatically on opening.
 */
/** Formats automatically triggered by ?export=, matching report-library menu.
 *  Deliberately exclude docx: backend _EXPORT_PARITY_CONTRACTS has none, only legacy job path. */
export const AUTO_EXPORT_FMTS = ["pdf", "pptx", "xlsx"] as const;
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

export function reportExportHrefForSession(sessionId: string, fmt: string): string {
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
  const ok = await copyTextWithFallback(href, t("copy_prompt"));
  if (ok) {
    toast({ kind: "success", text: t("copied_toast") });
  }
  return ok;
}
