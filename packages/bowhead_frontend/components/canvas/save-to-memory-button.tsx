"use client";

/**
 * V4 PRD-D-5 · Save to memory button.
 *
 * Hovers in the top-right of verified-fact canvas sections (kpi_row / chart /
 * data_table). Clicking opens a small inline composer to type the summary
 * (≤200 char), then POSTs to /api/v1/memory/notes/save-from-canvas.
 *
 * Section-kind whitelist enforced server-side too — this is just UX.
 */

import { isNetworkFailure } from "@/lib/transport-error";
import { useTranslations } from "next-intl";
import { useRef, useState } from "react";
import { fetchNotes, saveFromCanvas } from "@/lib/memory-api";
import { ErrorDiagnosticsInline, type ShownError, shownError } from "@/components/error-notice";
import { CharIcon, CharIconText } from "@/components/char-icon";

const VERIFIED_FACT_KINDS = new Set(["kpi_row", "chart", "data_table"]);
const MAX_SUMMARY_CHARS = 200;
/** memory-revive S1 volume guard (discovery §5.2): warn within popover at ≥80% notes usage. */
const NEAR_LIMIT_RATIO = 0.8;

export function isVerifiedFactKind(kind: string): boolean {
  return VERIFIED_FACT_KINDS.has(kind);
}

/** Failure of "保存到记忆" → user-visible message key under canvas.* (selected by status code, with remedy). */
export function saveToMemoryFailedKey(error: unknown):
  | "stm_failed_full"
  | "stm_failed_locked"
  | "stm_failed_invalid"
  | "stm_failed_gone"
  | "stm_failed_unavailable"
  | "stm_failed_network"
  | "stm_failed_plain" {
  const status =
    error && typeof error === "object" && typeof (error as { status?: unknown }).status === "number"
      ? (error as { status: number }).status
      : undefined;
  if (status === 413) return "stm_failed_full";
  if (status === 423) return "stm_failed_locked";
  if (status === 400 || status === 422) return "stm_failed_invalid";
  if (status === 404) return "stm_failed_gone";
  if (status === 503) return "stm_failed_unavailable";
  if (status === undefined && isNetworkFailure(error)) return "stm_failed_network";
  return "stm_failed_plain";
}

export function SaveToMemoryButton({
  canvasId,
  sectionId,
  defaultSummary,
}: {
  canvasId: string | null;
  sectionId: string;
  defaultSummary?: string;
}) {
  const t = useTranslations("canvas");
  const [open, setOpen] = useState(false);
  const [text, setText] = useState(defaultSummary?.slice(0, MAX_SUMMARY_CHARS) ?? "");
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [error, setError] = useState<ShownError | null>(null);
  const [nearLimit, setNearLimit] = useState<{ count: number; limit: number } | null>(null);

  // memory-revive S1 freshness guard (discovery §5.2 frozen as-of): prefill "截至 X年X月，"
  // Qualify expiring current-state assertions with a date; users can edit/discard it, but prefix alone is invalid (guard).
  // review-fix: freeze the inserted prefix in a ref — recomputing it during render across month boundaries
  // changes it, allowing an untouched bare prefix through the guard.
  const prefillRef = useRef<string | null>(null);

  if (!canvasId) return null;  // not attached to a saved canvas yet — hide

  const trimmed = text.trim();
  const canSave =
    trimmed.length >= 1 &&
    trimmed !== (prefillRef.current ?? "").trim() &&
    trimmed.length <= MAX_SUMMARY_CHARS &&
    !saving;

  function handleOpen() {
    setOpen(true);
    const now = new Date();
    const prefill = t("stm_asof_prefix", { year: now.getFullYear(), month: now.getMonth() + 1 });
    setText((cur) => {
      if (cur) return cur;
      prefillRef.current = prefill;
      return prefill;
    });
    // Full-library warning is best-effort: no disruption if unavailable (backend still returns 413 → error row on a real limit hit).
    // review-fix: clear old warning on every open — users who cleared notes no longer see a stale warning.
    setNearLimit(null);
    void fetchNotes()
      .then((n) => {
        const limit = n.char_limit ?? 3000;
        if (n.char_count >= limit * NEAR_LIMIT_RATIO) {
          setNearLimit({ count: n.char_count, limit });
        }
      })
      .catch(() => {});
  }

  async function handleSave() {
    setError(null);
    setSaving(true);
    try {
      const note = await saveFromCanvas({
        canvasId: canvasId!,
        sectionId,
        summaryText: trimmed,
        // memory-revive S1 volume guard: unpinned by default — pinned entries always take priority in notes budget
        // truncation order; pinning canvas entries by default crowds out learned preferences (discovery §5.2 main pitfall).
        pinned: false,
      });
      setSavedAt(note.verification_anchor?.saved_at ?? new Date().toISOString());
      setOpen(false);
      setText("");
    } catch (e: unknown) {
      // Say only what happened and what to do; status codes and raw API messages go in "复制诊断".
      setError(shownError(t(saveToMemoryFailedKey(e)), e));
    } finally {
      setSaving(false);
    }
  }

  if (!open) {
    return (
      <button
        type="button"
        className="v4-save-to-memory-btn"
        onClick={handleOpen}
        title={t("stm_button_title")}
        data-testid="save-to-memory-btn"
      >
        <CharIconText ch="📌">{t("stm_button")}</CharIconText>
        {savedAt ? <span className="v4-saved-tick" aria-label={t("stm_saved_aria")}><CharIcon ch="✓" /></span> : null}
      </button>
    );
  }

  return (
    <div className="v4-save-to-memory-popover" data-testid="save-to-memory-popover">
      <textarea
        className="v4-save-to-memory-input"
        value={text}
        onChange={(e) => setText(e.target.value.slice(0, MAX_SUMMARY_CHARS))}
        placeholder={t("stm_placeholder", { max: MAX_SUMMARY_CHARS })}
        rows={3}
        data-testid="save-to-memory-textarea"
        autoFocus
      />
      <div className="v4-save-to-memory-row">
        <span className="v4-save-to-memory-counter">
          {trimmed.length}/{MAX_SUMMARY_CHARS}
        </span>
        <div className="v4-save-to-memory-actions">
          <button
            type="button"
            className="v4-save-to-memory-cancel"
            onClick={() => { setOpen(false); setError(null); }}
            disabled={saving}
          >
            {t("stm_cancel")}
          </button>
          <button
            type="button"
            className="v4-save-to-memory-confirm"
            onClick={handleSave}
            disabled={!canSave}
            data-testid="save-to-memory-confirm"
          >
            {saving ? t("stm_saving") : t("stm_save")}
          </button>
        </div>
      </div>
      {nearLimit ? (
        <div className="v4-save-to-memory-warn" role="status" data-testid="save-to-memory-warn">
          {t("stm_near_limit", { count: nearLimit.count, limit: nearLimit.limit })}
        </div>
      ) : null}
      {error ? (
        <>
          <div className="v4-save-to-memory-error" role="alert">
            {error.message}
          </div>
          <ErrorDiagnosticsInline error={error.error} surface="task" />
        </>
      ) : null}
    </div>
  );
}
