/** Form validation for signed-out public pages (replaces native browser bubbles, which are unbranded and not localizable). */

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@.]{2,}$/u;

type Translate = (key: "field_error.email_required" | "field_error.email_invalid") => string;

/** Email field error: the "required" message when empty, the "invalid format" message when malformed, otherwise undefined. */
export function emailFieldError(value: string, t: Translate): string | undefined {
  const email = value.trim();
  if (!email) return t("field_error.email_required");
  if (!EMAIL_PATTERN.test(email)) return t("field_error.email_invalid");
  return undefined;
}
