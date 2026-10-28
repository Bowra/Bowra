/**
 * kb-matrix M3 wave-2—client select/multi_select option validation (pure functions).
 *
 * Server remains authoritative (addColumn/updateColumn 422: options_required/options_too_many/
 * option_too_long/…); mirror the same rules for inline errors before submission
 * (tech-design-m3 §4.6: 1–100 options / ≤64 characters per option / deduplicate).
 */

/** Maximum option count (matches server). */
export const OPTIONS_MAX = 100;
/** Maximum characters per option (matches server). */
export const OPTION_CHAR_MAX = 64;

export type OptionError = "too_long" | "dup" | "at_limit";

export interface OptionCheck {
  ok: boolean;
  /** Show inline error only when ok=false with error; empty input gives ok=false/no error (silently ignored). */
  error?: OptionError;
  /** Existing-option index matched on dup (for shake highlighting). */
  dupIndex?: number;
}

/** Validate a new column option: silently ignore empty; return respective errors for overlong/duplicate/count limit. */
export function checkNewOption(value: string, existing: string[]): OptionCheck {
  const v = value.trim();
  if (!v) return { ok: false };
  if (v.length > OPTION_CHAR_MAX) return { ok: false, error: "too_long" };
  const dupIndex = existing.indexOf(v);
  if (dupIndex >= 0) return { ok: false, error: "dup", dupIndex };
  if (existing.length >= OPTIONS_MAX) return { ok: false, error: "at_limit" };
  return { ok: true };
}
