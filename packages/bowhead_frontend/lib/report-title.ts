/**
 * chat-page-polish B1 — title display rules for Reports list cards.
 *
 * Backend `/api/v1/canvas` historically returned only `canvas.title` (usually null), making cards display
 * the raw ID as `Untitled (cv_xxx)`, difficult to recognize. Backend now also returns `first_section_title`;
 * frontend fallback chain:
 *
 *   1. canvas.title (explicitly set by user/system)
 *   2. first_section_title (earliest section's h1/h2/h3/title)
 *   3. created_at-derived "YYYY-MM-DD HH:mm 报告" (locale-stable)
 *
 * Never display raw `Untitled (cv_xxx)` again. Return an empty string if even ID is absent.
 */

import { staticTranslator } from "./locale-messages";
import type { ReportListItem } from "./api";

/**
 * i18n: pure helper (no React context) — phrases come from messages
 * `report_title_fallback` via staticTranslator (current UI locale).
 */
function _titleStrings() {
  const t = staticTranslator("report_title_fallback");
  return {
    ts_report: (ts: string) => t("ts_report", { ts }),
    id_report: (id: string) => t("id_report", { id }),
    untitled: t("untitled"),
  };
}

function _isMeaningful(s: string | null | undefined): s is string {
  return typeof s === "string" && s.trim().length > 0;
}

/** Pad to 2 digits (locale-stable, avoids Intl variability across envs). */
function _p2(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

/** "YYYY-MM-DD HH:mm" — locale-free, used in fallback title. */
function _fmtTs(d: Date): string {
  return (
    `${d.getFullYear()}-${_p2(d.getMonth() + 1)}-${_p2(d.getDate())} ` +
    `${_p2(d.getHours())}:${_p2(d.getMinutes())}`
  );
}

export function reportDisplayTitle(
  r: Pick<ReportListItem, "title" | "first_section_title" | "created_at" | "id">,
): string {
  if (_isMeaningful(r.title)) return r.title.trim();
  if (_isMeaningful(r.first_section_title)) return r.first_section_title.trim();
  const ts = _titleStrings();
  // Final fallback: timestamped report
  if (r.created_at) {
    try {
      const d = new Date(r.created_at);
      if (!Number.isNaN(d.getTime())) {
        return ts.ts_report(_fmtTs(d));
      }
    } catch { /* fall through */ }
  }
  // Fallback: short ID, only when even created_at is absent (theoretically impossible)
  if (r.id) return ts.id_report(r.id.slice(0, 8));
  return ts.untitled;
}
