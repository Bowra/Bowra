
import { isNetworkFailure } from "@/lib/transport-error";
import { ApiError } from "./api-client";

export type EmailConfigErrorKey =
  | "email_err_network"
  | "email_err_generic"
  | "email_err_invalid_fields"
  | "email_err_encryption_required"
  | "email_err_not_configured"
  | "email_err_rate_limited"
  | "email_err_insufficient_role"
  | "email_err_sign_in_again"
  | "email_err_readonly"
  | "email_err_entitlement_unavailable"
  | "email_err_smtp_control_characters"
  | "email_err_smtp_host_required"
  | "email_err_smtp_host_invalid"
  | "email_err_smtp_port_unsupported"
  | "email_err_smtp_from_address_invalid"
  | "email_err_smtp_password_required"
  | "email_err_smtp_host_blocked"
  | "email_err_test_delivery_failed"
  | "email_err_test_env_delivery_failed";

const EMAIL_CONFIG_ERROR_KEY: Readonly<
  Record<string, EmailConfigErrorKey>
> = {
  email_config_encryption_required: "email_err_encryption_required",
  email_not_configured: "email_err_not_configured",
  rate_limited: "email_err_rate_limited",
  insufficient_role: "email_err_insufficient_role",
  auth_required: "email_err_sign_in_again",
  invalid_token: "email_err_sign_in_again",
  password_changed_relogin_required: "email_err_sign_in_again",
  user_not_active: "email_err_sign_in_again",
  user_inactive: "email_err_sign_in_again",
  over_grace: "email_err_readonly",
  suspended: "email_err_readonly",
  needs_license: "email_err_readonly",
  tamper: "email_err_readonly",
  clock_tamper: "email_err_readonly",
  entitlement_unavailable: "email_err_entitlement_unavailable",
  smtp_control_characters: "email_err_smtp_control_characters",
  smtp_host_required: "email_err_smtp_host_required",
  smtp_host_invalid: "email_err_smtp_host_invalid",
  smtp_port_unsupported: "email_err_smtp_port_unsupported",
  smtp_from_address_invalid: "email_err_smtp_from_address_invalid",
  smtp_password_required: "email_err_smtp_password_required",
  smtp_host_blocked: "email_err_smtp_host_blocked",
  email_test_delivery_failed: "email_err_test_delivery_failed",
  email_test_env_delivery_failed: "email_err_test_env_delivery_failed",
};

interface EmailValidationIssue {
  code?: unknown;
}

function validationIssueCodes(error: ApiError): string[] {
  const detail = (
    error.body as {
      detail?: {
        issues?: EmailValidationIssue[];
      };
    } | undefined
  )?.detail;
  if (!Array.isArray(detail?.issues)) return [];
  return detail.issues.flatMap((issue) =>
    typeof issue.code === "string" ? [issue.code] : []);
}

export function emailConfigErrorKeys(error: unknown): EmailConfigErrorKey[] {
  if (!(error instanceof ApiError)) return [isNetworkFailure(error) ? "email_err_network" : "email_err_generic"];
  const issueKeys = validationIssueCodes(error)
    .map((code) => EMAIL_CONFIG_ERROR_KEY[code])
    .filter((key): key is EmailConfigErrorKey => Boolean(key));
  if (issueKeys.length) return Array.from(new Set(issueKeys));
  if (error.code && EMAIL_CONFIG_ERROR_KEY[error.code]) {
    return [EMAIL_CONFIG_ERROR_KEY[error.code]];
  }
  if (error.status === 401) return ["email_err_sign_in_again"];
  if (error.status === 403) return ["email_err_insufficient_role"];
  if (error.status === 422) return ["email_err_invalid_fields"];
  return ["email_err_generic"];
}

export function emailTestErrorKey(
  code: string | null | undefined,
): EmailConfigErrorKey {
  if (code && EMAIL_CONFIG_ERROR_KEY[code]) {
    return EMAIL_CONFIG_ERROR_KEY[code];
  }
  return "email_err_generic";
}
