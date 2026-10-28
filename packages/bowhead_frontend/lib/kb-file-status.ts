/**
 * User-facing knowledge-base file states—knowledge-base experience cleanup PRD §4.
 *
 * Lists/detail drawers share this mapping; the 8 internal parse_status values appear only here. Stuck files
 * (backend parse_stuck: ongoing but no executor remains) show failure and allow retry, never staying in Processing forever.
 */

import type { KbFileDTO } from "@/lib/kb-api";

export type KbFileState = "uploading" | "queued" | "processing" | "ready" | "failed";

export type KbFailReason =
  | "unsupported_format"
  | "empty_content"
  | "interrupted"
  | "timeout"
  | "service_unavailable"
  | "source_missing"
  | "parse_failed";

/** All failure reasons; each needs copy under knowledge_base.fail_reason and drawer.fail_title. */
export const KB_FAIL_REASONS: readonly KbFailReason[] = [
  "unsupported_format",
  "empty_content",
  "interrupted",
  "timeout",
  "service_unavailable",
  "source_missing",
  "parse_failed",
];
const FAIL_REASONS = new Set<KbFailReason>(KB_FAIL_REASONS);

export function kbFileState(f: Pick<KbFileDTO, "parse_status" | "parse_stuck">): KbFileState {
  if (f.parse_status === "failed" || f.parse_stuck) return "failed";
  switch (f.parse_status) {
    case "awaiting_upload":
      return "uploading";
    case "pending":
    case "queued":
      return "queued";
    case "parsing":
    case "chunking":
    case "embedding":
      return "processing";
    default:
      return "ready";
  }
}

/** Failure reason; null unless failed. Stuck files count as Processing Interrupted. Unknown codes use generic failure. */
export function kbFailReason(
  f: Pick<KbFileDTO, "parse_status" | "parse_stuck" | "parse_error_code">,
): KbFailReason | null {
  if (kbFileState(f) !== "failed") return null;
  if (f.parse_status !== "failed") return "interrupted";
  const code = f.parse_error_code as KbFailReason | null | undefined;
  return code && FAIL_REASONS.has(code) ? code : "parse_failed";
}

/** Failure reason snapshots the previous parse; the server rechecks the original file on retry. */
export function kbFailRetryable(reason: KbFailReason | null): boolean {
  return reason !== null;
}

/** States that still change on their own and warrant polling. */
export function kbFileSettling(f: Pick<KbFileDTO, "parse_status" | "parse_stuck">): boolean {
  const s = kbFileState(f);
  return s === "queued" || s === "processing";
}
