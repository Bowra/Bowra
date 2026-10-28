"use client";

import { useEffect, useId, useState } from "react";
import { useTranslations } from "next-intl";

import { Field, fieldDescribedBy } from "@/components/ui/field";
import { ApiError } from "@/lib/api-client";
import {
  DEFAULT_LLM_MAX_RETRIES,
  MAX_LLM_MAX_RETRIES,
  MIN_LLM_MAX_RETRIES,
  saveAdminLlmRetry,
  type SaveLlmRetryResp,
} from "@/lib/llm-config-api";

/**
 * Team-wide automatic retries after a model call fails before replying.
 *
 * Saved on its own endpoint with the loaded configuration version, so it never
 * goes through the model-assignment draft/readiness review.
 */
export function RetrySettingSection({
  value,
  version,
  disabled = false,
  onSaved,
  userFacingError,
}: {
  /** Saved value (default already applied when the configuration has none). */
  value: number;
  /** Loaded configuration version, sent as `expected_version`. */
  version: number;
  disabled?: boolean;
  onSaved: (response: SaveLlmRetryResp) => void;
  userFacingError: (cause: unknown) => string;
}) {
  const t = useTranslations("settings.models.retry");
  const id = useId();
  const inputId = `${id}-max-retries`;
  const [draft, setDraft] = useState(String(value));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  // A reload or another save changes the authoritative value; follow it.
  useEffect(() => {
    setDraft(String(value));
  }, [value]);

  const parsed = parseRetryDraft(draft);
  const rangeError = parsed === null
    ? t("error_range", { min: MIN_LLM_MAX_RETRIES, max: MAX_LLM_MAX_RETRIES })
    : null;
  const shownError = rangeError ?? error;
  const dirty = parsed !== null && parsed !== value;

  const save = async () => {
    if (parsed === null || saving) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const response = await saveAdminLlmRetry(parsed, version);
      onSaved(response);
      setSaved(true);
    } catch (cause) {
      setError(retryErrorMessage(cause, t, userFacingError));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="overview-section" aria-labelledby={`${id}-title`} data-testid="model-retry-setting">
      <div className="overview-section-head">
        <div>
          <h2 id={`${id}-title`}>{t("title")}</h2>
        </div>
      </div>
      <Field
        id={inputId}
        label={t("label")}
        hint={t("hint", { default: DEFAULT_LLM_MAX_RETRIES, min: MIN_LLM_MAX_RETRIES, max: MAX_LLM_MAX_RETRIES })}
        error={shownError}
      >
        <span className="model-retry-control">
          <input
            id={inputId}
            className="inp mono"
            type="number"
            inputMode="numeric"
            min={MIN_LLM_MAX_RETRIES}
            max={MAX_LLM_MAX_RETRIES}
            step={1}
            name="llm_max_retries"
            value={draft}
            disabled={disabled || saving}
            aria-invalid={shownError ? true : undefined}
            aria-describedby={fieldDescribedBy(inputId, { hint: true, error: Boolean(shownError) })}
            onChange={(event) => {
              setDraft(event.target.value);
              setError(null);
              setSaved(false);
            }}
          />
          <button
            type="button"
            className="btn sec sm"
            disabled={disabled || saving || !dirty}
            onClick={() => void save()}
          >
            {saving ? t("saving") : t("save")}
          </button>
        </span>
      </Field>
      {saved && !dirty ? <p role="status" className="overview-quiet">{t("saved")}</p> : null}
    </section>
  );
}

/** Integer in the allowed range, or null. */
export function parseRetryDraft(raw: string): number | null {
  const text = raw.trim();
  if (!/^\d+$/.test(text)) return null;
  const value = Number(text);
  return value >= MIN_LLM_MAX_RETRIES && value <= MAX_LLM_MAX_RETRIES ? value : null;
}

type RetryT = ReturnType<typeof useTranslations<"settings.models.retry">>;

/** Retry-specific actionable copy; other failures share the model settings error copy. */
export function retryErrorMessage(
  cause: unknown,
  t: RetryT,
  userFacingError: (cause: unknown) => string,
): string {
  if (cause instanceof ApiError) {
    if (cause.code === "config_version_conflict") return t("error_stale");
    if (cause.code === "invalid_llm_retry" || cause.status === 422) {
      return t("error_range", { min: MIN_LLM_MAX_RETRIES, max: MAX_LLM_MAX_RETRIES });
    }
    if (cause.code === "llm_config_not_configured") return t("error_not_configured");
  }
  return userFacingError(cause);
}
